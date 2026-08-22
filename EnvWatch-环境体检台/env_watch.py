# -*- coding: utf-8 -*-
"""EnvWatch 1.0 — 环境体检台（Windows，Python 3 标准库，零依赖）。

汇总常用开发工具版本，检查 PATH 冲突 / 重复项 / 失效目录 / Store 垫片等问题，
生成 HTML 体检报告并输出控制台摘要。

运行：
    python env_watch.py                  # 扫描 + 控制台摘要 + 生成并打开 HTML 报告
    python env_watch.py --no-browser     # 不自动打开报告
    python env_watch.py --json           # 只输出 JSON（不生成 HTML）
"""
import argparse
import ctypes
import datetime
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORT_PATH = os.path.join(DATA_DIR, "report.html")
APP_VERSION = "1.0.0"
CREATE_NO_WINDOW = 0x08000000
PROBE_TIMEOUT = 8

# (命令, 显示名, 分类, 说明)
TOOLS = [
    ("python", "Python", "语言运行时", "Python 解释器"),
    ("python3", "Python 3 别名", "语言运行时", "部分脚本使用 python3 调用"),
    ("py", "Python 启动器", "语言运行时", "Windows py launcher"),
    ("pip", "pip", "语言运行时", "Python 包安装器"),
    ("node", "Node.js", "语言运行时", "JavaScript 运行时"),
    ("npm", "npm", "语言运行时", "Node 包管理器"),
    ("npx", "npx", "语言运行时", "Node 包执行器"),
    ("java", "Java", "语言运行时", "Java 运行时"),
    ("dotnet", ".NET", "语言运行时", ".NET SDK/运行时"),
    ("gcc", "GCC", "语言运行时", "C/C++ 编译器"),
    ("cl", "MSVC cl", "语言运行时", "Visual C++ 编译器"),
    ("rustc", "Rust", "语言运行时", "Rust 编译器"),
    ("cargo", "Cargo", "语言运行时", "Rust 包管理器"),
    ("go", "Go", "语言运行时", "Go 工具链"),
    ("uv", "uv", "包管理", "极速 Python 包管理器"),
    ("pipx", "pipx", "包管理", "Python 全局工具安装器"),
    ("pnpm", "pnpm", "包管理", "高效 Node 包管理器"),
    ("yarn", "Yarn", "包管理", "Node 包管理器"),
    ("bun", "Bun", "包管理", "JS 运行时与包管理器"),
    ("git", "Git", "版本控制", "分布式版本控制"),
    ("gh", "GitHub CLI", "版本控制", "GitHub 命令行"),
    ("docker", "Docker", "容器与系统", "容器运行时"),
    ("wsl", "WSL", "容器与系统", "Windows Linux 子系统"),
    ("ffmpeg", "FFmpeg", "媒体与文档", "音视频处理"),
    ("magick", "ImageMagick", "媒体与文档", "图片批量处理"),
    ("pandoc", "Pandoc", "媒体与文档", "文档格式转换"),
    ("tesseract", "Tesseract", "媒体与文档", "OCR 识别"),
    ("yt-dlp", "yt-dlp", "媒体与文档", "视频下载"),
    ("rg", "ripgrep", "终端与检索", "高速文本搜索"),
    ("fzf", "fzf", "终端与检索", "模糊查找器"),
    ("fd", "fd", "终端与检索", "文件查找"),
    ("jq", "jq", "终端与检索", "JSON 处理"),
    ("bat", "bat", "终端与检索", "语法高亮 cat"),
    ("curl", "curl", "终端与检索", "HTTP 客户端"),
    ("7z", "7-Zip", "终端与检索", "压缩解压"),
    ("cmake", "CMake", "构建工具", "跨平台构建"),
    ("make", "make", "构建工具", "构建自动化"),
    ("ollama", "Ollama", "AI 相关", "本地大模型"),
    ("aider", "Aider", "AI 相关", "终端 AI 编程"),
    ("opencode", "opencode", "AI 相关", "终端 AI 编程"),
    ("code", "VS Code", "编辑器", "代码编辑器"),
    ("cursor", "Cursor", "编辑器", "AI 编辑器"),
    ("wt", "Windows Terminal", "编辑器", "现代终端"),
    ("winget", "winget", "系统工具", "Windows 包管理器"),
    ("choco", "Chocolatey", "系统工具", "Windows 包管理器"),
    ("scoop", "Scoop", "系统工具", "Windows 包管理器"),
]

VERSION_ARGS = {
    "python": ("--version",), "python3": ("--version",), "py": ("--version",),
    "pip": ("--version",), "node": ("--version",), "npm": ("--version",),
    "npx": ("--version",), "java": ("-version",), "dotnet": ("--version",),
    "gcc": ("--version",), "rustc": ("--version",), "cargo": ("--version",),
    "go": ("version",), "uv": ("--version",), "pipx": ("--version",),
    "pnpm": ("--version",), "yarn": ("--version",), "bun": ("--version",),
    "git": ("--version",), "gh": ("--version",), "docker": ("--version",),
    "wsl": ("--version",), "ffmpeg": ("-version",), "magick": ("-version",),
    "pandoc": ("--version",), "tesseract": ("--version",), "yt-dlp": ("--version",),
    "rg": ("--version",), "fzf": ("--version",), "fd": ("--version",),
    "jq": ("--version",), "bat": ("--version",), "curl": ("--version",),
    "7z": ("-help",), "cmake": ("--version",), "make": ("--version",),
    "ollama": ("--version",), "aider": ("--version",), "opencode": ("--version",),
    "code": ("--version",), "cursor": ("--version",), "wt": ("--version",),
    "winget": ("--version",), "choco": ("--version",), "scoop": ("--version",),
}


def run_cmd(cmd, args, timeout=PROBE_TIMEOUT):
    try:
        proc = subprocess.run([cmd, *args], capture_output=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
        out = proc.stdout.decode("utf-8", "replace").strip()
        err = proc.stderr.decode("utf-8", "replace").strip()
        text = out or err
        return (proc.returncode, (text.splitlines()[0] if text else "")[:200])
    except subprocess.TimeoutExpired:
        return (124, "超时")
    except OSError as exc:
        return (127, str(exc))


def probe_tools():
    results = []
    for cmd, name, category, desc in TOOLS:
        path = shutil.which(cmd)
        entry = {"cmd": cmd, "name": name, "category": category, "desc": desc,
                 "found": bool(path), "path": path or "", "version": "", "status": "missing"}
        if path:
            rc, text = run_cmd(cmd, VERSION_ARGS.get(cmd, ("--version",)))
            entry["version"] = text
            entry["status"] = "ok" if rc == 0 else ("error" if text and text != "超时" else "timeout")
        results.append(entry)
    return results


def analyze_path():
    """分析 PATH：失效目录、重复项、Store 垫片、总长度。"""
    raw = os.environ.get("PATH", "")
    entries = [e for e in raw.split(os.pathsep) if e.strip()]
    seen = {}
    issues = {"missing": [], "duplicates": [], "shims": []}
    for entry in entries:
        norm = os.path.normcase(os.path.normpath(entry))
        if norm in seen:
            issues["duplicates"].append(entry)
            continue
        seen[norm] = entry
        if not os.path.isdir(entry):
            issues["missing"].append(entry)
        elif "windowsapps" in norm:
            issues["shims"].append(entry)
    return {
        "count": len(entries),
        "total_length": len(raw),
        "missing": issues["missing"],
        "duplicates": issues["duplicates"],
        "shims": issues["shims"],
    }


def find_multi_install(cmd, exe_names):
    """在 PATH 的所有目录里找某个命令的多份安装。"""
    hits = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.strip():
            continue
        for name in exe_names:
            full = os.path.join(entry, name)
            if os.path.isfile(full):
                hits.append(full)
    return hits


def analyze_environment(tools):
    checks = []  # {level: ok|warn|error, title, detail}

    def add(level, title, detail=""):
        checks.append({"level": level, "title": title, "detail": detail})

    # Python
    python = next((t for t in tools if t["cmd"] == "python"), None)
    python_hits = find_multi_install("python", ["python.exe"])
    if python and python["found"]:
        store_shim = "windowsapps" in os.path.normcase(python["path"] or "")
        if store_shim:
            add("error", "Python 来自 Microsoft Store 垫片",
                "当前 python 指向 %s。Store 垫片可能未真正安装 Python，建议用 python.org 安装版或 uv。" % python["path"])
        if len(python_hits) > 1:
            add("warn", "PATH 中存在多份 python.exe（%d 份）" % len(python_hits),
                "可能存在版本混乱；第一顺位：%s。" % python["path"])
        if not store_shim and len(python_hits) == 1:
            add("ok", "Python 环境正常", python["version"] or python["path"])
    else:
        add("error", "未找到 python", "几乎所有本地工具都需要 Python 3，请安装 https://www.python.org/downloads/")

    # python3 别名
    py3 = next((t for t in tools if t["cmd"] == "python3"), None)
    if py3 and not py3["found"]:
        add("warn", "缺少 python3 别名", "部分 AI 代理会调用 python3；可用 `py -3` 或在 Python 安装时勾选 py launcher。")

    # Node
    node = next((t for t in tools if t["cmd"] == "node"), None)
    if node and node["found"]:
        add("ok", "Node.js", node["version"])
    else:
        add("warn", "未找到 Node.js", "Web 项目与很多 AI 工具需要 Node，可安装 https://nodejs.org/ 或使用 winget install OpenJS.NodeJS.LTS")

    # Git
    git = next((t for t in tools if t["cmd"] == "git"), None)
    if git and git["found"]:
        add("ok", "Git", git["version"])
    else:
        add("error", "未找到 Git", "vibe coding 的基础设施，建议 winget install Git.Git")

    # uv / pip
    uv = next((t for t in tools if t["cmd"] == "uv"), None)
    if uv and uv["found"]:
        add("ok", "uv 已安装", uv["version"])
    else:
        add("info" if python and python["found"] else "warn", "建议安装 uv",
            "AI 频繁装包时 uv 明显更快：powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"")

    # PATH
    path_info = analyze_path()
    if path_info["missing"]:
        add("warn", "PATH 有 %d 个失效目录" % len(path_info["missing"]),
            "、".join(path_info["missing"][:5]) + (" 等" if len(path_info["missing"]) > 5 else ""))
    if path_info["duplicates"]:
        add("info", "PATH 有 %d 个重复条目" % len(path_info["duplicates"]),
            "重复一般无害，但会增加命令查找时间。")
    if path_info["shims"]:
        add("warn", "PATH 有 %d 个 WindowsApps 垫片目录" % len(path_info["shims"]),
            "来自 Microsoft Store 的垫片可能弹出商店引导，而不是运行真实程序。")
    if path_info["total_length"] > 2000:
        add("warn", "PATH 总长度 %d 字符（超过 2000）" % path_info["total_length"],
            "过长可能被部分工具截断，建议精简。")
    else:
        add("ok", "PATH 共 %d 个目录，长度 %d 字符" % (path_info["count"], path_info["total_length"]))

    # 系统资源
    try:
        import shutil as _sh
        usage = _sh.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
        free_gb = usage.free / 1024 ** 3
        if free_gb < 10:
            add("warn", "系统盘剩余 %.1f GB" % free_gb, "AI 项目 + 模型缓存很吃空间，建议配合 CacheWatch 清理。")
        else:
            add("ok", "系统盘剩余 %.1f GB" % free_gb)
    except OSError:
        pass

    # 临时目录可写
    try:
        with tempfile.NamedTemporaryFile(delete=True):
            add("ok", "临时目录可写", tempfile.gettempdir())
    except OSError:
        add("error", "临时目录不可写", tempfile.gettempdir())

    # WSL / Docker
    wsl = next((t for t in tools if t["cmd"] == "wsl"), None)
    if wsl and not wsl["found"]:
        add("info", "未安装 WSL", "Windows 上很多 Linux 工具链需要它：wsl --install")
    docker = next((t for t in tools if t["cmd"] == "docker"), None)
    if docker and not docker["found"]:
        add("info", "未安装 Docker", "跑数据库 / 服务 demo 时会用到 Docker Desktop。")
    return checks


def system_overview():
    try:
        import platform
        os_name = platform.platform()
    except Exception:
        os_name = "Windows"
    cpu = os.cpu_count() or "?"
    try:
        ram_bytes = ctypes.c_ulonglong()
        if hasattr(ctypes, "windll"):
            kernel32 = ctypes.windll.kernel32
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            ram = "%.1f GB / %.1f GB" % (st.ullAvailPhys / 1024 ** 3, st.ullTotalPhys / 1024 ** 3)
        else:
            ram = "未知"
    except Exception:
        ram = "未知"
    return {"os": os_name, "cpu": cpu, "ram": ram,
            "python": sys.version.split()[0], "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def build_html(tools, checks, path_info, overview):
    level_names = {"ok": "正常", "warn": "警告", "error": "问题", "info": "建议"}
    level_colors = {"ok": "#34c08c", "warn": "#f0a03c", "error": "#e5534b", "info": "#4fa3ff"}

    check_html = "".join(
        '<li class="check %s"><b>%s</b> %s<div class="detail">%s</div></li>' % (
            c["level"], level_names[c["level"]], html.escape(c["title"]),
            html.escape(c["detail"]) if c["detail"] else "")
        for c in checks)

    categories = []
    for cat in ("语言运行时", "包管理", "版本控制", "容器与系统", "媒体与文档",
                "终端与检索", "构建工具", "AI 相关", "编辑器", "系统工具"):
        rows = []
        for t in tools:
            if t["category"] != cat:
                continue
            badge = {"ok": "ok", "missing": "miss", "error": "err", "timeout": "err"}[t["status"]]
            version = html.escape(t["version"]) if t["version"] else "—"
            path = html.escape(t["path"]) if t["path"] else "未找到"
            rows.append(
                '<tr><td>%s</td><td class="%s">%s</td><td class="ver">%s</td>'
                '<td class="path" title="%s">%s</td></tr>' % (
                    html.escape(t["name"]), badge,
                    {"ok": "已安装", "missing": "未安装", "error": "异常", "timeout": "超时"}[t["status"]],
                    version, path, path))
        categories.append('<section><h3>%s</h3><table><tr><th>工具</th><th>状态</th>'
                          '<th>版本</th><th>路径</th></tr>%s</table></section>' % (cat, "".join(rows)))

    path_issues = []
    if path_info["missing"]:
        path_issues.append('<div class="pitem"><b>失效目录（%d）</b><ul>%s</ul></div>' % (
            len(path_info["missing"]),
            "".join('<li>%s</li>' % html.escape(p) for p in path_info["missing"][:10])))
    if path_info["duplicates"]:
        path_issues.append('<div class="pitem"><b>重复条目（%d）</b><ul>%s</ul></div>' % (
            len(path_info["duplicates"]),
            "".join('<li>%s</li>' % html.escape(p) for p in path_info["duplicates"][:10])))
    if path_info["shims"]:
        path_issues.append('<div class="pitem"><b>Store 垫片目录（%d）</b><ul>%s</ul></div>' % (
            len(path_info["shims"]),
            "".join('<li>%s</li>' % html.escape(p) for p in path_info["shims"][:10])))
    if not path_issues:
        path_issues.append('<div class="pitem"><b>PATH 未见明显问题</b></div>')

    n_ok = sum(1 for c in checks if c["level"] == "ok")
    n_warn = sum(1 for c in checks if c["level"] == "warn")
    n_err = sum(1 for c in checks if c["level"] == "error")
    n_tools = sum(1 for t in tools if t["found"])

    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EnvWatch · 环境体检报告</title>
<style>
:root { --bg:#f4f8f9; --panel:#ffffff; --panel2:#eaf1f3;
  --border:rgba(23,50,58,0.10); --border-strong:rgba(23,50,58,0.18);
  --text:#17323a; --muted:#54707a; --faint:#8aa3ab; --accent:#1899a3;
  --ok:#2e9e6b; --warn:#d98e1b; --err:#d1453d; --info:#4f86c6; --neutral:#8aa3ab;
  --shadow:0 1px 2px rgba(23,50,58,0.06);
  --shadow-lift:0 8px 20px rgba(23,50,58,0.10);
  --sans:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
  --mono:ui-monospace,'Cascadia Mono',Consolas,monospace; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.7 var(--sans); }
.wrap { max-width:1080px; margin:0 auto; padding:24px 20px 60px; }
h1 { font-size:24px; letter-spacing:1px; margin:0 0 4px; }
h1 small { color:var(--muted); font-size:13px; font-weight:400; letter-spacing:0.5px; }
.seal { display:inline-block; margin-left:12px; padding:3px 12px; border-radius:999px;
  border:1px solid var(--accent); color:var(--accent); background:rgba(24,153,163,0.07);
  font-size:12px; font-weight:500; line-height:1.4; letter-spacing:2px; vertical-align:6px; }
.rule { height:3px; margin:14px 0 4px; background:var(--accent); border-radius:2px; }
h3 { margin:26px 0 10px; font-size:16px; letter-spacing:1px;
  border-left:3px solid var(--accent); padding-left:10px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:18px 0; }
.card { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:10px 14px;
  box-shadow:var(--shadow); transition:transform .15s ease, box-shadow .15s ease; }
.card:hover { transform:translateY(-2px); box-shadow:var(--shadow-lift); }
.card b { display:block; font-size:18px; color:var(--accent); letter-spacing:0.5px; }
.card span { color:var(--muted); font-size:12px; }
.summary { display:flex; gap:10px; flex-wrap:wrap; }
.pill { border-radius:999px; padding:3px 12px; font-size:12px; font-weight:500;
  border:1px solid var(--border-strong); background:var(--panel); color:var(--muted); }
.pill.ok { color:var(--ok); border-color:var(--ok); background:rgba(46,158,107,0.08); }
.pill.warn { color:var(--warn); border-color:var(--warn); background:rgba(217,142,27,0.08); }
.pill.err { color:var(--err); border-color:var(--err); background:rgba(209,69,61,0.08); }
ul.check { list-style:none; padding:0; display:flex; flex-direction:column; gap:8px; }
li.check { background:var(--panel); border:1px solid var(--border); border-left:3px solid var(--info); border-radius:8px; padding:8px 12px; box-shadow:var(--shadow); }
li.check.ok { border-left-color:var(--ok); } li.check.warn { border-left-color:var(--warn); }
li.check.error { border-left-color:var(--err); }
li.check b { margin-right:6px; font-size:12px; }
li.check.ok b { color:var(--ok); } li.check.warn b { color:var(--warn); } li.check.error b { color:var(--err); }
li.check.info b { color:var(--info); }
li.check .detail { color:var(--muted); font-size:12px; margin-top:2px; word-break:break-all; }
table { width:100%%; border-collapse:collapse; background:var(--panel); border:1px solid var(--border); border-radius:10px; overflow:hidden; box-shadow:var(--shadow); }
th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--border); font-size:13px; }
th { color:var(--muted); font-weight:500; background:var(--panel2); }
td.ok { color:var(--ok); font-weight:500; } td.miss { color:var(--faint); } td.err { color:var(--err); font-weight:500; }
td.ver, td.path { font-family:var(--mono); font-size:12px; color:var(--muted); word-break:break-all; }
.pitem { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:10px 14px; margin-bottom:8px; box-shadow:var(--shadow); }
.pitem ul { margin:4px 0 0; padding-left:20px; font-family:var(--mono); font-size:12px; color:var(--muted); word-break:break-all; }
footer { margin-top:30px; padding-top:12px; border-top:1px solid var(--border); color:var(--faint); font-size:12px; }
</style></head><body><div class="wrap">
<h1>EnvWatch <small>环境体检报告 · %s</small><span class="seal">体检</span></h1>
<div class="rule"></div>
<div class="cards">
  <div class="card"><b>%s</b><span>操作系统</span></div>
  <div class="card"><b>%s</b><span>CPU 核心</span></div>
  <div class="card"><b>%s</b><span>内存（可用/总量）</span></div>
  <div class="card"><b>%s</b><span>当前 Python</span></div>
  <div class="card"><b>%d / %d</b><span>已装工具</span></div>
</div>
<div class="summary">
  <span class="pill ok">正常 %d 项</span><span class="pill warn">警告 %d 项</span>
  <span class="pill err">问题 %d 项</span>
</div>
<h3>体检结论</h3><ul class="check">%s</ul>
<h3>PATH 检查</h3>%s
<h3>工具版本清单</h3>%s
<footer>EnvWatch %s · 生成时间 %s · 本报告全部数据来自本机，不上传任何信息。</footer>
</div></body></html>""" % (
        overview["time"], html.escape(overview["os"]), overview["cpu"], overview["ram"],
        overview["python"], n_tools, len(tools),
        n_ok, n_warn, n_err, check_html,
        "".join(path_issues), "".join(categories),
        APP_VERSION, overview["time"])


def build_summary(tools, checks, path_info, overview):
    lines = []
    lines.append("=" * 56)
    lines.append("EnvWatch 环境体检报告 · %s" % overview["time"])
    lines.append("=" * 56)
    lines.append("系统: %s | CPU %s 核 | 内存 %s | Python %s" % (
        overview["os"], overview["cpu"], overview["ram"], overview["python"]))
    lines.append("工具: %d/%d 已安装 | 结论: 正常 %d · 警告 %d · 问题 %d" % (
        sum(1 for t in tools if t["found"]), len(tools),
        sum(1 for c in checks if c["level"] == "ok"),
        sum(1 for c in checks if c["level"] == "warn"),
        sum(1 for c in checks if c["level"] == "error")))
    lines.append("-" * 56)
    for c in checks:
        if c["level"] != "ok":
            label = {"warn": "[警告]", "error": "[问题]", "info": "[建议]"}[c["level"]]
            lines.append("%s %s" % (label, c["title"]))
            if c["detail"]:
                lines.append("    %s" % c["detail"])
    lines.append("-" * 56)
    lines.append("PATH: %d 个目录（长度 %d）· 失效 %d · 重复 %d · Store 垫片 %d" % (
        path_info["count"], path_info["total_length"], len(path_info["missing"]),
        len(path_info["duplicates"]), len(path_info["shims"])))
    lines.append("未安装的工具: %s" % (
        "、".join(t["name"] for t in tools if not t["found"]) or "无"))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="EnvWatch 环境体检台")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开 HTML 报告")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    parser.add_argument("--out", default=None, help="HTML 报告输出路径")
    args = parser.parse_args()

    tools = probe_tools()
    path_info = analyze_path()
    checks = analyze_environment(tools)
    overview = system_overview()

    if args.json:
        print(json.dumps({"ok": True, "overview": overview, "checks": checks,
                          "path": path_info, "tools": tools},
                         ensure_ascii=False, indent=2))
        return

    report_html = build_html(tools, checks, path_info, overview)
    out_path = args.out or REPORT_PATH
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report_html)
    print(build_summary(tools, checks, path_info, overview))
    print("\n报告已生成: %s" % out_path)
    if not args.no_browser:
        webbrowser.open("file:///" + os.path.abspath(out_path).replace("\\", "/"))


if __name__ == "__main__":
    main()
