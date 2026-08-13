#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TokenWatch —— 跨工具 Token 用量自动统计
========================================

自动扫描本机 AI 编程工具的本地会话记录，汇总 Token 消耗与所用模型：
  * Codex      ~/.codex/sessions  + archived_sessions (token_count 事件)
  * Kimi Code  ~/.kimi-code/sessions/**/wire.jsonl    (usage.record 事件)
  * OpenClaw   ~/.openclaw/agents/**/sessions/*.jsonl (assistant 消息 usage 字段)

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
}

TOOL_LABELS = {"codex": "Codex", "kimi": "Kimi Code", "openclaw": "OpenClaw"}
TOOL_COLORS = {"codex": "#10a37f", "kimi": "#7c6cf0", "openclaw": "#f5a623"}
TOOL_ORDER = ["codex", "kimi", "openclaw"]


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
        dt = datetime.fromisoformat(ts)
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

def scan_codex_lines(path: Path, insert):
    """Codex: event_msg/token_count 事件，携带 last_token_usage。"""
    model, provider = None, None
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
            u = info.get("last_token_usage") or info.get("total_token_usage") or {}
            inp = int(u.get("input_tokens") or 0)
            out = int(u.get("output_tokens") or 0)
            total = int(u.get("total_tokens") or 0) or (inp + out)
            if inp == 0 and out == 0 and total == 0:
                continue
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


PARSERS = {
    "codex": scan_codex_lines,
    "kimi": scan_kimi_lines,
    "openclaw": scan_openclaw_lines,
}


def collect_files(tool: str):
    """返回 (tool, path) 列表，按工具过滤无用文件。"""
    out = []
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
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO records
              (key, tool, model, provider, ts, day, input, output, cached,
               cache_write, reasoning, total, cost, source)
            VALUES
              (:key, :tool, :model, :provider, :ts, :day, :input, :output, :cached,
               :cache_write, :reasoning, :total, :cost, :source)
            """,
            rec,
        )
        new_count += cur.rowcount

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

    conn.close()
    log(f"扫描完成：新增 {new_count} 条用量记录，错误 {error_count} 条", quiet=quiet)
    return new_count


# ---------------------------------------------------------------------------
# 报表
# ---------------------------------------------------------------------------

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
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W}" y2="{y:.1f}" stroke="#2a2f3a" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{PAD_L - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#8b93a7" font-size="11">{fmt_num(val)}</text>'
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
                f'<text x="{x + bw / 2:.1f}" y="{H - 14}" text-anchor="middle" fill="#8b93a7" font-size="10">{day[5:]}</text>'
            )
    parts.append("</svg>")
    legend = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:18px;color:#c9d1d9">'
        f'<span style="width:12px;height:12px;border-radius:3px;background:{TOOL_COLORS[t]}"></span>{TOOL_LABELS[t]}</span>'
        for t in TOOL_ORDER
    )
    return "".join(parts), legend


def _svg_modelbars(by_model, top=12):
    items = [m for m in by_model[:top] if m["total"]]
    if not items:
        return "<p style='color:#8b93a7'>暂无数据</p>"
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
        color = TOOL_COLORS.get(m["tool"], "#888")
        tip = ""
        if model_full:
            tip = (f'<title>{html.escape(model_full)} · '
                   f'{TOOL_LABELS.get(m["tool"], m["tool"])} · '
                   f'{fmt_num(m["total"])} tokens</title>')
        parts.append(f'<text x="4" y="{y + 4}" fill="#c9d1d9" font-size="12">{tip}{html.escape(label)}</text>')
        parts.append(
            f'<text x="310" y="{y + 4}" fill="#8b93a7" font-size="11" text-anchor="end">'
            f'{TOOL_LABELS.get(m["tool"], m["tool"])}</text>'
        )
        parts.append(f'<rect x="318" y="{y - 8}" width="{w:.1f}" height="14" rx="3" fill="{color}"/>')
        parts.append(
            f'<text x="{val_x}" y="{y + 4}" text-anchor="end" fill="#e6edf3" font-size="11">{fmt_num(m["total"])}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _svg_donut(by_tool, size=220):
    vals = [(t, float(by_tool.get(t, {}).get("total") or 0)) for t in TOOL_ORDER]
    total = sum(v for _, v in vals)
    if total <= 0:
        return "<p style='color:#8b93a7'>暂无数据</p>", ""
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
        f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" fill="#e6edf3" font-size="20" font-weight="600">{fmt_num(total)}</text>'
    )
    parts.append(
        f'<text x="{cx}" y="{cy + 16}" text-anchor="middle" fill="#8b93a7" font-size="11">Token 合计</text>'
    )
    parts.append("</svg>")
    legend = ""
    for t, v in vals:
        pct = v / total * 100
        legend += (
            f'<div style="display:flex;align-items:center;gap:8px;margin:4px 0;font-size:13px;color:#c9d1d9">'
            f'<span style="width:10px;height:10px;border-radius:3px;background:{TOOL_COLORS[t]}"></span>'
            f'{TOOL_LABELS[t]}  <b style="margin-left:auto">{fmt_num(v)}</b>'
            f'<span style="color:#8b93a7">{pct:.1f}%</span></div>'
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
        ("Token 总计", fmt_num(t["total"]), "全部历史累计", "#10a37f"),
        ("近 30 天", fmt_num(t30["total"]), f"占总量 {pct30}", "#38bdf8"),
        ("近 7 天", fmt_num(t7["total"]), f"占总量 {pct7}", "#f472b6"),
        ("输入 Token", fmt_num(t["input"]), f"缓存读 {fmt_num(t['cached'])}", "#38bdf8"),
        ("输出 Token", fmt_num(t["output"]), f"含推理 {fmt_num(t['reasoning'])}", "#f472b6"),
        ("记录条数", fmt_int(t["records"]), f"费用 ¥{t['cost']:.4f}" if t.get("cost") else "费用暂无", "#f5a623"),
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
            f"<td><span class='badge' style='background:{TOOL_COLORS.get(r['tool'], '#555')}'>"
            f"{TOOL_LABELS.get(r['tool'], r['tool'])}</span></td>"
            f"<td>{html.escape(r['model'] or '-')}</td>"
            f"<td>{fmt_int(r['input'])}</td><td>{fmt_int(r['output'])}</td>"
            f"<td><b>{fmt_int(r['total'])}</b></td></tr>"
        )
    table = "".join(rows) or "<tr><td colspan='6' style='color:#8b93a7'>暂无记录，先运行 scan</td></tr>"

    css = """
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0d1117; color: #e6edf3; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; padding: 24px; }
    h1 { font-size: 22px; margin-bottom: 4px; }
    .sub { color: #8b93a7; font-size: 13px; margin-bottom: 20px; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin-bottom: 20px; }
    .card { background: #161b22; border: 1px solid #21262d; border-radius: 12px; padding: 16px; }
    .card-label { color: #8b93a7; font-size: 13px; }
    .card-value { font-size: 30px; font-weight: 700; margin: 6px 0 2px; }
    .card-sub { color: #8b93a7; font-size: 12px; }
    .panel { background: #161b22; border: 1px solid #21262d; border-radius: 12px; padding: 18px; margin-bottom: 20px; }
    .panel h2 { font-size: 15px; margin-bottom: 14px; color: #c9d1d9; }
    .cols { display: grid; grid-template-columns: 2fr 1fr; gap: 20px; }
    @media (max-width: 900px) { .cols { grid-template-columns: 1fr; } }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th { text-align: left; color: #8b93a7; font-weight: 500; padding: 8px 10px; border-bottom: 1px solid #21262d; }
    td { padding: 7px 10px; border-bottom: 1px solid #1c2128; }
    .badge { display: inline-block; padding: 2px 10px; border-radius: 99px; color: #fff; font-size: 12px; }
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
<div class="sub">更新于 {now_local()}（Codex / Kimi Code / OpenClaw）· 页面每 1 分钟自动刷新</div>
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