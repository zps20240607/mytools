# -*- coding: utf-8 -*-
"""CostWatch 1.0 — 成本核算（Windows，Python 3 标准库，零依赖）。

只读 TokenWatch 的 SQLite 数据库（~/.token-watch/token_watch.db），
结合可编辑的模型价格表，按 工具 / 模型 / 日期 折算 USD 成本估算；
同时展示工具上报的成本（OpenClaw 等）作为对照。

- 只监听 127.0.0.1；写操作（仅修改价格表）要求 HttpOnly 会话 Cookie。
- 不写入、不修改 TokenWatch 数据库。

运行：python cost_watch.py [--preferred-port 9640] [--no-browser]
      python cost_watch.py report [--days 30] [--json]
"""
import argparse
import datetime
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import urllib.parse
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
STATIC_DIR = os.path.join(BASE_DIR, "static")
PRICES_PATH = os.path.join(DATA_DIR, "prices.json")
PID_PATH = os.path.join(DATA_DIR, "server.pid")
LOG_PATH = os.path.join(LOGS_DIR, "console.log")
DB_PATH = os.path.join(os.path.expanduser("~"), ".token-watch", "token_watch.db")

APP_VERSION = "1.0.0"
PORT_RANGE = range(9640, 9650)
COOKIE_NAME = "costwatch_session"
MAX_BODY = 1024 * 1024

# 默认价格（USD / 百万 Token）：pattern, input, output, cached, cache_write, 名称
DEFAULT_PRICE_RULES = [
    ("gpt-5.6", 1.25, 10.00, 0.125, 1.25, "GPT-5.6 系列"),
    ("gpt-5.5", 1.25, 10.00, 0.125, 1.25, "GPT-5.5 系列"),
    ("gpt-5.2", 1.25, 10.00, 0.125, 1.25, "GPT-5.2 系列"),
    ("gpt-5", 1.25, 10.00, 0.125, 1.25, "GPT-5 系列"),
    ("o4-mini", 1.10, 4.40, 0.28, 1.10, "o4-mini"),
    ("o3", 2.00, 8.00, 0.50, 2.00, "o3"),
    ("gpt-4.1-mini", 0.40, 1.60, 0.10, 0.40, "GPT-4.1 mini"),
    ("gpt-4.1", 2.00, 8.00, 0.50, 2.00, "GPT-4.1"),
    ("gpt-4o-mini", 0.15, 0.60, 0.075, 0.15, "GPT-4o mini"),
    ("gpt-4o", 2.50, 10.00, 1.25, 2.50, "GPT-4o"),
    ("claude-opus", 15.00, 75.00, 1.50, 15.00, "Claude Opus"),
    ("claude-sonnet", 3.00, 15.00, 0.30, 3.00, "Claude Sonnet"),
    ("claude-haiku", 0.80, 4.00, 0.08, 0.80, "Claude Haiku"),
    ("claude", 3.00, 15.00, 0.30, 3.00, "Claude"),
    ("gemini-2.5-pro", 1.25, 10.00, 0.31, 1.25, "Gemini 2.5 Pro"),
    ("gemini", 0.30, 2.50, 0.075, 0.30, "Gemini Flash"),
    ("deepseek-reasoner", 0.55, 2.19, 0.14, 0.55, "DeepSeek R1"),
    ("deepseek-chat", 0.27, 1.10, 0.07, 0.27, "DeepSeek V3"),
    ("deepseek", 0.27, 1.10, 0.07, 0.27, "DeepSeek"),
    ("kimi-k2", 0.60, 2.50, 0.15, 0.60, "Kimi K2"),
    ("kimi", 2.00, 8.00, 0.50, 2.00, "Kimi"),
    ("moonshot", 2.00, 8.00, 0.50, 2.00, "Moonshot"),
    ("glm", 0.50, 0.50, 0.05, 0.50, "GLM"),
    ("qwen", 0.20, 0.80, 0.02, 0.20, "Qwen"),
    ("codestral", 0.30, 0.90, 0.03, 0.30, "Codestral"),
    ("openclaw", 0.50, 2.00, 0.10, 0.50, "OpenClaw 默认"),
]
DEFAULT_FALLBACK = {"input": 0.50, "output": 2.00, "cached": 0.10, "cache_write": 0.50}

LOG = logging.getLogger("costwatch")


def setup_logging():
    os.makedirs(LOGS_DIR, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


# ---------------------------------------------------------------- 价格表

def default_prices():
    rules = [{"pattern": p, "input": i, "output": o, "cached": c,
              "cache_write": cw, "name": n}
             for p, i, o, c, cw, n in DEFAULT_PRICE_RULES]
    return {"rules": rules, "fallback": dict(DEFAULT_FALLBACK)}


def load_prices():
    prices = default_prices()
    try:
        with open(PRICES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if isinstance(data.get("rules"), list):
                clean = []
                for r in data["rules"]:
                    if isinstance(r, dict) and str(r.get("pattern", "")).strip():
                        clean.append({
                            "pattern": str(r["pattern"]).strip().lower(),
                            "input": _to_float(r.get("input", 0)),
                            "output": _to_float(r.get("output", 0)),
                            "cached": _to_float(r.get("cached", 0)),
                            "cache_write": _to_float(r.get("cache_write", 0)),
                            "name": str(r.get("name") or r["pattern"]),
                        })
                clean = [r for r in clean if not any(r[k] is None for k in _PRICE_KEYS)]
                clean.sort(key=lambda r: len(r["pattern"]), reverse=True)
                prices["rules"] = clean
            if isinstance(data.get("fallback"), dict):
                for k in ("input", "output", "cached", "cache_write"):
                    try:
                        prices["fallback"][k] = float(data["fallback"].get(k) or 0)
                    except (TypeError, ValueError):
                        pass
    except (OSError, ValueError):
        pass
    return prices


def save_prices(prices):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PRICES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prices, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PRICES_PATH)


def _to_float(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return None

_PRICE_KEYS = tuple('input output cached cache_write'.split())

def match_price(model, prices):
    lower = (model or "").lower()
    for rule in prices["rules"]:
        if rule["pattern"] in lower:
            return rule
    return {"pattern": "", "name": "默认价格（未匹配）", **prices["fallback"]}


def compute_cost(input_t, output_t, cached_t, cache_write_t, reasoning_t, price):
    """按百万 Token 单价折算 USD。reasoning 按输出价计。"""
    usd = (
        input_t * float(price["input"])
        + output_t * float(price["output"])
        + cached_t * float(price["cached"])
        + cache_write_t * float(price["cache_write"])
        + reasoning_t * float(price["output"])
    ) / 1_000_000.0
    return usd


# ---------------------------------------------------------------- 数据读取

def open_db():
    if not os.path.isfile(DB_PATH):
        raise RuntimeError("未找到 TokenWatch 数据库: %s" % DB_PATH)
    return sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(DB_PATH.replace("\\", "/")),
                           uri=True, timeout=10)


def load_stats(days=30):
    """读取聚合数据并计算成本估算。"""
    prices = load_prices()
    conn = open_db()
    try:
        where = ""
        args = []
        if days:
            start = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
            where = "WHERE day >= ?"
            args = [start]
        cur = conn.execute(
            "SELECT COALESCE(SUM(input),0), COALESCE(SUM(output),0), COALESCE(SUM(cached),0), "
            "COALESCE(SUM(cache_write),0), COALESCE(SUM(reasoning),0), COALESCE(SUM(total),0), "
            "COALESCE(SUM(cost),0), COUNT(*) FROM records " + where, args)
        s_input, s_output, s_cached, s_cw, s_reasoning, s_total, s_cost, s_records = cur.fetchone()
        summary = {
            "input": s_input, "output": s_output, "cached": s_cached,
            "cache_write": s_cw, "reasoning": s_reasoning, "total": s_total,
            "reported_cost": s_cost or 0, "records": s_records,
            "estimated_usd": 0.0, "days": days or 0,
        }

        def new_group(key, price):
            return {"key": key, "input": 0, "output": 0, "cached": 0, "cache_write": 0,
                    "reasoning": 0, "total": 0, "reported_cost": 0, "records": 0,
                    "estimated_usd": 0.0, "price_name": price["name"], "price": price}

        by_tool_map, by_model_map, day_map, model_map = {}, {}, {}, {}
        total_est = 0.0
        for tool, model, i, o, c, cw, r, t, cost, cnt in conn.execute(
                "SELECT tool, model, SUM(input), SUM(output), SUM(cached), SUM(cache_write), "
                "SUM(reasoning), SUM(total), SUM(cost), COUNT(*) FROM records %s "
                "GROUP BY tool, model" % where, args):
            price = match_price(model or "unknown", prices)
            usd = compute_cost(i, o, c, cw, r, price)
            total_est += usd
            tool_key = tool or "(未知)"
            model_key = model or "(未知)"
            for bucket, key in ((by_tool_map, tool_key), (by_model_map, model_key)):
                row = bucket.get(key)
                if row is None:
                    row = new_group(key, price)
                    bucket[key] = row
                row["input"] += i; row["output"] += o; row["cached"] += c
                row["cache_write"] += cw; row["reasoning"] += r; row["total"] += t
                row["reported_cost"] += cost or 0; row["records"] += cnt
                row["estimated_usd"] += usd
            mrow = model_map.get(model_key)
            if mrow is None:
                mrow = {"model": model_key, "records": 0, "total": 0, "price_name": price["name"]}
                model_map[model_key] = mrow
            mrow["records"] += cnt
            mrow["total"] += t

        for day, model, i, o, c, cw, r, t, cost in conn.execute(
                "SELECT day, model, SUM(input), SUM(output), SUM(cached), SUM(cache_write), "
                "SUM(reasoning), SUM(total), SUM(cost) FROM records %s "
                "GROUP BY day, model" % where, args):
            price = match_price(model or "unknown", prices)
            row = day_map.get(day)
            if row is None:
                row = {"day": day, "input": 0, "output": 0, "cached": 0, "cache_write": 0,
                       "reasoning": 0, "total": 0, "reported_cost": 0, "estimated_usd": 0.0}
                day_map[day] = row
            row["input"] += i; row["output"] += o; row["cached"] += c
            row["cache_write"] += cw; row["reasoning"] += r; row["total"] += t
            row["reported_cost"] += cost or 0
            row["estimated_usd"] += compute_cost(i, o, c, cw, r, price)

        by_tool = sorted(by_tool_map.values(), key=lambda x: -x["estimated_usd"])
        by_model = sorted(by_model_map.values(), key=lambda x: -x["estimated_usd"])
        daily = sorted(day_map.values(), key=lambda x: x["day"], reverse=True)
        for row in [*by_tool, *by_model, *daily]:
            row["estimated_usd"] = round(row["estimated_usd"], 4)
        models = sorted(model_map.values(), key=lambda x: -x["total"])
        summary["estimated_usd"] = round(total_est, 4)
        return {"ok": True, "summary": summary, "by_tool": by_tool,
                "by_model": by_model, "daily": daily, "models": models,
                "prices": prices, "db": DB_PATH}
    finally:
        conn.close()


# ---------------------------------------------------------------- HTTP

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class CostWatchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler_cls, control_token):
        super().__init__(addr, handler_cls)
        self.control_token = control_token


class Handler(BaseHTTPRequestHandler):
    server_version = "CostWatch/" + APP_VERSION

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
            self._deny(403, "访问被拒绝，请从 CostWatch 页面重试")
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
                                   "db_exists": os.path.isfile(DB_PATH), "db": DB_PATH})
        if path == "/api/stats":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                days = int(qs.get("days", ["30"])[0])
            except ValueError:
                days = 30
            try:
                return self.send_json(load_stats(days))
            except RuntimeError as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 404)
            except sqlite3.Error as exc:
                return self.send_json({"ok": False, "error": "读取数据库失败: %s" % exc}, 500)
        if path == "/api/prices":
            return self.send_json({"ok": True, "prices": load_prices(),
                                   "path": PRICES_PATH})
        self._send(b"404 Not Found", 404, set_cookie=False)

    def do_POST(self):
        if not self.authorize(mutating=True):
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/api/prices":
            body = self.read_json_body()
            if not body or "prices" not in body:
                return self.send_json({"ok": False, "error": "请求体无效"}, 400)
            try:
                prices = body["prices"]
                rules = []
                for r in prices.get("rules", []):
                    rules.append({
                        "pattern": str(r["pattern"]).strip().lower(),
                        "input": float(r.get("input", 0) or 0),
                        "output": float(r.get("output", 0) or 0),
                        "cached": float(r.get("cached", 0) or 0),
                        "cache_write": float(r.get("cache_write", 0) or 0),
                        "name": str(r.get("name") or r["pattern"]),
                    })
                rules.sort(key=lambda r: len(r["pattern"]), reverse=True)
                fb = dict(DEFAULT_FALLBACK)
                for k in fb:
                    try:
                        fb[k] = float(prices.get("fallback", {}).get(k) or 0)
                    except (TypeError, ValueError):
                        pass
                save_prices({"rules": rules, "fallback": fb})
                return self.send_json({"ok": True, "prices": load_prices()})
            except (KeyError, TypeError, ValueError) as exc:
                return self.send_json({"ok": False, "error": "价格表格式错误: %s" % exc}, 400)
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
    raise RuntimeError("9640-9649 端口均被占用")


def write_pid():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PID_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def fmt_usd(v):
    return "$%.4f" % (v or 0)


def cli_report(args):
    try:
        data = load_stats(args.days)
    except RuntimeError as exc:
        print("错误: %s" % exc)
        sys.exit(1)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    s = data["summary"]
    print("=" * 62)
    print("CostWatch 成本估算 · 近 %d 天" % (args.days or "全部"))
    print("=" * 62)
    print("记录 %d 条 | Token 合计 %s | 估算成本 %s" % (
        s["records"], f"{s['total']:,}", fmt_usd(s["estimated_usd"])))
    if s["reported_cost"]:
        print("工具上报成本合计: %s" % fmt_usd(s["reported_cost"]))
    print("-" * 62)
    print("按工具:")
    for row in sorted(data["by_tool"], key=lambda r: -r["estimated_usd"]):
        print("  %-12s %8s  %s Token" % (row["key"], fmt_usd(row["estimated_usd"]), f"{row['total']:,}"))
    print("按模型 Top 10:")
    for row in sorted(data["by_model"], key=lambda r: -r["estimated_usd"])[:10]:
        print("  %-32s %8s  (%s)" % (row["key"][:32], fmt_usd(row["estimated_usd"]), row["price_name"]))
    unknown = [m for m in data["models"] if m["price_name"].startswith("默认")]
    if unknown:
        print("未匹配价格表的模型: %s" % "、".join(m["model"] for m in unknown[:10]))
        print("可在看板「价格表」中补充规则，或编辑 data\\prices.json")


def main():
    parser = argparse.ArgumentParser(description="CostWatch 成本核算")
    sub = parser.add_subparsers(dest="command")
    rep = sub.add_parser("report", help="终端输出成本报表")
    rep.add_argument("--days", type=int, default=30, help="统计最近 N 天（0 表示全部）")
    rep.add_argument("--json", action="store_true")
    parser.add_argument("--preferred-port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.command == "report":
        return cli_report(args)

    setup_logging()
    write_pid()
    port = find_port(args.preferred_port)
    control_token = secrets.token_urlsafe(32)
    server = CostWatchServer(("127.0.0.1", port), Handler, control_token)
    LOG.info("CostWatch 启动: http://127.0.0.1:%d/", port)
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
