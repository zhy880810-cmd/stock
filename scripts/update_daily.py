#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日更新脚本：拉行情 + 汇率 + 让 AI 联网生成中文「每日雷达 / 交易计划」，写入 data.json。

- 不含任何持仓数量/成本/金额（隐私默认）。公开文件只写代码、价格、买点缺口和新闻。
- 买点表、定投收盘价、距买点跌幅、标普 500 涨跌幅均由 yfinance 等行情接口计算，不由模型编造。
- AI（Grok / Gemini）只负责：隔夜综述、自选消息、涨跌榜原因、1–3 条大新闻（各附一个链接）、可选换股提议。
- AI 失败不影响行情：保留价格，并在 data.json 里写入错误说明。
- 仅供学习与记录参考，不构成投资建议。

环境变量：
  AI_PROVIDER      gemini（默认）或 grok
  GEMINI_API_KEY   / GEMINI_MODEL（默认 gemini-3.8-flash）
  XAI_API_KEY      / XAI_MODEL（默认 grok-4.7）/ XAI_BASE_URL（默认 https://api.x.ai/v1）
  SERVERCHAN_KEY   可选，Server酱推送（不含金额）
  PAGES_URL        可选，推送里附带的网页地址
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

BJ = timezone(timedelta(hours=8))
ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISCLAIMER = "仅供学习与记录参考，不构成投资建议。"
PLAN_DISCLAIMER = "三倍杠杆 ETF 只适合短线。以上不构成投资建议。"

# 交易计划（写死在脚本里，不依赖用户改 config.json；也不写入任何持仓数量）
NVDA_EARNINGS = "2026-11-18"          # 美股交易日；该日收盘/盘后之后才启用买点
ADBE_EARNINGS_BJ = "2026-12-10"       # 北京时间上午
BRK_DECIDE_BY = "2026-12-15"
DCA_DEADLINE = "2026-12-31"
LEVERED = ("TQQQ", "SOXL", "KORU")

PLAN_SPECS = [
    # symbol 为页面展示代码；yf 为 yfinance 代码
    {"symbol": "NVDA", "yf": "NVDA", "name": "英伟达", "levels": [150, 140, 125, 110], "kind": "nvda"},
    {"symbol": "ADBE", "yf": "ADBE", "name": "Adobe", "levels": [225, 205, 190], "kind": "stock"},
    {"symbol": "BRK.B", "yf": "BRK-B", "name": "伯克希尔B", "levels": [560, 530], "kind": "brk"},
    {"symbol": "TQQQ", "yf": "TQQQ", "name": "纳指3倍做多", "levels": [52, 50], "kind": "levered"},
    {"symbol": "SOXL", "yf": "SOXL", "name": "半导体3倍做多", "levels": [47, 41], "kind": "levered"},
    {"symbol": "KORU", "yf": "KORU", "name": "韩国3倍做多", "levels": [7, 6.5], "kind": "levered"},
]
DCA_SPECS = [
    {"symbol": "QQQM", "yf": "QQQM", "name": "纳指100 ETF"},
    {"symbol": "SCHD", "yf": "SCHD", "name": "美国红利 ETF"},
]
SP500_CSV_URLS = [
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv",
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
]


def log(*a):
    print(*a, flush=True)


def warn(msg):
    # GitHub Actions 里会显示成黄色警告
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"[警告] {msg}", flush=True)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def retry(fn, tries=3, delay=3, what="操作"):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  {what}失败（第 {i + 1}/{tries} 次）：{e}")
            if i < tries - 1:
                time.sleep(delay * (i + 1))
    raise last


def _r(v, n=4):
    try:
        return round(float(v), n) if v is not None and v == v else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ 行情
def guess_currency(sym):
    s = sym.upper()
    if s.endswith(".HK"):
        return "HKD"
    if s.endswith(".SS") or s.endswith(".SZ"):
        return "CNY"
    return "USD"


def _to_et(ts):
    try:
        if getattr(ts, "tzinfo", None) is None:
            return ts.replace(tzinfo=timezone.utc).astimezone(ET)
        return ts.astimezone(ET)
    except Exception:  # noqa: BLE001
        return None


def _is_extended_bar(ts):
    et = _to_et(ts)
    if et is None:
        return False
    minutes = et.hour * 60 + et.minute
    # 美股常规交易 9:30–16:00 ET；之外视为盘前/盘后
    return minutes < 9 * 60 + 30 or minutes >= 16 * 60


def _attach_extended(t, rec):
    """若能拿到盘后/盘前价，写入 extended_price，并把 display 价改成该价格。"""
    ext = None
    # 1) 带盘前盘后的分钟线，取最新一根（若已离开常规时段）
    try:
        h = t.history(period="1d", interval="5m", prepost=True, auto_adjust=False)
        if h is not None and len(h) and "Close" in h:
            h = h.dropna(subset=["Close"])
        if h is not None and len(h):
            last_ts, last_px = h.index[-1], float(h["Close"].iloc[-1])
            if last_px > 0 and _is_extended_bar(last_ts):
                ext = last_px
    except Exception:  # noqa: BLE001
        pass
    # 2) 回退：ticker.info 的盘后/盘前字段（较慢，仅在上面失败时尝试）
    if ext is None:
        try:
            info = t.info or {}
            for k in ("postMarketPrice", "preMarketPrice"):
                v = info.get(k)
                if v and float(v) > 0:
                    ext = float(v)
                    break
        except Exception:  # noqa: BLE001
            pass
    if ext is None:
        return rec
    close = rec.get("close") or rec.get("price")
    # 与收盘几乎相同则不必标盘后
    if close and abs(ext - close) < 0.0005:
        return rec
    rec = dict(rec)
    rec["extended_price"] = round(ext, 4)
    rec["price"] = round(ext, 4)
    rec["price_label"] = "盘后"
    prev = rec.get("prev_close")
    if prev:
        rec["change_pct"] = round((ext / prev - 1) * 100, 2)
    return rec


def _attach_range(t, rec):
    """附带近一年 / 近三个月高低，供模型判断支撑阻力，不直接当作止盈止损。"""
    try:
        h = t.history(period="1y", auto_adjust=False)
        if h is None or h.empty:
            return rec
        need = [c for c in ("High", "Low") if c in h.columns]
        if len(need) < 2:
            return rec
        h = h.dropna(subset=need)
        if not len(h):
            return rec
        rec = dict(rec)
        rec["high_52w"] = round(float(h["High"].max()), 4)
        rec["low_52w"] = round(float(h["Low"].min()), 4)
        last = h.tail(63)
        rec["high_3m"] = round(float(last["High"].max()), 4)
        rec["low_3m"] = round(float(last["Low"].min()), 4)
    except Exception:  # noqa: BLE001
        return rec
    return rec


def fetch_one(sym, want_extended=False):
    import yfinance as yf

    def go():
        t = yf.Ticker(sym)
        h = t.history(period="15d", auto_adjust=False)
        h = h.dropna(subset=["Close"])
        if len(h) < 1:
            raise RuntimeError("没有返回数据（代码可能写错，或接口暂时不可用）")
        last = float(h["Close"].iloc[-1])
        prev = float(h["Close"].iloc[-2]) if len(h) >= 2 else None
        date = h.index[-1].strftime("%Y-%m-%d")
        cur = guess_currency(sym)
        try:
            c = t.fast_info.get("currency") if hasattr(t.fast_info, "get") else t.fast_info["currency"]
            if c:
                cur = str(c).upper()
        except Exception:  # noqa: BLE001
            pass
        pct = (last / prev - 1) * 100 if prev else None
        rec = {
            "price": round(last, 4),
            "close": round(last, 4),
            "prev_close": round(prev, 4) if prev else None,
            "change_pct": round(pct, 2) if pct is not None else None,
            "date": date,
            "currency": cur,
            "price_label": "收盘",
        }
        if want_extended:
            rec = _attach_extended(t, rec)
            rec = _attach_range(t, rec)
        return rec

    return retry(go, tries=3, delay=3, what=f"获取 {sym}")


def fetch_prices(symbols, prev_prices, want_extended=False):
    prices, errors = {}, {}
    for sym in symbols:
        try:
            prices[sym] = fetch_one(sym, want_extended=want_extended)
            tag = prices[sym].get("price_label") or "收盘"
            log(f"  {sym}: {prices[sym]['price']} ({prices[sym]['change_pct']}%) {prices[sym]['date']} {tag}")
        except Exception as e:  # noqa: BLE001
            errors[sym] = str(e)[:200]
            warn(f"{sym} 行情获取失败：{e}")
            old = (prev_prices or {}).get(sym)
            if old and old.get("price"):  # 保留上次的价格，标记为过期
                prices[sym] = dict(old, stale=True)
    return prices, errors


def fetch_fx():
    def go():
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=15)
        r.raise_for_status()
        d = r.json()
        rates = d.get("rates") or {}
        if not rates.get("CNY"):
            raise RuntimeError("汇率接口没有返回人民币汇率")
        return {"USDCNY": round(float(rates["CNY"]), 4),
                "USDHKD": round(float(rates["HKD"]), 4) if rates.get("HKD") else None,
                "source": "open.er-api.com"}

    try:
        return retry(go, tries=3, delay=2, what="获取汇率")
    except Exception as e:  # noqa: BLE001
        warn(f"汇率获取失败：{e}")
        return {}


# ------------------------------------------------------------------ 标普 500 涨跌榜
def _yf_ticker(sym):
    s = str(sym or "").strip().upper()
    return s.replace(".", "-") if s else s


def fetch_sp500_constituents(prev_tickers):
    """成分股来源：datasets/s-and-p-500-companies 的公开 CSV；失败则沿用上次成功列表。"""
    last_err = None
    for url in SP500_CSV_URLS:
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "invest-daily-radar/1.0"})
            r.raise_for_status()
            rows = list(csv.DictReader(io.StringIO(r.text)))
            out = []
            seen = set()
            for row in rows:
                sym = (row.get("Symbol") or row.get("symbol") or row.get("Ticker") or "").strip()
                name = (row.get("Name") or row.get("Security") or row.get("name") or "").strip()
                if not sym:
                    continue
                key = sym.upper()
                if key in seen:
                    continue
                seen.add(key)
                out.append({"symbol": key, "name": name, "yf": _yf_ticker(key)})
            if len(out) >= 400:
                log(f"  标普500成分股 {len(out)} 只（来源 {url}）")
                return out, None
            last_err = f"成分股数量异常：{len(out)}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:200]
            warn(f"拉取标普500成分股失败（{url}）：{e}")
    if prev_tickers and len(prev_tickers) >= 400:
        warn("改用上次成功的成分股列表")
        return prev_tickers, None
    return [], last_err or "未能获取标普500成分股"


def fetch_sp500_movers(prev_plan):
    """上一完整交易日标普 500 涨幅、跌幅各前 3 名。失败时写 error，绝不编造名单或涨跌幅。"""
    prev = (prev_plan or {}).get("sp500_movers") or {}
    prev_tickers = prev.get("constituents") or []
    cons, err = fetch_sp500_constituents(prev_tickers)
    if err and not cons:
        return {"session_date": None, "gainers": [], "losers": [], "error": err, "constituents": prev_tickers or []}

    import yfinance as yf

    yf_list = [c["yf"] for c in cons]
    name_of = {c["yf"]: c.get("name") or c["symbol"] for c in cons}
    disp_of = {c["yf"]: c["symbol"] for c in cons}
    frames = None
    last_err = None
    # 分批下载，降低一次请求过大导致的失败概率
    try:
        def go():
            parts = []
            for i in range(0, len(yf_list), 80):
                batch = yf_list[i:i + 80]
                d = yf.download(
                    batch, period="10d", interval="1d", group_by="ticker",
                    auto_adjust=False, threads=True, progress=False, timeout=60,
                )
                if d is None or d.empty:
                    raise RuntimeError(f"批次 {i // 80 + 1} 没有返回数据")
                parts.append((batch, d))
                time.sleep(0.4)
            return parts
        frames = retry(go, tries=2, delay=8, what="下载标普500日线")
    except Exception as e:  # noqa: BLE001
        last_err = str(e)[:200]
        warn(f"标普500日线下载失败：{e}")
        return {
            "session_date": None, "gainers": [], "losers": [],
            "error": f"未能下载标普500行情：{last_err}",
            "constituents": cons,
        }

    rows = []
    session = None
    for batch, d in frames:
        # yfinance：多标的时 columns 是 MultiIndex (ticker, field)；单标的时可能是单层
        for yf_sym in batch:
            try:
                if hasattr(d.columns, "levels") and yf_sym in d.columns.get_level_values(0):
                    close = d[yf_sym]["Close"].dropna()
                elif "Close" in d.columns and len(batch) == 1:
                    close = d["Close"].dropna()
                else:
                    continue
                if len(close) < 2:
                    continue
                last, prev_c = float(close.iloc[-1]), float(close.iloc[-2])
                if not (last > 0 and prev_c > 0):
                    continue
                date = close.index[-1].strftime("%Y-%m-%d")
                session = session or date
                if date != session:
                    # 个别停牌/补齐日期不一致时，仍用各自最后两个收盘
                    pass
                pct = (last / prev_c - 1) * 100
                rows.append({
                    "ticker": disp_of.get(yf_sym, yf_sym.replace("-", ".")),
                    "name": name_of.get(yf_sym, ""),
                    "price": round(last, 4),
                    "change_pct": round(pct, 2),
                    "date": date,
                })
            except Exception:  # noqa: BLE001
                continue

    if len(rows) < 100:
        return {
            "session_date": session,
            "gainers": [], "losers": [],
            "error": f"有效行情不足（{len(rows)} 只），为避免名单失真，今日不发布涨跌榜",
            "constituents": cons,
        }

    # 只用与最多日期相同的那一批，保证“上一交易日”
    dates = {}
    for r in rows:
        dates[r["date"]] = dates.get(r["date"], 0) + 1
    session = max(dates, key=dates.get)
    day_rows = [r for r in rows if r["date"] == session]
    day_rows.sort(key=lambda x: x["change_pct"], reverse=True)
    gainers = day_rows[:3]
    losers = list(reversed(day_rows[-3:]))
    log(f"  标普500 交易日 {session}：有效 {len(day_rows)} 只；涨 {', '.join(g['ticker']+' '+str(g['change_pct'])+'%' for g in gainers)}；跌 {', '.join(l['ticker']+' '+str(l['change_pct'])+'%' for l in losers)}")
    return {
        "session_date": session,
        "gainers": gainers,
        "losers": losers,
        "error": None,
        "constituents": cons,  # 供下次失败时回退；体积较大，写入前会去掉
    }


# ------------------------------------------------------------------ 买点表
def nvda_watch_only(session_date):
    """财报日（含）开盘前只看不买：已有 2026-11-18 收盘/盘后的那次简报才启用买点。"""
    return (not session_date) or session_date < NVDA_EARNINGS


def build_buy_row(spec, rec, err, prev_row, session_hint, beijing_date):
    price = (rec or {}).get("price")
    close = (rec or {}).get("close") or price
    prev_px = None
    if prev_row and prev_row.get("price"):
        try:
            prev_px = float(prev_row["price"])
        except (TypeError, ValueError):
            prev_px = None
    levels = list(spec["levels"])
    watch_only = spec["kind"] == "nvda" and nvda_watch_only((rec or {}).get("date") or session_hint)
    nearest = gap_price = gap_pct = None
    touched_now, newly = [], []
    if price and levels:
        # 距各买点的剩余跌幅（负值=已跌破该买点）
        scored = []
        for lv in levels:
            if not lv:
                continue
            gp = price - lv
            gpct = (price / lv - 1) * 100
            scored.append((lv, gp, gpct))
            if price <= lv + 1e-9:
                touched_now.append(lv)
                was = False
                if prev_px is not None:
                    was = prev_px <= lv + 1e-9
                elif prev_row and lv in (prev_row.get("touched_now") or []):
                    was = True
                if prev_px is not None or (prev_row and prev_row.get("price")):
                    if not was:
                        newly.append(lv)
        if scored:
            # 最近买点：尚未跌到的最高买点；若已全部跌破，则用最低买点
            above = [x for x in scored if x[2] > 0]
            if above:
                nearest, gap_price, gap_pct = min(above, key=lambda x: x[2])
            else:
                nearest, gap_price, gap_pct = min(scored, key=lambda x: x[0])
    notes = []
    if spec["kind"] == "nvda" and watch_only:
        notes.append(f"在 {NVDA_EARNINGS} 财报前只看不买；财报后再按 150 / 140 / 125 / 110 提醒。")
    if spec["kind"] == "brk" and beijing_date >= BRK_DECIDE_BY:
        notes.append(f"到 {BRK_DECIDE_BY} 仍未标记买入的话，请决定要不要直接买（买入后可在页面标记，本表将隐藏）。")
    if spec["symbol"] == "ADBE" and beijing_date <= ADBE_EARNINGS_BJ:
        notes.append(f"财报约北京时间 {ADBE_EARNINGS_BJ} 早上。")
    at_buy = (not watch_only) and bool(touched_now)
    sort_gap = None if watch_only or gap_pct is None else gap_pct
    return {
        "symbol": spec["symbol"],
        "yf": spec["yf"],
        "name": spec["name"],
        "kind": spec["kind"],
        "price": _r(price),
        "close": _r(close),
        "extended_price": _r((rec or {}).get("extended_price")),
        "price_label": (rec or {}).get("price_label") or ("盘后" if (rec or {}).get("extended_price") else "收盘"),
        "prev_close": _r((rec or {}).get("prev_close")),
        "change_pct": _r((rec or {}).get("change_pct"), 2),
        "date": (rec or {}).get("date"),
        "currency": (rec or {}).get("currency") or "USD",
        "stale": bool((rec or {}).get("stale")),
        "error": err,
        "levels": levels,
        "watch_only": watch_only,
        "nearest_level": _r(nearest, 4),
        "gap_price": _r(gap_price, 4),
        "gap_pct": _r(gap_pct, 2),
        "touched_now": touched_now,
        "newly_touched": newly,
        "at_buy": at_buy,
        "sort_gap_pct": _r(sort_gap, 4),
        "high_52w": _r((rec or {}).get("high_52w")),
        "low_52w": _r((rec or {}).get("low_52w")),
        "high_3m": _r((rec or {}).get("high_3m")),
        "low_3m": _r((rec or {}).get("low_3m")),
        "extra_tranches": [],
        "tranche_levels": list(levels),
        "ref_sl": None,
        "ref_tp": None,
        "sl_tp_reason": "",
        "sl_tp_url": "",
        "notes": notes,
    }


def _near(a, b, tol=0.008):
    try:
        return abs(float(a) - float(b)) <= max(0.01, abs(float(b)) * tol)
    except (TypeError, ValueError):
        return False


def _merge_level_advice(row, ai_bits, prev_row):
    """把模型给出的参考止盈止损、补充批次并入买点行；不覆盖用户既定核心梯子。失败时沿用上次参考价。"""
    advice = (ai_bits or {}).get("levels_advice") or {}
    a = advice.get(row["symbol"]) or advice.get((row.get("yf") or "").upper())
    if not a and prev_row:
        a = {
            "stop_loss": prev_row.get("ref_sl"),
            "take_profit": prev_row.get("ref_tp"),
            "extra_tranches": prev_row.get("extra_tranches") or [],
            "reason": prev_row.get("sl_tp_reason") or "",
            "url": prev_row.get("sl_tp_url") or "",
            "stale": True,
        }
    if not a:
        return row
    row = dict(row)
    sl, tp = a.get("stop_loss"), a.get("take_profit")
    if sl and sl > 0:
        row["ref_sl"] = _r(sl, 4)
    if tp and tp > 0:
        row["ref_tp"] = _r(tp, 4)
    row["sl_tp_reason"] = (a.get("reason") or "")[:300]
    row["sl_tp_url"] = a.get("url") or ""
    row["sl_tp_stale"] = bool(a.get("stale"))
    core = list(row.get("levels") or [])
    extras = []
    for x in a.get("extra_tranches") or []:
        try:
            x = float(x)
        except (TypeError, ValueError):
            continue
        if x <= 0 or any(_near(x, c) for c in core + extras):
            continue
        extras.append(round(x, 4))
    row["extra_tranches"] = extras
    row["tranche_levels"] = sorted(set(core + extras), reverse=True)
    return row


def sort_buy_table(rows):
    def key(r):
        # 财报前的 NVDA 置顶，只看不买，不参与买点缺口排序
        if r.get("watch_only"):
            return (-1, 0)
        g = r.get("sort_gap_pct")
        if g is None:
            return (2, 0)
        return (1, g)
    return sorted(rows, key=key)


def build_dca_row(spec, rec, err):
    return {
        "symbol": spec["symbol"],
        "name": spec["name"],
        "price": _r((rec or {}).get("price")),
        "close": _r((rec or {}).get("close") or (rec or {}).get("price")),
        "extended_price": _r((rec or {}).get("extended_price")),
        "price_label": (rec or {}).get("price_label") or "收盘",
        "prev_close": _r((rec or {}).get("prev_close")),
        "change_pct": _r((rec or {}).get("change_pct"), 2),
        "date": (rec or {}).get("date"),
        "currency": (rec or {}).get("currency") or "USD",
        "stale": bool((rec or {}).get("stale")),
        "error": err,
    }


def build_plan(prices, errors, prev_plan, beijing_date, movers, ai_bits, stale_ai=False):
    prev_rows = {r.get("symbol"): r for r in (prev_plan or {}).get("buy_table") or [] if isinstance(r, dict)}
    session = None
    rows = []
    for spec in PLAN_SPECS:
        rec = prices.get(spec["symbol"]) or prices.get(spec["yf"])
        err = errors.get(spec["symbol"]) or errors.get(spec["yf"])
        if rec and rec.get("date"):
            session = session or rec["date"]
        rows.append(build_buy_row(spec, rec, err, prev_rows.get(spec["symbol"]), session, beijing_date))
    rows = [_merge_level_advice(r, ai_bits, prev_rows.get(r["symbol"])) for r in rows]
    rows = sort_buy_table(rows)
    dca = [build_dca_row(s, prices.get(s["symbol"]) or prices.get(s["yf"]),
                         errors.get(s["symbol"]) or errors.get(s["yf"])) for s in DCA_SPECS]
    reached = [r["symbol"] for r in rows if r.get("kind") == "levered" and r.get("at_buy")]
    return {
        "schema": 1,
        "session_date": session or (movers or {}).get("session_date"),
        "buy_table": rows,
        "dca": dca,
        "levered": {
            "symbols": list(LEVERED),
            "rule": "三只杠杆 ETF 同时只拿一只：哪只先到买点就买哪只；买了之后，另外两只只观察、不提示买入。",
            "reached_buy": reached,
        },
        "sp500_movers": {
            "session_date": (movers or {}).get("session_date"),
            "gainers": (movers or {}).get("gainers") or [],
            "losers": (movers or {}).get("losers") or [],
            "error": (movers or {}).get("error"),
        },
        "swap_proposal": (ai_bits or {}).get("swap_proposal"),
        "opportunities": (ai_bits or {}).get("opportunities") or [],
        "levels_advice": (ai_bits or {}).get("levels_advice") or {},
        "big_news": (ai_bits or {}).get("big_news") or [],
        "ai_stale": bool(stale_ai),
        "calendar": {
            "nvda_earnings": NVDA_EARNINGS,
            "adbe_earnings_beijing": ADBE_EARNINGS_BJ,
            "brk_decide_by": BRK_DECIDE_BY,
            "dca_deadline": DCA_DEADLINE,
        },
        "disclaimer": PLAN_DISCLAIMER,
    }


# ------------------------------------------------------------------ AI
SYSTEM = ("你是一名谨慎的中文市场信息整理助手。你必须通过联网搜索核实信息，只报道你在搜索结果里确实看到的内容；"
          "查不到或无法确认时，明确写“未能核实”，绝不编造数字、新闻或来源。你的输出仅供学习参考，不构成投资建议。"
          "你不知道、也不要猜测用户的持仓数量、成本或金额。")

SCHEMA_TEXT = """{
  "market_summary": {
    "headline": "一句话总结隔夜美股（不超过40字）",
    "overview": "隔夜美股三大指数表现及原因，80-200字",
    "macro": ["美联储/利率/通胀/就业等宏观要闻，每条一句话，0-4条，没有则空数组"]
  },
  "watchlist_news": [
    {"symbol": "关注或计划里的代码", "summary": "过去24小时与该标的相关的重要消息，没有则写“暂无重大消息”", "sentiment": "利好|利空|中性"}
  ],
  "sp500_mover_reasons": [
    {"ticker": "必须是上面给出的涨跌榜代码", "reason": "一句已核实的原因；查不到写未能核实"}
  ],
  "big_news": [
    {"summary": "一句重大新闻", "url": "https://来源链接"}
  ],
  "levels_advice": [
    {"symbol": "NVDA", "stop_loss": 0, "take_profit": 0, "extra_tranches": [], "reason": "一句依据（历史支撑/阻力或新闻）", "url": "https://仅新闻驱动时填写，否则空字符串"}
  ],
  "opportunities": [
    {"ticker": "代码", "name": "名称", "vs": "对比的现有代码", "why": "为什么明显更好", "tranches": [0], "stop_loss": 0, "take_profit": 0, "reason": "一句依据", "url": "https://可选"}
  ]
}"""


def build_prompt(cfg, today_bj, prices, indices, fx, plan_rows, dca_rows, movers, beijing_date):
    wl = cfg["watchlist"]
    names = cfg.get("names", {})
    plan_syms = [s["symbol"] for s in PLAN_SPECS] + [s["symbol"] for s in DCA_SPECS]
    # 关注列表 + 计划标的（仅代码，不含持仓）
    seen, combo = set(), []
    for s in list(wl) + plan_syms:
        u = str(s).upper()
        if u in seen:
            continue
        seen.add(u)
        combo.append(u)
    wl_lines = []
    for s in combo:
        p = prices.get(s) or prices.get(s.replace(".", "-"))
        extra = ""
        if p and p.get("change_pct") is not None:
            lab = p.get("price_label") or "收盘"
            extra = f"，最近{lab} {p['price']} {p.get('currency','')}，涨跌 {p['change_pct']}%（{p['date']}）"
        nm = names.get(s) or next((x["name"] for x in PLAN_SPECS + DCA_SPECS if x["symbol"] == s), "")
        wl_lines.append(f"- {s}（{nm}）{extra}")
    idx_lines = [f"- {n}（{k}）：{v['price']}，涨跌 {v['change_pct']}%（{v['date']}）"
                 for k, v in indices.items() for n in [v.get('name', k)] if v.get("change_pct") is not None]
    buy_lines = []
    for r in plan_rows or []:
        if r.get("error") and not r.get("price"):
            buy_lines.append(f"- {r['symbol']}：行情获取失败（{r['error']}）")
            continue
        gap = "只看不买，不提示买入" if r.get("watch_only") else (
            f"距买点 {r.get('nearest_level')} 还差 {r.get('gap_pct')}%（{r.get('gap_price')}）"
            if r.get("nearest_level") is not None else "买点缺口暂缺"
        )
        if r.get("newly_touched"):
            gap += f"；新触及买点 {', '.join(str(x) for x in r['newly_touched'])}"
        elif r.get("touched_now") and not r.get("watch_only"):
            gap += f"；已到买点 {', '.join(str(x) for x in r['touched_now'])}"
        lab = r.get("price_label") or "收盘"
        rng = ""
        if r.get("high_52w") and r.get("low_52w"):
            rng = f"；近一年高/低 {r['high_52w']}/{r['low_52w']}"
            if r.get("high_3m") and r.get("low_3m"):
                rng += f"，近三个月高/低 {r['high_3m']}/{r['low_3m']}"
        buy_lines.append(
            f"- {r['symbol']} {lab} {r.get('price')}，涨跌 {r.get('change_pct')}%（{r.get('date')}）；核心买点梯子 {r.get('levels')}；{gap}{rng}"
        )
    dca_lines = []
    for r in dca_rows or []:
        lab = r.get("price_label") or "收盘"
        dca_lines.append(f"- {r['symbol']} {lab} {r.get('price')}，涨跌 {r.get('change_pct')}%（{r.get('date')}）")
    mover_lines = []
    if movers and not movers.get("error") and (movers.get("gainers") or movers.get("losers")):
        mover_lines.append(f"交易日：{movers.get('session_date') or '未知'}（数字已由行情接口计算，请不要改涨跌幅）")
        mover_lines.append("涨幅前3：")
        for g in movers.get("gainers") or []:
            mover_lines.append(f"- {g['ticker']} {g.get('name','')} {g.get('change_pct')}% 收盘 {g.get('price')}")
        mover_lines.append("跌幅前3：")
        for g in movers.get("losers") or []:
            mover_lines.append(f"- {g['ticker']} {g.get('name','')} {g.get('change_pct')}% 收盘 {g.get('price')}")
    else:
        mover_lines.append("今日未能从行情接口生成标普500涨跌榜" + (f"：{movers.get('error')}" if movers and movers.get("error") else "。") + "请不要编造涨跌榜名单或涨跌幅，让 sp500_mover_reasons 为空数组。")

    fx_line = ""
    if fx and fx.get("USDCNY"):
        fx_line = f"参考汇率：1 美元 ≈ {fx['USDCNY']} 人民币。"

    return f"""今天是北京时间 {today_bj}。请联网搜索，为我生成一份中文「每日盘前计划」的文字部分。

{fx_line}

我的关注 / 交易计划标的（只是代码和公开买点，不涉及持仓数量或金额）：
{chr(10).join(wl_lines)}

已用行情接口算好的买点表（请直接采用数字，不要改价格或缺口）：
{chr(10).join(buy_lines) if buy_lines else '（暂无）'}

定投行情：
{chr(10).join(dca_lines) if dca_lines else '（暂无）'}

已核实的指数收盘数据（来自行情接口，请优先采用）：
{chr(10).join(idx_lines) if idx_lines else '（暂无）'}

标普500涨跌榜：
{chr(10).join(mover_lines)}

要求：
1. market_summary：用一两段话总结最近一个美股交易日（隔夜）的表现及主要原因；macro 只列确实查到的宏观要闻。
2. watchlist_news：尽量覆盖关注列表和计划里的代码（NVDA、ADBE、BRK.B、TQQQ、SOXL、KORU、QQQM、SCHD 等），没有重大消息就写“暂无重大消息”。
3. sp500_mover_reasons：仅为上面涨跌榜里出现的代码各写一句已核实原因，不要增减名单，不要改涨跌幅。查不到就写“未能核实”。
4. big_news：只写真正重大的消息，最多 3 条，可以是 0 条。范围仅限：美联储和利率、英伟达、ADBE 财报（约北京时间 {ADBE_EARNINGS_BJ} 早上）、重要宏观数据，以及会影响长城短债的国内债市大波动。每条必须是一句摘要 + 一个可打开的 http(s) 来源链接。没有大消息就输出空数组，不要凑数、不要编造。
5. levels_advice：请为买点表里的每只都给出参考止损 stop_loss 和参考止盈 take_profit（尤其是 NVDA、TQQQ、SOXL、KORU 等激进品种，ADBE、BRK.B 也要给）。依据历史支撑/阻力（可参考上面给出的近一年、近三个月高低）以及你检索到的最近新闻。必须写一句 reason；若主要受新闻驱动，再给一个可打开的 http(s) url，否则 url 填空字符串。这些只是参考，不是下单指令，不要写成“必须卖出/必须买入”。不要修改、删除或改写用户既定的核心买点梯子。若根据历史结构认为再增加 1–3 个分批买点有帮助，放入 extra_tranches（必须明显不同于核心买点，不要重复、不要把现价写成买点）。NVDA 在财报前仍可给参考止盈止损，但不要暗示现在就买。查不到就让数字留空并在 reason 写“未能核实”，不要编造精确到分的假支撑。
6. opportunities：若你发现明显好于当前观察池 / 云端自选 / 计划标的（上面列表里的代码）的机会，请列出来，每天 0–3 条。填写 ticker、name、vs（对比的现有代码，必须是上面列表中的一个）、why、tranches（建议分批买点，1–4 个数字）、stop_loss、take_profit、reason；新闻驱动时加 url。这是给用户看的观察或替换建议，系统不会自动改自选清单。不要因为普通涨跌就硬凑；没有就输出空数组。
7. 全部用简体中文。不确定就写“未能核实”，不要编造。
8. 正文里不要出现任何网址、Markdown 链接、引用标记或脚注（big_news.url、levels_advice.url、opportunities.url 字段除外）。不要写持仓数量、成本、金额或账户余额（你也不掌握这些信息）。
9. 只输出一个 JSON 对象，不要任何多余文字、不要 Markdown 代码块。JSON 结构如下：
{SCHEMA_TEXT}
"""


def extract_json(text):
    if not text:
        raise ValueError("AI 没有返回内容")
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("AI 返回的内容里找不到 JSON")
    return json.loads(t[i:j + 1])


def _s(v, n=600):
    return (str(v).strip() if v is not None else "")[:n]


def _trim_url(u):
    u = (u or "").strip()
    u = re.sub(r"[.,;:!?）】]+$", "", u)
    while u.endswith(")") and u.count(")") > u.count("("):
        u = u[:-1]
    return u


def _http_url(u):
    u = _trim_url(u)
    if not u or len(u) > 2000 or re.search(r"""[\s<>"'`]""", u):
        return ""
    if re.match(r"^(javascript|data|vbscript|file|about|blob):", u, re.I):
        return ""
    if not re.match(r"^https?://", u, re.I):
        return ""
    return u


def _clean_cites(v):
    """去掉正文里的引用标记，并收集其中的 http(s) 链接。"""
    s = "" if v is None else str(v)
    urls, out = [], []
    i, n = 0, len(s)

    def add(u):
        u = _http_url(u)
        if u:
            urls.append(u)
        return u

    def parse_paren_url(j):
        depth, k = 1, j
        while k < n:
            c = s[k]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return s[j:k].strip(), k + 1
            elif c == "\n":
                break
            k += 1
        return None, j

    while i < n:
        if s.startswith("[[", i):
            m = re.match(r"\[\[(\d+)\]\]", s[i:])
            if m:
                after = i + m.end()
                if after < n and s[after] == "(":
                    url, end = parse_paren_url(after + 1)
                    if url is not None:
                        add(url)
                        i = end
                        continue
                i = after
                continue
        if s[i] == "[":
            m = re.match(r"\[(\d+)\]:\s*(https?://\S+)", s[i:], re.I)
            if m:
                add(m.group(2))
                i += m.end()
                continue
            m = re.match(r"\[\^?(\d+)\](?!\()", s[i:])
            if m:
                i += m.end()
                continue
            m = re.match(r"\[([^\]]+)\]", s[i:])
            if m and i + m.end() < n and s[i + m.end()] == "(":
                url, end = parse_paren_url(i + m.end() + 1)
                if url is not None:
                    url = re.sub(r"""\s+("[^"]*"|'[^']*')\s*$""", "", url)
                    add(url)
                    label = m.group(1).strip()
                    if label and not label.isdigit() and not re.match(r"^https?://", label, re.I):
                        out.append(label)
                    i = end
                    continue
        if s[i] == "<":
            m = re.match(r"<(https?://[^>\s]+)>", s[i:], re.I)
            if m:
                add(m.group(1))
                i += m.end()
                continue
        m = re.match(r"https?://[^\s<>\"'）】，。；、]+", s[i:], re.I)
        if m:
            raw = m.group(0)
            trimmed = _trim_url(raw)
            add(trimmed)
            i += len(trimmed) if trimmed and raw.startswith(trimmed) else len(raw)
            continue
        out.append(s[i])
        i += 1
    text = "".join(out)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([，。；、,.!?？！])", r"\1", text)
    return text.strip(), urls




def _plan_symbol_set():
    return {s["symbol"].upper() for s in PLAN_SPECS} | {s["yf"].upper() for s in PLAN_SPECS}


def validate_radar(obj, cfg, movers=None):
    """校验并整理 AI 输出；不合格时抛出异常。"""
    if not isinstance(obj, dict):
        raise ValueError("JSON 顶层不是对象")
    ms = obj.get("market_summary")
    if not isinstance(ms, dict):
        ms = {}
    if not _s(ms.get("overview")) and not _s(ms.get("headline")):
        # 允许只有新闻/涨跌原因，但至少要有一段市场文字，避免空壳成功
        if not (obj.get("big_news") or obj.get("sp500_mover_reasons")):
            raise ValueError("缺少 market_summary.overview")
    macro = ms.get("macro") or []
    if isinstance(macro, str):
        macro = [macro]
    wl = {s.upper() for s in cfg["watchlist"]} | _plan_symbol_set() | {s["symbol"].upper() for s in DCA_SPECS}
    news = []
    for it in obj.get("watchlist_news") or []:
        if isinstance(it, dict) and _s(it.get("symbol")) and _s(it.get("summary")):
            sent = _s(it.get("sentiment"), 4)
            news.append({"symbol": _s(it["symbol"], 12).upper(), "summary": _s(it["summary"]),
                         "sentiment": sent if sent in ("利好", "利空", "中性") else "中性"})
    # 兼容旧字段 unusual_movers（可选，不再作为计划主体）
    movers_u = []
    for it in obj.get("unusual_movers") or []:
        if not isinstance(it, dict) or not _s(it.get("ticker")):
            continue
        tk = _s(it["ticker"], 12).upper()
        if tk in wl:
            continue
        movers_u.append({"ticker": tk, "name": _s(it.get("name"), 60), "move": _s(it.get("move"), 20) or "未核实",
                         "reason": _s(it.get("reason")), "relevance": _s(it.get("relevance"))})
    hi = cfg.get("radar", {}).get("movers_max", 5)
    extra = []
    hl, u = _clean_cites(ms.get("headline")); extra += u
    ov, u = _clean_cites(ms.get("overview")); extra += u
    macros = []
    for m in macro:
        t1, u = _clean_cites(m)
        if _s(t1):
            macros.append(_s(t1, 300))
            extra += u
    news2 = []
    for it in news:
        t1, u = _clean_cites(it["summary"]); extra += u
        news2.append({**it, "summary": _s(t1)})
    movers2 = []
    for it in movers_u:
        r1, u1 = _clean_cites(it["reason"]); extra += u1
        r2, u2 = _clean_cites(it["relevance"]); extra += u2
        movers2.append({**it, "reason": _s(r1), "relevance": _s(r2)})

    allowed = set()
    if movers:
        for row in (movers.get("gainers") or []) + (movers.get("losers") or []):
            if row.get("ticker"):
                allowed.add(str(row["ticker"]).upper())
                allowed.add(str(row["ticker"]).upper().replace(".", "-"))
                allowed.add(str(row["ticker"]).upper().replace("-", "."))
    reasons = {}
    for it in obj.get("sp500_mover_reasons") or []:
        if not isinstance(it, dict) or not _s(it.get("ticker")):
            continue
        tk = _s(it["ticker"], 16).upper()
        reason, u = _clean_cites(it.get("reason"))
        extra += u
        if allowed and tk not in allowed and tk.replace("-", ".") not in allowed and tk.replace(".", "-") not in allowed:
            continue
        reasons[tk] = _s(reason, 400) or "未能核实"

    big = []
    for it in obj.get("big_news") or []:
        if not isinstance(it, dict):
            continue
        summary, u = _clean_cites(it.get("summary"))
        extra += u
        url = _http_url(it.get("url") or (u[0] if u else ""))
        if not _s(summary) or not url:
            continue
        big.append({"summary": _s(summary, 300), "url": url})
        if len(big) >= 3:
            break

    def _canon_sym(s):
        s = _s(s, 16).upper()
        if s in ("BRK-B", "BRKB"):
            return "BRK.B"
        return s.replace("_", ".")

    def _pos_nums(v, cap=6):
        if v is None or v == "":
            return []
        if not isinstance(v, (list, tuple)):
            v = [v]
        out = []
        for x in v:
            try:
                n = float(x)
            except (TypeError, ValueError):
                continue
            if n > 0:
                out.append(round(n, 4))
            if len(out) >= cap:
                break
        return out

    known = {s.upper() for s in cfg["watchlist"]} | _plan_symbol_set() | {s["symbol"].upper() for s in DCA_SPECS}
    known |= {x.replace("-", ".") for x in known} | {x.replace(".", "-") for x in known}

    advice = {}
    for it in obj.get("levels_advice") or []:
        if not isinstance(it, dict) or not _s(it.get("symbol")):
            continue
        sym = _canon_sym(it.get("symbol"))
        reason, u = _clean_cites(it.get("reason") or it.get("sl_tp_reason"))
        extra += u
        url = _http_url(it.get("url") or (u[0] if u else ""))
        sl = _r(it.get("stop_loss") or it.get("sl"), 4)
        tp = _r(it.get("take_profit") or it.get("tp"), 4)
        if sl is not None and not (sl > 0):
            sl = None
        if tp is not None and not (tp > 0):
            tp = None
        advice[sym] = {
            "stop_loss": sl,
            "take_profit": tp,
            "extra_tranches": _pos_nums(it.get("extra_tranches") or it.get("extra_levels")),
            "reason": _s(reason, 300),
            "url": url,
        }

    opps = []
    seen_tk = set()
    for it in obj.get("opportunities") or []:
        if not isinstance(it, dict):
            continue
        tk = _canon_sym(it.get("ticker") or it.get("with_ticker") or it.get("symbol"))
        if not tk or tk in seen_tk:
            continue
        why, u1 = _clean_cites(it.get("why"))
        reason, u2 = _clean_cites(it.get("reason"))
        extra += u1 + u2
        url = _http_url(it.get("url") or ((u1 + u2)[0] if (u1 + u2) else ""))
        vs = _canon_sym(it.get("vs") or it.get("replace") or it.get("versus"))
        if vs and vs not in known and vs.replace("-", ".") not in known:
            vs = ""
        if not _s(why) and not _s(reason):
            continue
        # 不要把已经在核心计划里的代码再当成“新机会”
        plan_only = _plan_symbol_set()
        if tk in plan_only:
            continue
        sl = _r(it.get("stop_loss") or it.get("sl"), 4)
        tp = _r(it.get("take_profit") or it.get("tp"), 4)
        tranches = _pos_nums(it.get("tranches") or it.get("buy_points") or it.get("levels"))
        if it.get("buy_point") and not tranches:
            tranches = _pos_nums([it.get("buy_point")])
        opps.append({
            "ticker": tk,
            "name": _s(it.get("name") or it.get("with_name"), 60),
            "vs": vs,
            "why": _s(why or reason, 400),
            "tranches": tranches,
            "stop_loss": sl if sl and sl > 0 else None,
            "take_profit": tp if tp and tp > 0 else None,
            "reason": _s(reason, 300),
            "url": url,
        })
        seen_tk.add(tk)
        if len(opps) >= 3:
            break

    # 兼容旧的 swap_proposal：并入 opportunities
    swap = obj.get("swap_proposal")
    swap_out = None
    if isinstance(swap, dict) and _s(swap.get("replace")) and _s(swap.get("with_ticker") or swap.get("with")):
        why, u = _clean_cites(swap.get("why") or swap.get("reason"))
        extra += u
        rep = _canon_sym(swap.get("replace"))
        wit = _canon_sym(swap.get("with_ticker") or swap.get("with"))
        if wit and wit != rep and _s(why) and wit not in seen_tk:
            bp = swap.get("buy_point") or swap.get("buyPoint")
            try:
                bp = float(bp) if bp is not None and str(bp).strip() != "" else None
            except (TypeError, ValueError):
                bp = None
            swap_out = {
                "replace": rep,
                "with_ticker": wit,
                "with_name": _s(swap.get("with_name") or swap.get("name"), 60),
                "why": _s(why, 400),
                "buy_point": _r(bp, 4),
            }
            if len(opps) < 3:
                opps.append({
                    "ticker": wit,
                    "name": swap_out["with_name"],
                    "vs": rep,
                    "why": swap_out["why"],
                    "tranches": [swap_out["buy_point"]] if swap_out["buy_point"] else [],
                    "stop_loss": _r(swap.get("stop_loss"), 4),
                    "take_profit": _r(swap.get("take_profit"), 4),
                    "reason": "",
                    "url": _http_url(swap.get("url")),
                })
                seen_tk.add(wit)

    return {
        "market_summary": {"headline": _s(hl, 80), "overview": _s(ov, 1500), "macro": macros[:6]},
        "watchlist_news": news2,
        "unusual_movers": movers2[:hi],
        "sp500_mover_reasons": reasons,
        "big_news": big,
        "levels_advice": advice,
        "opportunities": opps,
        "swap_proposal": swap_out,
        "_extracted_urls": extra,
    }


def _attach_reasons(movers, reasons):
    if not movers:
        return movers
    out = dict(movers)

    def one(row):
        tk = str(row.get("ticker") or "").upper()
        reason = (
            (reasons or {}).get(tk)
            or (reasons or {}).get(tk.replace(".", "-"))
            or (reasons or {}).get(tk.replace("-", "."))
            or ""
        )
        return {**row, "reason": reason or "未能核实"}

    out["gainers"] = [one(r) for r in (movers.get("gainers") or [])]
    out["losers"] = [one(r) for r in (movers.get("losers") or [])]
    return out


def call_gemini(prompt):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("没有设置 GEMINI_API_KEY")
    from google import genai
    from google.genai import types

    model = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.8-flash"
    client = genai.Client(api_key=key)
    # 注意：使用 google_search 工具时，接口不允许同时指定 JSON 响应格式，所以在提示词里要求 JSON，再自行解析和校验。
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.3,
        ),
    )
    text = resp.text or ""
    sources = []
    try:
        gm = resp.candidates[0].grounding_metadata
        for ch in (gm.grounding_chunks or []):
            if ch.web and ch.web.uri:
                sources.append({"title": ch.web.title or "", "url": ch.web.uri})
    except Exception:  # noqa: BLE001
        pass
    return text, sources, model


def call_grok(prompt):
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("没有设置 XAI_API_KEY")
    model = os.environ.get("XAI_MODEL", "").strip() or "grok-4.7"
    base = (os.environ.get("XAI_BASE_URL", "").strip() or "https://api.x.ai/v1").rstrip("/")
    # xAI Responses API + 内置 web_search 工具（见 docs.x.ai/developers/tools/web-search）
    body = {
        "model": model,
        "input": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        "tools": [{"type": "web_search"}],
    }
    r = requests.post(f"{base}/responses", json=body, timeout=400,
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"xAI 接口返回 {r.status_code}：{r.text[:300]}")
    j = r.json()
    text, sources = "", []
    if isinstance(j.get("output_text"), str):
        text = j["output_text"]
    for item in j.get("output", []) or []:
        if item.get("type") == "message":
            for part in item.get("content", []) or []:
                if part.get("type") in ("output_text", "text"):
                    if not text:
                        text += part.get("text", "")
                    for an in part.get("annotations", []) or []:
                        if an.get("url"):
                            sources.append({"title": an.get("title", ""), "url": an["url"]})
    for c in j.get("citations", []) or []:
        if isinstance(c, str):
            sources.append({"title": "", "url": c})
    return text, sources, model


def run_ai(cfg, today_bj, prices, indices, fx, plan_rows, dca_rows, movers, beijing_date):
    provider = (os.environ.get("AI_PROVIDER", "") or "gemini").strip().lower()
    if provider in ("xai", "grok"):
        provider, fn = "grok", call_grok
    elif provider == "gemini":
        fn = call_gemini
    else:
        return None, {"provider": provider, "model": None, "ok": False,
                      "error": f"不认识的 AI_PROVIDER：{provider}（只能填 gemini 或 grok）"}
    prompt = build_prompt(cfg, today_bj, prices, indices, fx, plan_rows, dca_rows, movers, beijing_date)
    model = (os.environ.get("XAI_MODEL", "").strip() or "grok-4.7") if provider == "grok" else (os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.8-flash")
    last = None
    for i in range(2):  # 最多尝试 2 次（含 JSON 格式不合格时重试）
        try:
            text, sources, model = fn(prompt)
            radar = validate_radar(extract_json(text), cfg, movers)
            extra = radar.pop("_extracted_urls", []) or []
            seen, src = set(), []
            # 正文里抽到的引用优先，再补接口返回的来源；大新闻链接一并纳入
            news_urls = [{"title": "", "url": n["url"]} for n in (radar.get("big_news") or []) if n.get("url")]
            for a in (radar.get("levels_advice") or {}).values():
                if isinstance(a, dict) and a.get("url"):
                    news_urls.append({"title": "", "url": a["url"]})
            for o in radar.get("opportunities") or []:
                if isinstance(o, dict) and o.get("url"):
                    news_urls.append({"title": "", "url": o["url"]})
            for s in (news_urls + [{"title": "", "url": u} for u in extra] + list(sources or [])):
                u = _http_url((s or {}).get("url") if isinstance(s, dict) else s)
                if not u or u in seen:
                    continue
                seen.add(u)
                title = (s.get("title") or "") if isinstance(s, dict) else ""
                src.append({"title": title, "url": u})
            radar["sources"] = src[:16]
            return radar, {"provider": provider, "model": model, "ok": True, "error": None}
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  AI 调用/解析失败（第 {i + 1}/2 次）：{e}")
            if "没有设置" in str(e):
                break
            time.sleep(5)
    return None, {"provider": provider, "model": model, "ok": False, "error": str(last)[:300]}


# ------------------------------------------------------------------ Server酱
def push_serverchan(data, radar_ok):
    key = os.environ.get("SERVERCHAN_KEY", "").strip()
    if not key:
        log("未设置 SERVERCHAN_KEY，跳过微信推送。")
        return
    lines = []
    for s in data["watchlist"]:
        p = data["prices"].get(s)
        if p and p.get("change_pct") is not None:
            lines.append(f"- {s} {p['change_pct']:+.2f}%")
    plan = data.get("plan") or {}
    newly = []
    for r in plan.get("buy_table") or []:
        if r.get("newly_touched"):
            newly.append(f"{r['symbol']} " + "、".join(str(x) for x in r["newly_touched"]))
    if newly:
        lines.append("\n新触及买点：" + "；".join(newly))
    radar = data.get("radar")
    if radar and radar_ok:
        if radar.get("market_summary") and radar["market_summary"].get("headline"):
            lines.insert(0, radar["market_summary"]["headline"] + "\n")
        news = plan.get("big_news") or []
        if news:
            lines.append("\n大新闻：" + "；".join(n.get("summary", "") for n in news[:3] if n.get("summary")))
    elif not radar_ok:
        lines.append("\n（今日 AI 雷达生成失败，只更新了行情）")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    url = os.environ.get("PAGES_URL", "").strip()
    if not url and "/" in repo:
        o, n = repo.split("/", 1)
        url = f"https://{o}.github.io/{n}/"
    if url:
        lines.append(f"\n查看详情：{url}")
    lines.append(f"\n{PLAN_DISCLAIMER}")
    title = f"每日雷达 {data['updated_at_beijing'][:10]}"
    try:
        r = requests.post(f"https://sctapi.ftqq.com/{key}.send", data={"title": title, "desp": "\n".join(lines)}, timeout=15)
        log(f"Server酱推送：HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        warn(f"Server酱推送失败：{e}")


def _alias_prices(prices, errors):
    """把 BRK-B / BRK.B 等别名统一写进 prices，方便页面查找。"""
    out = dict(prices)
    err = dict(errors)
    for spec in PLAN_SPECS + DCA_SPECS:
        a, b = spec["symbol"], spec["yf"]
        if a != b:
            if a in out and b not in out:
                out[b] = out[a]
            if b in out and a not in out:
                out[a] = out[b]
            if a in err and b not in err:
                err[b] = err[a]
            if b in err and a not in err:
                err[a] = err[b]
    return out, err


def _strip_constituents(movers):
    if not movers:
        return {}, None
    d = dict(movers)
    # 成分股列表只作脚本内部回退，不写入公开 data.json（体积大，也无必要）
    cons = d.pop("constituents", None)
    return d, cons


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data.json"))
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--skip-ai", action="store_true", help="只拉行情和买点表，不调用 AI（本地调试）")
    ap.add_argument("--skip-sp500", action="store_true", help="跳过标普500涨跌榜（本地调试）")
    args = ap.parse_args()

    cfg = load_json(args.config)
    if not cfg or not isinstance(cfg.get("watchlist"), list) or not cfg["watchlist"]:
        warn("config.json 读取失败或 watchlist 为空")
        return 1
    cfg["watchlist"] = [str(s).strip().upper() for s in cfg["watchlist"] if str(s).strip()]
    prev = load_json(args.out, {}) or {}
    prev_real = prev if not prev.get("example") else {}
    prev_plan = prev_real.get("plan") or {}

    now = datetime.now(BJ)
    today_bj = now.strftime("%Y-%m-%d %H:%M")
    beijing_date = now.strftime("%Y-%m-%d")
    log(f"== 每日更新 {today_bj}（北京时间）==")

    # 自选 + 计划标的（计划标的拉盘后价）
    plan_yf = []
    for s in PLAN_SPECS + DCA_SPECS:
        if s["yf"] not in plan_yf:
            plan_yf.append(s["yf"])
    watch = list(cfg["watchlist"])
    # 先拉自选（不含已在计划里的，避免重复且计划需要盘后价）
    watch_only = [s for s in watch if s not in plan_yf and s.replace(".", "-") not in plan_yf
                  and s.replace("-", ".") not in {x["symbol"] for x in PLAN_SPECS}]
    log("拉取自选行情…")
    prices, errors = fetch_prices(watch_only, prev_real.get("prices"), want_extended=False)
    log("拉取交易计划标的（含盘后价，如有）…")
    p2, e2 = fetch_prices(plan_yf, prev_real.get("prices"), want_extended=True)
    # 以展示代码写入（BRK.B 而不是 BRK-B）
    for spec in PLAN_SPECS + DCA_SPECS:
        rec = p2.get(spec["yf"])
        if rec:
            prices[spec["symbol"]] = rec
            if spec["symbol"] != spec["yf"]:
                prices[spec["yf"]] = rec
        err = e2.get(spec["yf"])
        if err:
            errors[spec["symbol"]] = err
            if spec["symbol"] != spec["yf"]:
                errors[spec["yf"]] = err
    prices, errors = _alias_prices(prices, errors)

    log("拉取指数…")
    idx_raw, idx_err = fetch_prices(list((cfg.get("indices") or {}).keys()), prev_real.get("indices"), want_extended=False)
    errors.update({k: v for k, v in (idx_err or {}).items()})
    indices = {k: dict(v, name=cfg["indices"].get(k, k)) for k, v in idx_raw.items()}
    log("拉取汇率…")
    fx = fetch_fx() or prev_real.get("fx", {})

    if args.skip_sp500:
        movers = {"session_date": None, "gainers": [], "losers": [], "error": "已跳过标普500涨跌榜（调试）", "constituents": []}
    else:
        log("计算标普500涨跌榜（上一交易日）…")
        movers = fetch_sp500_movers(prev_plan)
        if movers.get("error"):
            warn(movers["error"])

    # 先用行情算出买点表，再交给 AI 写原因和新闻（不让模型改数字）
    draft_plan = build_plan(prices, errors, prev_plan, beijing_date, movers, {}, stale_ai=False)

    radar, ai = None, {"provider": None, "model": None, "ok": False, "error": None}
    if args.skip_ai:
        ai = {"provider": None, "model": None, "ok": False, "error": "已跳过 AI（调试）"}
        warn("已按 --skip-ai 跳过模型调用")
    else:
        log("生成每日雷达（AI 联网，只写原因/新闻/换股提议）…")
        radar, ai = run_ai(cfg, today_bj, prices, indices, fx,
                           draft_plan.get("buy_table"), draft_plan.get("dca"), movers, beijing_date)
    radar_date = now.strftime("%Y-%m-%d")
    stale = False
    ai_bits = {"big_news": [], "swap_proposal": None, "sp500_mover_reasons": {}, "levels_advice": {}, "opportunities": []}
    if radar is None:
        if not args.skip_ai:
            warn(f"AI 雷达生成失败：{ai['error']}（行情与买点表照常更新）")
        old = prev_real.get("radar")
        if old:
            radar, stale = old, True
            radar_date = prev_real.get("radar_date") or (prev_real.get("updated_at_beijing") or "")[:10]
        old_plan = prev_plan or {}
        # 行情已是新的；新闻/原因/换股/参考止盈止损沿用上次（并标记 ai_stale）
        prev_advice = old_plan.get("levels_advice") or {}
        if not prev_advice:
            prev_advice = {}
            for r in old_plan.get("buy_table") or []:
                if not isinstance(r, dict) or not r.get("symbol"):
                    continue
                if r.get("ref_sl") or r.get("ref_tp") or r.get("extra_tranches"):
                    prev_advice[r["symbol"]] = {
                        "stop_loss": r.get("ref_sl"), "take_profit": r.get("ref_tp"),
                        "extra_tranches": r.get("extra_tranches") or [],
                        "reason": r.get("sl_tp_reason") or "", "url": r.get("sl_tp_url") or "",
                        "stale": True,
                    }
        ai_bits = {
            "big_news": old_plan.get("big_news") or [],
            "swap_proposal": old_plan.get("swap_proposal"),
            "sp500_mover_reasons": {},
            "levels_advice": prev_advice,
            "opportunities": old_plan.get("opportunities") or [],
        }
        # 若旧雷达/旧计划里已有原因，尽量补到今日涨跌榜（仅当代码仍出现在今日榜上）
        old_m = (old_plan.get("sp500_movers") or {})
        old_reasons = {}
        for row in (old_m.get("gainers") or []) + (old_m.get("losers") or []):
            if row.get("ticker") and row.get("reason"):
                old_reasons[str(row["ticker"]).upper()] = row["reason"]
        if radar and isinstance(radar, dict) and radar.get("sp500_mover_reasons"):
            old_reasons.update(radar.get("sp500_mover_reasons") or {})
        movers = _attach_reasons(movers, old_reasons)
        # 若今日榜与昨日不同，原因可能不匹配，_attach_reasons 对不上的会写“未能核实”
    else:
        ai_bits = {
            "big_news": radar.get("big_news") or [],
            "swap_proposal": radar.get("swap_proposal"),
            "sp500_mover_reasons": radar.get("sp500_mover_reasons") or {},
            "levels_advice": radar.get("levels_advice") or {},
            "opportunities": radar.get("opportunities") or [],
        }
        movers = _attach_reasons(movers, ai_bits["sp500_mover_reasons"])

    plan = build_plan(prices, errors, prev_plan, beijing_date, movers, ai_bits, stale_ai=stale)
    movers_pub, _cons = _strip_constituents(movers)
    plan["sp500_movers"] = {
        "session_date": movers_pub.get("session_date"),
        "gainers": movers_pub.get("gainers") or [],
        "losers": movers_pub.get("losers") or [],
        "error": movers_pub.get("error"),
    }

    # 从将写入的雷达里去掉仅供脚本使用的原因字典（原因已合并进 plan.sp500_movers）
    radar_out = None
    if radar:
        radar_out = {k: v for k, v in radar.items() if k not in ("sp500_mover_reasons", "levels_advice")}

    data = {
        "example": False,
        "schema": 2,
        "updated_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at_beijing": today_bj,
        "watchlist": cfg["watchlist"],
        "names": cfg.get("names", {}),
        "prices": prices,
        "price_errors": errors,
        "indices": indices,
        "fx": fx,
        "plan": plan,
        "radar": radar_out,
        "radar_date": radar_date if radar_out else None,
        "radar_stale": stale,
        "ai": ai,
        "disclaimer": DISCLAIMER,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log(f"已写入 {args.out}：{len(prices)} 个价格，买点 {len(plan.get('buy_table') or [])} 行，AI {'成功' if ai['ok'] else '失败'}")

    if not args.no_push:
        push_serverchan(data, ai["ok"])
    if not prices:
        warn("一个价格都没拿到（网络或接口问题）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
