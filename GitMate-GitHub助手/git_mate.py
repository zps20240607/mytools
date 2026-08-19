# -*- coding: utf-8 -*-
"""GitMate 1.0 — GitHub 图形助手（Windows，Python 3 标准库，零依赖）。

面向 git 新手的图形化工具：
- 在 GitHub 创建对应仓库并把本地代码推送上去（git init → 建仓 → 关联 → 推送）
- 图形化分支管理：查看 / 创建 / 切换 / 合并 / 删除，冲突时给出中文提示并可放弃合并
- 查看我的 GitHub 仓库列表，一键克隆到本地

凭据：GitHub Token 用 Windows DPAPI（CryptProtectData）加密后存 data\\secret.bin，
不落明文；只监听 127.0.0.1，写操作要求 HttpOnly 会话 Cookie。

运行：python git_mate.py [--preferred-port 9650] [--no-browser]
"""
import argparse
import base64
import ctypes
import ctypes.wintypes
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
STATIC_DIR = os.path.join(BASE_DIR, "static")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
SECRET_PATH = os.path.join(DATA_DIR, "secret.bin")
PID_PATH = os.path.join(DATA_DIR, "server.pid")
LOG_PATH = os.path.join(LOGS_DIR, "console.log")

APP_VERSION = "1.0.0"
PORT_RANGE = range(9650, 9660)
CREATE_NO_WINDOW = 0x08000000
COOKIE_NAME = "gitmate_session"
MAX_BODY = 512 * 1024
GITHUB_API = "https://api.github.com"

SKIP_DIRS = {
    ".git", "node_modules", "bower_components", "__pycache__", ".venv", "venv",
    "env", ".env", "site-packages", "dist", "build", "out", ".next", ".nuxt",
    ".cache", ".idea", ".vscode", ".vs", "target", ".gradle", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".history", "appdata", "windows", "program files",
    "program files (x86)", "$recycle.bin", "system volume information",
    "onedrivetemp", "temp", ".cargo", ".rustup", ".npm", ".local", ".ollama",
    ".lmstudio", ".huggingface", "onedrive", "bin", "lib", "include", "packages",
}

LOG = logging.getLogger("gitmate")


def setup_logging():
    os.makedirs(LOGS_DIR, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


# ---------------------------------------------------------------- 配置与凭据

def default_config():
    roots = [
        os.path.expandvars(r"%USERPROFILE%\Desktop"),
        os.path.expandvars(r"%USERPROFILE%\Documents"),
    ]
    for cand in (r"C:\dev", r"D:\dev", r"D:\code", r"D:\projects"):
        if os.path.isdir(cand):
            roots.append(cand)
    return {"roots": roots, "default_private": False}


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if isinstance(data.get("roots"), list):
                cfg["roots"] = data["roots"]
            if isinstance(data.get("default_private"), bool):
                cfg["default_private"] = data["default_private"]
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_protect(plain: bytes) -> bytes:
    """DPAPI 加密（绑定当前 Windows 用户）。"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _DATA_BLOB(len(plain), ctypes.cast(
        ctypes.create_string_buffer(plain), ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(blob_in), "GitMate GitHub Token", None,
                                    None, None, 0x1, ctypes.byref(blob_out)):
        raise RuntimeError("DPAPI 加密失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def dpapi_unprotect(blob: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _DATA_BLOB(len(blob), ctypes.cast(
        ctypes.create_string_buffer(blob), ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                      0x1, ctypes.byref(blob_out)):
        raise RuntimeError("DPAPI 解密失败（是否换了 Windows 账户？）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def save_token(token):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SECRET_PATH, "wb") as f:
        f.write(base64.b64encode(dpapi_protect(token.encode("utf-8"))))


def load_token():
    try:
        with open(SECRET_PATH, "rb") as f:
            raw = base64.b64decode(f.read().strip())
        return dpapi_unprotect(raw).decode("utf-8")
    except (OSError, ValueError, RuntimeError):
        return None


def clear_token():
    try:
        os.remove(SECRET_PATH)
    except OSError:
        pass


def mask_token(token):
    if len(token) <= 8:
        return "****"
    return token[:7] + "…" + token[-4:]


# ---------------------------------------------------------------- GitHub API

def gh_request(method, path, token=None, body=None):
    """调用 GitHub REST API；先 Bearer，401 时回退 token（兼容 classic PAT）。"""
    token = token or load_token()
    if not token:
        raise RuntimeError("尚未配置 GitHub Token")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    attempts = [("Bearer " + token,), ("token " + token,)]
    last_err = None
    for auth in attempts:
        req = urllib.request.Request(GITHUB_API + path, data=data, method=method)
        req.add_header("Authorization", auth[0])
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "GitMate/1.0")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                try:
                    return resp.status, json.loads(payload.decode("utf-8"))
                except ValueError:
                    return resp.status, {"raw": payload.decode("utf-8", "replace")}
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code != 401:
                try:
                    return exc.code, json.loads(exc.read().decode("utf-8"))
                except ValueError:
                    return exc.code, {"message": str(exc)}
        except urllib.error.URLError as exc:
            last_err = exc
            break
    raise RuntimeError("GitHub API 请求失败: %s" % last_err)


def gh_get_user(token=None):
    status, data = gh_request("GET", "/user", token)
    if status != 200:
        raise RuntimeError("验证失败: %s" % data.get("message", status))
    return data


def gh_create_repo(name, description, private, token=None):
    body = {"name": name, "description": description or "", "private": bool(private),
            "auto_init": False}
    status, data = gh_request("POST", "/user/repos", token, body)
    if status in (200, 201):
        return data
    if status == 422:
        raise RuntimeError("GitHub 上已存在同名仓库: %s（可换个名字，或直接推送已有仓库）" % name)
    raise RuntimeError("创建仓库失败: %s" % data.get("message", status))


def gh_list_repos(token=None):
    status, data = gh_request("GET", "/user/repos?per_page=100&sort=updated", token)
    if status != 200:
        raise RuntimeError("获取仓库列表失败: %s" % (data.get("message") if isinstance(data, dict) else status))
    return data


# ---------------------------------------------------------------- Git 工具

def git(repo_path, *args, timeout=60):
    env = os.environ.copy()
    env.update({"LANG": "C.UTF-8", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    cmd = ["git", "-C", repo_path, "-c", "core.quotepath=false", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env,
                              creationflags=CREATE_NO_WINDOW)
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace").strip(),
                proc.stderr.decode("utf-8", "replace").strip())
    except subprocess.TimeoutExpired:
        return (124, "", "git 命令超时")
    except OSError as exc:
        return (127, "", str(exc))


def sanitize_name(name):
    name = re.sub(r"[^\w\-.]", "-", name.strip() or "repo")
    name = name.strip(".-")
    return name or "repo"


def sanitize_branch(name):
    name = re.sub(r"[^\w\-./]", "-", name.strip())
    name = name.strip(".-/")
    name = re.sub(r"/{2,}", "/", name)
    name = re.sub(r"(^|/)\./", r"\1", name)
    return name or "branch"


def find_repos(roots, max_depth=5):
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
                key = os.path.normcase(os.path.abspath(cur))
                if key not in seen:
                    seen.add(key)
                    found.append(cur)
                continue
            for e in entries:
                try:
                    if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                        continue
                    if e.is_dir(follow_symlinks=False) and e.name.lower() not in SKIP_DIRS:
                        stack.append((e.path, depth + 1))
                except OSError:
                    continue
    return sorted(found, key=lambda p: p.lower())


def repo_brief(path):
    info = {
        "id": hashlib.md5(path.encode("utf-8", "replace")).hexdigest()[:12],
        "path": path, "name": os.path.basename(path.rstrip("\\/")) or path,
        "branch": "", "dirty": 0, "remote": "", "github": False,
    }
    rc, out, _ = git(path, "branch", "--show-current", timeout=10)
    info["branch"] = out.splitlines()[0] if rc == 0 and out else "(无提交)"
    rc, out, _ = git(path, "status", "--porcelain", timeout=15)
    if rc == 0:
        info["dirty"] = len([l for l in out.splitlines() if l.strip()])
    rc, out, _ = git(path, "remote", "get-url", "origin", timeout=10)
    if rc == 0 and out:
        info["remote"] = out.splitlines()[0]
        info["github"] = "github.com" in out.lower()
    return info


def branch_list(path):
    """本地/远程分支 + 当前 + 合并状态 + 领先落后。"""
    rc, out, _ = git(path, "branch", "--format=%(refname:short)", timeout=15)
    locals_ = [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []
    rc2, out2, _ = git(path, "branch", "--format=%(refname)", "-r", timeout=15)
    remote_names = []
    if rc2 == 0:
        for l in out2.splitlines():
            l = l.strip()
            if not l or l.endswith("/HEAD"):
                continue
            remote_names.append(l[len("refs/remotes/"):] if l.startswith("refs/remotes/") else l)
    rc3, out3, _ = git(path, "branch", "--show-current", timeout=10)
    current = out3 if rc3 == 0 else ""
    rc4, out4, _ = git(path, "branch", "--merged", "--format=%(refname:short)", timeout=15)
    merged = {l.strip() for l in out4.splitlines() if l.strip()} if rc4 == 0 else set()
    branches = []
    for name in locals_:
        entry = {"name": name, "current": name == current, "merged": name in merged,
                 "upstream": "", "ahead": 0, "behind": 0}
        rc5, out5, _ = git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                           name + "@{u}", timeout=10)
        if rc5 == 0 and out5:
            entry["upstream"] = out5
            rc6, out6, _ = git(path, "rev-list", "--left-right", "--count",
                               name + "..." + out5, timeout=15)
            if rc6 == 0 and out6:
                parts = out6.split()
                if len(parts) == 2:
                    entry["ahead"], entry["behind"] = int(parts[0]), int(parts[1])
        branches.append(entry)
    local_names = set(locals_)
    remotes = []
    for name in remote_names:
        if name.endswith("/HEAD"):
            continue
        entry = {"name": name, "ahead": 0, "behind": 0,
                 "tracked": False, "local": ""}
        local_name = name.split("/", 1)[-1] if "/" in name else name
        if local_name in local_names:
            entry["tracked"] = True
            entry["local"] = local_name
            rc6, out6, _ = git(path, "rev-list", "--left-right", "--count",
                               local_name + "..." + name, timeout=15)
            if rc6 == 0 and out6:
                parts = out6.split()
                if len(parts) == 2:
                    entry["ahead"], entry["behind"] = int(parts[0]), int(parts[1])
        remotes.append(entry)
    return {"branches": branches, "remotes": remotes, "current": current,
            "dirty": repo_brief(path)["dirty"]}


ACTION_LOCK = threading.Lock()


def with_lock(fn, *args, **kwargs):
    if not ACTION_LOCK.acquire(blocking=False):
        raise RuntimeError("另一个 Git 操作正在进行，请稍后再试")
    try:
        return fn(*args, **kwargs)
    finally:
        ACTION_LOCK.release()


def create_branch(path, name, source=None):
    name = sanitize_branch(name)
    if not name:
        raise RuntimeError("分支名无效")
    rc, out, err = git(path, "checkout", "-b", name, *([source] if source else []), timeout=60)
    if rc != 0:
        raise RuntimeError("创建分支失败: %s" % (err or "未知错误"))
    return {"ok": True, "branch": name, "message": "已创建并切换到分支 %s" % name}


def checkout_remote(path, name):
    """把远程分支检出为同名本地分支并切换（自动跟踪上游）。"""
    name = (name or "").strip()
    if not name or name.startswith("-") or name.endswith("/HEAD"):
        raise RuntimeError("远程分支无效")
    if "/" not in name:
        raise RuntimeError("远程分支无效")
    info = repo_brief(path)
    if info["dirty"] > 0:
        raise RuntimeError("有 %d 个未提交变更，切换分支会覆盖工作区。请先提交或快照。" % info["dirty"])
    local_name = name.split("/", 1)[-1]
    existing = {b["name"] for b in branch_list(path)["branches"]}
    if local_name in existing:
        raise RuntimeError("本地分支 %s 已存在，可直接在本地分支列表中切换" % local_name)
    rc, out, err = git(path, "switch", "-c", local_name, name, timeout=60)
    if rc != 0:
        raise RuntimeError("检出本地分支失败: %s" % (err or "未知错误"))
    return {"ok": True, "branch": local_name,
            "message": "已创建并切换到本地分支 %s（跟踪 %s）" % (local_name, name)}


def switch_branch(path, name):
    info = repo_brief(path)
    if info["dirty"] > 0:
        raise RuntimeError("有 %d 个未提交变更，切换分支会覆盖工作区。请先提交或快照。" % info["dirty"])
    rc, out, err = git(path, "switch", name, timeout=60)
    if rc != 0:
        raise RuntimeError("切换失败: %s" % (err or "未知错误"))
    return {"ok": True, "branch": name, "message": "已切换到 %s" % name}


def merge_branch(path, name):
    rc, out, err = git(path, "merge", "--no-ff", "-m",
                       "merge: %s" % name, name, timeout=180)
    if rc == 0:
        return {"ok": True, "conflict": False, "message": "合并成功", "detail": out}
    conflicted = []
    rc2, out2, _ = git(path, "status", "--porcelain", timeout=15)
    if rc2 == 0:
        for line in out2.splitlines():
            if line.startswith(("UU", "AA", "DD", "AU", "UA", "DU", "UD")):
                conflicted.append(line[3:])
    if not conflicted:
        raise RuntimeError("合并失败: %s" % (err or "未知错误"))
    return {"ok": False, "conflict": True,
            "message": "合并产生冲突（%d 个文件）。请在编辑器中解决冲突后提交，"
                       "或点击「放弃合并」回到合并前状态。" % len(conflicted),
            "files": conflicted, "detail": err}


def abort_merge(path):
    rc, out, err = git(path, "merge", "--abort", timeout=60)
    if rc != 0:
        raise RuntimeError("放弃合并失败: %s" % (err or "未知错误"))
    return {"ok": True, "message": "已放弃合并，工作区恢复到合并前状态"}


def delete_branch(path, name, force=False):
    if branch_list(path)["current"] == name:
        raise RuntimeError("不能删除当前所在的分支，请先切换到其他分支")
    rc, out, err = git(path, "branch", "-D" if force else "-d", name, timeout=60)
    if rc != 0:
        raise RuntimeError("删除失败: %s" % (err or "未知错误"))
    return {"ok": True, "message": "已删除分支 %s" % name}


def pull_ff(path):
    rc, out, err = git(path, "pull", "--ff-only", timeout=180)
    if rc != 0:
        raise RuntimeError("拉取失败: %s" % (err or "未知错误"))
    return {"ok": True, "message": "拉取完成", "detail": out}


def push_current(path):
    rc, out, err = git(path, "push", "-u", "origin", "HEAD", timeout=300)
    if rc != 0:
        raise RuntimeError("推送失败: %s" % (err or "未知错误"))
    return {"ok": True, "message": "推送完成", "detail": out}


def snapshot(path, message=None):
    message = message or ("chore: snapshot %s" % time.strftime("%Y-%m-%d %H:%M"))
    steps = []
    rc, out, err = git(path, "add", "-A", timeout=120)
    steps.append({"step": "add", "ok": rc == 0, "detail": err or "已完成"})
    if rc != 0:
        return steps
    rc, _, _ = git(path, "diff", "--cached", "--quiet")
    if rc == 0:
        steps.append({"step": "commit", "ok": True, "detail": "没有变更，跳过提交"})
        return steps
    rc, out, err = git(path, "commit", "-m", message, timeout=120)
    steps.append({"step": "commit", "ok": rc == 0, "detail": (out + "\n" + err).strip()})
    if rc != 0:
        return steps
    rc, _, _ = git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc != 0:
        steps.append({"step": "push", "ok": True, "detail": "没有上游分支，跳过推送"})
        return steps
    rc, out, err = git(path, "push", timeout=300)
    steps.append({"step": "push", "ok": rc == 0, "detail": (out + "\n" + err).strip()})
    return steps


def publish(path, repo_name=None, private=None, description="", message=None,
            overwrite_remote=False):
    """创建 GitHub 仓库并推送：init → 建仓 → 关联 origin → commit → push。"""
    steps = []
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise RuntimeError("目录不存在: %s" % path)
    repo_name = sanitize_name(repo_name or os.path.basename(path))

    if not os.path.isdir(os.path.join(path, ".git")):
        rc, out, err = git(path, "init", timeout=60)
        if rc != 0:
            raise RuntimeError("git init 失败: %s" % err)
        steps.append({"step": "init", "ok": True, "detail": "已初始化为 Git 仓库"})

    rc, old_remote, _ = git(path, "remote", "get-url", "origin", timeout=10)
    if rc == 0 and old_remote and not overwrite_remote:
        raise RuntimeError("本地已有关联远程 origin (%s)。如需替换，请勾选「覆盖已有 origin」。" % old_remote)

    steps.append({"step": "create", "ok": True, "detail": "正在 GitHub 创建仓库 %s …" % repo_name})
    created = gh_create_repo(repo_name, description, bool(private))
    clone_url = created.get("clone_url") or created.get("html_url") + ".git"
    steps[-1]["detail"] = "GitHub 仓库已创建: %s" % (created.get("html_url") or repo_name)

    if rc == 0 and old_remote:
        rc, out, err = git(path, "remote", "set-url", "origin", clone_url, timeout=30)
        steps.append({"step": "remote", "ok": rc == 0, "detail": "origin 已更新为 %s" % clone_url})
    else:
        rc, out, err = git(path, "remote", "add", "origin", clone_url, timeout=30)
        steps.append({"step": "remote", "ok": rc == 0, "detail": "已关联远程 origin"})
    if rc != 0:
        raise RuntimeError("关联远程失败: %s" % err)

    for s in snapshot(path, message):
        steps.append(s)
    if not any(s["step"] == "commit" and s["ok"] and "跳过" not in s["detail"] for s in steps):
        rc, out, err = git(path, "commit", "--allow-empty", "-m",
                           message or ("chore: snapshot %s" % time.strftime("%Y-%m-%d %H:%M")),
                           timeout=120)
        detail = (out + "\n" + err).strip() or ("已创建初始提交" if rc == 0 else "提交失败")
        steps.append({"step": "commit", "ok": rc == 0, "detail": detail})
    rc_head, _, _ = git(path, "rev-parse", "HEAD")
    if rc_head != 0:
        steps.append({"step": "push", "ok": False, "detail": "仓库没有可推送的提交（前面的提交步骤失败）"})
        return {"ok": False, "steps": steps, "html_url": created.get("html_url", ""), "clone_url": clone_url}
    rc, out, err = git(path, "push", "-u", "origin", "HEAD", timeout=600)
    steps.append({"step": "push", "ok": rc == 0, "detail": (out + "\n" + err).strip()})
    if rc == 0:
        steps.append({"step": "done", "ok": True,
                      "detail": "完成！仓库地址: %s" % (created.get("html_url") or clone_url)})
    return {"ok": all(s["ok"] for s in steps), "steps": steps,
            "html_url": created.get("html_url", ""), "clone_url": clone_url}


def clone_repo(clone_url, root):
    root = os.path.abspath(os.path.expanduser(root))
    os.makedirs(root, exist_ok=True)
    name = sanitize_name(os.path.basename(clone_url.rstrip("/")).removesuffix(".git"))
    target = os.path.join(root, name)
    if os.path.exists(target):
        raise RuntimeError("目标目录已存在: %s" % target)
    env = os.environ.copy()
    proc = subprocess.run(["git", "clone", clone_url, target], capture_output=True,
                          timeout=900, env=env, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError("克隆失败: %s" % proc.stderr.decode("utf-8", "replace").strip())
    return {"ok": True, "path": target, "message": "已克隆到 %s" % target}


# ---------------------------------------------------------------- HTTP

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class GitMateServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler_cls, control_token):
        super().__init__(addr, handler_cls)
        self.control_token = control_token


class Handler(BaseHTTPRequestHandler):
    server_version = "GitMate/" + APP_VERSION

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
            self._deny(403, "访问被拒绝，请从 GitMate 页面重试")
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
            return self.send_json({"ok": True, "version": APP_VERSION})
        if path == "/api/account":
            return self._account()
        if path == "/api/config":
            return self.send_json({"ok": True, "config": load_config()})
        if path == "/api/repos":
            return self._local_repos()
        if path == "/api/repos/overview":
            return self._repos_overview()
        if path == "/api/gh/repos":
            return self._gh_repos()
        m = re.match(r"^/api/repo/([0-9a-fA-F]{12})/branches$", path)
        if m:
            return self._branches(m.group(1))
        self._send(b"404 Not Found", 404, set_cookie=False)

    def do_POST(self):
        if not self.authorize(mutating=True):
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        body = self.read_json_body() or {}
        if path == "/api/auth":
            return self._auth(body)
        if path == "/api/logout":
            clear_token()
            return self.send_json({"ok": True, "message": "已清除凭据"})
        if path == "/api/config":
            return self._config(body)
        if path == "/api/gh/repos":
            return self._gh_repos()
        if path == "/api/gh/clone":
            return self._gh_clone(body)
        if path == "/api/publish":
            return self._publish(body)
        if path == "/api/repo/action":
            return self._repo_action(body)
        m = re.match(r"^/api/repo/([0-9a-fA-F]{12})/(publish|action|branches)$", path)
        if m:
            if m.group(2) == "publish":
                return self._publish({**body, "id": m.group(1)})
            if m.group(2) == "branches":
                return self._branches(m.group(1))
            return self._repo_action({**body, "id": m.group(1)})
        self._send(b"404 Not Found", 404, set_cookie=False)

    # ---- 业务 ----

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

    def _account(self):
        token = load_token()
        if not token:
            return self.send_json({"ok": True, "authed": False})
        try:
            user = gh_get_user(token)
            return self.send_json({"ok": True, "authed": True,
                                   "login": user.get("login"),
                                   "name": user.get("name") or "",
                                   "avatar": user.get("avatar_url") or "",
                                   "token_masked": mask_token(token)})
        except RuntimeError as exc:
            return self.send_json({"ok": True, "authed": False, "error": str(exc)})

    def _auth(self, body):
        token = str(body.get("token") or "").strip()
        if not token:
            return self.send_json({"ok": False, "error": "Token 不能为空"}, 400)
        try:
            user = gh_get_user(token)
        except RuntimeError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 401)
        save_token(token)
        return self.send_json({"ok": True, "login": user.get("login"),
                               "token_masked": mask_token(token),
                               "message": "验证成功，已加密保存凭据"})

    def _config(self, body):
        cfg = load_config()
        if isinstance(body.get("roots"), list):
            cfg["roots"] = [os.path.abspath(os.path.expanduser(str(r)))
                            for r in body["roots"] if str(r).strip()]
        if isinstance(body.get("default_private"), bool):
            cfg["default_private"] = body["default_private"]
        save_config(cfg)
        return self.send_json({"ok": True, "config": cfg})

    def _local_repos(self):
        cfg = load_config()
        repos = [repo_brief(p) for p in find_repos(cfg["roots"])]
        return self.send_json({"ok": True, "repos": repos, "roots": cfg["roots"]})

    def _repos_overview(self):
        """分支总览：每个仓库的本地/远程分支、当前分支、领先落后、脏文件数。"""
        cfg = load_config()

        def build(path):
            brief = repo_brief(path)
            row = {"id": brief["id"], "path": path, "name": brief["name"],
                   "remote": brief["remote"]}
            try:
                data = branch_list(path)
                row.update({
                    "current": data["current"] or "(无提交)",
                    "dirty": data["dirty"],
                    "branches": data["branches"],
                    "remotes": data["remotes"],
                    "ahead": 0, "behind": 0,
                })
                for b in data["branches"]:
                    if b["current"]:
                        row["ahead"], row["behind"] = b["ahead"], b["behind"]
                        break
            except Exception as exc:
                row["error"] = str(exc)
            return row

        repos = find_repos(cfg["roots"])
        if len(repos) <= 2:
            rows = [build(p) for p in repos]
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(6, len(repos))) as pool:
                rows = list(pool.map(build, repos))
        return self.send_json({"ok": True, "repos": rows, "roots": cfg["roots"]})

    def _gh_repos(self):
        try:
            repos = gh_list_repos()
        except RuntimeError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 401)
        rows = [{"name": r.get("name"), "full_name": r.get("full_name"),
                 "html_url": r.get("html_url"), "clone_url": r.get("clone_url"),
                 "private": r.get("private"), "default_branch": r.get("default_branch"),
                 "description": r.get("description") or "",
                 "updated_at": (r.get("updated_at") or "")[:10]}
                for r in repos]
        return self.send_json({"ok": True, "repos": rows})

    def _gh_clone(self, body):
        clone_url = str(body.get("clone_url") or "")
        root = str(body.get("root") or "").strip() or load_config()["roots"][0]
        if not clone_url:
            return self.send_json({"ok": False, "error": "缺少 clone_url"}, 400)
        try:
            result = clone_repo(clone_url, root)
            return self.send_json({"ok": True, **result})
        except RuntimeError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 409)

    def _find_repo(self, repo_id):
        cfg = load_config()
        for p in find_repos(cfg["roots"]):
            if repo_brief(p)["id"] == repo_id:
                return p
        return None

    def _branches(self, repo_id):
        path = self._find_repo(repo_id)
        if not path:
            return self.send_json({"ok": False, "error": "仓库不存在，请刷新列表"}, 404)
        try:
            data = branch_list(path)
            return self.send_json({"ok": True, "path": path, **data})
        except Exception as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 500)

    def _publish(self, body):
        path = str(body.get("path") or "").strip()
        if not path:
            repo_id = str(body.get("id") or "")
            path = self._find_repo(repo_id)
        if not path:
            return self.send_json({"ok": False, "error": "请先选择本地目录"}, 400)
        try:
            result = with_lock(
                publish, path,
                repo_name=body.get("name") or None,
                private=body.get("private"),
                description=str(body.get("description") or ""),
                message=str(body.get("message") or "").strip() or None,
                overwrite_remote=bool(body.get("overwrite_remote")))
            return self.send_json({"ok": True, **result})
        except RuntimeError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 409)

    def _repo_action(self, body):
        path = self._find_repo(str(body.get("id") or ""))
        if not path:
            return self.send_json({"ok": False, "error": "仓库不存在，请刷新列表"}, 404)
        action = str(body.get("action") or "")
        try:
            if action == "create_branch":
                result = with_lock(create_branch, path, str(body.get("name") or ""),
                                   body.get("source") or None)
            elif action == "switch":
                result = with_lock(switch_branch, path, str(body.get("name") or ""))
            elif action == "checkout_remote":
                result = with_lock(checkout_remote, path, str(body.get("name") or ""))
            elif action == "merge":
                result = with_lock(merge_branch, path, str(body.get("name") or ""))
            elif action == "abort_merge":
                result = with_lock(abort_merge, path)
            elif action == "delete_branch":
                result = with_lock(delete_branch, path, str(body.get("name") or ""),
                                   bool(body.get("force")))
            elif action == "pull":
                result = with_lock(pull_ff, path)
            elif action == "push":
                result = with_lock(push_current, path)
            elif action == "snapshot":
                steps = with_lock(snapshot, path, str(body.get("message") or "").strip() or None)
                result = {"ok": all(s["ok"] for s in steps), "steps": steps}
            else:
                return self.send_json({"ok": False, "error": "未知操作"}, 400)
            return self.send_json({"ok": True, **result})
        except RuntimeError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 409)
        except Exception as exc:
            LOG.exception("仓库操作失败")
            return self.send_json({"ok": False, "error": str(exc)}, 500)


# ---------------------------------------------------------------- 启动

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
    raise RuntimeError("9650-9659 端口均被占用")


def write_pid():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PID_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def main():
    parser = argparse.ArgumentParser(description="GitMate GitHub 图形助手")
    parser.add_argument("--preferred-port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    setup_logging()
    write_pid()
    port = find_port(args.preferred_port)
    control_token = secrets.token_urlsafe(32)
    server = GitMateServer(("127.0.0.1", port), Handler, control_token)
    LOG.info("GitMate 启动: http://127.0.0.1:%d/", port)
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
