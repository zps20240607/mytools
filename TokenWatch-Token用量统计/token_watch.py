#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TokenWatch —— 跨工具 Token 用量自动统计
========================================

自动扫描本机 AI 编程工具的本地会话记录，汇总 Token 消耗与所用模型：
  * Codex          ~/.codex/sessions  + archived_sessions (token_count 事件)
  * Kimi Code      ~/.kimi-code/sessions/**/wire.jsonl    (usage.record 事件)
  * OpenClaw       ~/.openclaw/agents/**/sessions/*.jsonl (assistant 消息 usage 字段)
  * DeepSeek Harness ~/.dsh/storages/session_projcache.json + sessions/**/session.jsonl(.zstd)

用法:
  python token_watch.py scan [--force]      # 增量扫描入库（计划任务调用这个）
  python token_watch.py report [--json]     # 终端汇总报表
  python token_watch.py dashboard [--out F] # 生成 HTML 看板
  python token_watch.py install             # 注册 Windows 计划任务（每 10 分钟自动扫描）
  python token_watch.py uninstall           # 删除计划任务
  python token_watch.py watch               # 前台循环扫描（演示）

数据保存在 ~/.token-watch/token_watch.db，看板在 ~/.token-watch/dashboard.html。
"""

import argparse
import hashlib
import html
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOME = Path(os.environ.get("USERPROFILE") or str(Path.home()))
DSH_HOME = Path(os.environ.get("DSH_HOME") or str(HOME / ".dsh")).expanduser()
DATA_DIR = HOME / ".token-watch"
DB_PATH = DATA_DIR / "token_watch.db"
LOG_PATH = DATA_DIR / "scan.log"
DASHBOARD_PATH = DATA_DIR / "dashboard.html"

SOURCES = {
    "codex": [
        HOME / ".codex" / "sessions",
        HOME / ".codex" / "archived_sessions",
    ],
    "kimi": [HOME / ".kimi-code" / "sessions"],
    "openclaw": [HOME / ".openclaw" / "agents"],
    "deepseek": [
        DSH_HOME / "storages",
        DSH_HOME / "sessions",
    ],
}

TOOL_LABELS = {"codex": "Codex", "kimi": "Kimi Code", "openclaw": "OpenClaw", "deepseek": "DeepSeek Harness"}
TOOL_COLORS = {"codex": "#e56399", "kimi": "#8b7cf6", "openclaw": "#f0a03c", "deepseek": "#4fa3ff"}
TOOL_ORDER = ["codex", "kimi", "openclaw", "deepseek"]


# ---------------------------------------------------------------------------
# 基础工具函数
# ---------------------------------------------------------------------------

def key_of(raw: str) -> str:
    """按行内容生成去重 ID，同一事件即使出现在多个文件副本中也只记一次。"""
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def iso_from_ms(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return ""


CN_TZ = timezone(timedelta(hours=8))  # 北京时间（统计口径统一用 UTC+8）


def today_local() -> date:
    """北京时间（UTC+8）的今天。"""
    return datetime.now(CN_TZ).date()


def local_day(ts: str) -> str:
    """UTC ISO 时间 -> 北京时间日期 YYYY-MM-DD。"""
    if not ts:
        return ""
    try:
        # Codex/OpenClaw 的原始时间戳可能是 Z 结尾，先归一化为 +00:00
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CN_TZ).date().isoformat()


def fmt_num(n) -> str:
    """人类可读数字：1.2K / 3.4M / 1.2B。"""
    n = float(n or 0)
    if abs(n) >= 1e9:
        return f"{n / 1e9:.2f}B"
    if abs(n) >= 1e6:
        return f"{n / 1e6:.2f}M"
    if abs(n) >= 1e3:
        return f"{n / 1e3:.1f}K"
    return f"{int(n)}"


def fmt_int(n) -> str:
    return f"{int(n or 0):,}"


def now_local() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, quiet: bool = False) -> None:
    line = f"[{now_local()}] {msg}"
    if not quiet:
        print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS records(
          key TEXT PRIMARY KEY,
          tool TEXT NOT NULL,
          model TEXT,
          provider TEXT,
          ts TEXT,
          day TEXT,
          input INTEGER DEFAULT 0,
          output INTEGER DEFAULT 0,
          cached INTEGER DEFAULT 0,
          cache_write INTEGER DEFAULT 0,
          reasoning INTEGER DEFAULT 0,
          total INTEGER DEFAULT 0,
          cost REAL,
          source TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS file_meta(
          path TEXT PRIMARY KEY,
          mtime REAL,
          size INTEGER,
          scanned_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_day ON records(day)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool ON records(tool)")
    return conn


# ---------------------------------------------------------------------------
# 三种工具的解析器（各自返回统一格式的记录）
# ---------------------------------------------------------------------------

def _codex_snapshot_key(total_snap, last_snap):
    """Codex 会把同一 usage 快照重复发射两次，按快照内容去重。"""
    keys = ("input_tokens", "output_tokens", "cached_input_tokens",
            "cache_write_input_tokens", "reasoning_output_tokens", "total_tokens")

    def norm(snap):
        return [int((snap or {}).get(k) or 0) for k in keys]

    return tuple(norm(total_snap) + norm(last_snap))


def scan_codex_lines(path: Path, insert):
    """Codex: event_msg/token_count 事件，携带 last_token_usage。"""
    model, provider = None, None
    seen = set()
    for raw in path.open("r", encoding="utf-8", errors="replace"):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        typ = obj.get("type")
        payload = obj.get("payload") or {}
        if typ == "session_meta":
            provider = payload.get("model_provider") or provider
            continue
        if typ != "event_msg":
            continue
        pt = payload.get("type")
        if pt == "thread_settings_applied":
            st = payload.get("thread_settings") or {}
            if st.get("model"):
                model = st["model"]
            if st.get("model_provider_id"):
                provider = st["model_provider_id"]
            continue
        if pt == "token_count":
            info = payload.get("info") or {}
            total_snap = info.get("total_token_usage") or {}
            last_snap = info.get("last_token_usage")
            if isinstance(last_snap, dict) and last_snap:
                u = last_snap
            elif last_snap is None:
                u = total_snap
            else:
                u = {}
            if not u:
                continue
            inp = int(u.get("input_tokens") or 0)
            out = int(u.get("output_tokens") or 0)
            total = int(u.get("total_tokens") or 0) or (inp + out)
            if inp == 0 and out == 0 and total == 0:
                continue
            snap_key = _codex_snapshot_key(total_snap, last_snap)
            if snap_key in seen:
                continue
            seen.add(snap_key)
            insert(
                {
                    "tool": "codex",
                    "model": model or provider or "unknown",
                    "provider": provider or "",
                    "ts": obj.get("timestamp") or "",
                    "input": inp,
                    "output": out,
                    "cached": int(u.get("cached_input_tokens") or 0),
                    "cache_write": int(u.get("cache_write_input_tokens") or 0),
                    "reasoning": int(u.get("reasoning_output_tokens") or 0),
                    "total": total,
                    "cost": None,
                    "source": f"codex: {path.name}",
                    "key": key_of(line),
                }
            )


def scan_kimi_lines(path: Path, insert):
    """Kimi Code: usage.record 事件，time 为毫秒时间戳。"""
    for raw in path.open("r", encoding="utf-8", errors="replace"):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "usage.record":
            continue
        usage = obj.get("usage") or {}
        inp = (
            int(usage.get("inputOther") or 0)
            + int(usage.get("inputCacheRead") or 0)
            + int(usage.get("inputCacheCreation") or 0)
        )
        out = int(usage.get("output") or 0)
        if inp == 0 and out == 0:
            continue
        insert(
            {
                "tool": "kimi",
                "model": obj.get("model") or "unknown",
                "provider": "kimi",
                "ts": iso_from_ms(obj.get("time")),
                "input": inp,
                "output": out,
                "cached": int(usage.get("inputCacheRead") or 0),
                "cache_write": int(usage.get("inputCacheCreation") or 0),
                "reasoning": 0,
                "total": inp + out,
                "cost": None,
                "source": f"kimi: {path.parent.parent.name}",
                "key": key_of(line),
            }
        )


def scan_openclaw_lines(path: Path, insert):
    """OpenClaw: assistant 消息上的 usage 对象。"""
    for raw in path.open("r", encoding="utf-8", errors="replace"):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "message":
            continue
        msg = obj.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        usage = msg.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        inp = int(usage.get("input") or 0)
        out = int(usage.get("output") or 0)
        total = int(usage.get("totalTokens") or 0) or (inp + out)
        if inp == 0 and out == 0 and total == 0:
            continue
        cost_obj = usage.get("cost")
        cost = cost_obj.get("total") if isinstance(cost_obj, dict) else None
        cost = float(cost) if isinstance(cost, (int, float)) else None
        ts = msg.get("timestamp") or obj.get("timestamp")
        ts_iso = iso_from_ms(ts) if isinstance(ts, (int, float)) else (ts or "")
        insert(
            {
                "tool": "openclaw",
                "model": msg.get("model") or msg.get("api") or "unknown",
                "provider": msg.get("provider") or "",
                "ts": ts_iso,
                "input": inp,
                "output": out,
                "cached": int(usage.get("cacheRead") or 0),
                "cache_write": int(usage.get("cacheWrite") or 0),
                "reasoning": int(usage.get("reasoningTokens") or 0),
                "total": total,
                "cost": cost,
                "source": f"openclaw: {path.name}",
                "key": key_of(line),
            }
        )


def _deepseek_iso(ts) -> str:
    """dsh 日志里的时间可能是 ISO 字符串、毫秒或秒时间戳。"""
    if ts is None or ts == "":
        return ""
    if isinstance(ts, (int, float)):
        if ts > 1e12:
            return iso_from_ms(ts)
        if ts > 1e9:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
        return ""
    return str(ts)


def _open_jsonl_lines(path: Path):
    """逐行读取 .jsonl 或 .jsonl.zstd（优先 zstandard 库，缺失时退回系统 zstd 命令）。"""
    if path.name.endswith(".zstd"):
        try:
            import zstandard as zstd

            dctx = zstd.ZstdDecompressor()
            with path.open("rb") as fh:
                raw = dctx.stream_reader(fh).read()
            return raw.decode("utf-8", "replace").splitlines()
        except ImportError:
            try:
                out = subprocess.run(
                    ["zstd", "-dc", str(path)], capture_output=True, timeout=120
                )
                if out.returncode == 0:
                    return out.stdout.decode("utf-8", "replace").splitlines()
            except Exception:
                pass
            log(
                f"缺少 zstandard 库且系统无 zstd 命令，无法读取 {path.name}"
                "（pip install zstandard 可修复）"
            )
            return []
        except Exception:
            return []
    return path.open("r", encoding="utf-8", errors="replace")


def _deepseek_model_from_log(path: Path):
    """从单个会话日志提取模型名：取最后一次 request/header 的 header.config.model。

    一个会话中途换模型时以最后一次为准（tokenUsage 汇总大部分发生在最后一次请求周期）。
    """
    model = None
    try:
        for raw in _open_jsonl_lines(path):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "request/header":
                continue
            data = obj.get("data") or {}
            hdr = data.get("header") if isinstance(data.get("header"), dict) else data
            cfg = hdr.get("config") if isinstance(hdr.get("config"), dict) else {}
            m = cfg.get("model") or hdr.get("model")
            if m:
                model = m
    except Exception:
        pass
    return model


def _deepseek_models_by_session():
    """session uuid -> 模型名映射，用于给 projcache 汇总回填真实模型。"""
    models = {}
    for root in SOURCES["deepseek"]:
        if not root.exists() or root.name == "storages":
            continue
        try:
            logs = [p for p in root.rglob("*") if p.is_file() and _deepseek_log_filter(p)]
        except Exception:
            continue
        for p in logs:
            sid = p.parent.name
            if sid in models:
                continue
            m = _deepseek_model_from_log(p)
            if m:
                models[sid] = m
    return models


def scan_deepseek_lines(path: Path, insert):
    """DeepSeek Harness: ~/.dsh/sessions(**/session.jsonl[.zstd]) + storages/session_projcache.json。

    优先读取 ~/.dsh/storages/session_projcache.json 里每个会话的 tokenUsage 汇总；
    也兼容 ~/.dsh/sessions 的 session.jsonl / session.jsonl.zstd 事件日志
    （assistant/message 的 usage 字段，模型取 request/header 的 header.config.model）。
    """
    model, provider = None, None
    chunk_seqs = set()
    if path.name == "session_projcache.json":
        try:
            with path.open("r", encoding="utf-8") as fh:
                cache = json.load(fh)
        except Exception:
            return
        session_models = _deepseek_models_by_session()
        sessions = (cache.get("tables") or {}).get("sessions") or {}
        if isinstance(sessions, dict):
            for sid, sess in sessions.items():
                if not isinstance(sess, dict):
                    continue
                rows = sess.get("rows") or {}
                if not isinstance(rows, dict):
                    continue
                tu = rows.get("tokenUsage") or {}
                val = tu.get("val") if isinstance(tu, dict) else None
                if not isinstance(val, dict):
                    continue
                totals = val.get("totals") or {}
                if not isinstance(totals, dict):
                    continue
                unc = int(totals.get("uncachedInputTokens") or 0)
                cache_read = int(totals.get("cacheReadTokens") or 0)
                cache_write = int(totals.get("cacheWriteTokens") or 0)
                out = int(totals.get("outputTokens") or 0)
                inp = unc + cache_read + cache_write
                total = inp + out
                if inp == 0 and out == 0 and total == 0:
                    continue
                identity = sess.get("identity") or {}
                ts = identity.get("createdAt")
                insert(
                    {
                        "tool": "deepseek",
                        "model": session_models.get(sid) or "deepseek-harness",
                        "provider": "deepseek",
                        "ts": _deepseek_iso(ts),
                        "input": inp,
                        "output": out,
                        "cached": cache_read,
                        "cache_write": cache_write,
                        "reasoning": 0,
                        "total": total,
                        "cost": None,
                        "source": f"deepseek: {path.name}",
                        "key": f"deepseek-proj:{sid}",
                    }
                )
        return
    for raw in _open_jsonl_lines(path):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        typ = obj.get("type")
        if typ == "request/header":
            data = obj.get("data") or obj.get("payload") or {}
            hdr = data.get("header") if isinstance(data.get("header"), dict) else data
            cfg = hdr.get("config") if isinstance(hdr.get("config"), dict) else {}
            if cfg.get("model") or hdr.get("model"):
                model = cfg.get("model") or hdr.get("model")
            if cfg.get("provider") or hdr.get("provider"):
                provider = cfg.get("provider") or hdr.get("provider")
            continue
        data = obj.get("data")
        if not isinstance(data, dict):
            data = obj.get("payload")
        if not isinstance(data, dict):
            data = {}

        def need(value):
            return int(value or 0)

        if typ == "assistant/message":
            msg = data
            if msg.get("model"):
                model = msg["model"]
            if msg.get("provider"):
                provider = msg["provider"]
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            src_seqs = data.get("sourceEventSeqs")
            if isinstance(src_seqs, list) and src_seqs and any(s in chunk_seqs for s in src_seqs):
                continue
            use = usage if isinstance(usage, dict) else msg
            inp_u = need(use.get("uncachedInputTokens") or use.get("inputTokens") or use.get("input"))
            cache_read = need(use.get("cacheReadTokens") or use.get("cachedTokens") or use.get("cached"))
            cache_write = need(use.get("cacheWriteTokens") or use.get("cacheWrite"))
            out = need(use.get("outputTokens") or use.get("completionTokens") or use.get("output"))
            reasoning = need(use.get("reasoningTokens") or use.get("reasoning_output_tokens"))
            inp = inp_u + cache_read + cache_write
            total = need(use.get("totalTokens")) or (inp + out)
            if inp == 0 and out == 0 and total == 0:
                continue
            ts = msg.get("timestamp") or msg.get("time") or obj.get("timestamp") or obj.get("time")
            insert(
                {
                    "tool": "deepseek",
                    "model": model or msg.get("model") or provider or "unknown",
                    "provider": provider or "",
                    "ts": _deepseek_iso(ts),
                    "input": inp,
                    "output": out,
                    "cached": cache_read,
                    "cache_write": cache_write,
                    "reasoning": reasoning,
                    "total": total,
                    "cost": None,
                    "source": f"deepseek: {path.name}",
                    "key": key_of(raw),
                }
            )
            continue
        if typ == "assistant/chunk" and data.get("type") == "usage":
            seq = obj.get("seq")
            if seq is not None:
                chunk_seqs.add(seq)
            use = data.get("usage") if isinstance(data.get("usage"), dict) else data
            inp_u = need(use.get("uncachedInputTokens") or use.get("inputTokens") or use.get("input"))
            cache_read = need(use.get("cacheReadTokens") or use.get("cachedTokens") or use.get("cached"))
            cache_write = need(use.get("cacheWriteTokens") or use.get("cacheWrite"))
            out = need(use.get("outputTokens") or use.get("completionTokens") or use.get("output"))
            reasoning = need(use.get("reasoningTokens") or use.get("reasoning_output_tokens"))
            inp = inp_u + cache_read + cache_write
            total = need(use.get("totalTokens")) or (inp + out)
            if inp == 0 and out == 0 and total == 0:
                continue
            ts = data.get("timestamp") or data.get("time") or obj.get("timestamp") or obj.get("time")
            insert(
                {
                    "tool": "deepseek",
                    "model": model or data.get("model") or provider or "unknown",
                    "provider": provider or data.get("provider") or "",
                    "ts": _deepseek_iso(ts),
                    "input": inp,
                    "output": out,
                    "cached": cache_read,
                    "cache_write": cache_write,
                    "reasoning": reasoning,
                    "total": total,
                    "cost": None,
                    "source": f"deepseek: {path.name}",
                    "key": key_of(raw),
                }
            )



PARSERS = {
    "codex": scan_codex_lines,
    "kimi": scan_kimi_lines,
    "openclaw": scan_openclaw_lines,
    "deepseek": scan_deepseek_lines,
}


def _deepseek_log_filter(p: Path) -> bool:
    """DeepSeek Harness 会话日志：普通 .jsonl 或 zstd 压缩的 .jsonl.zstd。"""
    name = p.name
    if name.startswith(".") or "trajectory" in name or ".reset." in name or ".deleted." in name:
        return False
    if "-checkpoint" in name:
        return False
    return name.endswith(".jsonl") or name.endswith(".jsonl.zstd")


def collect_files(tool: str):
    """返回 (tool, path) 列表，按工具过滤无用文件。

    DeepSeek Harness 优先读 storages/session_projcache.json 的每会话汇总；
    仅当汇总文件不存在时，才回退扫描 sessions/**/session.jsonl(.zstd) 日志，
    避免两边重复统计同一会话。
    """
    out = []
    if tool == "deepseek":
        proj, sess = [], []
        for root in SOURCES[tool]:
            if not root.exists():
                continue
            try:
                if root.name == "storages":
                    proj = [p for p in root.rglob("session_projcache.json") if p.is_file()]
                else:
                    sess = [p for p in root.rglob("*") if p.is_file() and _deepseek_log_filter(p)]
            except Exception:
                pass
        chosen = proj if proj else sess
        return [(tool, p) for p in chosen]
    for root in SOURCES[tool]:
        if not root.exists():
            continue
        if tool == "codex":
            files = root.rglob("rollout-*.jsonl")
        elif tool == "kimi":
            files = root.rglob("wire.jsonl")
        else:  # openclaw
            files = (
                p
                for p in root.rglob("*.jsonl")
                if not p.name.startswith(".")
                and "trajectory" not in p.name
                and ".reset." not in p.name
                and ".deleted." not in p.name
            )
        for p in files:
            try:
                if p.is_file():
                    out.append((tool, p))
            except Exception:
                pass
    return out


def scan(force: bool = False, quiet: bool = False) -> int:
    conn = get_db()
    new_count = 0

    def insert(rec):
        nonlocal new_count
        rec = dict(rec)
        rec["day"] = local_day(rec.get("ts") or "")
        exists = conn.execute(
            "SELECT 1 FROM records WHERE key=?", (rec["key"],)
        ).fetchone()
        conn.execute(
            """
            INSERT OR REPLACE INTO records
              (key, tool, model, provider, ts, day, input, output, cached,
               cache_write, reasoning, total, cost, source)
            VALUES
              (:key, :tool, :model, :provider, :ts, :day, :input, :output, :cached,
               :cache_write, :reasoning, :total, :cost, :source)
            """,
            rec,
        )
        # INSERT OR REPLACE 会把同 key 的旧记录删掉重插（rowcount 恒为 1），
        # 只有 key 原本不存在才是真正的新增，避免 --force 重扫把替换也计成“新增”。
        if exists is None:
            new_count += 1

    error_count = 0
    files = []
    for tool in TOOL_ORDER:
        files.extend(collect_files(tool))

    for tool, path in files:
        try:
            stat = path.stat()
            meta = conn.execute(
                "SELECT mtime, size FROM file_meta WHERE path=?", (str(path),)
            ).fetchone()
            if (
                not force
                and meta is not None
                and abs(meta[0] - stat.st_mtime) < 1e-6
                and meta[1] == stat.st_size
            ):
                continue
            parser = PARSERS[tool]
            parser(path, insert)
            conn.execute(
                "INSERT OR REPLACE INTO file_meta(path, mtime, size, scanned_at) VALUES (?,?,?,?)",
                (str(path), stat.st_mtime, stat.st_size, now_local()),
            )
            conn.commit()
        except Exception as exc:
            error_count += 1
            log(f"扫描失败 {path}: {exc}", quiet=quiet)

    removed = dedupe_records(conn)
    if removed:
        conn.commit()
        log(f"清理历史重复记录 {removed} 条", quiet=quiet)
    conn.close()
    log(f"扫描完成：新增 {new_count} 条用量记录，错误 {error_count} 条", quiet=quiet)
    return new_count


# ---------------------------------------------------------------------------
# 报表
# ---------------------------------------------------------------------------

def dedupe_records(conn):
    """清理历史重复记录：同一来源文件的相同用量快照只保留首条。"""
    cur = conn.execute(
        """
        DELETE FROM records
        WHERE tool='codex' AND rowid NOT IN (
            SELECT MIN(rowid) FROM records
            WHERE tool='codex'
            GROUP BY source, input, output, cached, cache_write, reasoning, total
        )
        """
    )
    return cur.rowcount


def _totals_range(conn, days):
    """最近 N 天（含今天）的汇总，按北京时间日期窗口。"""
    cutoff = (today_local() - timedelta(days=days - 1)).isoformat()
    return _totals(conn, "WHERE day >= ?", (cutoff,))


def _totals(conn, where="", params=()):
    sql = f"""
        SELECT
          COALESCE(SUM(input),0), COALESCE(SUM(output),0),
          COALESCE(SUM(cached),0), COALESCE(SUM(cache_write),0),
          COALESCE(SUM(reasoning),0), COALESCE(SUM(total),0),
          COALESCE(SUM(cost),0), COUNT(*)
        FROM records {where}
    """
    row = conn.execute(sql, params).fetchone()
    return {
        "input": row[0], "output": row[1], "cached": row[2],
        "cache_write": row[3], "reasoning": row[4], "total": row[5],
        "cost": row[6], "records": row[7],
    }


def build_stats(conn, days=None):
    """汇总所有统计数据，供 report 与 dashboard 共用。"""
    totals = _totals(conn)
    totals_7d = _totals_range(conn, 7)
    totals_30d = _totals_range(conn, 30)
    by_tool = {}
    for tool in TOOL_ORDER:
        row = conn.execute(
            "SELECT COALESCE(SUM(input),0), COALESCE(SUM(output),0), COALESCE(SUM(cached),0),"
            " COALESCE(SUM(cache_write),0), COALESCE(SUM(reasoning),0), COALESCE(SUM(total),0),"
            " COALESCE(SUM(cost),0), COUNT(*)"
            " FROM records WHERE tool=?",
            (tool,),
        ).fetchone()
        by_tool[tool] = {
            "input": row[0], "output": row[1], "cached": row[2],
            "cache_write": row[3], "reasoning": row[4], "total": row[5],
            "cost": row[6], "records": row[7],
        }

    by_model = [
        {
            "model": r[0], "tool": r[1], "input": r[2], "output": r[3],
            "cached": r[4], "reasoning": r[5], "total": r[6], "cost": r[7],
            "records": r[8],
        }
        for r in conn.execute(
            """
            SELECT model, tool, COALESCE(SUM(input),0), COALESCE(SUM(output),0),
                   COALESCE(SUM(cached),0), COALESCE(SUM(reasoning),0),
                   COALESCE(SUM(total),0), COALESCE(SUM(cost),0), COUNT(*)
            FROM records GROUP BY model, tool ORDER BY SUM(total) DESC
            """
        )
    ]

    # 按天（北京时间）统计
    where, params = "", ()
    if days:
        cutoff = (today_local() - timedelta(days=days - 1)).isoformat()
        where = "WHERE day >= ?"
        params = (cutoff,)
    by_day = {}
    for r in conn.execute(
        f"""
        SELECT day, tool, COALESCE(SUM(total),0)
        FROM records {where}
        GROUP BY day, tool ORDER BY day
        """,
        params,
    ):
        by_day.setdefault(r[0], {})[r[1]] = r[2]

    recent = [
        {
            "ts": r[0], "day": r[1], "tool": r[2], "model": r[3],
            "input": r[4], "output": r[5], "total": r[6], "cost": r[7],
        }
        for r in conn.execute(
            """
            SELECT ts, day, tool, model, input, output, total, cost
            FROM records ORDER BY ts DESC, rowid DESC LIMIT 100
            """
        )
    ]
    return {"totals": totals, "totals_7d": totals_7d, "totals_30d": totals_30d,
            "by_tool": by_tool, "by_model": by_model,
            "by_day": by_day, "recent": recent}


def print_report(stats):
    line = "-" * 64
    print()
    print(f"TokenWatch 用量统计    更新于 {now_local()}")
    print(line)
    print(f"{'工具':<14}{'记录数':>8}{'输入':>12}{'输出':>12}{'缓存读':>12}{'合计':>14}")
    for tool in TOOL_ORDER:
        s = stats["by_tool"].get(tool, {})
        if not s.get("total"):
            continue
        print(
            f"{TOOL_LABELS[tool]:<14}{fmt_int(s['records']):>8}"
            f"{fmt_num(s['input']):>12}{fmt_num(s['output']):>12}"
            f"{fmt_num(s['cached']):>12}{fmt_num(s['total']):>14}"
        )
    t = stats["totals"]
    print(line)
    print(
        f"{'合计':<14}{fmt_int(t['records']):>8}{fmt_num(t['input']):>12}"
        f"{fmt_num(t['output']):>12}{fmt_num(t['cached']):>12}{fmt_num(t['total']):>14}"
    )
    t7 = stats.get("totals_7d")
    t30 = stats.get("totals_30d")
    if t30 is not None:
        print(
            f"\n近 30 天: {fmt_num(t30['total'])}    近 7 天: {fmt_num(t7['total'])}"
            f"    全历史: {fmt_num(t['total'])}"
        )
    if t.get("cost"):
        print(f"已记录费用（OpenClaw 上报）: ¥{t['cost']:.4f}")

    print(f"\n按模型（前 15）:")
    print(f"{'模型':<42}{'工具':<12}{'输入':>12}{'输出':>12}{'合计':>14}")
    for m in stats["by_model"][:15]:
        print(
            f"{m['model'][:40]:<42}{TOOL_LABELS.get(m['tool'], m['tool']):<12}"
            f"{fmt_num(m['input']):>12}{fmt_num(m['output']):>12}{fmt_num(m['total']):>14}"
        )

    by_day = stats["by_day"]
    if by_day:
        print(f"\n每日用量（最近 {min(len(by_day), 7)} 天）:")
        print(f"{'日期':<12}" + "".join(f"{TOOL_LABELS[t]:>12}" for t in TOOL_ORDER) + f"{'合计':>14}")
        for day, tool_totals in list(by_day.items())[-7:]:
            row = f"{day:<12}"
            day_sum = 0
            for tool in TOOL_ORDER:
                v = tool_totals.get(tool, 0)
                day_sum += v
                row += f"{fmt_num(v):>12}"
            row += f"{fmt_num(day_sum):>14}"
            print(row)


# ---------------------------------------------------------------------------
# HTML 看板
# ---------------------------------------------------------------------------

def _svg_stackbar(by_day, days=30):
    """最近 N 天按工具堆叠柱状图。"""
    end = today_local()
    start = end - timedelta(days=days - 1)
    days_list = []
    d = start
    while d <= end:
        days_list.append(d.isoformat())
        d += timedelta(days=1)
    daily = {
        day: [float(by_day.get(day, {}).get(t, 0) or 0) for t in TOOL_ORDER]
        for day in days_list
    }
    maxv = max((sum(v) for v in daily.values()), default=1) or 1
    W, H, PAD_L, PAD_B, PAD_T = 1080, 320, 70, 40, 20
    bw = (W - PAD_L) / max(len(days_list), 1)
    parts = [
        f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto" role="img" aria-label="每日Token用量">'
    ]
    for i in range(5):
        y = PAD_T + (H - PAD_T - PAD_B) * (4 - i) / 4
        val = maxv * i / 4
        parts.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W}" y2="{y:.1f}" stroke="rgba(232,230,239,0.06)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{PAD_L - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#9b97a8" font-size="11">{fmt_num(val)}</text>'
        )
    for idx, day in enumerate(days_list):
        x = PAD_L + idx * bw
        y_cursor = H - PAD_B
        for ti, tool in enumerate(TOOL_ORDER):
            v = daily[day][ti]
            if v <= 0:
                continue
            h = (H - PAD_T - PAD_B) * v / maxv
            y = y_cursor - h
            parts.append(
                f'<rect x="{x + 1:.1f}" y="{y:.1f}" width="{max(bw - 2, 1):.1f}" height="{h:.1f}" '
                f'fill="{TOOL_COLORS[tool]}" rx="1"/>'
            )
            y_cursor = y
        if idx % 5 == 0 or idx == len(days_list) - 1:
            parts.append(
                f'<text x="{x + bw / 2:.1f}" y="{H - 14}" text-anchor="middle" fill="#9b97a8" font-size="10">{day[5:]}</text>'
            )
    parts.append("</svg>")
    legend = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:18px;color:#9b97a8">'
        f'<span style="width:12px;height:12px;border-radius:3px;background:{TOOL_COLORS[t]}"></span>{TOOL_LABELS[t]}</span>'
        for t in TOOL_ORDER
    )
    return "".join(parts), legend


def _svg_modelbars(by_model, top=12):
    items = [m for m in by_model[:top] if m["total"]]
    if not items:
        return "<p style='color:#9b97a8'>暂无数据</p>"
    maxv = max(m["total"] for m in items) or 1
    W, H = 1080, len(items) * 34 + 20
    parts = [
        f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto" role="img" aria-label="模型用量排行">'
    ]
    bar_max = W - 318 - 108  # 条形最大宽度，右侧预留数值空间，避免数字被裁剪
    val_x = W - 12  # 数值固定右对齐，任何长度都不会超出视口
    for i, m in enumerate(items):
        y = 14 + i * 34
        model_full = m["model"] or ""
        label = model_full[:30]
        w = max(bar_max * m["total"] / maxv, 2)
        color = TOOL_COLORS.get(m["tool"], "#6f6b7d")
        tip = ""
        if model_full:
            tip = (f'<title>{html.escape(model_full)} · '
                   f'{TOOL_LABELS.get(m["tool"], m["tool"])} · '
                   f'{fmt_num(m["total"])} tokens</title>')
        parts.append(f'<text x="4" y="{y + 4}" fill="#e8e6ef" font-size="12">{tip}{html.escape(label)}</text>')
        parts.append(
            f'<text x="310" y="{y + 4}" fill="#9b97a8" font-size="11" text-anchor="end">'
            f'{TOOL_LABELS.get(m["tool"], m["tool"])}</text>'
        )
        parts.append(f'<rect x="318" y="{y - 8}" width="{w:.1f}" height="14" rx="3" fill="{color}"/>')
        parts.append(
            f'<text x="{val_x}" y="{y + 4}" text-anchor="end" fill="#e8e6ef" font-size="11">{fmt_num(m["total"])}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _svg_donut(by_tool, size=220):
    vals = [(t, float(by_tool.get(t, {}).get("total") or 0)) for t in TOOL_ORDER]
    total = sum(v for _, v in vals)
    if total <= 0:
        return "<p style='color:#9b97a8'>暂无数据</p>", ""
    r, cx, cy, sw = 70, size / 2, size / 2, 34
    circ = 2 * 3.14159265 * r
    parts = [
        f'<svg viewBox="0 0 {size} {size}" style="width:{size}px;height:auto" role="img" aria-label="工具占比">'
    ]
    acc = 0
    for t, v in vals:
        frac = v / total
        dash = frac * circ
        gap = circ - dash
        off = -acc * circ
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{TOOL_COLORS[t]}" '
            f'stroke-width="{sw}" stroke-dasharray="{dash:.2f} {gap:.2f}" '
            f'stroke-dashoffset="{off:.2f}" transform="rotate(-90 {cx} {cy})"/>'
        )
        acc += frac
    parts.append(
        f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" fill="#e8e6ef" font-size="20" font-weight="600">{fmt_num(total)}</text>'
    )
    parts.append(
        f'<text x="{cx}" y="{cy + 16}" text-anchor="middle" fill="#9b97a8" font-size="11">Token 合计</text>'
    )
    parts.append("</svg>")
    legend = ""
    for t, v in vals:
        pct = v / total * 100
        legend += (
            f'<div style="display:flex;align-items:center;gap:8px;margin:4px 0;font-size:13px;color:#9b97a8">'
            f'<span style="width:10px;height:10px;border-radius:3px;background:{TOOL_COLORS[t]}"></span>'
            f'{TOOL_LABELS[t]}  <b style="margin-left:auto">{fmt_num(v)}</b>'
            f'<span style="color:#9b97a8">{pct:.1f}%</span></div>'
        )
    return "".join(parts), legend


def _fmt_local(ts):
    """把记录的 UTC/带时区时间戳转为北京时间显示。"""
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        dt = dt.astimezone(CN_TZ)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts[:19].replace("T", " ")


def build_dashboard(stats, out_path):
    stack, legend_stack = _svg_stackbar(stats["by_day"])
    donut, legend_donut = _svg_donut(stats["by_tool"])
    modelbars = _svg_modelbars(stats["by_model"])
    t = stats["totals"]
    t7 = stats.get("totals_7d") or t
    t30 = stats.get("totals_30d") or t
    pct7 = f"{t7['total'] / t['total'] * 100:.1f}%" if t["total"] else "0%"
    pct30 = f"{t30['total'] / t['total'] * 100:.1f}%" if t["total"] else "0%"

    cards = [
        ("Token 总计", fmt_num(t["total"]), "全部历史累计", "#e56399"),
        ("近 30 天", fmt_num(t30["total"]), f"占总量 {pct30}", "#8b7cf6"),
        ("近 7 天", fmt_num(t7["total"]), f"占总量 {pct7}", "#f0a03c"),
        ("输入 Token", fmt_num(t["input"]), f"缓存读 {fmt_num(t['cached'])}", "#8b7cf6"),
        ("输出 Token", fmt_num(t["output"]), f"含推理 {fmt_num(t['reasoning'])}", "#e56399"),
        ("记录条数", fmt_int(t["records"]), f"费用 ¥{t['cost']:.4f}" if t.get("cost") else "费用暂无", "#f0a03c"),
    ]
    card_html = "".join(
        f'<div class="card"><div class="card-label">{label}</div>'
        f'<div class="card-value" style="color:{color}">{value}</div>'
        f'<div class="card-sub">{sub}</div></div>'
        for label, value, sub, color in cards
    )

    rows = []
    for r in stats["recent"][:50]:
        ts = _fmt_local(r["ts"])
        rows.append(
            f"<tr><td>{html.escape(ts)}</td>"
            f"<td><span class='badge' style='background:{TOOL_COLORS.get(r['tool'], '#6f6b7d')}'>"
            f"{TOOL_LABELS.get(r['tool'], r['tool'])}</span></td>"
            f"<td>{html.escape(r['model'] or '-')}</td>"
            f"<td>{fmt_int(r['input'])}</td><td>{fmt_int(r['output'])}</td>"
            f"<td><b>{fmt_int(r['total'])}</b></td></tr>"
        )
    table = "".join(rows) or "<tr><td colspan='6' style='color:#9b97a8'>暂无记录，先运行 scan</td></tr>"

    css = """
    /* 暗夜仪表盘 · Night Ops */
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #101014; color: #e8e6ef; padding: 24px;
      font: 14px/1.6 -apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
      background-image: radial-gradient(900px 300px at 50% -140px, rgba(229,99,153,0.08), transparent 70%);
      background-repeat: no-repeat;
    }
    h1 { font-size: 22px; font-weight: 600; letter-spacing: 1px; margin-bottom: 4px; }
    h1::after { content: ""; display: block; width: 56px; height: 2px; background: #e56399; margin-top: 8px; border-radius: 1px; }
    .sub { color: #9b97a8; font-size: 13px; margin-bottom: 20px; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin-bottom: 20px; }
    .card { background: #18181f; border: 1px solid rgba(232,230,239,0.08); border-radius: 10px; padding: 16px; box-shadow: 0 1px 2px rgba(0,0,0,0.4); }
    .card:hover { transform: translateY(-2px); transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease; box-shadow: 0 12px 32px rgba(0,0,0,0.5); border-color: rgba(232,230,239,0.15); }
    .card-label { color: #9b97a8; font-size: 12px; letter-spacing: 1px; }
    .card-value { font-family: ui-monospace,'Cascadia Mono',Consolas,monospace; font-size: 30px; font-weight: 700; margin: 6px 0 2px; font-variant-numeric: tabular-nums; }
    .card-sub { color: #6f6b7d; font-size: 12px; font-family: ui-monospace,'Cascadia Mono',Consolas,monospace; }
    .panel { background: #18181f; border: 1px solid rgba(232,230,239,0.08); border-radius: 10px; padding: 18px; margin-bottom: 20px; box-shadow: 0 1px 2px rgba(0,0,0,0.4); }
    .panel h2 { font-size: 14px; font-weight: 600; letter-spacing: 1px; margin-bottom: 14px; color: #e8e6ef; }
    .panel h2::after { content: ""; display: block; width: 40px; height: 2px; background: #e56399; margin-top: 6px; border-radius: 1px; opacity: 0.8; }
    .cols { display: grid; grid-template-columns: 2fr 1fr; gap: 20px; }
    @media (max-width: 900px) { .cols { grid-template-columns: 1fr; } }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th { text-align: left; color: #9b97a8; font-weight: 500; padding: 8px 10px; background: #1e1e26; border-bottom: 1px solid rgba(232,230,239,0.15); white-space: nowrap; }
    td { padding: 7px 10px; border-bottom: 1px solid rgba(232,230,239,0.06); font-family: ui-monospace,'Cascadia Mono',Consolas,monospace; font-variant-numeric: tabular-nums; }
    tbody tr:hover { background: rgba(232,230,239,0.04); }
    .badge { display: inline-block; padding: 2px 10px; border-radius: 6px; color: #101014; font-size: 12px; font-weight: 600; font-family: -apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif; }
    svg text { font-family: ui-monospace,'Cascadia Mono',Consolas,monospace; }
    """

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>TokenWatch 用量看板</title>
<style>{css}</style>
</head>
<body>
<h1>TokenWatch · AI 工具 Token 用量</h1>
<div class="sub">更新于 {now_local()}（Codex / Kimi Code / OpenClaw / DeepSeek Harness）· 页面每 1 分钟自动刷新</div>
<div class="cards">{card_html}</div>
<div class="cols">
  <div class="panel">
    <h2>每日 Token 用量（最近 30 天，按工具堆叠）</h2>
    {stack}
    <div style="margin-top:10px">{legend_stack}</div>
  </div>
  <div class="panel">
    <h2>工具占比</h2>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <div>{donut}</div>
      <div style="flex:1;min-width:180px">{legend_donut}</div>
    </div>
  </div>
</div>
<div class="panel">
  <h2>模型用量排行（Top 12）</h2>
  {modelbars}
</div>
<div class="panel">
  <h2>最近 50 条用量记录</h2>
  <table>
    <thead><tr><th>时间(本地)</th><th>工具</th><th>模型</th><th>输入</th><th>输出</th><th>合计</th></tr></thead>
    <tbody>{table}</tbody>
  </table>
</div>
</body>
</html>
"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")
    return str(out_path)


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def cmd_scan(args):
    n = scan(force=args.force, quiet=args.quiet)
    if not args.quiet:
        print(f"新增 {n} 条记录。运行 report 查看汇总。")


def cmd_report(args):
    conn = get_db()
    stats = build_stats(conn, days=args.days)
    conn.close()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print_report(stats)


def cmd_dashboard(args):
    conn = get_db()
    stats = build_stats(conn, days=args.days)
    conn.close()
    out = build_dashboard(stats, args.out or DASHBOARD_PATH)
    print(f"看板已生成: {out}")


def _find_pythonw():
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        return exe
    pw = Path(exe).with_name("pythonw.exe")
    return str(pw) if pw.exists() else exe


def cmd_install(args):
    if os.name != "nt":
        print("计划任务仅支持 Windows。")
        return 1
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / "token_watch.py"
    src = Path(__file__).resolve()
    if src != target.resolve():
        shutil.copy2(src, target)
        print(f"[OK] 脚本已复制到 {target}")
    else:
        print(f"[OK] 已从 {target} 运行，跳过自复制")
    interp = _find_pythonw()
    tasks = [
        ("TokenWatch\\UsageScan", "/SC MINUTE /MO 10", "每 10 分钟扫描一次新用量"),
        ("TokenWatch\\OnLogon", "/SC ONLOGON", "登录时补扫一次"),
    ]
    for name, sched, desc in tasks:
        tr = f'"{interp}" "{target}" scan --quiet'
        cmd = ["schtasks", "/Create", "/F", "/TN", name, "/TR", tr, "/RL", "LIMITED"] + sched.split()
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            print(f"[OK] {name} 已创建（{desc}）")
        elif name.endswith("OnLogon") and _register_logon_run(interp, target):
            print(f"[OK] {name} 创建需要管理员权限，已改用注册表 Run 登录自启（{desc}）")
        else:
            print(f"[失败] {name}: {r.stderr.strip() or r.stdout.strip()}")
    print(f"\n脚本已安装到 {target}")
    print("可用 schtasks /Query /TN \"TokenWatch\\UsageScan\" 查看任务状态。")


def _register_logon_run(interp, target):
    """ONLOGON 计划任务需要管理员权限，降级为 HKCU Run 注册表自启（无需管理员）。"""
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        cmd = f'"{interp}" "{target}" scan --quiet'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "TokenWatch UsageScan", 0, winreg.REG_SZ, cmd)
        return True
    except Exception:
        return False


def cmd_uninstall(args):
    for name in ["TokenWatch\\UsageScan", "TokenWatch\\OnLogon"]:
        r = subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", name],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            print(f"[OK] {name} 已删除")
        else:
            print(f"[跳过] {name}: {r.stderr.strip() or '不存在'}")
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "TokenWatch UsageScan")
        print("[OK] 注册表 Run 登录自启项已删除")
    except FileNotFoundError:
        print("[跳过] 注册表 Run 登录自启项不存在")
    except Exception as exc:
        print(f"[跳过] 注册表清理: {exc}")


def cmd_watch(args):
    import time

    interval = args.interval
    if args.dashboard:
        print(f"后台监控模式：每 {interval} 秒扫描一次并刷新看板（{args.dashboard}），Ctrl+C 退出")
    else:
        print(f"前台监控模式：每 {interval} 秒扫描一次，Ctrl+C 退出")
    while True:
        try:
            n = scan(quiet=True)
            msg = f"[{now_local()}] 新增 {n} 条"
            if args.dashboard:
                conn = get_db()
                stats = build_stats(conn, days=args.days)
                conn.close()
                build_dashboard(stats, args.dashboard)
                msg += "，看板已刷新"
            print(msg)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[{now_local()}] 扫描异常: {exc}")
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(
        prog="token_watch",
        description="跨 AI 工具（Codex / Kimi Code / OpenClaw）Token 用量自动统计",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="增量扫描并入库")
    p.add_argument("--force", action="store_true", help="忽略文件缓存，全量重扫")
    p.add_argument("--quiet", action="store_true", help="安静模式（计划任务用）")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("report", help="终端汇总报表")
    p.add_argument("--days", type=int, default=30, help="每日趋势天数（默认30）")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("dashboard", help="生成 HTML 看板")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--out", help="输出路径（默认 ~/.token-watch/dashboard.html）")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("install", help="注册 Windows 计划任务自动扫描")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="删除计划任务")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("watch", help="循环扫描（带 --dashboard 时同步刷新看板）")
    p.add_argument("--interval", type=int, default=30, help="扫描间隔秒数")
    p.add_argument("--dashboard", help="每次扫描后重新生成看板到该路径")
    p.add_argument("--days", type=int, default=30, help="看板每日趋势天数（默认30）")
    p.set_defaults(func=cmd_watch)

    args = ap.parse_args()
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
