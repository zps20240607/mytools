# -*- coding: utf-8 -*-
"""RepoWatch 1.0 — 本机 Git 仓库总控台（Windows，Python 3 标准库，零依赖）。

- 扫描配置的根目录，发现所有 Git 仓库：分支 / 脏文件 / 领先落后 / 最后提交。
- 一键打开（编辑器 / 终端 / 目录），一键快照（git add + commit + push）。
- 只监听 127.0.0.1；写操作要求 HttpOnly 会话 Cookie，并校验 Host/Origin。

运行：python repo_watch.py [--preferred-port 9610] [--no-browser] [--scan-now]
"""
import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 路径与常量

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
STATIC_DIR = os.path.join(BASE_DIR, "static")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
PID_PATH = os.path.join(DATA_DIR, "server.pid")
LOG_PATH = os.path.join(LOGS_DIR, "console.log")

APP_VERSION = "1.0.0"
PORT_RANGE = range(9610, 9620)
CREATE_NO_WINDOW = 0x08000000
MAX_DEPTH = 6
MAX_DIRS_PER_LEVEL = 300
GIT_TIMEOUT = 20
AUTO_RESCAN_SECONDS = 60
COOKIE_NAME = "repowatch_session"
DEFAULT_EDITOR = "code"
DEFAULT_EDITOR = "code"
MAX_BODY = 256 * 1024

# 扫描时跳过的目录（小写比较）
SKIP_DIRS = {
    ".git", "node_modules", "bower_components", "__pycache__", ".venv", "venv",
    "env", ".env", "site-packages", "dist", "build", "out", ".next", ".nuxt",
    ".cache", ".idea", ".vscode", ".vs", "target", ".gradle", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".eggs", "appdata", "windows",
    "program files", "program files (x86)", "$recycle.bin", "system volume information",
    "onedrivetemp", ".tmp", "temp", ".cargo", ".rustup", ".npm", ".npm-cache",
    ".local", ".ollama", ".lmstudio", ".huggingface", "onedrive", "bin", "lib",
    "include", "share", "packages", "winsxs",
}

AHEAD_BEHIND_RE = re.compile(r"\[(?:ahead\s+(\d+))?(?:,\s*)?(?:behind\s+(\d+))?\]")
STATUS_TYPES = {
    "staged": "MADRC",
    "unstaged": "MD",
    "untracked": "??",
}

LOG = logging.getLogger("repowatch")


def setup_logging():
    os.makedirs(LOGS_DIR, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


# ---------------------------------------------------------------- 配置

def default_config():
    roots = [
        os.path.expandvars(r"%USERPROFILE%\Desktop"),
        os.path.expandvars(r"%USERPROFILE%\Documents"),
    ]
    for cand in (r"C:\dev", r"D:\dev", r"D:\code", r"D:\projects",
                 os.path.expandvars(r"%USERPROFILE%\source\repos")):
        if os.path.isdir(cand):
            roots.append(cand)
    return {
        "roots": roots,
        "editor_command": "code",
        "terminal_command": "wt -d",
        "rescan_seconds": AUTO_RESCAN_SECONDS,
    }


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in ("roots", "editor_command", "terminal_command", "rescan_seconds"):
                if key in data:
                    cfg[key] = data[key]
            if not isinstance(cfg["roots"], list):
                cfg["roots"] = default_config()["roots"]
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    return cfg


# ---------------------------------------------------------------- Git 工具

def git(repo_path, *args, timeout=GIT_TIMEOUT):
    """运行 git 命令，返回 (returncode, stdout, stderr)，全部为 utf-8 文本。"""
    env = os.environ.copy()
    env.update({"LANG": "C.UTF-8", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    cmd = ["git", "-C", repo_path, "-c", "core.quotepath=false", *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout, env=env,
            creationflags=CREATE_NO_WINDOW)
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace").strip(),
                proc.stderr.decode("utf-8", "replace").strip())
    except subprocess.TimeoutExpired:
        return (124, "", "git 命令超时")
    except OSError as exc:
        return (127, "", str(exc))


def find_repos(roots, max_depth=MAX_DEPTH):
    """广度优先扫描根目录，发现所有包含 .git 的目录；不进入仓库内部。"""
    found = []
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        stack = [(root, 0)]
        while stack:
            cur, depth = stack.pop()
            if depth > max_depth:
                continue
            try:
                entries = list(os.scandir(cur))
            except OSError:
                continue
            if any(e.name == ".git" for e in entries):
                found.append(cur)
                continue
            subdirs = []
            for e in entries:
                name_lower = e.name.lower()
                if name_lower in SKIP_DIRS:
                    continue
                try:
                    if e.is_symlink():
                        continue
                    if hasattr(e, "is_junction") and e.is_junction():
                        continue
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                subdirs.append((e.path, depth + 1))
            if len(subdirs) > MAX_DIRS_PER_LEVEL:
                subdirs = subdirs[:MAX_DIRS_PER_LEVEL]
            stack.extend(reversed(subdirs))
    # 去重（按 realpath，忽略大小写）
    unique, seen = [], set()
    for path in found:
        try:
            key = os.path.realpath(path).lower()
        except OSError:
            key = os.path.abspath(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return sorted(unique, key=lambda p: p.lower())


def classify_porcelain(line):
    if line.startswith("??"):
        return "untracked"
    if len(line) >= 2:
        index_ch = line[0]
        if index_ch in "MADRC":
            return "staged"
        if line[1] in "MD":
            return "unstaged"
    return "other"


def repo_info(path):
    """收集单个仓库信息；任何 git 失败都记录到 errors 而不中断。"""
    info = {
        "id": hashlib.md5(path.encode("utf-8", "replace")).hexdigest()[:12],
        "path": path,
        "name": os.path.basename(path.rstrip("\\/")) or path,
        "branch": "", "detached": False, "dirty": 0,
        "staged": 0, "unstaged": 0, "untracked": 0,
        "ahead": 0, "behind": 0, "has_upstream": False,
        "last_hash": "", "last_author": "", "last_time": "", "last_message": "",
        "remote": "", "errors": [],
    }
    # 分支
    rc, out, err = git(path, "branch", "--show-current")
    if rc == 0 and out:
        info["branch"] = out.splitlines()[0]
    else:
        rc2, out2, _ = git(path, "rev-parse", "--short", "HEAD")
        info["detached"] = True
        info["branch"] = out2 if rc2 == 0 and out2 else "(空仓库)"
        if info["branch"] == "(空仓库)":
            return info
    # 状态与领先落后
    rc, out, err = git(path, "status", "-sb")
    if rc == 0 and out:
        first = out.splitlines()[0] if out.splitlines() else ""
        m = AHEAD_BEHIND_RE.search(first)
        if m:
            if m.group(1):
                info["ahead"] = int(m.group(1))
            if m.group(2):
                info["behind"] = int(m.group(2))
        if "..." in first:
            info["has_upstream"] = True
        lines = out.splitlines()[1:]
        for line in lines:
            kind = classify_porcelain(line)
            if kind == "staged":
                info["staged"] += 1
            elif kind == "unstaged":
                info["unstaged"] += 1
            elif kind == "untracked":
                info["untracked"] += 1
            else:
                info["unstaged"] += 1
        info["dirty"] = info["staged"] + info["unstaged"] + info["untracked"]
    else:
        info["errors"].append("git status 失败: %s" % (err or "未知错误"))
    # 最后提交
    rc, out, err = git(path, "log", "-1", "--format=%h%x1f%an%x1f%ad%x1f%s",
                       "--date=format:%Y-%m-%dT%H:%M:%S")
    if rc == 0 and out:
        parts = out.split("\x1f", 3)
        info["last_hash"] = parts[0] if len(parts) > 0 else ""
        info["last_author"] = parts[1] if len(parts) > 1 else ""
        info["last_time"] = parts[2] if len(parts) > 2 else ""
        info["last_message"] = parts[3] if len(parts) > 3 else ""
    # 远程
    rc, out, err = git(path, "remote", "get-url", "origin")
    if rc == 0 and out:
        info["remote"] = out.splitlines()[0]
    return info


# ---------------------------------------------------------------- 扫描状态

STATE = {
    "repos": [], "stats": {}, "errors": [], "scanned_at": None, "scanning": False,
}
SCAN_LOCK = threading.Lock()
ACTION_LOCK = threading.Lock()
CONFIG = load_config()


def run_scan():
    """全量扫描（后台线程调用）。"""
    if not SCAN_LOCK.acquire(blocking=False):
        return
    try:
        STATE["scanning"] = True
        t0 = time.time()
        repos = find_repos(CONFIG["roots"])
        infos, errors = [], []
        for path in repos:
            info = repo_info(path)
            infos.append(info)
            errors.extend({"repo": path, "error": e} for e in info["errors"])
        infos.sort(key=lambda r: r["name"].lower())
        dirty = sum(1 for r in infos if r["dirty"] > 0)
        changes = sum(r["dirty"] for r in infos)
        STATE.update({
            "repos": infos,
            "stats": {"total": len(infos), "dirty_repos": dirty,
                      "total_changes": changes,
                      "scan_seconds": round(time.time() - t0, 1)},
            "errors": errors,
            "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        LOG.info("扫描完成: %d 个仓库，%d 个有变更，耗时 %.1fs",
                 len(infos), dirty, time.time() - t0)
    except Exception:
        LOG.exception("扫描失败")
    finally:
        STATE["scanning"] = False
        SCAN_LOCK.release()


def do_snapshot(path, message):
    """对单个仓库执行 add + commit + push，返回步骤列表；并发时抛 RuntimeError。"""
    if not ACTION_LOCK.acquire(blocking=False):
        raise RuntimeError("另一个仓库操作正在进行，请稍后再试")
    steps = []
    try:
        rc, out, err = git(path, "add", "-A", timeout=120)
        steps.append({"step": "add", "ok": rc == 0,
                      "detail": (out + "\n" + err).strip() or ("git add 完成" if rc == 0 else "git add 失败")})
        if rc != 0:
            return steps
        rc, _, _ = git(path, "diff", "--cached", "--quiet")
        if rc == 0:
            steps.append({"step": "commit", "ok": True, "detail": "没有变更，跳过提交"})
            return steps
        rc, out, err = git(path, "commit", "-m", message, timeout=120)
        steps.append({"step": "commit", "ok": rc == 0,
                      "detail": (out + "\n" + err).strip() or ("提交完成" if rc == 0 else "提交失败")})
        if rc != 0:
            return steps
        rc, up_out, _ = git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if rc != 0:
            steps.append({"step": "push", "ok": True, "detail": "没有上游分支，跳过推送"})
            return steps
        rc, out, err = git(path, "push", timeout=180)
        steps.append({"step": "push", "ok": rc == 0,
                      "detail": (out + "\n" + err).strip() or ("推送完成" if rc == 0 else "推送失败")})
        return steps
    finally:
        ACTION_LOCK.release()


def open_in_editor(path):
    exe = None
    cmd = CONFIG.get("editor_command") or DEFAULT_EDITOR
    try:
        parts = shlex.split(cmd, posix=False)
    except ValueError:
        parts = [cmd]
    if parts:
        exe = shutil.which(parts[0])
        if not exe:
            for cand in ("code", "cursor", "windsurf", "claude"):
                found = shutil.which(cand)
                if found:
                    exe, parts = found, [found]
                    break
    if not exe:
        os.startfile(path)
        return "未找到编辑器命令，已用资源管理器打开"
    subprocess.Popen([*parts, path], creationflags=CREATE_NO_WINDOW)
    return "已在编辑器中打开"


def open_in_terminal(path):
    wt = shutil.which("wt")
    if wt:
        subprocess.Popen([wt, "-d", path])
        return "已在 Windows Terminal 打开"
    subprocess.Popen(["cmd.exe", "/c", "start", "", "cmd", "/k",
                      'cd /d "%s"' % path])
    return "已在 cmd 打开"


def open_folder(path):
    os.startfile(path)
    return "已在资源管理器打开"


# ---------------------------------------------------------------- HTTP 服务

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class RepoWatchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler_cls, control_token):
        super().__init__(addr, handler_cls)
        self.control_token = control_token


class Handler(BaseHTTPRequestHandler):
    server_version = "RepoWatch/" + APP_VERSION

    # ---- 基础输出与鉴权（对齐 PortWatch） ----

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.address_string(), fmt % args)

    def _request_host_allowed(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _has_control_cookie(self):
        cookie = self.headers.get("Cookie")
        if not cookie:
            return False
        parsed = SimpleCookie()
        try:
            parsed.load(cookie)
        except Exception:
            return False
        morsel = parsed.get(COOKIE_NAME)
        return bool(morsel and morsel.value == self.server.control_token)

    def _deny(self, status, message):
        try:
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorize(self, mutating=False):
        if not self._request_host_allowed():
            self._deny(403, "请求被拒绝：Host 不允许")
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                parts = urllib.parse.urlsplit(origin)
                allowed = parts.scheme == "http" and parts.netloc == (self.headers.get("Host") or "")
            except ValueError:
                allowed = False
            if not allowed:
                self._deny(403, "请求被拒绝：来源不允许")
                return False
        if mutating and not self._has_control_cookie():
            self._deny(403, "访问被拒绝，请从 RepoWatch 页面重试")
            return False
        return True

    def _send(self, body: bytes, status=200, ctype="text/plain; charset=utf-8",
              set_cookie=True):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if set_cookie else "no-cache")
            if set_cookie:
                self.send_header("Set-Cookie",
                                 "%s=%s; HttpOnly; SameSite=Strict; Path=/" % (
                                     COOKIE_NAME, self.server.control_token))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, obj, status=200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(payload, status, "application/json; charset=utf-8")

    def read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    # ---- 路由 ----

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/":
            return self._serve_static(os.path.join(STATIC_DIR, "index.html"))
        if path.startswith("/static/"):
            return self._serve_static(os.path.join(STATIC_DIR, path[len("/static/"):]))
        if not self.authorize(mutating=False):
            return
        if path == "/api/health":
            return self.send_json({"ok": True, "version": APP_VERSION,
                                   "scanning": STATE["scanning"]})
        if path == "/api/repos":
            return self.send_json({"ok": True, **STATE})
        if path == "/api/config":
            return self.send_json({"ok": True, "config": CONFIG,
                                   "defaults": default_config()})
        self._send(b"404 Not Found", 404, set_cookie=False)

    def do_POST(self):
        if not self.authorize(mutating=True):
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        body = self.read_json_body() if path not in ("/api/rescan",) else {}

        if path == "/api/rescan":
            threading.Thread(target=run_scan, daemon=True).start()
            return self.send_json({"ok": True, "message": "扫描已开始"})
        if path == "/api/config":
            if body is None:
                return self.send_json({"ok": False, "error": "请求体无效"}, 400)
            return self._update_config(body)
        if path == "/api/snapshot-all":
            return self._snapshot_all(body)
        m = re.match(r"^/api/repos/([0-9a-fA-F]{12})/(open|snapshot)$", path)
        if m:
            return self._repo_action(m.group(1), m.group(2), body)
        self._send(b"404 Not Found", 404, set_cookie=False)

    def _serve_static(self, filepath):
        filepath = os.path.abspath(filepath)
        if not filepath.startswith(os.path.abspath(STATIC_DIR) + os.sep):
            self._deny(403, "禁止访问")
            return
        if not os.path.isfile(filepath):
            self._send(b"404 Not Found", 404, set_cookie=False)
            return
        ext = os.path.splitext(filepath)[1].lower()
        ctype = STATIC_TYPES.get(ext, "application/octet-stream")
        with open(filepath, "rb") as f:
            self._send(f.read(), 200, ctype, set_cookie=True)

    def _update_config(self, body):
        global CONFIG
        try:
            if "roots" in body:
                roots = [os.path.abspath(os.path.expanduser(str(r)))
                         for r in body["roots"] if str(r).strip()]
                CONFIG["roots"] = roots
            if "editor_command" in body and str(body["editor_command"]).strip():
                CONFIG["editor_command"] = str(body["editor_command"]).strip()
            if "terminal_command" in body and str(body["terminal_command"]).strip():
                CONFIG["terminal_command"] = str(body["terminal_command"]).strip()
            if isinstance(body.get("rescan_seconds"), int) and 10 <= body["rescan_seconds"] <= 3600:
                CONFIG["rescan_seconds"] = body["rescan_seconds"]
            save_config(CONFIG)
            threading.Thread(target=run_scan, daemon=True).start()
            return self.send_json({"ok": True, "config": CONFIG})
        except (OSError, ValueError) as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 400)

    def _find_repo(self, repo_id):
        for repo in STATE["repos"]:
            if repo["id"] == repo_id:
                return repo
        return None

    def _repo_action(self, repo_id, action, body):
        repo = self._find_repo(repo_id)
        if not repo:
            return self.send_json({"ok": False, "error": "仓库不存在，请先重新扫描"}, 404)
        path = repo["path"]
        if not os.path.isdir(path):
            return self.send_json({"ok": False, "error": "目录已不存在"}, 404)
        try:
            if action == "open":
                mode = (body or {}).get("mode", "editor")
                if mode == "terminal":
                    detail = open_in_terminal(path)
                elif mode == "folder":
                    detail = open_folder(path)
                else:
                    detail = open_in_editor(path)
                return self.send_json({"ok": True, "message": detail})
            if action == "snapshot":
                message = str((body or {}).get("message") or "").strip()
                if not message:
                    message = "chore: snapshot %s" % time.strftime("%Y-%m-%d %H:%M")
                steps = do_snapshot(path, message)
                ok = all(s["ok"] for s in steps)
                return self.send_json({"ok": ok, "repo": repo["name"], "steps": steps})
        except RuntimeError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 409)
        except Exception as exc:
            LOG.exception("仓库操作失败 %s", path)
            return self.send_json({"ok": False, "error": str(exc)}, 500)

    def _snapshot_all(self, body):
        message = str((body or {}).get("message") or "").strip()
        if not message:
            message = "chore: snapshot %s" % time.strftime("%Y-%m-%d %H:%M")
        targets = [r for r in STATE["repos"] if r["dirty"] > 0]
        if not targets:
            return self.send_json({"ok": True, "results": [], "message": "没有需要快照的仓库"})
        results = []
        for repo in targets:
            try:
                steps = do_snapshot(repo["path"], message)
                results.append({"repo": repo["name"], "path": repo["path"],
                                "ok": all(s["ok"] for s in steps), "steps": steps})
            except RuntimeError as exc:
                results.append({"repo": repo["name"], "path": repo["path"],
                                "ok": False, "steps": [{"step": "lock", "ok": False, "detail": str(exc)}]})
        ok = all(r["ok"] for r in results)
        return self.send_json({"ok": ok, "results": results,
                               "summary": "%d/%d 个仓库快照成功" % (
                                   sum(1 for r in results if r["ok"]), len(results))})


def auto_rescan_loop():
    """后台周期重扫，发现新增仓库。"""
    while True:
        try:
            time.sleep(int(CONFIG.get("rescan_seconds") or AUTO_RESCAN_SECONDS))
        except ValueError:
            time.sleep(AUTO_RESCAN_SECONDS)
        run_scan()


# ---------------------------------------------------------------- 启动

def find_port(preferred=None):
    if preferred:
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", preferred))
            s.close()
            return preferred
        except OSError:
            pass
    for port in PORT_RANGE:
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError("9610-9619 端口均被占用")


def write_pid():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PID_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def main():
    parser = argparse.ArgumentParser(description="RepoWatch 仓库总控台")
    parser.add_argument("--preferred-port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--scan-now", action="store_true", help="启动后立即扫描一次（默认后台线程自动扫描）")
    args = parser.parse_args()

    setup_logging()
    write_pid()
    port = find_port(args.preferred_port)
    control_token = secrets.token_urlsafe(32)
    server = RepoWatchServer(("127.0.0.1", port), Handler, control_token)
    LOG.info("RepoWatch 启动: http://127.0.0.1:%d/", port)

    threading.Thread(target=run_scan, daemon=True).start()
    threading.Thread(target=auto_rescan_loop, daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open("http://127.0.0.1:%d/" % port)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.remove(PID_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    main()

