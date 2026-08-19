# -*- coding: utf-8 -*-
"""PortWatch 2.0 后端（Windows 版，API 契约对齐 laogou717/local-ops「总控台」，MIT）。

- 前端与 API 契约 1:1 还原 local-ops，采集层保留 Windows 原生实现
  （ctypes kernel32 / ntdll / iphlpapi），不依赖 netstat 与周期 powershell；
  仅进程命令行（cmdline）每 30 秒用隐藏窗口 powershell 查询一次。
- 运行：python server.py [--preferred-port 9600] [--no-browser]
"""
import ctypes
import ctypes.wintypes as wintypes
import functools
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 路径与常量

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
ICONS_DIR = os.path.join(DATA_DIR, "icons")
STATIC_DIR = os.path.join(BASE_DIR, "static")
THEMES_DIR = os.path.join(STATIC_DIR, "themes")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
PID_PATH = os.path.join(DATA_DIR, "server.pid")
VERSION_PATH = os.path.join(BASE_DIR, "VERSION")

APP_VERSION = "2.0.0"
CURRENT_SCHEMA_VERSION = 1
DEFAULT_UI_THEME = "ops"
RUN_TOKEN_ENV = "PORTWATCH_RUN_TOKEN"
REFRESH_INTERVAL = 2.0
STATE_CACHE_TTL = 2.2
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 2
CREATE_NO_WINDOW = 0x08000000
TASK_CANCELED_EXIT_CODE = 130
LOGICAL_CORES = max(1, os.cpu_count() or 1)
APP_ROUTE_RE = re.compile(
    r"^/api/apps/([0-9a-fA-F]{8})(?:/(start|stop|restart|icon|logs|favicon|diagnose|attach))?$")

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".otf": "font/otf",
}
ICON_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".svg"}

LOG = logging.getLogger("portwatch")


def _read_version():
    try:
        with open(VERSION_PATH, "r", encoding="utf-8-sig") as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    return APP_VERSION


APP_VERSION = _read_version()

# ---------------------------------------------------------------- 工具

def log(msg):
    LOG.info(msg)


def now_str(ts=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))


def classify_task_exit(code):
    """把一次性任务的退出码归一为稳定的产品语义。"""
    if code == 0:
        return "succeeded"
    if code == TASK_CANCELED_EXIT_CODE:
        return "canceled"
    return "failed"


def public_last_exit(app):
    """兼容旧配置：只在 API 输出时补齐任务状态，不改写磁盘。"""
    value = app.get("lastExit")
    if not isinstance(value, dict):
        return value
    result = dict(value)
    if (app.get("kind") or "service") == "task":
        if result.get("status") == "canceled" and result.get("code") is None:
            result["status"] = "stopped"
        elif (result.get("status") not in
              {"succeeded", "canceled", "failed", "stopped"}
              and isinstance(result.get("code"), int)):
            result["status"] = classify_task_exit(result["code"])
    return result


def new_id():
    return secrets.token_hex(4)


def _pid_alive(pid):
    """ctypes 查询进程是否存在（不启动任何外部进程）。"""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _check_running_instance():
    """已有实例在运行则返回其 pid，否则返回 None（顺带清理过期 pid 文件）。"""
    try:
        with open(PID_PATH, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if _pid_alive(pid):
        return pid
    try:
        os.remove(PID_PATH)
    except OSError:
        pass
    return None


def append_marker(log_path, text):
    try:
        with open(log_path, "ab") as f:
            f.write(("\n\n=== %s ===\n" % text).encode("utf-8", "replace"))
    except OSError:
        pass


def ensure_dirs():
    for d in (DATA_DIR, LOGS_DIR, ICONS_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            LOG.error("创建目录失败 %s: %s", d, e)


# ---------------------------------------------------------------- 配置

def migrate_config(raw):
    """旧版 config（tasks[]）→ 新 schema（apps[]）。返回 (data, source_version)。"""
    if not isinstance(raw, dict):
        raise ValueError("配置必须是 JSON 对象")
    version = raw.get("schemaVersion", 0)
    data = dict(raw)
    apps = []
    for item in raw.get("tasks") or []:
        app = dict(Config.APP_DEFAULT)
        app["id"] = item.get("id") or new_id()
        app["name"] = item.get("name") or "未命名服务"
        app["kind"] = "task" if item.get("mode") == "batch" else "service"
        app["command"] = item.get("command") or ""
        app["cwd"] = item.get("cwd") or None
        app["port"] = item.get("port")
        app["createdAt"] = item.get("createdAt") or int(time.time())
        app["stopPattern"] = item.get("stop_pattern") or None
        apps.append(app)
    if apps:
        data["apps"] = apps
    if "hidden_ports" in raw:
        data["legacyHiddenPorts"] = [int(p) for p in (raw.get("hidden_ports") or [])]
    return data, version


class ConfigSchemaError(ValueError):
    pass


class Config:
    DEFAULT = {"schemaVersion": CURRENT_SCHEMA_VERSION,
               "apps": [], "hidden": [], "pinned": [], "promoted": [],
               "watchedKeywords": [], "uiTheme": DEFAULT_UI_THEME,
               "legacyHiddenPorts": []}
    APP_DEFAULT = {"id": None, "name": "", "command": "", "cwd": None,
                   "port": None, "emoji": None, "glyph": None, "icon": None,
                   "favicon": None, "kind": "service", "lastPid": None,
                   "runToken": None, "attached": False, "lastExit": None,
                   "createdAt": 0, "stopPattern": None}

    def __init__(self, path):
        self._lock = threading.RLock()
        self._path = path
        self._writable = True
        self._recovered_from_backup = False
        self._migration_from = None
        self._health_issues = []
        self._data = self._load()

    @staticmethod
    def _payload(data):
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    @classmethod
    def _normalize(cls, raw):
        data = {"schemaVersion": CURRENT_SCHEMA_VERSION,
                "apps": [], "hidden": [], "pinned": [], "promoted": [],
                "watchedKeywords": [], "uiTheme": DEFAULT_UI_THEME,
                "legacyHiddenPorts": []}
        for key in ("hidden", "pinned", "promoted", "watchedKeywords"):
            value = raw.get(key)
            if isinstance(value, list):
                data[key] = [str(v) for v in value if str(v).strip()]
        theme = raw.get("uiTheme")
        if isinstance(theme, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", theme):
            data["uiTheme"] = theme
        ports = raw.get("legacyHiddenPorts")
        if isinstance(ports, list):
            data["legacyHiddenPorts"] = [int(p) for p in ports if str(p).isdigit()]
        apps = []
        for item in raw.get("apps") or []:
            if not isinstance(item, dict):
                continue
            app = dict(cls.APP_DEFAULT)
            for key in app:
                if key in item:
                    app[key] = item[key]
            if app.get("id") is None:
                app["id"] = new_id()
            if not str(app.get("name") or "").strip():
                continue
            app["kind"] = "task" if app.get("kind") == "task" else "service"
            if app["kind"] == "task":
                app["port"] = None
            apps.append(app)
        data["apps"] = apps
        return data

    def _load(self):
        paths = (self._path, self._path + ".bak")
        found_candidate = False
        for index, path in enumerate(paths):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                migrated, source_version = migrate_config(raw)
                data = self._normalize(migrated)
                if index:
                    self._recovered_from_backup = True
                    LOG.warning("主配置不可读，已从备份恢复: %s", path)
                if source_version < CURRENT_SCHEMA_VERSION:
                    self._migration_from = source_version
                self._persist_loaded_state(
                    data, raw, source_index=index, source_version=source_version)
                return data
            except FileNotFoundError:
                continue
            except (OSError, UnicodeError, json.JSONDecodeError,
                    ConfigSchemaError, TypeError, ValueError):
                found_candidate = True
                LOG.exception("读取配置失败: %s", path)
        data = self._normalize(self.DEFAULT)
        if found_candidate:
            self._writable = False
            self._health_issues.append(
                "主配置与备份均不可读，已进入只读保护状态")
            return data
        try:
            self._write_atomic(self._path, self._payload(data))
        except OSError as e:
            self._writable = False
            self._health_issues.append("无法创建配置文件: %s" % e)
        return data

    def _persist_loaded_state(self, data, raw, source_index, source_version):
        needs_migration = source_version < CURRENT_SCHEMA_VERSION
        if not source_index and not needs_migration:
            return
        try:
            if not source_index and needs_migration:
                self._write_atomic(self._path + ".bak", self._payload(raw))
            self._write_atomic(self._path, self._payload(data))
        except OSError as e:
            self._writable = False
            self._health_issues.append("配置恢复/迁移落盘失败: %s" % e)

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    def health_info(self):
        with self._lock:
            return {
                "writable": self._writable,
                "recoveredFromBackup": self._recovered_from_backup,
                "migratedFromSchema": self._migration_from,
                "issues": list(self._health_issues),
            }

    def update(self, fn):
        with self._lock:
            if not self._writable:
                raise OSError("配置处于只读保护状态，请先恢复配置或权限")
            previous = json.loads(json.dumps(self._data, ensure_ascii=False))
            try:
                result = fn(self._data)
                payload = self._payload(self._data)
                previous_payload = self._payload(previous)
                self._write_atomic(self._path + ".bak", previous_payload)
                self._write_atomic(self._path, payload)
                invalidate_state_cache()
                return result
            except Exception:
                self._data = previous
                raise

    @staticmethod
    def _write_atomic(path, payload):
        ensure_dirs()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def find_app(cfg, app_id):
    for app in cfg.get("apps") or []:
        if app.get("id") == app_id:
            return app
    return None
# ================================================================
# Windows 原生采集（ctypes，不依赖 netstat / 周期 powershell）
# ================================================================

class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.ULONG),
        ("NumberOfThreads", wintypes.ULONG),
        ("Reserved1", ctypes.c_ubyte * 48),
        ("ImageNameLength", wintypes.USHORT),
        ("ImageNameMaxLength", wintypes.USHORT),
        ("ImageNameBuffer", ctypes.c_void_p),
        ("BasePriority", wintypes.LONG),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", wintypes.ULONG),
        ("SessionId", wintypes.ULONG),
        ("PageDirectoryBase", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", wintypes.ULONG),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
        ("KernelTime", ctypes.c_ulonglong),
        ("UserTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_ulonglong),
    ]


class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD), ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD), ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD),
    ]


class _MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("LocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("RemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
_iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)
_ws2 = ctypes.WinDLL("ws2_32", use_last_error=True)

_k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
_k32.GetSystemTimes.argtypes = [ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME)]
_k32.GetTickCount64.restype = ctypes.c_ulonglong
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.TerminateProcess.restype = wintypes.BOOL
_k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(wintypes.ULONG)]
_ntdll.NtQuerySystemInformation.restype = ctypes.c_long
_iphlp.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
_iphlp.GetExtendedTcpTable.restype = wintypes.DWORD
_ws2.inet_ntop.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
_ws2.inet_ntop.restype = ctypes.c_char_p


def _ft_int(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


_query_prev = {"idle": None, "total": None}


def query_system_native():
    ms = _MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not _k32.GlobalMemoryStatusEx(ctypes.byref(ms)):
        return {}
    total = ms.ullTotalPhys
    avail = ms.ullAvailPhys
    cpu = None
    idle = _FILETIME()
    kernel = _FILETIME()
    user = _FILETIME()
    if _k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        i = _ft_int(idle)
        t = _ft_int(kernel) + _ft_int(user)
        if _query_prev["total"] is not None and t != _query_prev["total"]:
            pct = 100.0 * (1.0 - (i - _query_prev["idle"]) / (t - _query_prev["total"]))
            cpu = round(max(0.0, min(100.0, pct)), 1)
        _query_prev["idle"] = i
        _query_prev["total"] = t
    return {
        "cpu": cpu,
        "mem_total_mb": round(total / 1048576, 0),
        "mem_used_mb": round((total - avail) / 1048576, 0),
        "mem_pct": round((total - avail) / total * 100, 1) if total else None,
        "uptime": int(_k32.GetTickCount64() // 1000),
    }


def query_processes_native():
    """全量进程快照：pid → {name, cpu(秒), mem(bytes), ppid, etime}。"""
    size = 512 * 1024
    buf = None
    while size <= 16 * 1024 * 1024:
        buf = ctypes.create_string_buffer(size)
        ret = _ntdll.NtQuerySystemInformation(5, buf, size, None)
        if ret == 0:
            break
        size *= 2
    else:
        return {}
    now_100ns = int(time.time() * 10000000) + 116444736000000000
    res = {}
    off = 0
    while True:
        spi = ctypes.cast(ctypes.byref(buf, off), ctypes.POINTER(_SYSTEM_PROCESS_INFORMATION)).contents
        pid = spi.UniqueProcessId
        if pid:
            name = ""
            if spi.ImageNameBuffer and spi.ImageNameLength:
                try:
                    name = ctypes.wstring_at(spi.ImageNameBuffer, spi.ImageNameLength // 2)
                except Exception:
                    name = ""
            create = spi.CreateTime or now_100ns
            etime = max(0, int((now_100ns - create) / 10000000))
            res[str(int(pid))] = {
                "name": name or "?",
                "cpu": (spi.KernelTime + spi.UserTime) / 10000000.0,
                "mem": spi.WorkingSetSize,
                "ppid": int(spi.InheritedFromUniqueProcessId or 0),
                "etime": etime,
            }
        if not spi.NextEntryOffset:
            break
        off += spi.NextEntryOffset
    return res


def _port_addr_v4(dw):
    return "%d.%d.%d.%d" % tuple(ctypes.c_uint32(dw).value.to_bytes(4, "little"))


def _port_addr_v6(raw16):
    out = ctypes.create_string_buffer(46)
    if _ws2.inet_ntop(23, ctypes.cast(raw16, ctypes.c_void_p), out, 46):
        return out.value.decode("ascii", "replace")
    return "::"


def query_ports_native():
    """监听端口列表：[{addr, port, pid}]（TCP LISTEN，含 v4/v6）。"""
    res = []
    for family in (2, 23):
        size = wintypes.DWORD(0)
        _iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, family, 5, 0)
        if not size.value or size.value > 8 * 1024 * 1024:
            continue
        buf = ctypes.create_string_buffer(size.value)
        if _iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, family, 5, 0) != 0:
            continue
        n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
        if family == 2:
            rows = ctypes.cast(ctypes.byref(buf, 4), ctypes.POINTER(_MIB_TCPROW_OWNER_PID))
            for i in range(n):
                r = rows[i]
                if r.dwState != 2 or not r.dwOwningPid:
                    continue
                port = socket.ntohs(r.dwLocalPort)
                if not port:
                    continue
                res.append({"addr": _port_addr_v4(r.dwLocalAddr), "port": port,
                            "pid": int(r.dwOwningPid)})
        else:
            rows = ctypes.cast(ctypes.byref(buf, 4), ctypes.POINTER(_MIB_TCP6ROW_OWNER_PID))
            for i in range(n):
                r = rows[i]
                if r.dwState != 2 or not r.dwOwningPid:
                    continue
                port = socket.ntohs(r.dwLocalPort)
                if not port:
                    continue
                res.append({"addr": _port_addr_v6(r.LocalAddr), "port": port,
                            "pid": int(r.dwOwningPid)})
    res.sort(key=lambda x: (x["port"], x["pid"]))
    return res


# ---------------------------------------------------------------- 命令行缓存（每 30 秒一次）

def run_ps(script, timeout=30):
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW)
    except Exception:
        return ""
    raw = r.stdout
    if not raw:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def query_cmdlines():
    out = run_ps(
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,ExecutablePath,CommandLine "
        "| ConvertTo-Json -Compress")
    if not out:
        return {}
    try:
        data = json.loads(out)
    except Exception:
        return {}
    if isinstance(data, dict):
        data = [data]
    res = {}
    for it in data or []:
        try:
            pid = int(it.get("ProcessId"))
            res[str(pid)] = {
                "name": it.get("Name") or "",
                "exe": it.get("ExecutablePath") or "",
                "cmdline": it.get("CommandLine") or "",
            }
        except Exception:
            continue
    return res


# ---------------------------------------------------------------- 快照与后台线程

_snap_lock = threading.Lock()
_snap = {"ts": 0.0, "system": {}, "procs": {}, "cmdlines": {}, "ports": []}
_prev_cpu = {}
_runtime = {}          # app_id → {proc, pid, started, log}
_last_run = {}         # app_id → 上次运行信息（进程退出后保留）


def query_system():
    return query_system_native()


def query_processes():
    return query_processes_native()


def query_ports():
    return query_ports_native()


def update_processes():
    global _prev_cpu
    raw = query_processes()
    now = time.time()
    with _snap_lock:
        dt = now - _snap["ts"] if _snap["ts"] else REFRESH_INTERVAL
        if dt <= 0:
            dt = REFRESH_INTERVAL
        procs = {}
        for pid, d in raw.items():
            cpu_sec = d.get("cpu") or 0.0
            old = _prev_cpu.get(pid, cpu_sec)
            pct = max(0.0, (cpu_sec - old) / dt * 100.0)
            procs[pid] = {
                "name": d.get("name") or "?",
                "cpu_pct": round(pct, 1),
                "mem": d.get("mem") or 0,
                "ppid": d.get("ppid") or 0,
                "etime": d.get("etime") or 0,
            }
        _prev_cpu = {pid: (d.get("cpu") or 0.0) for pid, d in raw.items()}
        _snap["procs"] = procs
        _snap["ts"] = now


def watchdog():
    tick = 0
    while True:
        try:
            _snap["system"] = query_system()
        except Exception as e:
            log("system: %s" % e)
        try:
            update_processes()
        except Exception as e:
            log("processes: %s" % e)
        if tick == 1 or tick % 15 == 0:
            try:
                _snap["cmdlines"] = query_cmdlines()
            except Exception as e:
                log("cmdlines: %s" % e)
        try:
            _snap["ports"] = query_ports()
        except Exception as e:
            log("ports: %s" % e)
        try:
            reap()
        except Exception as e:
            log("reap: %s" % e)
        tick += 1
        time.sleep(REFRESH_INTERVAL)


def proc_info(pid):
    with _snap_lock:
        return _snap["procs"].get(str(pid))


def cmdline_of(pid):
    with _snap_lock:
        return (_snap["cmdlines"].get(str(pid)) or {}).get("cmdline") or ""


def exe_of(pid):
    with _snap_lock:
        return (_snap["cmdlines"].get(str(pid)) or {}).get("exe") or ""


def pid_alive(pid):
    return bool(pid and pid > 0 and proc_info(pid) is not None)


def reap():
    """回收已退出的受管进程，记录 lastExit。"""
    with _snap_lock:
        for app_id, info in list(_runtime.items()):
            proc = info.get("proc")
            if proc is not None and proc.poll() is not None:
                # 移动前补上结束时间，否则下面的 3600 秒清理会把
                # 没有 ended 字段的记录当成超时直接删掉，兜底记录失效。
                info["ended"] = info.get("ended") or time.time()
                _runtime.pop(app_id, None)
                _last_run[app_id] = info
        for app_id, info in list(_last_run.items()):
            if not _runtime.get(app_id) and time.time() - (info.get("ended") or 0) > 3600:
                _last_run.pop(app_id, None)
    for app_id, info in list(_last_run.items()):
        if info.get("recorded"):
            continue
        if info.get("stopped_by_user"):
            # 用户手动停止：stop_app_and_clear 已写入 stopped 记录，
            # 这里不能再按退出码记成 failed（taskkill /F 退出码为 1）。
            continue
        proc = info.get("proc")
        code = proc.returncode if proc is not None else None
        started = info.get("started") or time.time()
        ended = info.get("ended") or time.time()
        _record_exit(app_id, code, started, ended)
        info["recorded"] = True


def _record_exit(app_id, code, started, ended):
    """进程退出后把 lastExit 写入配置（通过全局 Config 实例保持内存同步）。"""
    global _config
    cfg = _config
    if cfg is None:
        return
    try:
        def op(c):
            app = find_app(c, app_id)
            if app is None:
                return False
            status = classify_task_exit(code) if code is not None else "stopped"
            app["lastExit"] = {
                "code": code,
                "status": status,
                "at": ended,
                "startedAt": started,
                "durationSec": round(max(0.0, ended - started), 1),
            }
            return True

        cfg.update(op)
        invalidate_state_cache()
    except Exception as e:
        log("record_exit: %s" % e)
# ================================================================
# 服务 / 应用状态构建（对齐 local-ops /api/state 契约）
# ================================================================

HOME_DIR = os.path.expanduser("~")

SYSTEM_SERVICE_NAMES = {
    "svchost.exe", "services.exe", "lsass.exe", "wininit.exe", "csrss.exe",
    "winlogon.exe", "fontdrvhost.exe", "dwm.exe", "dllhost.exe", "spoolsv.exe",
    "sihost.exe", "taskhostw.exe", "smss.exe", "conhost.exe", "ctfmon.exe",
    "explorer.exe", "runtimebroker.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchapp.exe", "searchindexer.exe",
    "msmpeng.exe", "mssense.exe", "backgroundtaskhost.exe", "widgets.exe",
    "textinputhost.exe", "applicationframehost.exe", "systemsettings.exe",
    "securityhealthservice.exe", "audiodg.exe",
    "system", "registry", "memory compression",
    "secure system", "secure system processes",
}

DEV_KEYWORDS = ("python", "node", "npm", "npx", "pnpm", "yarn", "deno",
                "bun", "go", "cargo", "rust", "dotnet", "java", "nginx",
                "redis", "mysql", "postgres", "mongod", "docker", "httpd",
                "uvicorn", "gunicorn", "flask", "django", "vite", "webpack",
                "hexo", "hugo", "vue", "react", "electron", "ollama", "jupyter")

_ORIGIN_SKIP_NAMES = {
    "cmd.exe", "powershell.exe", "pwsh.exe", "python.exe", "pythonw.exe",
    "node.exe", "npm", "npx", "pnpm", "yarn", "deno.exe", "bun.exe",
    "conhost.exe", "taskkill.exe", "where.exe", "chcp.com", "wscript.exe",
    "services.exe", "lsass.exe", "svchost.exe", "wininit.exe", "winlogon.exe",
    "dwm.exe", "dllhost.exe", "spoolsv.exe", "sihost.exe", "runtimebroker.exe",
    "fontdrvhost.exe", "searchindexer.exe", "msmpeng.exe", "mssense.exe",
}

_ORIGIN_AGENT_PATTERNS = (
    (re.compile(r"\bcodex\b", re.I), "Codex"),
    (re.compile(r"claude-code|\bclaude\b", re.I), "Claude Code"),
    (re.compile(r"\bkimi\b", re.I), "Kimi"),
    (re.compile(r"\bgemini\b", re.I), "Gemini"),
    (re.compile(r"\bcursor\b", re.I), "Cursor"),
    (re.compile(r"\bwindsurf\b", re.I), "Windsurf"),
    (re.compile(r"\btrae\b", re.I), "Trae"),
    (re.compile(r"\bqwen\b", re.I), "Qwen"),
    (re.compile(r"\bcodebuddy\b", re.I), "CodeBuddy"),
)

_ORIGIN_APP_ALIASES = {
    "code.exe": ("VS Code", "code"), "cursor.exe": ("Cursor", "code"),
    "trae.exe": ("Trae", "code"), "windsurf.exe": ("Windsurf", "code"),
    "zed.exe": ("Zed", "code"), "idea64.exe": ("IDEA", "code"),
    "pycharm64.exe": ("PyCharm", "code"), "webstorm64.exe": ("WebStorm", "code"),
    "goland64.exe": ("GoLand", "code"),
    "cmd.exe": ("终端", "terminal"), "powershell.exe": ("PowerShell", "terminal"),
    "pwsh.exe": ("PowerShell", "terminal"),
    "windowsterminal.exe": ("Windows Terminal", "terminal"),
    "wt.exe": ("Windows Terminal", "terminal"),
    "explorer.exe": ("资源管理器", "folder"),
    "docker.exe": ("Docker", "package"), "ollama.exe": ("Ollama", "package"),
    "obsidian.exe": ("Obsidian", "package"),
}


def project_name(cwd):
    if not cwd:
        return None
    cwd = cwd.rstrip("\\/")
    if not cwd or cwd.lower() == HOME_DIR.lower() or re.match(r"^[a-z]:$", cwd, re.I):
        return None
    return os.path.basename(cwd) or None


def classify_group(key, name, exe, promoted):
    if key in promoted:
        return "mine"
    text = (name or "").lower()
    if any(k in text for k in DEV_KEYWORDS):
        return "mine"
    if text in SYSTEM_SERVICE_NAMES:
        return "background"
    if exe and re.match(r"^[a-z]:\\windows\\", exe, re.I):
        base = os.path.basename(exe).lower()
        if base not in ("python.exe", "pythonw.exe", "node.exe", "git.exe",
                        "powershell.exe", "pwsh.exe"):
            return "background"
    return "mine"


def attribute_origin(pid, snap_procs, snap_cmdlines):
    """沿 PPID 链识别来源应用，返回 {"label", "icon"} 或 None。"""
    cur, seen, candidate = pid, set(), None
    for _ in range(12):
        info = snap_procs.get(str(cur))
        if not info:
            break
        ppid = info.get("ppid") or 0
        if ppid in seen:
            break
        seen.add(ppid)
        if ppid == SELF_PID:
            return {"label": "PortWatch", "icon": "rocket"}
        if ppid <= 4:
            return candidate or {"label": "系统", "icon": "server"}
        parent_args = (snap_cmdlines.get(str(ppid)) or {}).get("cmdline") or ""
        hay = parent_args.casefold()
        for pattern, label in _ORIGIN_AGENT_PATTERNS:
            if pattern.search(hay):
                return {"label": label, "icon": "bot"}
        parent_exe = (snap_cmdlines.get(str(ppid)) or {}).get("exe") or ""
        parent_name = os.path.basename(parent_exe) or ""
        if parent_name:
            alias = _ORIGIN_APP_ALIASES.get(parent_name.lower())
            if alias:
                return {"label": alias[0], "icon": alias[1]}
        base = (parent_name or (parent_args.split() or ["?"])[0]).lower()
        if base and base not in _ORIGIN_SKIP_NAMES and candidate is None:
            label = parent_name or (parent_args.split()[0] if parent_args.split() else "")
            if label and label != "?":
                candidate = {"label": label, "icon": "package"}
        cur = ppid
    if candidate and candidate.get("label") == "?":
        return None
    return candidate


def listener_open_host(listeners, port, pids=None):
    """返回浏览器访问该端口应使用的主机名。"""
    allowed_pids = set(pids) if pids is not None else None
    hosts = set()
    for (pid, listening_port), values in listeners.items():
        if listening_port != port or (allowed_pids is not None and pid not in allowed_pids):
            continue
        if isinstance(values, str):
            hosts.add(values)
        elif isinstance(values, (set, list, tuple)):
            hosts.update(v for v in values if isinstance(v, str))
    normalized = {h.strip("[]").casefold() for h in hosts if h}
    ipv4_capable = any(h in ("*", "0.0.0.0") or h.startswith("127.") for h in normalized)
    ipv6_loopback_only = bool(normalized) and not ipv4_capable and all(
        h in ("::", "::1", "localhost") for h in normalized)
    return "localhost" if ipv6_loopback_only else "127.0.0.1"


def scan_listeners():
    """{(pid, port): {addr, ...}}"""
    result = {}
    for item in query_ports():
        key = (item["pid"], item["port"])
        result.setdefault(key, set()).add(item["addr"])
    return result


def build_services(cfg, snap):
    """返回 (services, listeners)。对齐 local-ops services[] 结构。"""
    try:
        listeners = scan_listeners()
    except Exception as e:
        LOG.exception("扫描监听端口失败")
        listeners = {}
    procs = snap.get("procs") or {}
    cmdlines = snap.get("cmdlines") or {}
    hidden = set(cfg.get("hidden") or [])
    pinned = set(cfg.get("pinned") or [])
    promoted = set(cfg.get("promoted") or [])
    legacy_hidden_ports = set(cfg.get("legacyHiddenPorts") or [])
    app_by_pid = {}
    for app in cfg.get("apps") or []:
        for pid in _live_pids(app):
            app_by_pid[pid] = app
    services = []
    for (pid, port) in sorted(listeners, key=lambda x: (x[1], x[0])):
        if pid == SELF_PID:
            continue
        info = procs.get(str(pid))
        if not info:
            continue
        name = info.get("name") or "?"
        key = "%s:%d" % (name, port)
        exe = (cmdlines.get(str(pid)) or {}).get("exe") or ""
        cwd = os.path.dirname(exe) if exe else None
        app = app_by_pid.get(pid)
        total_mem = (_snap["system"].get("mem_total_mb") or 1) * 1048576
        mem_pct = round((info.get("mem") or 0) / total_mem * 100, 1)
        services.append({
            "key": key,
            "instanceKey": "%d:%d" % (pid, port),
            "pid": pid, "name": name, "port": port,
            "openHost": listener_open_host(listeners, port, {pid}),
            "cwd": cwd, "project": project_name(cwd),
            "cmd": (cmdlines.get(str(pid)) or {}).get("cmdline") or info.get("name") or "",
            "cpu": info.get("cpu_pct") or 0.0,
            "mem": mem_pct,
            "memPct": mem_pct,
            "uptimeSec": info.get("etime") or 0,
            "group": classify_group(key, name, exe, promoted),
            "pinned": key in pinned,
            "hidden": key in hidden or port in legacy_hidden_ports,
            "promoted": key in promoted,
            "appId": app.get("id") if app else None,
            "appName": app.get("name") if app else None,
            "origin": attribute_origin(pid, procs, cmdlines),
        })
    return services, listeners


def build_watched(keywords, snap):
    normalized = []
    seen_keywords = set()
    for keyword in (keywords or []):
        if not isinstance(keyword, str) or not keyword.strip():
            continue
        keyword = keyword.strip()
        lowered = keyword.casefold()
        if lowered in seen_keywords:
            continue
        seen_keywords.add(lowered)
        normalized.append((keyword, lowered))
    if not normalized:
        return []
    procs = snap.get("procs") or {}
    cmdlines = snap.get("cmdlines") or {}
    total_mem = ((snap.get("system") or {}).get("mem_total_mb") or 1) * 1048576
    result = []
    for pid, info in sorted(procs.items()):
        if str(pid) == str(SELF_PID):
            continue
        name = info.get("name") or "?"
        args = (cmdlines.get(pid) or {}).get("cmdline") or name
        args_lower = args.casefold()
        matched = [keyword for keyword, lowered in normalized if lowered in args_lower]
        if not matched:
            continue
        result.append({
            "pid": int(pid), "name": name, "cmd": args,
            "cpu": info.get("cpu_pct") or 0.0,
            "mem": round((info.get("mem") or 0) / total_mem * 100, 1),
            "uptimeSec": info.get("etime") or 0,
            "keyword": "、".join(matched), "keywords": matched,
        })
    return result


def _cmd_target_paths(app):
    """从启动命令里提取脚本/程序路径，用于重启后按进程命令行认领。"""
    cwd = resolve_cwd(app)
    tokens = _simple_command_tokens(app.get("command") or "") or []
    paths = set()
    for tok in tokens:
        tok = tok.strip('"')
        if not tok:
            continue
        candidate = tok
        if not os.path.isabs(candidate):
            candidate = os.path.join(cwd, candidate)
        try:
            candidate = os.path.abspath(candidate)
        except OSError:
            continue
        if os.path.isfile(candidate) or re.search(
                r"\.(py|pyw|ps1|bat|cmd|js|mjs|cjs|ts|tsx|jsx|sh|exe)$", tok, re.I):
            paths.add(candidate)
    return paths


def _normalize_cmd(cmd):
    """去掉命令行引号并压缩空白，供 stopPattern 等正则匹配使用。"""
    return re.sub(r"\s+", " ", (cmd or "").replace('"', "")).strip()


def _pid_matches_app(app, pid, cmd):
    """进程命令行是否属于该应用：runToken、stopPattern 或脚本路径命中。"""
    cmd = (cmd or "").strip()
    if not cmd:
        return False
    token = app.get("runToken")
    if token and token in cmd:
        return True
    pattern = app.get("stopPattern")
    if pattern:
        try:
            if re.compile(pattern, re.I).search(_normalize_cmd(cmd)):
                return True
        except re.error:
            pass
    cmd_lower = cmd.lower()
    return any(p.lower() in cmd_lower for p in _cmd_target_paths(app))


def _live_pids(app):
    """受管进程 PID 列表：根进程 + 整棵后代进程树。

    Windows 上很多启动命令要经过 cmd.exe 包装（如 python / node 等非
    文件型命令），实际监听端口的是子进程；只有把整棵树算作受管，
    端口归属、停止和溯源才能覆盖到真正的监听者。
    """
    app_id = app.get("id")
    roots = []
    info = _runtime.get(app_id)
    if info and info.get("proc") is not None and info["proc"].poll() is None:
        roots.append(info["pid"])
    if app.get("attached") and app.get("lastPid"):
        pid = app["lastPid"]
        if pid_alive(pid) and pid not in roots:
            roots.append(pid)
    elif not roots and app.get("lastPid"):
        pid = app["lastPid"]
        if pid_alive(pid):
            cmd = cmdline_of(pid)
            if _pid_matches_app(app, pid, cmd):
                roots.append(pid)
    if not roots and app.get("stopPattern"):
        # 包装进程退出后，后台进程会脱离进程树；按正则认领仍存活的匹配进程。
        try:
            pattern = re.compile(app["stopPattern"], re.I)
        except re.error:
            pattern = None
        if pattern:
            with _snap_lock:
                cmdlines = dict(_snap["cmdlines"])
            for cpid, cmeta in cmdlines.items():
                if str(cpid) == str(SELF_PID):
                    continue
                try:
                    cpid_int = int(cpid)
                except (TypeError, ValueError):
                    continue
                if not pid_alive(cpid_int):
                    continue
                if pattern.search(_normalize_cmd(cmeta.get("cmdline"))):
                    roots.append(cpid_int)
                    break
    if not roots:
        return []
    with _snap_lock:
        procs = _snap["procs"]
    children = {}
    for pid_str, info2 in procs.items():
        ppid = info2.get("ppid") or 0
        children.setdefault(ppid, []).append(int(pid_str))
    live = []
    stack = list(roots)
    while stack:
        cur = stack.pop()
        if cur in live:
            continue
        live.append(cur)
        stack.extend(children.get(cur, []))
    return live


def build_apps(cfg, listeners, snap):
    procs = snap.get("procs") or {}
    cmdlines = snap.get("cmdlines") or {}
    listen_by_pid = {}
    for pid, port in listeners:
        listen_by_pid.setdefault(pid, []).append(port)
    configured_ports = {app["port"] for app in cfg.get("apps") or [] if app.get("port")}
    port_map = {}
    for pid, port in listeners:
        port_map.setdefault(port, []).append(pid)
    apps = []
    for app in cfg.get("apps") or []:
        live = _live_pids(app)
        pid = live[0] if live else None
        port = app.get("port")
        configured_listeners = port_map.get(port, []) if port else []
        listening = bool(port and any(p in live for p in configured_listeners))
        occupied = bool(port and configured_listeners and not listening)
        owner_pid = configured_listeners[0] if occupied else None
        owner_info = procs.get(str(owner_pid)) if owner_pid else None
        owner_exe = (cmdlines.get(str(owner_pid)) or {}).get("exe") or ""
        owner_cmd = (cmdlines.get(str(owner_pid)) or {}).get("cmdline") or ""
        port_owner = None
        if owner_pid:
            owner_name = owner_info.get("name") if owner_info else ""
            port_owner = {
                "pid": owner_pid,
                "openHost": listener_open_host(listeners, port, {owner_pid}),
                "name": os.path.basename(owner_exe) or owner_name or "?",
                "cmd": owner_cmd or owner_name or "",
                "cwd": os.path.dirname(owner_exe) if owner_exe else None,
                "project": project_name(os.path.dirname(owner_exe)) if owner_exe else None,
                "currentUser": True,
                "uptimeSec": (owner_info or {}).get("etime") or 0,
                "appId": None, "appName": None,
            }
        actual_ports = sorted({p for member in live for p in listen_by_pid.get(member, [])})
        open_hosts = {
            str(actual_port): listener_open_host(listeners, actual_port, set(live))
            for actual_port in actual_ports
        }
        try:
            health = inspect_app_health(app)
        except Exception as exc:
            LOG.warning("检查应用配置失败（%s）：%s", app.get("id"), exc)
            health = {"status": "unknown", "blocking": False, "issues": []}
        live_info = procs.get(str(pid)) if pid else None
        apps.append({
            "id": app["id"], "name": app["name"], "command": app["command"],
            "cwd": app.get("cwd"), "port": port,
            "emoji": app.get("emoji"), "glyph": app.get("glyph"), "icon": app.get("icon"),
            "favicon": app.get("favicon"),
            "running": bool(live), "pid": pid,
            "uptimeSec": (live_info or {}).get("etime") if pid else None,
            "kind": app.get("kind") or "service",
            "attached": bool(app.get("attached")),
            "stopPattern": app.get("stopPattern") or None,
            "lastExit": public_last_exit(app),
            "health": health,
            "ports": actual_ports,
            "openHosts": open_hosts,
            "listening": listening,
            "portOccupied": occupied,
            "portOccupiedPid": configured_listeners[0] if occupied else None,
            "portOwner": port_owner,
            "portConflict": False,
            "portConflictApps": [],
            "legacyManaged": False,
        })
    return apps



def dedup_by_pid(services, group="mine"):
    """同进程多端口只计一次，返回 (cpu, mem) 合计（去重后）。"""
    seen = set()
    cpu = 0.0
    mem = 0.0
    for s in services:
        if s.get("group") != group or s.get("hidden"):
            continue
        pid = s.get("pid")
        if pid is None or pid in seen:
            continue
        seen.add(pid)
        cpu += s.get("cpu") or 0.0
        mem += s.get("mem") or 0.0
    return round(cpu, 1), round(mem, 1)
def build_state(cfg, console_port, config_health=None):
    degraded_reasons = []
    snap = dict(_snap)
    try:
        services, listeners = build_services(cfg, snap)
        cpu_pct, mem_pct = dedup_by_pid(services)
    except Exception as e:
        LOG.exception("构建服务监控状态失败")
        services, listeners = [], {}
        cpu_pct, mem_pct = 0.0, 0.0
        degraded_reasons.append({"component": "services"})
    cpu_avg_pct = round(cpu_pct / LOGICAL_CORES, 1)
    try:
        watched = build_watched(cfg.get("watchedKeywords"), snap)
    except Exception as e:
        LOG.exception("构建关注进程状态失败")
        watched = []
        degraded_reasons.append({"component": "watched"})
    try:
        apps = build_apps(cfg, listeners, snap)
    except Exception as e:
        LOG.exception("构建启动台状态失败")
        apps = []
        degraded_reasons.append({"component": "apps"})
    for issue in (config_health or {}).get("issues", []):
        degraded_reasons.append({"component": "config", "error": issue})
    return {
        "services": services,
        "cpuPct": cpu_pct,
        "cpuAvgPct": cpu_avg_pct,
        "cpuCores": LOGICAL_CORES,
        "memPct": mem_pct,
        "watched": watched,
        "apps": apps,
        "watchedKeywords": cfg.get("watchedKeywords") or [],
        "consolePort": console_port,
        "consolePid": SELF_PID,
        "consoleCwd": BASE_DIR,
        "version": APP_VERSION,
        "schemaVersion": cfg.get("schemaVersion", CURRENT_SCHEMA_VERSION),
        "degraded": bool(degraded_reasons),
        "degradedReasons": degraded_reasons,
        "configHealth": dict(config_health or {}),
        "uiTheme": cfg.get("uiTheme") or DEFAULT_UI_THEME,
        "themes": list_themes(),
    }


_state_cache_lock = threading.Lock()
_state_cache = {"mono": 0.0, "state": None}


def invalidate_state_cache():
    with _state_cache_lock:
        _state_cache["state"] = None


def get_state_snapshot(cfg, console_port):
    now = time.monotonic()
    with _state_cache_lock:
        cached = _state_cache["state"]
        if cached is not None and now - _state_cache["mono"] < STATE_CACHE_TTL:
            return cached
    # 构建状态时不能持有 _state_cache_lock：Config.update 持 cfg._lock 时
    # 会调用 invalidate_state_cache 抢同一把锁；反过来这里又要拿 cfg._lock
    # 做 snapshot()，两线程同时发生即 ABBA 死锁，故先释放再构建。
    state = build_state(cfg.snapshot(), console_port, cfg.health_info())
    with _state_cache_lock:
        _state_cache["mono"] = time.monotonic()
        _state_cache["state"] = state
    return state


def build_health(cfg):
    health = cfg.health_info()
    issues = list(health.get("issues") or [])
    for label, path in (("data", DATA_DIR), ("icons", ICONS_DIR),
                        ("logs", LOGS_DIR)):
        if not os.path.isdir(path):
            issues.append("%s 目录不存在" % label)
        elif not os.access(path, os.R_OK | os.W_OK):
            issues.append("%s 目录不可读写" % label)
    if not os.path.isfile(CONFIG_PATH):
        issues.append("主配置文件不存在")
    degraded = bool(issues)
    return {
        "ok": not degraded,
        "status": "degraded" if degraded else "ok",
        "version": APP_VERSION,
        "schemaVersion": cfg.snapshot().get("schemaVersion", CURRENT_SCHEMA_VERSION),
        "degraded": degraded,
        "issues": issues,
        "config": health,
    }


def list_themes():
    themes = []
    try:
        names = sorted(os.listdir(THEMES_DIR))
    except OSError:
        return themes
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(THEMES_DIR, name), "r", encoding="utf-8") as f:
                meta = json.load(f)
            theme_id = str(meta.get("id") or os.path.splitext(name)[0])
            if not theme_id or not os.path.isfile(os.path.join(THEMES_DIR, theme_id + ".css")):
                continue
            themes.append({
                "id": theme_id,
                "name": str(meta.get("name") or theme_id),
                "author": str(meta.get("author") or ""),
                "desc": str(meta.get("desc") or ""),
                "colors": [str(c) for c in (meta.get("colors") or [])][:6],
            })
        except Exception:
            LOG.exception("读取主题清单失败: %s", name)
    themes.sort(key=lambda t: t["id"] != DEFAULT_UI_THEME)
    return themes
# ================================================================
# 启动预检 / 诊断（对齐 local-ops inspect_app_health / diagnose_app）
# ================================================================

def _simple_command_tokens(command):
    """Windows 风格命令解析：双引号分组、剥离外层引号、引号内空格不拆分。

    注意不能用 shlex.split(posix=False)：它在 Windows 上不剥离引号，
    会把 `python -c "import time; time.sleep(1)"` 拆成多个错误 token。
    """
    if not command or not command.strip():
        return []
    tokens = []
    cur = []
    in_quotes = False
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if c == '"':
            in_quotes = not in_quotes
            if not in_quotes and not cur:
                # 空引号对 "" 保留占位
                cur.append("")
        elif c.isspace() and not in_quotes:
            if cur:
                tokens.append("".join(cur))
                cur = []
        else:
            cur.append(c)
        i += 1
    if cur:
        tokens.append("".join(cur))
    return tokens


def _script_target(tokens, cwd):
    """判断命令是否直接指向脚本/可执行文件。返回 (path|None, direct, was_relative)。

    目标“看起来像脚本/程序”时即使文件缺失也返回路径，由调用方检查
    isfile 并给出 script-missing 提示（否则缺失脚本永远不会被发现）。
    """
    if not tokens:
        return None, False, False
    first = tokens[0].strip('"')
    if not first:
        return None, False, False
    path = first
    if not os.path.isabs(path):
        path = os.path.join(cwd or ".", path)
    if os.path.isfile(path):
        return os.path.abspath(path), True, not os.path.isabs(first)
    if os.path.isabs(first) or re.search(
            r"\.(py|pyw|ps1|bat|cmd|js|mjs|cjs|ts|tsx|jsx|sh|exe)$", first, re.I):
        return os.path.abspath(path), True, not os.path.isabs(first)
    return None, False, False


def inspect_app_health(app):
    """静态检查配置是否可运行；只读文件系统，绝不执行用户命令。"""
    issues = []

    def add(kind, title, detail, fix, action):
        issues.append({
            "kind": kind, "severity": "error",
            "title": title, "detail": detail, "fix": fix, "action": action,
        })

    configured_cwd = app.get("cwd")
    cwd = configured_cwd or HOME_DIR
    cwd_ok = os.path.isdir(cwd)
    if configured_cwd and not cwd_ok:
        add("cwd-missing", "工作目录不可用",
            "找不到配置的工作目录：%s" % configured_cwd,
            "编辑这个项目，重新选择工作区文件夹。", "pick-cwd")

    tokens = _simple_command_tokens(app.get("command") or "")
    if tokens is None:
        return {"status": "error" if issues else "unknown",
                "blocking": bool(issues), "issues": issues}

    script_path, direct, script_was_relative = _script_target(tokens, cwd)
    if script_path and (cwd_ok or not script_was_relative):
        if not os.path.isfile(script_path):
            add("script-missing", "脚本不可用",
                "找不到脚本：%s" % script_path,
                "编辑这个任务，重新选择脚本或修改执行命令。", "pick-script")
        elif not os.access(script_path, os.R_OK):
            add("path-unreadable", "脚本不可读取",
                "当前用户没有读取权限：%s" % script_path,
                "检查脚本权限，或重新选择一个可读取的脚本。", "pick-script")
        elif direct and script_path.lower().endswith(".ps1"):
            if not shutil.which("powershell"):
                add("runtime-missing", "找不到 PowerShell",
                    "系统里找不到 powershell.exe。",
                    "Windows 自带 PowerShell，请确认系统环境正常。", "edit-command")
        elif direct and script_path.lower().endswith((".py", ".pyw")):
            if not shutil.which("python") and not shutil.which("pythonw"):
                add("runtime-missing", "找不到 Python",
                    "系统里找不到 python 命令。",
                    "安装 Python 3，或修改启动命令为完整路径。", "edit-command")

    # 首个运行时检查（直接脚本已由上面覆盖；检查 PATH 中的可执行程序）
    index = 0
    while tokens and index < len(tokens):
        word = tokens[index].strip('"')
        if not word or word.startswith("-") or re.match(r"^[a-z]:", word, re.I):
            index += 1
            continue
        if index == 0:
            exe = shutil.which(word)
            if exe is None and not word.lower().endswith((".exe", ".bat", ".cmd")):
                add("runtime-missing", "找不到运行时：%s" % word,
                    "系统 PATH 里找不到 %s 这个命令。" % word,
                    "确认该运行时已安装并加入 PATH（如 python / node / npm）。",
                    "edit-command")
            break
        break

    # 依赖目录检查（常见开发项目）
    if cwd_ok:
        pkg_json = os.path.join(cwd, "package.json")
        if os.path.isfile(pkg_json) and not os.path.isdir(os.path.join(cwd, "node_modules")):
            mgr = ("pnpm" if os.path.isfile(os.path.join(cwd, "pnpm-lock.yaml"))
                   else "yarn" if os.path.isfile(os.path.join(cwd, "yarn.lock"))
                   else "npm")
            add("deps-missing", "依赖未安装（node_modules 缺失）",
                "目录里有 package.json，但没有 node_modules。",
                "终端执行：cd \"%s\" && %s install，装完再启动。" % (cwd, mgr),
                "edit-command")

    return {
        "status": "error" if issues else "ok",
        "blocking": bool(issues),
        "issues": issues,
    }


def diagnose_app(cfg, app):
    """运行时诊断：日志模式 + 退出码 + 端口占用。返回 {ok, issues, summary}。"""
    issues = []

    def add(kind, title, detail, fix, action=None):
        issues.append({"kind": kind, "title": title, "detail": detail,
                       "fix": fix, "action": action})

    app_id = app.get("id")
    log_path = os.path.join(LOGS_DIR, app_id + ".log")
    log_lines, _, _ = tail_log(log_path, 60)
    log_tail = "\n".join(log_lines)
    log_lower = log_tail.lower()
    code = None
    last_exit = app.get("lastExit")
    if isinstance(last_exit, dict) and isinstance(last_exit.get("code"), int):
        code = last_exit["code"]
    port = app.get("port")
    cwd = app.get("cwd") or HOME_DIR
    has_pkg = os.path.isfile(os.path.join(cwd, "package.json"))
    pkg_json = os.path.join(cwd, "package.json")

    m = re.search(r"cannot find module '([^']+)'", log_lower)
    if m:
        add("deps-missing", "找不到模块 %s" % m.group(1),
            "日志报 Cannot find module '%s'，通常是依赖没装或装坏了。" % m.group(1),
            "终端执行：cd \"%s\" && npm install（仍报错再删掉 node_modules 后重装）。"
            % (cwd or "<项目目录>"))
    m = re.search(r"modulenotfounderror: no module named '([^']+)'", log_lower)
    if m:
        add("pip-missing", "缺少 Python 包：%s" % m.group(1),
            "日志报 ModuleNotFoundError: No module named '%s'。" % m.group(1),
            "在项目目录执行：pip install %s" % m.group(1))
    m = re.search(r"(?:'|\xe2\x80\x9c)?([\w.\-]+)(?:'|\xe2\x80\x9d)? "
                  r"(?:\u4e0d\u662f\u5185\u90e8\u6216\u5916\u90e8\u547d\u4ee4|"
                  r"\u672a\u88ab\u8bc6\u522b\u4e3a cmdlet|command not found|"
                  r"no such file or directory)", log_lower)
    if m:
        add("runtime-missing", "找不到运行时：%s" % m.group(1),
            "日志报找不到 %s 这个命令。" % m.group(1),
            "确认该运行时已安装并加入 PATH。")
    if "missing script" in log_lower and has_pkg:
        script_names = []
        try:
            with open(pkg_json, "r", encoding="utf-8") as f:
                script_names = list((json.load(f).get("scripts") or {}).keys())
        except Exception:
            pass
        hint = ("package.json 里可用的脚本：%s。" % "、".join(script_names)
                if script_names else "package.json 里没有 scripts。")
        add("npm-script", "npm 脚本名写错了",
            "日志报 missing script。%s" % hint,
            "把启动命令改成上面列出的脚本名，例如 npm run %s。"
            % (script_names[0] if script_names else "dev"))
    if ("eaddrinuse" in log_lower or "address already in use" in log_lower
            or "地址已被占用" in log_lower or "端口被占用" in log_lower):
        add("port-busy", "端口被占用",
            "日志报地址已占用%s。" % ("（:%s）" % port if port else ""),
            "点卡片上的端口数字看是谁占用的，停掉它或给本应用换个端口。")
    if ("eacces" in log_lower or "permission denied" in log_lower
            or "拒绝访问" in log_lower):
        add("perm", "权限不足",
            "日志报权限不足（EACCES / permission denied / 拒绝访问）。",
            "检查文件/目录权限，或用管理员权限启动。")
    if "traceback" in log_lower or "exception" in log_lower:
        add("python-error", "Python 运行时报错",
            "日志里有 Traceback / Exception。",
            "打开完整日志看具体报错行。", "open-logs")
    if re.search(r"no such file or directory", log_lower) and not issues:
        add("file-missing", "命令里的文件/脚本不存在",
            "日志报 No such file or directory，命令里引用的路径可能写错了。",
            "检查启动命令和工作目录里的相对路径是否正确。")

    if not issues:
        if code == 126:
            add("not-exec", "命令没有执行权限（exit 126）",
                "退出码 126 表示文件不可执行。",
                "检查脚本权限，或用解释器（python / powershell）启动。")
        elif code == 127:
            add("not-found", "命令不存在（exit 127）",
                "退出码 127 表示 shell 找不到这个命令。",
                "确认命令已安装且在 PATH 里。")
        elif code == 9009:
            add("not-found", "命令不存在（exit 9009）",
                "退出码 9009 是 cmd 的“找不到命令”错误。",
                "确认命令已安装且在 PATH 里。")
        elif (isinstance(code, int) and code == 0
              and (app.get("kind") or "service") != "task"):
            add("quick-exit", "命令立即正常退出（exit 0）",
                "进程启动后马上正常结束——长期服务命令不应立刻退出。",
                "确认写的是常驻命令（如 npm run dev），而不是一次就完成的命令。")
        elif isinstance(code, int) and code < 0:
            add("signaled", "进程被信号终止（signal %d）" % -code,
                "进程不是自然退出，是被外部终止的。",
                "常见于被任务管理器结束或系统回收；查看系统日志确认原因。")

    if port:
        listeners = scan_listeners()
        occupying = [pid for (pid, p) in listeners if p == port
                     and pid not in _live_pids(app)]
        if occupying:
            names = []
            for pid in occupying[:3]:
                info = _snap["procs"].get(str(pid))
                names.append("%s(pid %s)" % (info.get("name") if info else "?", pid))
            add("port-busy", "端口被其他进程占用",
                "端口 :%s 当前被 %s 监听。" % (port, "、".join(names)),
                "结束占用进程，或给本应用换一个端口。", "open-port-diag")

    if issues:
        summary = "发现 %d 个可能原因，按「修复建议」处理后再启动。" % len(issues)
    elif not log_tail.strip():
        summary = "暂无日志可供诊断；先启动一次让日志产生，再看完整日志定位。"
    elif code is None:
        summary = "该应用还没有退出记录；当前日志未见明显异常。"
    else:
        summary = "日志里没有命中常见错误模式，建议打开完整日志人工排查。"
    return {"ok": True, "issues": issues, "summary": summary}


# ================================================================
# 应用生命周期（Windows 适配）
# ================================================================

_SHELL_META_RE = re.compile(r"[|&<>^]")


def build_argv(app):
    """把应用命令转成 Windows argv 列表。

    优先直接执行解析后的程序（避免 cmd /c 的引号剥离坑，例如
    python -c "import time; ..." 会被 cmd 拆坏）；只有 .bat/.cmd
    包装（npm/yarn 等）或含 shell 元字符的命令才交给 cmd 处理。
    """
    cmd = (app.get("command") or "").strip()
    if not cmd:
        return []
    tokens = _simple_command_tokens(cmd) or [cmd]
    first = (tokens[0] or "").strip('"')
    low = first.lower()
    if _SHELL_META_RE.search(cmd):
        # 重定向/管道/&& 等交给 cmd 解释；引号内的 &|<> 在 cmd 里按字面处理。
        # 必须先于脚本直启分支，否则 `run.py > out.txt` 会把 `>` 当参数传给脚本。
        return ["cmd.exe", "/d", "/c", cmd]
    if low.endswith((".py", ".pyw")):
        return [sys.executable] + tokens
    if low.endswith(".ps1"):
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File"] + tokens
    if low.endswith((".bat", ".cmd")):
        return ["cmd.exe", "/d", "/c", "call"] + tokens
    resolved = shutil.which(first)
    if resolved:
        rlow = resolved.lower()
        if rlow.endswith((".bat", ".cmd")):
            return ["cmd.exe", "/d", "/c", "call"] + tokens
        if rlow.endswith(".exe"):
            return [resolved] + tokens[1:]
    return ["cmd.exe", "/d", "/c", cmd]


def resolve_cwd(app):
    cwd = (app.get("cwd") or "").strip()
    if cwd and os.path.isdir(cwd):
        return cwd
    cmd = (app.get("command") or "").strip()
    tokens = _simple_command_tokens(cmd) or []
    if tokens:
        first = tokens[0].strip('"')
        if os.path.isfile(first):
            return os.path.dirname(first)
        if not os.path.isabs(first):
            cand = os.path.join(BASE_DIR, first)
            if os.path.isfile(cand):
                return os.path.dirname(cand)
    return BASE_DIR


def app_running(app):
    return bool(_live_pids(app))


def start_app(cfg, app):
    app_id = app["id"]
    live = _live_pids(app)
    if live:
        return False, "应用已在运行 (pid %s)" % live[0]
    info = _runtime.get(app_id)
    if info and info.get("proc") is not None and info["proc"].poll() is None:
        return False, "应用已在运行 (pid %s)" % info["pid"]
    log_path = os.path.join(LOGS_DIR, app_id + ".log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
    except OSError:
        pass
    argv = build_argv(app)
    if not argv:
        return False, "启动命令为空"
    cwd = resolve_cwd(app)
    if not os.path.isdir(cwd):
        cwd = BASE_DIR
    token = secrets.token_urlsafe(12)
    env = dict(os.environ)
    env[RUN_TOKEN_ENV] = token
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    append_marker(log_path, "启动 %s at %s" % (app["name"], now_str()))
    f = None
    try:
        f = open(log_path, "ab")
        proc = subprocess.Popen(argv, cwd=cwd, stdout=f,
                                stderr=subprocess.STDOUT,
                                creationflags=CREATE_NO_WINDOW, env=env)
    except Exception as e:
        append_marker(log_path, "启动失败: %s" % e)
        return False, "启动失败: %s" % e
    finally:
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
    started = time.time()
    _runtime[app_id] = {"proc": proc, "pid": proc.pid, "started": started,
                        "log": log_path, "app_id": app_id}

    def op(c):
        for item in c.get("apps") or []:
            if item.get("id") == app_id:
                item["lastPid"] = proc.pid
                item["runToken"] = token
                item["attached"] = False
        return True

    try:
        cfg.update(op)
    except OSError as e:
        LOG.warning("记录运行状态失败: %s", e)

    threading.Thread(target=watch_app_exit, args=(cfg, app_id, proc, started),
                     daemon=True).start()
    return True, "已启动 (pid %s)" % proc.pid


def watch_app_exit(cfg, app_id, proc, started_at):
    code = proc.wait()
    ended = time.time()
    info = _runtime.pop(app_id, None)
    if info is not None and info.get("stopped_by_user"):
        return
    if (_last_run.get(app_id) or {}).get("stopped_by_user"):
        return
    _last_run[app_id] = {"proc": proc, "pid": proc.pid, "started": started_at,
                         "ended": ended, "recorded": False, "app_id": app_id}
    append_marker(os.path.join(LOGS_DIR, app_id + ".log"),
                  "退出，exit code %s at %s" % (code, now_str()))
    _record_exit(app_id, code, started_at, ended)
    invalidate_state_cache()


def stop_pid_tree(pid, force=False):
    """结束进程树。force=False 先发 WM_CLOSE（taskkill 不带 /F），
    force=True 用 taskkill /F /T 杀整棵进程树。"""
    if not pid:
        return False
    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    try:
        r = subprocess.run(args, capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False


def kill_by_pattern(stop_pattern):
    """按正则匹配命令行并结束匹配进程树，返回成功结束的进程数。"""
    if not stop_pattern:
        return 0
    try:
        pattern = re.compile(stop_pattern, re.I)
    except re.error:
        return 0
    with _snap_lock:
        cmdlines = dict(_snap["cmdlines"])
    killed = 0
    for cpid, cmeta in cmdlines.items():
        if str(cpid) == str(SELF_PID):
            continue
        if pattern.search(_normalize_cmd(cmeta.get("cmdline"))):
            r = subprocess.run(["taskkill", "/PID", str(cpid), "/T", "/F"],
                               capture_output=True, timeout=15,
                               creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0:
                killed += 1
    return killed


def stop_app_and_clear(cfg, app, force=True):
    """停止应用并记录 stopped 状态。"""
    app_id = app["id"]
    info = _runtime.get(app_id)
    if info is not None:
        info["stopped_by_user"] = True
    pid = None
    if info and info.get("proc") is not None and info["proc"].poll() is None:
        pid = info["pid"]
    elif app.get("lastPid") and pid_alive(app["lastPid"]):
        pid = app["lastPid"]
    stop_pattern = app.get("stopPattern")
    kill_by_pattern(stop_pattern)
    ok = True
    if pid:
        ok = stop_pid_tree(pid, force=force)
    if info and info.get("proc") is not None:
        try:
            info["proc"].wait(timeout=5)
        except Exception:
            pass
    started = info.get("started") if info else None
    _runtime.pop(app_id, None)
    if started is None:
        last = _last_run.get(app_id)
        started = (last or {}).get("started")
    ended = time.time()
    _last_run[app_id] = {"proc": None, "pid": pid, "started": started or ended,
                         "ended": ended, "recorded": True, "app_id": app_id,
                         "exit_code": None, "stopped_by_user": True}

    def op(c):
        for item in c.get("apps") or []:
            if item.get("id") == app_id:
                item["lastExit"] = {
                    "code": None, "status": "stopped",
                    "at": ended,
                    "startedAt": started or ended,
                    "durationSec": round(max(0.0, (ended - (started or ended))), 1),
                }
        return True

    try:
        cfg.update(op)
    except OSError as e:
        LOG.warning("记录停止状态失败: %s", e)
    append_marker(os.path.join(LOGS_DIR, app_id + ".log"),
                  "已停止 at %s" % now_str())
    return ok


def inspect_attach_process(cfg, app, pid):
    """检查能否把 pid 认领为 app。返回 (ok, error, identity)。"""
    if not pid_alive(pid):
        return False, "进程不存在或已退出 (pid %s)" % pid, {"status": 409}
    exe = exe_of(pid)
    cmd = cmdline_of(pid)
    name = os.path.basename(exe) if exe else "?"
    identity = {
        "status": 200,
        "cwd": os.path.dirname(exe) if exe else None,
        "name": name,
        "cmd": cmd,
        "pid": pid,
    }
    return True, None, identity


def attach_app_process(cfg, app, pid):
    def op(c):
        app_id = app["id"]
        if any(other.get("lastPid") == pid and other.get("id") != app_id
               for other in c.get("apps") or []):
            return False
        for item in c.get("apps") or []:
            if item.get("id") == app_id:
                item["lastPid"] = pid
                item["attached"] = True
                item["runToken"] = None
        return True

    try:
        result = cfg.update(op)
    except OSError as e:
        return False, str(e)
    if result is False:
        return False, "该进程已由其他卡片管理"
    return True, None


def kill_process(pid, force):
    """结束任意进程（服务监控用）。force=True 用 ctypes 强制结束进程树。"""
    if not pid or pid <= 0:
        return False, "无效的 PID"
    if force:
        procs = {}
        with _snap_lock:
            procs = dict(_snap["procs"])
        children = {int(k): int(v.get("ppid") or 0) for k, v in procs.items()}
        tree = []
        stack = [pid]
        while stack:
            cur = stack.pop()
            tree.append(cur)
            stack.extend(p for p, pp in children.items() if pp == cur and p not in tree)
        for target in sorted(set(tree), reverse=True):
            handle = _k32.OpenProcess(0x0001, False, target)  # PROCESS_TERMINATE
            if handle:
                _k32.TerminateProcess(handle, 1)
                _k32.CloseHandle(handle)
        return True, "已强制结束进程树 (%d 个进程)" % len(tree)
    ok = stop_pid_tree(pid, force=False)
    return ok, "已发送结束请求" if ok else "进程可能没有响应，可勾选强制结束"
# ================================================================
# 日志
# ================================================================

def rotate_log_file(path, max_bytes=MAX_LOG_BYTES, backups=LOG_BACKUPS):
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size < max_bytes:
        return
    try:
        for i in range(backups, 0, -1):
            src = "%s.%d" % (path, i - 1) if i > 1 else path
            dst = "%s.%d" % (path, i)
            if os.path.exists(src):
                if os.path.exists(dst):
                    os.remove(dst)
                os.replace(src, dst)
    except OSError as e:
        LOG.warning("轮转日志失败: %s", e)


def _tail_file_lines(path, count, block_size=65536):
    """从文件尾部按块倒读，最多返回 count 行（对齐 local-ops 尾读优化）。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size == 0:
        return []
    lines = []
    with open(path, "rb") as f:
        pos = size
        read = b""
        while pos > 0:
            chunk_size = min(block_size, pos)
            pos -= chunk_size
            f.seek(pos)
            chunk = f.read(chunk_size)
            read = chunk + read
            lines = read.splitlines()
            if len(lines) > count:
                break
    if len(lines) > count:
        lines = lines[-count:]
    text = None
    for enc in ("utf-8", "gbk"):
        try:
            text = b"\n".join(lines).decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = b"\n".join(lines).decode("utf-8", "replace")
    return text.splitlines()


def tail_log(log_path, lines=300):
    """兼容旧接口：返回 (lines_list, total, size)。"""
    content = _tail_file_lines(log_path, lines)
    try:
        size = os.path.getsize(log_path)
    except OSError:
        size = 0
    return content, 0, size


def read_log_tail(app_id, count=300):
    """读应用日志尾部，返回纯文本。app_id='console' 读控制台日志。"""
    if app_id == "console":
        path = os.path.join(LOGS_DIR, "console.log")
    else:
        path = os.path.join(LOGS_DIR, app_id + ".log")
    return "\n".join(_tail_file_lines(path, count))


def start_log_maintenance():
    def _maintain():
        while True:
            try:
                rotate_log_file(os.path.join(LOGS_DIR, "console.log"))
            except Exception:
                pass
            time.sleep(300)
    threading.Thread(target=_maintain, daemon=True).start()


# ================================================================
# 项目检测 / 文件选择
# ================================================================

def detect_project(root):
    """只读分析项目根目录，返回可由启动台直接使用的启动候选。"""
    if not isinstance(root, str) or not root.strip():
        return None, "请选择项目文件夹"
    root = os.path.abspath(os.path.expanduser(root.strip()))
    if not os.path.isdir(root):
        return None, "项目文件夹不存在或不可访问"

    candidates = []
    detected_files = []

    def note_file(name, text=None):
        path = os.path.join(root, name)
        exists = text is not None or os.path.isfile(path)
        if exists and name not in detected_files:
            detected_files.append(name)
        return exists

    def add(command, label, source, port=None, priority=50, detail=None,
            kind="service"):
        if not command or any(item["command"] == command for item in candidates):
            return
        if port is not None and not (isinstance(port, int) and 1 <= port <= 65535):
            port = None
        candidates.append({
            "command": command, "label": label, "source": source,
            "port": port, "kind": "task" if kind == "task" else "service",
            "detail": detail, "_priority": priority,
        })

    def port_from_text(text):
        m = re.search(r"\b(?:port|PORT)\s*[:=]\s*['\"]?(\d{2,5})", text or "")
        if m and 1 <= int(m.group(1)) <= 65535:
            return int(m.group(1))
        m = re.search(r"--port\s+(\d{2,5})", text or "")
        if m and 1 <= int(m.group(1)) <= 65535:
            return int(m.group(1))
        return None

    package = {}
    scripts = {}
    pkg_text = None
    if os.path.isfile(os.path.join(root, "package.json")):
        note_file("package.json")
        try:
            with open(os.path.join(root, "package.json"), "r", encoding="utf-8") as f:
                package = json.load(f)
            pkg_text = json.dumps(package, ensure_ascii=False)
            scripts = package.get("scripts") or {}
        except Exception:
            package = {}
    if scripts:
        for name, script in list(scripts.items())[:6]:
            if isinstance(script, str) and script.strip():
                port = port_from_text(script) or port_from_text(pkg_text)
                add("npm run %s" % name, "npm run %s" % name,
                    "package.json", port, 40 if name in ("dev", "start") else 50)
    elif package.get("scripts") is None and package:
        add("npm start", "npm start（默认）", "package.json",
            port_from_text(pkg_text), 55)

    if os.path.isfile(os.path.join(root, "requirements.txt")):
        note_file("requirements.txt")
    if os.path.isfile(os.path.join(root, "pyproject.toml")):
        note_file("pyproject.toml")
    py_candidates = []
    for py_name in ("main.py", "app.py", "manage.py", "run.py", "server.py",
                    "bot.py", "api.py", "index.py"):
        if os.path.isfile(os.path.join(root, py_name)):
            note_file(py_name)
            py_candidates.append(py_name)
    if py_candidates:
        for py_name in py_candidates[:3]:
            command = "python %s" % py_name
            label = "Django 开发服务器" if py_name == "manage.py" else "Python 应用"
            port = 8000 if py_name == "manage.py" else None
            add(command, label, py_name, port, 45)

    if os.path.isfile(os.path.join(root, "index.html")) and not candidates:
        note_file("index.html")
        add("python -m http.server 8000", "静态网站预览", "index.html", 8000, 90)

    if os.path.isfile(os.path.join(root, "go.mod")):
        note_file("go.mod")
        add("go run .", "Go 项目", "go.mod", None, 60)
    if os.path.isfile(os.path.join(root, "Cargo.toml")):
        note_file("Cargo.toml")
        add("cargo run", "Rust 项目", "Cargo.toml", None, 61)

    for script_name in ("start.bat", "dev.bat", "run.bat", "start.cmd",
                        "启动.bat", "start.ps1"):
        if os.path.isfile(os.path.join(root, script_name)):
            note_file(script_name)
            add(script_name, "现有启动脚本", script_name, None, 70,
                "也可以继续使用“选择脚本”手动指定")
            break

    try:
        entries = os.listdir(root)
    except OSError:
        entries = []
    csproj = [f for f in entries if f.lower().endswith(".csproj")]
    if csproj:
        note_file(csproj[0])
        add("dotnet run", ".NET 项目", csproj[0], None, 62)

    candidates.sort(key=lambda item: item.pop("_priority"))
    return {
        "ok": True, "cwd": root,
        "name": os.path.basename(root) or root,
        "files": detected_files,
        "candidates": candidates[:8],
    }, None


def pick_path(what):
    """Windows 文件/目录选择。tkinter 优先，失败时用一次 powershell。"""
    if what not in ("dir", "script"):
        return None, "what 必须是 dir/script"
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if what == "dir":
            path = filedialog.askdirectory(title="选择项目文件夹")
        else:
            path = filedialog.askopenfilename(
                title="选择脚本",
                filetypes=[("脚本文件", "*.py *.ps1 *.bat *.cmd *.sh *.js *.exe"),
                           ("所有文件", "*.*")])
        root.destroy()
        if path:
            return path, None
        return None, "canceled"
    except Exception:
        pass
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$d = New-Object System.Windows.Forms.%s; "
        "$d.Title = '%s'; "
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Write-Output $d.SelectedPath }"
        % ("FolderBrowserDialog" if what == "dir" else "OpenFileDialog",
           "选择项目文件夹" if what == "dir" else "选择脚本"))
    out = run_ps(script, timeout=120)
    out = (out or "").strip()
    if not out:
        return None, "canceled"
    if os.path.exists(out):
        return out, None
    return None, "无法解析选择结果"


# ================================================================
# Favicon / 图标
# ================================================================

def _remove_icon_files(app_id, keep_ext=None):
    """清理该应用在本地的图标文件（切换格式/删除应用时避免残留）。"""
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
        if ext == keep_ext:
            continue
        try:
            os.remove(os.path.join(ICONS_DIR, app_id + ext))
        except OSError:
            pass


def sniff_image(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:5] in (b"<?xml", b"<svg "):
        return "svg"
    if b"<svg" in data[:200]:
        return "svg"
    return None


def sniff_icon_bytes(data, ctype=""):
    ext = sniff_image(data)
    if ext:
        return data, ext
    ctype = (ctype or "").lower()
    if "png" in ctype:
        return data, "png"
    if "jpeg" in ctype or "jpg" in ctype:
        return data, "jpg"
    if "webp" in ctype:
        return data, "webp"
    if "svg" in ctype:
        return data, "svg"
    return None, None


def http_get(url, port, timeout=3, limit=262144):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PortWatch/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(limit + 1)
            ctype = r.headers.get("Content-Type", "")
            if len(data) > limit:
                return None, None, "响应过大"
            return data, ctype, None
    except Exception as e:
        return None, None, str(e)


def fetch_favicon(port, host="127.0.0.1"):
    for url in ("http://%s:%d/favicon.ico" % (host, port),
                "http://%s:%d/favicon.png" % (host, port)):
        data, ctype, err = http_get(url, port)
        if data:
            return data, ctype, None
    return None, None, "未找到 favicon（端口 %s）" % port
# ================================================================
# HTTP 服务
# ================================================================

class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler_cls, cfg, port):
        super().__init__(addr, handler_cls)
        self.cfg = cfg
        self.console_port = self.server_address[1]
        self.control_token = secrets.token_urlsafe(32)
        self._app_locks = {}
        self._app_locks_guard = threading.Lock()
        self._console_action_guard = threading.Lock()
        self._console_action = None
        self._console_helper_pid = None

    def handle_error(self, request, client_address):
        exc_type, exc, _ = sys.exc_info()
        if exc_type and isinstance(exc, (TimeoutError, BrokenPipeError,
                                         ConnectionResetError)):
            return
        super().handle_error(request, client_address)

    def try_app_operation(self, app_id):
        with self._app_locks_guard:
            lock = self._app_locks.setdefault(app_id, threading.Lock())
        return lock if lock.acquire(blocking=False) else None

    def forget_app_lock(self, app_id):
        with self._app_locks_guard:
            self._app_locks.pop(app_id, None)

    def reserve_console_action(self, action):
        with self._console_action_guard:
            if self._console_action is not None:
                return False, self._console_action, self._console_helper_pid
            self._console_action = action
            return True, action, None

    def set_console_helper_pid(self, pid):
        with self._console_action_guard:
            self._console_helper_pid = pid

    def release_console_action(self, action):
        with self._console_action_guard:
            if self._console_action == action:
                self._console_action = None
                self._console_helper_pid = None


def serialized_app_operation(fn):
    @functools.wraps(fn)
    def wrapped(self, app_id, *args, **kwargs):
        lock = self.server.try_app_operation(app_id)
        if lock is None:
            self.send_err(409, "该应用正在执行其他操作，请稍后重试")
            return None
        try:
            return fn(self, app_id, *args, **kwargs)
        finally:
            lock.release()
    return wrapped


PLACEHOLDER_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>PortWatch</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:"Microsoft YaHei",sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#0a0e14;color:#e6edf3}
.card{background:#131a24;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:36px 44px;max-width:540px;text-align:center}
h1{font-size:20px;margin:0 0 14px}p{color:#8b98a9;font-size:14px;line-height:1.8;margin:6px 0}
code{background:#0a0e14;border:1px solid rgba(255,255,255,.08);border-radius:6px;padding:2px 7px;font-size:13px}
</style></head>
<body><div class="card">
<h1>🖥 PortWatch 后端运行中</h1>
<p>前端文件 <code>static/index.html</code> 尚未提供，界面暂不可用。</p>
<p>API 已就绪：<code>GET /api/state</code></p>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self._parsed_body = None

    def log_message(self, fmt, *args):
        LOG.debug(fmt % args)

    def _parsed_request_host(self):
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return None, None
        parts = host.rsplit(":", 1)
        if len(parts) == 1:
            return parts[0], None
        try:
            return parts[0], int(parts[1])
        except ValueError:
            return parts[0], None

    def _request_host_allowed(self):
        host, port = self._parsed_request_host()
        if not host:
            return False
        host = host.strip("[]").casefold()
        if host not in ("127.0.0.1", "localhost"):
            return False
        if port is not None and port != self.server.console_port:
            return False
        return True

    def _same_origin(self, origin, host):
        if not origin:
            return True
        try:
            parts = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        return parts.scheme == "http" and parts.netloc == host

    def _has_control_cookie(self):
        cookie = self.headers.get("Cookie")
        if not cookie:
            return False
        parsed = SimpleCookie()
        parsed.load(cookie)
        morsel = parsed.get("portwatch_session")
        return bool(morsel and morsel.value == self.server.control_token)

    def _deny_request(self, status, message):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message.encode("utf-8"))))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(message.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_request_error(self, method, exc):
        LOG.exception("%s 请求处理失败", method)
        try:
            self.send_err(500, "服务器内部错误")
        except Exception:
            pass

    def authorize_request(self, mutating=False, content_kind=None):
        if not self._request_host_allowed():
            self._deny_request(403, "请求被拒绝：Host 不允许")
            return False
        origin = self.headers.get("Origin")
        host_header = self.headers.get("Host") or ""
        if origin and not self._same_origin(origin, host_header):
            self._deny_request(403, "请求被拒绝：来源不允许")
            return False
        if mutating and not self._has_control_cookie():
            self._deny_request(403, "访问被拒绝，请从 PortWatch 页面重试")
            return False
        return True

    def _send(self, body, status=200, ctype="text/plain; charset=utf-8",
              set_cookie=True):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if set_cookie else "no-cache")
            if set_cookie:
                self.send_header("Set-Cookie",
                                 "portwatch_session=%s; HttpOnly; SameSite=Strict; Path=/"
                                 % self.server.control_token)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, obj, status=200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(payload, status, "application/json; charset=utf-8")

    def send_err(self, status, msg):
        self.send_json({"ok": False, "error": msg}, status)

    def discard_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 0:
                self.rfile.read(min(length, 1024 * 1024))
        except Exception:
            pass

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, "Content-Length 无效"
        if length <= 0:
            return {}, None
        if length > 2 * 1024 * 1024:
            return None, "请求体过大"
        try:
            raw = self.rfile.read(length)
        except Exception:
            return None, "读取请求体失败"
        try:
            return json.loads(raw.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "请求体不是有效的 JSON"

    def _get_app_or_404(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if app is None:
            self.send_err(404, "应用不存在")
            return None, None
        return cfg, app

    # ---------------- GET ----------------

    def do_GET(self):
        try:
            if not self.authorize_request():
                return
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/favicon.ico":
                self.serve_static("/assets/favicon.ico")
                return
            if path == "/api/health":
                self.send_json(build_health(self.server.cfg))
                return
            if path == "/api/state":
                self.send_json(get_state_snapshot(self.server.cfg,
                                                  self.server.console_port))
                return
            if path == "/api/console/log":
                self.handle_console_log(parsed.query)
                return
            m = APP_ROUTE_RE.match(path)
            if m and m.group(2) == "logs":
                self.handle_logs(m.group(1), parsed.query)
                return
            if path.startswith("/api/"):
                self.send_err(404, "接口不存在")
                return
            if path.startswith("/icons/"):
                self.serve_icon(path)
                return
            self.serve_static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._handle_request_error("GET", e)

    def serve_static(self, path):
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        try:
            inside = os.path.commonpath(
                [os.path.realpath(STATIC_DIR), os.path.realpath(full)]
            ) == os.path.realpath(STATIC_DIR)
        except (ValueError, OSError):
            inside = False
        if not inside or not os.path.isfile(full):
            if rel == "index.html":
                self._send(PLACEHOLDER_HTML.encode("utf-8"), 200,
                           "text/html; charset=utf-8", set_cookie=False)
            else:
                self._send(b"404 Not Found", 404, set_cookie=False)
            return
        ctype = STATIC_TYPES.get(os.path.splitext(full)[1].lower(),
                                 "application/octet-stream")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self._send(b"404 Not Found", 404, set_cookie=False)
            return
        self._send(data, 200, ctype, set_cookie=False)

    def serve_icon(self, path):
        name = os.path.basename(urllib.parse.unquote(path[len("/icons/"):]))
        ext = os.path.splitext(name)[1].lower()
        if ext not in ICON_EXTS:
            self._send(b"404 Not Found", 404)
            return
        full = os.path.join(ICONS_DIR, name)
        if not os.path.isfile(full):
            self._send(b"404 Not Found", 404, set_cookie=False)
            return
        ctype = STATIC_TYPES.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self._send(b"404 Not Found", 404, set_cookie=False)
            return
        self._send(data, 200, ctype, set_cookie=False)

    def handle_logs(self, app_id, query):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        tail = self._parse_log_tail(query)
        self.send_json({"text": read_log_tail(app_id, tail)})

    def handle_console_log(self, query):
        tail = self._parse_log_tail(query)
        self.send_json({"text": read_log_tail("console", tail)})

    @staticmethod
    def _parse_log_tail(query, default=300):
        try:
            tail = int(urllib.parse.parse_qs(query).get("tail", [default])[0])
        except (ValueError, IndexError):
            tail = default
        return max(1, min(tail, 5000))
    # ---------------- POST ----------------

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if not self.authorize_request(mutating=True):
                return
            if path == "/api/kill":
                self.handle_kill()
                return
            if path == "/api/services/flag":
                self.handle_flag()
                return
            if path == "/api/watch":
                self.handle_watch()
                return
            if path == "/api/ui/theme":
                self.handle_ui_theme()
                return
            if path == "/api/pick":
                self.handle_pick()
                return
            if path == "/api/project/detect":
                self.handle_project_detect()
                return
            if path == "/api/console/restart":
                self.discard_body()
                self.handle_console_restart()
                return
            if path == "/api/console/stop":
                self.discard_body()
                self.handle_console_stop()
                return
            if path == "/api/apps":
                self.handle_app_create()
                return
            if path == "/api/apps/reorder":
                self.handle_apps_reorder()
                return
            if path == "/api/apps/batch-start":
                self.discard_body()
                self.handle_apps_batch_start()
                return
            if path == "/api/apps/batch-stop":
                self.discard_body()
                self.handle_apps_batch_stop()
                return
            m = APP_ROUTE_RE.match(path)
            if m:
                app_id, action = m.group(1), m.group(2)
                if action == "start":
                    self.discard_body()
                    self.handle_app_start(app_id)
                    return
                if action == "stop":
                    self.discard_body()
                    self.handle_app_stop(app_id)
                    return
                if action == "restart":
                    self.discard_body()
                    self.handle_app_restart(app_id)
                    return
                if action == "diagnose":
                    self.discard_body()
                    self.handle_app_diagnose(app_id)
                    return
                if action == "attach":
                    self.handle_app_attach(app_id)
                    return
                if action == "icon":
                    self.handle_icon_upload(app_id)
                    return
                if action == "favicon":
                    self.discard_body()
                    self.handle_fetch_favicon(app_id)
                    return
            self.send_err(404, "接口不存在")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._handle_request_error("POST", e)

    def handle_pick(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        what = data.get("what")
        if what not in ("dir", "script"):
            self.send_err(400, "what 必须是 dir/script")
            return
        path, error = pick_path(what)
        if error == "canceled":
            self.send_json({"ok": True, "canceled": True})
        elif not path:
            self.send_json({"ok": False, "error": error or "无法打开系统选择框"})
        else:
            self.send_json({"ok": True, "path": path})

    def handle_project_detect(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        result, error = detect_project(data.get("cwd"))
        if error:
            self.send_err(400, error)
            return
        self.send_json(result)

    def handle_app_diagnose(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if not app:
            self.send_err(404, "应用不存在")
            return
        self.send_json(diagnose_app(cfg, app))

    def handle_ui_theme(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        theme_id = str(data.get("theme") or "")
        known = {t["id"] for t in list_themes()}
        if theme_id not in known:
            self.send_err(400, "未知主题: %s" % theme_id)
            return
        self.server.cfg.update(lambda d: d.__setitem__("uiTheme", theme_id))
        self.send_json({"ok": True, "theme": theme_id})

    def handle_console_restart(self):
        reserved, action, helper = self.server.reserve_console_action("restart")
        if not reserved:
            self.send_json({"ok": False, "error": "控制台正在%s，请稍候"
                            % {"restart": "重启", "stop": "停止"}.get(action, action)},
                           409)
            return
        try:
            self.send_json({"ok": True, "message": "正在重启…"})
        except Exception:
            pass
        threading.Thread(target=schedule_console_restart,
                         args=(self.server, self.server.console_port),
                         daemon=True).start()

    def handle_console_stop(self):
        reserved, action, helper = self.server.reserve_console_action("stop")
        if not reserved:
            self.send_json({"ok": False, "error": "控制台正在%s，请稍候"
                            % {"restart": "重启", "stop": "停止"}.get(action, action)},
                           409)
            return
        try:
            self.send_json({"ok": True, "message": "正在停止…"})
        except Exception:
            pass
        threading.Thread(target=schedule_console_stop,
                         args=(self.server,), daemon=True).start()

    def handle_kill(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        try:
            pid = int(data.get("pid"))
        except (TypeError, ValueError):
            self.send_err(400, "pid 必须是整数")
            return
        if pid == SELF_PID or pid <= 4:
            self.send_json({"ok": False, "error": "拒绝结束 PortWatch 自身或系统进程"}, 400)
            return
        force = bool(data.get("force"))
        ok, msg = kill_process(pid, force)
        if not ok:
            self.send_json({"ok": False, "error": msg}, 409)
            return
        self.send_json({"ok": True, "message": msg})
        invalidate_state_cache()

    def handle_flag(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        key = str(data.get("key") or "")
        flag = str(data.get("flag") or "")
        value = bool(data.get("value"))
        if not key or flag not in ("hidden", "pinned", "promoted"):
            self.send_err(400, "flag 必须是 hidden/pinned/promoted")
            return

        def op(c):
            for name in ("hidden", "pinned", "promoted"):
                items = c.setdefault(name, [])
                if name == flag:
                    if value and key not in items:
                        items.append(key)
                    elif not value and key in items:
                        items.remove(key)
                elif flag == "promoted" and name == "hidden" and value and key in items:
                    items.remove(key)
                elif flag == "hidden" and name == "promoted" and value and key in items:
                    items.remove(key)
            return True

        self.server.cfg.update(op)
        self.send_json({"ok": True})

    def handle_watch(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        query = str(data.get("query") or "").strip()
        if not query:
            self.send_err(400, "query 不能为空")
            return

        def op(c):
            keywords = [str(k) for k in (c.get("watchedKeywords") or [])]
            if query in keywords:
                keywords.remove(query)
            else:
                keywords.append(query)
            c["watchedKeywords"] = keywords
            return keywords

        result = self.server.cfg.update(op)
        self.send_json({"ok": True, "keywords": result})

    def handle_app_create(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        attach_pid = data.get("attachPid")
        if attach_pid is not None and (
                not isinstance(attach_pid, int)
                or isinstance(attach_pid, bool)
                or attach_pid <= 0):
            self.send_err(400, "attachPid 必须是正整数")
            return
        fields, err = validate_app_fields(data, partial=False)
        if err:
            self.send_err(400, err)
            return

        snapshot = self.server.cfg.snapshot()
        new_app_id = secrets.token_hex(4)
        while find_app(snapshot, new_app_id):
            new_app_id = secrets.token_hex(4)
        app = {"id": new_app_id, "name": fields["name"],
               "command": fields["command"], "cwd": fields["cwd"],
               "port": fields["port"], "emoji": fields["emoji"],
               "glyph": fields["glyph"], "kind": fields["kind"],
               "icon": None, "favicon": None, "lastPid": None,
               "runToken": None, "attached": False, "lastExit": None,
               "createdAt": int(time.time()), "stopPattern": None}
        cwd_updated = False
        if attach_pid is not None:
            ok, error, identity = inspect_attach_process(
                self.server.cfg, app, attach_pid)
            if not ok:
                self.send_json(
                    {"ok": False, "error": error},
                    identity.get("status", 409))
                return
            actual_cwd = identity["cwd"]
            try:
                cwd_updated = (
                    not app.get("cwd")
                    or os.path.realpath(app["cwd"]) != os.path.realpath(actual_cwd)
                )
            except OSError:
                cwd_updated = True
            app["cwd"] = actual_cwd
            app["lastPid"] = attach_pid
            app["attached"] = True

        attach_conflict = [False]

        def op(c):
            if find_app(c, new_app_id):
                return None
            if attach_pid is not None and any(
                    other.get("lastPid") == attach_pid
                    for other in c.get("apps") or []):
                attach_conflict[0] = True
                return None
            c["apps"].append(app)
            return dict(app)

        created = self.server.cfg.update(op)
        if created is None:
            if attach_conflict[0]:
                self.send_json(
                    {"ok": False, "error": "该进程已由其他卡片管理"}, 409)
            else:
                self.send_err(409, "应用标识发生冲突，请重试")
            return
        if attach_pid is not None:
            created.update({
                "attached": True,
                "running": True,
                "pid": attach_pid,
                "cwdUpdated": cwd_updated,
            })
        self.send_json(created)

    def handle_apps_batch_start(self):
        cfg = self.server.cfg.snapshot()
        results = []
        for app in cfg.get("apps") or []:
            entry = {"id": app.get("id"), "name": app.get("name")}
            if app_running(app):
                entry.update({"ok": True, "skipped": True})
                results.append(entry)
                continue
            try:
                ok, message = start_app(self.server.cfg, app)
                entry.update({"ok": bool(ok), "message": message})
            except Exception as e:
                LOG.exception("批量启动失败: %s", app.get("id"))
                entry.update({"ok": False, "message": str(e)})
            results.append(entry)
        invalidate_state_cache()
        self.send_json({"ok": True, "results": results})

    def handle_apps_batch_stop(self):
        cfg = self.server.cfg.snapshot()
        results = []
        for app in cfg.get("apps") or []:
            entry = {"id": app.get("id"), "name": app.get("name")}
            if not app_running(app):
                entry.update({"ok": True, "skipped": True})
                results.append(entry)
                continue
            try:
                ok = stop_app_and_clear(self.server.cfg, app)
                entry.update({"ok": bool(ok)})
            except Exception as e:
                LOG.exception("批量停止失败: %s", app.get("id"))
                entry.update({"ok": False, "message": str(e)})
            results.append(entry)
        invalidate_state_cache()
        self.send_json({"ok": True, "results": results})

    def handle_apps_reorder(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        ids = data.get("ids")
        if not isinstance(ids, list):
            self.send_err(400, "ids 必须是数组")
            return

        def op(c):
            apps = c.get("apps") or []
            index = {app["id"]: app for app in apps}
            seen = set()
            ordered = []
            for app_id in ids:
                app = index.get(app_id)
                if app and app_id not in seen:
                    seen.add(app_id)
                    ordered.append(app)
            for app in apps:
                if app["id"] not in seen:
                    ordered.append(app)
            c["apps"] = ordered
            return [app["id"] for app in ordered]

        result = self.server.cfg.update(op)
        self.send_json({"ok": True, "ids": result})

    @serialized_app_operation
    def handle_app_start(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if not app:
            self.send_err(404, "应用不存在")
            return
        health = inspect_app_health(app)
        if health.get("blocking"):
            self.send_json({"ok": False, "error": "配置存在阻断问题，请先修复",
                            "health": health}, 422)
            return
        ok, message = start_app(self.server.cfg, app)
        if not ok:
            self.send_json({"ok": False, "error": message}, 409)
            return
        info = _runtime.get(app_id, {})
        self.send_json({"ok": True, "message": message, "pid": info.get("pid")})
        invalidate_state_cache()

    @serialized_app_operation
    def handle_app_stop(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if not app:
            self.send_err(404, "应用不存在")
            return
        if not app_running(app):
            if (app.get("kind") or "service") == "task" and app.get("stopPattern"):
                killed = kill_by_pattern(app.get("stopPattern"))
                if killed:
                    self.send_json({"ok": True, "message": "已停止 %d 个后台进程" % killed})
                else:
                    self.send_json({"ok": False, "error": "未发现匹配的后台进程"}, 409)
                invalidate_state_cache()
                return
            self.send_json({"ok": False, "error": "应用未在运行"}, 409)
            return
        stop_app_and_clear(self.server.cfg, app)
        self.send_json({"ok": True, "message": "已停止"})
        invalidate_state_cache()

    @serialized_app_operation
    def handle_app_restart(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if not app:
            self.send_err(404, "应用不存在")
            return
        health = inspect_app_health(app)
        if health.get("blocking"):
            self.send_json({"ok": False, "error": "配置存在阻断问题，请先修复",
                            "health": health}, 422)
            return
        if app_running(app):
            stop_app_and_clear(self.server.cfg, app)
        ok, message = start_app(self.server.cfg, app)
        if not ok:
            self.send_json({"ok": False, "error": message}, 409)
            return
        info = _runtime.get(app_id, {})
        self.send_json({"ok": True, "message": message, "pid": info.get("pid")})
        invalidate_state_cache()

    @serialized_app_operation
    def handle_app_attach(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if not app:
            self.send_err(404, "应用不存在")
            return
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        try:
            pid = int(data.get("pid"))
        except (TypeError, ValueError):
            self.send_err(400, "pid 必须是整数")
            return
        ok, error, identity = inspect_attach_process(cfg, app, pid)
        if not ok:
            self.send_json({"ok": False, "error": error},
                           identity.get("status", 409))
            return
        ok, error = attach_app_process(self.server.cfg, app, pid)
        if not ok:
            self.send_json({"ok": False, "error": error}, 409)
            return
        self.send_json({"ok": True, "pid": pid, "cwdUpdated": False})
        invalidate_state_cache()

    def handle_icon_upload(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if not app:
            self.send_err(404, "应用不存在")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_err(400, "Content-Length 无效")
            return
        if length <= 0 or length > 2 * 1024 * 1024:
            self.send_err(400, "图标文件大小需在 2MB 以内")
            return
        try:
            raw = self.rfile.read(length)
        except Exception:
            self.send_err(400, "读取图标失败")
            return
        data, ext = sniff_icon_bytes(raw, self.headers.get("Content-Type", ""))
        if data is None:
            self.send_err(400, "不支持的图片格式（png / jpg / webp / svg）")
            return
        ensure_dirs()
        filename = app_id + "." + ext
        target = os.path.join(ICONS_DIR, filename)
        try:
            with open(target, "wb") as f:
                f.write(data)
        except OSError as e:
            self.send_err(500, "保存图标失败: %s" % e)
            return
        _remove_icon_files(app_id, keep_ext=ext)

        def op(c):
            for item in c.get("apps") or []:
                if item.get("id") == app_id:
                    item["icon"] = filename
                    item["glyph"] = None
            return True

        try:
            self.server.cfg.update(op)
        except OSError as e:
            self.send_err(500, str(e))
            return
        self.send_json({"ok": True, "icon": filename, "version": int(time.time())})

    def handle_fetch_favicon(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if not app:
            self.send_err(404, "应用不存在")
            return
        port = app.get("port")
        if not port:
            self.send_json({"ok": False, "error": "该应用没有配置端口"}, 400)
            return
        if app_running(app):
            data, ctype, error = fetch_favicon(port, "127.0.0.1")
        else:
            data, ctype, error = None, None, "应用未运行，无法抓取 favicon"
        if data is None:
            self.send_json({"ok": False, "error": error or "抓取失败"}, 409)
            return
        sniffed, ext = sniff_icon_bytes(data, ctype)
        if sniffed is None:
            self.send_json({"ok": False, "error": "抓到的内容不是可用图片"}, 409)
            return
        ensure_dirs()
        filename = app_id + "." + ext
        target = os.path.join(ICONS_DIR, filename)
        try:
            with open(target, "wb") as f:
                f.write(sniffed)
        except OSError as e:
            self.send_err(500, "保存图标失败: %s" % e)
            return
        _remove_icon_files(app_id, keep_ext=ext)

        def op(c):
            for item in c.get("apps") or []:
                if item.get("id") == app_id:
                    item["favicon"] = filename
            return True

        try:
            self.server.cfg.update(op)
        except OSError as e:
            self.send_err(500, str(e))
            return
        self.send_json({"ok": True, "favicon": filename})
    # ---------------- PUT / DELETE ----------------

    def do_PUT(self):
        try:
            if not self.authorize_request(mutating=True):
                return
            path = urllib.parse.urlparse(self.path).path
            m = APP_ROUTE_RE.match(path)
            if not m or m.group(2) is not None:
                self.send_err(404, "接口不存在")
                return
            self.handle_app_update(m.group(1))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._handle_request_error("PUT", e)

    def handle_app_update(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if not app:
            self.send_err(404, "应用不存在")
            return
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        fields, err = validate_app_fields(data, partial=True)
        if err:
            self.send_err(400, err)
            return

        def op(c):
            for item in c.get("apps") or []:
                if item.get("id") == app_id:
                    for key, value in fields.items():
                        item[key] = value
                    if fields.get("kind") == "task":
                        item["port"] = None
                    return dict(item)
            return None

        updated = self.server.cfg.update(op)
        if updated is None:
            self.send_err(404, "应用不存在")
            return
        self.send_json(updated)

    def do_DELETE(self):
        try:
            if not self.authorize_request(mutating=True):
                return
            path = urllib.parse.urlparse(self.path).path
            if path.startswith("/api/apps/"):
                m = APP_ROUTE_RE.match(path)
                if not m:
                    self.send_err(404, "接口不存在")
                    return
                app_id, action = m.group(1), m.group(2)
                if action == "icon":
                    self.handle_icon_delete(app_id)
                elif action is None:
                    self.handle_app_delete(app_id)
                else:
                    self.send_err(404, "接口不存在")
                return
            self.send_err(404, "接口不存在")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._handle_request_error("DELETE", e)

    def handle_app_delete(self, app_id):
        lock = self.server.try_app_operation(app_id)
        if lock is None:
            self.send_err(409, "该应用正在执行其他操作，请稍后重试")
            return
        try:
            cfg = self.server.cfg.snapshot()
            app = find_app(cfg, app_id)
            if not app:
                self.send_err(404, "应用不存在")
                return
            if app_running(app):
                stop_app_and_clear(self.server.cfg, app)

            def op(c):
                c["apps"] = [item for item in (c.get("apps") or [])
                             if item.get("id") != app_id]
                return True

            try:
                self.server.cfg.update(op)
            except OSError as e:
                self.send_err(500, str(e))
                return
            _remove_icon_files(app_id)
            self.server.forget_app_lock(app_id)
            self.send_json({"ok": True})
        finally:
            lock.release()

    def handle_icon_delete(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if not app:
            self.send_err(404, "应用不存在")
            return
        icon = app.get("icon")
        favicon = app.get("favicon")

        def op(c):
            for item in c.get("apps") or []:
                if item.get("id") == app_id:
                    item["icon"] = None
                    item["favicon"] = None
            return True

        try:
            self.server.cfg.update(op)
        except OSError as e:
            self.send_err(500, str(e))
            return
        for name in (icon, favicon):
            if not name:
                continue
            try:
                path = os.path.join(ICONS_DIR, os.path.basename(name))
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        self.send_json({"ok": True})

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass


def validate_port(value):
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "端口必须是整数或 null"
    if not (1 <= value <= 65535):
        return None, "端口必须在 1-65535 之间"
    return value, None


def validate_app_fields(data, partial):
    """校验/规范化应用字段。partial=True 时仅校验出现的字段。"""
    fields = {}
    for key in ("name", "command"):
        if key in data:
            v = data[key]
            if not isinstance(v, str) or not v.strip():
                return None, "字段 %s 必须是非空字符串" % key
            fields[key] = v.strip()
        elif not partial:
            return None, "缺少字段 %s" % key
    if "cwd" in data:
        v = data["cwd"]
        if v is not None and not isinstance(v, str):
            return None, "cwd 必须是字符串或 null"
        fields["cwd"] = (v or "").strip() or None if isinstance(v, str) else None
    elif not partial:
        fields["cwd"] = None
    if "port" in data:
        port, err = validate_port(data["port"])
        if err:
            return None, err
        fields["port"] = port
    elif not partial:
        fields["port"] = None
    if "emoji" in data:
        v = data["emoji"]
        if v is not None and not isinstance(v, str):
            return None, "emoji 必须是字符串或 null"
        fields["emoji"] = (v or None)
    elif not partial:
        fields["emoji"] = None
    if "glyph" in data:
        v = data["glyph"]
        if v is not None and (not isinstance(v, str) or len(v) > 40):
            return None, "glyph 必须是字符串或 null"
        fields["glyph"] = (v or None)
    elif not partial:
        fields["glyph"] = None
    if "kind" in data:
        if data["kind"] not in ("service", "task"):
            return None, "kind 必须是 service/task"
        fields["kind"] = data["kind"]
    elif not partial:
        fields["kind"] = "service"
    if fields.get("kind") == "task":
        fields["port"] = None
    return fields, None


# ================================================================
# 控制台自身：重启 / 停止 / 启动
# ================================================================

def open_browser_later(port, delay=0.8):
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open("http://127.0.0.1:%d/" % port)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def _remove_pid_file():
    """os._exit 会跳过 finally，退出前主动清掉 pid 文件，避免残留。"""
    try:
        os.remove(PID_PATH)
    except OSError:
        pass


def schedule_console_restart(server, preferred_port):
    time.sleep(0.8)
    try:
        env = dict(os.environ)
        env["PORTWATCH_RESTART"] = "1"
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             "--preferred-port", str(preferred_port), "--no-browser"],
            cwd=BASE_DIR, creationflags=CREATE_NO_WINDOW, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server.set_console_helper_pid(proc.pid)
    except Exception as e:
        LOG.exception("重启失败: %s", e)
        server.release_console_action("restart")
        return
    time.sleep(1.2)
    server.release_console_action("restart")
    _remove_pid_file()
    os._exit(0)


def schedule_console_stop(server):
    time.sleep(0.8)
    server.release_console_action("stop")
    _remove_pid_file()
    os._exit(0)


def find_port(preferred=None):
    if preferred and isinstance(preferred, int):
        start = preferred
        end = start + 1
    else:
        start, end = 9600, 9610
    for port in range(start, end):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Windows 上不要设置 SO_REUSEADDR，否则端口已被占用时 bind 仍会成功。
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    return None


def setup_logging():
    ensure_dirs()
    console_log = os.path.join(LOGS_DIR, "console.log")
    try:
        handler = logging.FileHandler(console_log, encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    root = logging.getLogger("portwatch")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)


def main(preferred_port=None, open_browser=True):
    global _config
    setup_logging()
    ensure_dirs()
    start_log_maintenance()
    restart_mode = os.environ.get("PORTWATCH_RESTART") == "1"
    if not restart_mode:
        running_pid = _check_running_instance()
        if running_pid:
            LOG.warning("检测到已在运行 (pid %s)，本次启动退出", running_pid)
            print("[PortWatch] 已在运行 (pid %s)，无需重复启动" % running_pid)
            return 1
    cfg = Config(CONFIG_PATH)
    _config = cfg
    if cfg.health_info().get("issues"):
        LOG.warning("配置健康问题: %s", cfg.health_info().get("issues"))
    if restart_mode:
        # 页面重启：旧进程还占着端口，轮询等它退出后再绑定。
        port = None
        deadline = time.time() + 15
        while time.time() < deadline:
            port = find_port(preferred_port)
            if port is not None:
                break
            time.sleep(0.4)
        if port is None:
            LOG.error("重启后端口仍不可用")
            return 1
    else:
        port = find_port(preferred_port)
        if port is None:
            LOG.error("9600-9609 端口均不可用")
            print("[PortWatch] 9600-9609 端口均不可用")
            return 1
    server = ConsoleServer(("127.0.0.1", port), Handler, cfg, port)
    global SELF_PID
    SELF_PID = os.getpid()
    try:
        with open(PID_PATH, "w", encoding="utf-8") as f:
            f.write(str(SELF_PID))
    except OSError as e:
        LOG.warning("写入 pid 文件失败: %s", e)
    threading.Thread(target=watchdog, daemon=True).start()
    log("PortWatch 已启动，端口 %s" % port)
    print("[PortWatch] http://127.0.0.1:%d/  (Ctrl+C 停止)" % port)
    if open_browser:
        open_browser_later(port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            os.remove(PID_PATH)
        except OSError:
            pass
    return 0


_config = None


_config = None
SELF_PID = os.getpid()

if __name__ == "__main__":
    args = sys.argv[1:]
    preferred = None
    browser = True
    i = 0
    while i < len(args):
        if args[i] == "--preferred-port" and i + 1 < len(args):
            try:
                preferred = int(args[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if args[i] == "--no-browser":
            browser = False
        i += 1
    sys.exit(main(preferred_port=preferred, open_browser=browser))
