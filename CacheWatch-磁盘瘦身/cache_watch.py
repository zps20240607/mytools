# -*- coding: utf-8 -*-
"""CacheWatch 1.0 — 磁盘瘦身（Windows，Python 3 标准库，零依赖）。

扫描 node_modules / __pycache__ 及 pip、uv、npm、pnpm、bun、cargo、go、
gradle、huggingface、ollama 等缓存目录，可视化占用，支持预览（dry-run）
与安全一键清理；.git 目录只展示大小并提供 git gc 压缩。

- 只监听 127.0.0.1；写操作要求 HttpOnly 会话 Cookie。
- 删除前严格校验：目标必须是预期类型目录、非 junction/符号链接、位于允许范围内。

运行：python cache_watch.py [--preferred-port 9630] [--no-browser]
      python cache_watch.py scan [--json]
      python cache_watch.py clean <id> [--dry-run]
"""
import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
STATIC_DIR = os.path.join(BASE_DIR, "static")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
PID_PATH = os.path.join(DATA_DIR, "server.pid")
LOG_PATH = os.path.join(LOGS_DIR, "console.log")

APP_VERSION = "1.0.0"
PORT_RANGE = range(9630, 9640)
CREATE_NO_WINDOW = 0x08000000
COOKIE_NAME = "cachewatch_session"
MAX_BODY = 256 * 1024

# 扫描根目录时跳过的目录
SKIP_DIRS = {
    ".git", "dist", "build", "out", ".next", ".nuxt", ".idea", ".vscode", ".vs",
    "target", ".gradle", ".cache", ".venv", "venv", "env", "site-packages",
    "appdata", "windows", "program files", "program files (x86)", "$recycle.bin",
    "system volume information", "onedrive", "onedrivetemp", "bin", "lib",
    "include", "packages", "winsxs",
}

PYCACHE_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox"}

# 固定缓存目录：kind -> (显示名, 路径模板, 是否默认参与“全部清理”)
def fixed_caches():
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    return [
        ("pip", "pip 缓存", os.path.join(local, "pip", "cache"), True),
        ("uv", "uv 缓存", os.path.join(local, "uv", "cache"), True),
        ("npm", "npm 缓存", os.path.join(local, "npm-cache"), True),
        ("pnpm", "pnpm 缓存", os.path.join(local, "pnpm-cache"), True),
        ("pnpm-store", "pnpm 存储", os.path.join(local, "pnpm", "store"), True),
        ("bun", "bun 缓存", os.path.join(home, ".bun", "install", "cache"), True),
        ("yarn", "Yarn 缓存", os.path.join(local, "Yarn", "Cache"), True),
        ("go", "Go 构建缓存", os.path.join(local, "go-build"), True),
        ("gradle", "Gradle 缓存", os.path.join(home, ".gradle", "caches"), True),
        ("huggingface", "HuggingFace 缓存", os.path.join(home, ".cache", "huggingface"), True),
        ("nuget", "NuGet 包缓存", os.path.join(home, ".nuget", "packages"), True),
        ("electron", "Electron 缓存", os.path.join(local, "electron", "Cache"), True),
        ("cargo", "Cargo registry", os.path.join(home, ".cargo", "registry"), False),
        ("ollama", "Ollama 模型", os.path.join(home, ".ollama", "models"), False),
        ("lmstudio", "LM Studio 模型", os.path.join(home, ".lmstudio", "models"), False),
        ("temp", "用户临时目录", os.environ.get("TEMP") or os.path.join(local, "Temp"), False),
    ]

LOG = logging.getLogger("cachewatch")


def setup_logging():
    os.makedirs(LOGS_DIR, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def default_config():
    roots = [
        os.path.expandvars(r"%USERPROFILE%\Desktop"),
        os.path.expandvars(r"%USERPROFILE%\Documents"),
    ]
    for cand in (r"C:\dev", r"D:\dev", r"D:\code", r"D:\projects"):
        if os.path.isdir(cand):
            roots.append(cand)
    return {"roots": roots}


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("roots"), list):
            cfg["roots"] = data["roots"]
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


# ---------------------------------------------------------------- 扫描

def is_reparse(path, entry=None):
    try:
        if entry is not None:
            if entry.is_symlink():
                return True
            if hasattr(entry, "is_junction") and entry.is_junction():
                return True
            return False
        if os.path.islink(path):
            return True
    except OSError:
        pass
    return False


def dir_size(path):
    """迭代计算目录大小；跳过 junction / 符号链接。"""
    total, files = 0, 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if hasattr(e, "is_junction") and e.is_junction():
                    continue
                if e.is_file(follow_symlinks=False):
                    total += e.stat(follow_symlinks=False).st_size
                    files += 1
                elif e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
            except OSError:
                continue
    return total, files


def find_dirs(roots, names, max_depth=6, limit=500):
    """在根目录下找指定名称的目录，返回列表。"""
    found, seen = [], set()
    names_lower = {n.lower() for n in names}
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        stack = [(root, 0)]
        while stack:
            cur, depth = stack.pop()
            if depth > max_depth or len(found) >= limit:
                continue
            try:
                entries = list(os.scandir(cur))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                        continue
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if e.name.lower() in names_lower:
                    key = os.path.normcase(e.path)
                    if key not in seen:
                        seen.add(key)
                        found.append(e.path)
                elif e.name.lower() not in SKIP_DIRS:
                    stack.append((e.path, depth + 1))
    return found


def find_git_dirs(roots, max_depth=6):
    found, seen = [], set()
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
                git_path = os.path.join(cur, ".git")
                if os.path.normcase(git_path) not in seen:
                    seen.add(os.path.normcase(git_path))
                    found.append(git_path)
                continue
            for e in entries:
                try:
                    if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                        continue
                    if e.is_dir(follow_symlinks=False) and e.name.lower() not in SKIP_DIRS:
                        stack.append((e.path, depth + 1))
                except OSError:
                    continue
    return found


def build_items(config):
    """扫描所有清理项，返回 (items, stats)。"""
    items = []
    roots = config["roots"]

    for path in find_dirs(roots, ["node_modules"], max_depth=5):
        size, files = dir_size(path)
        items.append(_item("node_modules", "node_modules", path, size, files, True))

    for path in find_dirs(roots, list(PYCACHE_NAMES), max_depth=6, limit=800):
        size, files = dir_size(path)
        name = os.path.basename(path)
        items.append(_item("pycache", name, path, size, files, True))

    for kind, label, path, safe in fixed_caches():
        if path and os.path.isdir(path):
            size, files = dir_size(path)
            items.append(_item(kind, label, path, size, files, safe))

    for git_path in find_git_dirs(roots, max_depth=6):
        size, files = dir_size(git_path)
        items.append(_item("git", ".git（仓库历史）", git_path, size, files, False))

    items.sort(key=lambda x: x["size"], reverse=True)
    stats = {
        "items": len(items),
        "total_bytes": sum(i["size"] for i in items),
        "cleanable_bytes": sum(i["size"] for i in items if i["safe"]),
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return items, stats


def _item(kind, label, path, size, files, safe):
    return {
        "id": hashlib.md5((kind + "|" + path).encode("utf-8", "replace")).hexdigest()[:12],
        "kind": kind, "label": label, "path": path,
        "size": size, "files": files, "safe": safe,
    }


STATE = {"items": [], "stats": {}, "scanning": False, "config": load_config()}
SCAN_LOCK = threading.Lock()
ACTION_LOCK = threading.Lock()


def run_scan():
    if not SCAN_LOCK.acquire(blocking=False):
        return
    try:
        STATE["scanning"] = True
        t0 = time.time()
        items, stats = build_items(STATE["config"])
        stats["scan_seconds"] = round(time.time() - t0, 1)
        STATE.update({"items": items, "stats": stats})
        LOG.info("扫描完成: %d 项，共 %.1f GB，耗时 %.1fs",
                 len(items), stats["total_bytes"] / 1024 ** 3, time.time() - t0)
    except Exception:
        LOG.exception("扫描失败")
    finally:
        STATE["scanning"] = False
        SCAN_LOCK.release()


# ---------------------------------------------------------------- 清理

ALLOWED_DYNAMIC = {"node_modules", "__pycache__", ".pytest_cache",
                   ".mypy_cache", ".ruff_cache", ".tox"}
SAFE_FIXED_KINDS = {k for k, _, _, safe in fixed_caches() if safe}


def _assert_safe_target(path, kind, config):
    """删除前的安全校验：路径类型、名称、范围、非链接。"""
    if not os.path.isdir(path):
        raise RuntimeError("目录不存在: %s" % path)
    if is_reparse(path):
        raise RuntimeError("拒绝删除 junction/符号链接: %s" % path)
    name = os.path.basename(path.rstrip("\\/"))
    if kind in ALLOWED_DYNAMIC or kind == "pycache":
        if kind == "pycache":
            if name.lower() not in PYCACHE_NAMES:
                raise RuntimeError("目录名与 Python 缓存类型不符: %s" % name)
        elif os.path.normcase(name) != os.path.normcase(kind):
            raise RuntimeError("目录名与类型不符: %s" % name)
        # 必须在扫描根目录范围内
        allowed = False
        target = os.path.normcase(os.path.abspath(path))
        for root in config["roots"]:
            root = os.path.normcase(os.path.abspath(os.path.expanduser(root)))
            if target.startswith(root + os.sep) or target == root:
                allowed = True
                break
        if not allowed:
            raise RuntimeError("目录不在扫描范围内，拒绝删除")
    else:
        # 固定缓存：路径必须与配置完全一致
        expected = [p for k, _, p, _ in fixed_caches() if k == kind]
        if os.path.normcase(os.path.abspath(path)) not in {
                os.path.normcase(os.path.abspath(p)) for p in expected}:
            raise RuntimeError("缓存路径与配置不一致，拒绝删除")
    # 额外护栏：不允许删除盘符根 / 用户主目录 / Windows 目录
    norm = os.path.normcase(os.path.abspath(path))
    protected = {
        os.path.normcase(os.path.expanduser("~")),
        os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows")),
        os.path.normcase(os.environ.get("SystemDrive", "C:") + "\\"),
    }
    if norm in protected:
        raise RuntimeError("受保护目录，拒绝删除")
    return True


def _rmtree(path):
    def fix(func, target, exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass
    shutil.rmtree(path, onerror=fix)


def clean_item(item_id, dry_run=False, config=None):
    """清理单个项目；返回结果字典。"""
    if not ACTION_LOCK.acquire(blocking=False):
        raise RuntimeError("另一个清理操作正在进行，请稍后再试")
    try:
        config = config or STATE["config"]
        item = next((i for i in STATE["items"] if i["id"] == item_id), None)
        if not item:
            raise RuntimeError("清理项不存在，请先重新扫描")
        if not item["safe"]:
            raise RuntimeError("该类型默认不参与清理（可能需重新下载模型等），请三思后手动处理")
        path = item["path"]
        _assert_safe_target(path, item["kind"], config)
        size = item["size"]
        if dry_run:
            return {"ok": True, "dry_run": True, "id": item_id,
                    "freed": size, "message": "预览：将释放 %.2f GB" % (size / 1024 ** 3)}
        _rmtree(path)
        remaining = os.path.isdir(path)
        LOG.info("已清理 %s (%s, %.2f GB)", path, item["kind"], size / 1024 ** 3)
        return {"ok": not remaining, "dry_run": False, "id": item_id, "freed": size,
                "message": "已清理 %.2f GB" % (size / 1024 ** 3) if not remaining else "部分文件可能被占用，未能完全删除"}
    finally:
        ACTION_LOCK.release()


def clean_many(item_ids, dry_run=False, config=None):
    results = []
    for item_id in item_ids:
        try:
            results.append(clean_item(item_id, dry_run=dry_run, config=config))
        except RuntimeError as exc:
            results.append({"ok": False, "dry_run": dry_run, "id": item_id, "message": str(exc)})
    freed = sum(r.get("freed", 0) for r in results if r.get("ok"))
    return {"ok": all(r.get("ok") for r in results), "results": results,
            "summary": "%s %d 项，%s %.2f GB" % (
                "预览将清理" if dry_run else "已清理", len(results),
                "共释放" if not dry_run else "预估释放", freed / 1024 ** 3)}


def git_gc(git_path):
    """对 .git 目录执行 git gc 压缩（安全操作）。"""
    repo = os.path.dirname(git_path)
    if not ACTION_LOCK.acquire(blocking=False):
        raise RuntimeError("另一个操作正在进行，请稍后再试")
    try:
        before, _ = dir_size(git_path)
        env = os.environ.copy()
        env.update({"LANG": "C.UTF-8", "LC_ALL": "C"})
        proc = subprocess.run(
            ["git", "-C", repo, "gc", "--prune=now"],
            capture_output=True, timeout=300, env=env, creationflags=CREATE_NO_WINDOW)
        after, _ = dir_size(git_path)
        if proc.returncode == 0:
            return {"ok": True, "before": before, "after": after,
                    "message": "压缩完成：%.2f GB → %.2f GB" % (before / 1024 ** 3, after / 1024 ** 3)}
        return {"ok": False, "message": "git gc 失败: %s" %
                (proc.stderr.decode("utf-8", "replace").strip() or "未知错误")}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "git gc 超时"}
    finally:
        ACTION_LOCK.release()


# ---------------------------------------------------------------- HTTP

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class CacheWatchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler_cls, control_token):
        super().__init__(addr, handler_cls)
        self.control_token = control_token


class Handler(BaseHTTPRequestHandler):
    server_version = "CacheWatch/" + APP_VERSION

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
            self._deny(403, "访问被拒绝，请从 CacheWatch 页面重试")
            return False
        return True

    def _send(self, body: bytes, status=200, ctype="text/plain; charset=utf-8", set_cookie=True):
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
        if path == "/api/scan":
            return self.send_json({"ok": True, **STATE})
        if path == "/api/config":
            return self.send_json({"ok": True, "config": STATE["config"]})
        self._send(b"404 Not Found", 404, set_cookie=False)

    def do_POST(self):
        if not self.authorize(mutating=True):
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        body = self.read_json_body() if path != "/api/rescan" else {}
        if path == "/api/rescan":
            threading.Thread(target=run_scan, daemon=True).start()
            return self.send_json({"ok": True, "message": "扫描已开始"})
        if path == "/api/config":
            return self._update_config(body)
        if path == "/api/clean":
            return self._clean(body)
        if path == "/api/git-gc":
            return self._git_gc(body)
        m = re.match(r"^/api/clean/([0-9a-fA-F]{12})$", path)
        if m:
            return self._clean({**body, "id": m.group(1)}) if body else self._clean({"id": m.group(1)})
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
        with open(filepath, "rb") as f:
            self._send(f.read(), 200, STATIC_TYPES.get(ext, "application/octet-stream"))

    def _update_config(self, body):
        if body is None or "roots" not in body:
            return self.send_json({"ok": False, "error": "请求体无效"}, 400)
        roots = [os.path.abspath(os.path.expanduser(str(r)))
                 for r in body["roots"] if str(r).strip()]
        STATE["config"]["roots"] = roots
        save_config(STATE["config"])
        threading.Thread(target=run_scan, daemon=True).start()
        return self.send_json({"ok": True, "config": STATE["config"]})

    def _clean(self, body):
        if not body:
            return self.send_json({"ok": False, "error": "请求体无效"}, 400)
        dry_run = bool(body.get("dry_run"))
        try:
            if body.get("id"):
                result = clean_item(str(body["id"]), dry_run=dry_run)
                threading.Thread(target=run_scan, daemon=True).start()
                return self.send_json({"ok": True, **result})
            ids = [str(i) for i in body.get("ids", [])]
            if not ids:
                return self.send_json({"ok": False, "error": "未指定清理项"}, 400)
            result = clean_many(ids, dry_run=dry_run)
            threading.Thread(target=run_scan, daemon=True).start()
            return self.send_json({"ok": True, **result})
        except RuntimeError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 409)
        except Exception as exc:
            LOG.exception("清理失败")
            return self.send_json({"ok": False, "error": str(exc)}, 500)

    def _git_gc(self, body):
        if not body or not body.get("id"):
            return self.send_json({"ok": False, "error": "请求体无效"}, 400)
        item = next((i for i in STATE["items"] if i["id"] == str(body["id"]) and i["kind"] == "git"), None)
        if not item:
            return self.send_json({"ok": False, "error": "git 项目不存在"}, 404)
        result = git_gc(item["path"])
        threading.Thread(target=run_scan, daemon=True).start()
        return self.send_json({"ok": result.get("ok"), **result})


# ---------------------------------------------------------------- CLI

def find_port(preferred=None):
    import socket
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
    raise RuntimeError("9630-9639 端口均被占用")


def write_pid():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PID_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def cli_scan(args):
    items, stats = build_items(load_config())
    if args.json:
        print(json.dumps({"ok": True, "items": items, "stats": stats},
                         ensure_ascii=False, indent=2))
        return
    print("CacheWatch 扫描结果 · %s" % stats["scanned_at"])
    print("共 %d 项，合计 %.2f GB，其中可安全清理 %.2f GB" % (
        stats["items"], stats["total_bytes"] / 1024 ** 3,
        stats["cleanable_bytes"] / 1024 ** 3))
    for i in items:
        flag = "可清理" if i["safe"] else "需谨慎"
        print("  [%s] %-8s %-18s %8.1f MB  %s" % (
            i["id"], i["kind"], i["label"][:16], i["size"] / 1024 ** 2, i["path"]))


def cli_clean(args):
    items, _ = build_items(load_config())
    STATE['items'] = items
    item = next((i for i in items if i["id"] == args.id), None)
    if not item:
        print("未找到清理项: %s" % args.id)
        sys.exit(1)
    try:
        result = clean_item(args.id, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except RuntimeError as exc:
        print("拒绝执行: %s" % exc)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="CacheWatch 磁盘瘦身")
    sub = parser.add_subparsers(dest="command")
    scan_p = sub.add_parser("scan", help="扫描并输出占用报告")
    scan_p.add_argument("--json", action="store_true")
    clean_p = sub.add_parser("clean", help="清理指定项目")
    clean_p.add_argument("id", help="项目 id")
    clean_p.add_argument("--dry-run", action="store_true", help="只预览不删除")
    parser.add_argument("--preferred-port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.command == "scan":
        return cli_scan(args)
    if args.command == "clean":
        return cli_clean(args)

    setup_logging()
    write_pid()
    port = find_port(args.preferred_port)
    control_token = secrets.token_urlsafe(32)
    server = CacheWatchServer(("127.0.0.1", port), Handler, control_token)
    LOG.info("CacheWatch 启动: http://127.0.0.1:%d/", port)
    threading.Thread(target=run_scan, daemon=True).start()
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
