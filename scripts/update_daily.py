#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日更新脚本：拉行情 + 汇率 + 让 AI 联网生成中文「每日雷达 / 交易计划」，写入 data.json。

- 不含任何持仓数量/成本/金额（隐私默认）。公开文件只写代码、价格、买点缺口和新闻。
- 买点表、定投收盘价、距买点跌幅、标普 500 涨跌幅、止损/止盈/补充批次（技术参考：ATR + 近期高低点）均在本地计算，不由模型编造。
- 止损/止盈分两层：执行价（我的＞已定＞技术参考，触发/距离/预警/微信推送都用它）和最新参考价（ATR/高低点技术计算，以及既有的 AI 策略参考）。参考价始终单独写出并显示，不会自动覆盖执行价。所有距离以常规时段现价为分母；日涨跌为常规时段，盘后单列。
- AI（Grok / Gemini）主流程只调用一次，只负责：标普 500 涨跌榜每只一句原因、1–3 条大新闻（各附一个链接）、至多一条明显更好的机会（只给代码和理由，价格仍由本地算）。若这次返回无法解析为 JSON，再追加一次很短的「只输出 JSON」修复调用。
- 明显更好的机会会由脚本直接自动替换计划里被点名的标的（无需用户确认）：当前计划写在 data.json 的 plan.active（只有代码、买点、名称），config.json 只是首次的种子；替换记录写在 plan.swap_log。被换出的标的进入 plan.retired（网页上只在你标记已买入/持仓里有它时继续显示止损止盈）。
- 若设置了 SERVERCHAN_KEY，每次运行后推送一条简短中文摘要到微信（不含任何持仓金额）。
- AI 失败不影响行情：保留价格，并在 data.json 里写入错误说明。
- 仅供学习与记录参考，不构成投资建议。

环境变量：
  AI_PROVIDER      gemini（默认）或 grok
  GEMINI_API_KEY   / GEMINI_MODEL（默认 gemini-3.8-flash）
  XAI_API_KEY      / XAI_MODEL（默认 grok-4.7）/ XAI_BASE_URL（默认 https://api.x.ai/v1）
  SERVERCHAN_KEY   可选，Server酱推送
  HOLDINGS_JSON    可选，仅用于生成微信推送里的「持有仓」（数量/成本/止盈止损）。绝不会写入 data.json、日志或任何提交文件。
  PAGES_URL        可选，推送里附带的网页地址（默认 https://stock-6d3.pages.dev）
"""
import argparse
import csv
import io
import json
import math
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
# 用户已定的固定止损/止盈（美元价位；止盈可有两档）。不含任何数量、成本、盈亏。
# config.json 的 agreed_sltp 可覆盖/追加同样结构；页面里「我的」改价优先级更高。
AGREED_SLTP_DEFAULT = {
    "TQQQ": {"sl": 45, "tp": [60, 68], "as_of": "2026-11-14"},
    "SOXL": {"sl": 38, "tp": [60, 75], "as_of": "2026-11-14"},
    "KORU": {"sl": 6.0, "tp": [8.5, 10.5], "as_of": "2026-11-14"},
}
FIXED_CANCELLED = set()                     # config 里写成空对象的代码：不再沿用旧的已定价
FIXED_SLTP = {}                             # main() 里由默认值 + config.json 合并而来
SEED_SPECS = [dict(s) for s in PLAN_SPECS]   # 首次种子；之后以 data.json 的 plan.active 为准
RETIRED_SPECS = []                          # 已被换出的标的（页面只在你持有/标记已买入时显示）
SWAP_LOG = []                               # 自动替换记录
DROPPED = []                                # 已被换出、不再回到自选的代码
DEFAULT_PAGES_URL = "https://stock-6d3.pages.dev"
LEVERED_RE = re.compile(r"^(TQQQ|SQQQ|UPRO|SPXU|SOXL|SOXS|TNA|TZA|TECL|TECS|QLD|QID|SSO|SDS|UDOW|SDOW|TMF|TMV|LABU|LABD|FAS|FAZ|NUGT|DUST|JNUG|JDST|BOIL|KOLD|UVXY|SVXY|VXX|VIXY|SPXL|SPXS|TARK|CURE|DFEN|DUSL|NAIL|RETL|CWEB|YINN|YANG|FNGU|FNGD|BULZ|BERZ|AGQ|ZSL|UCO|SCO|GUSH|DRIP|TSLL|TSLQ|NVDL|NVDX|CONL|MSFU|GGLL|AAPU|AMDL|AMZU|BITX|ETHU|MSTU|MSTX|KORU|KORD)$", re.I)
SWAP_COOLDOWN_DAYS = 5      # 刚换进来的标的 5 天内不再被换
SWAP_NO_FLIPBACK_DAYS = 14  # 刚换出的标的 14 天内不换回
SWAP_MAX_PER_14D = 3
RETIRED_KEEP_DAYS = 180
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


def _ext_session(ts):
    """按美东时间（自动含夏令时）判断一根分钟线属于哪个盘外时段：04:00–09:30 盘前，16:00–20:00 盘后；
    其他时间（含常规时段、20:00–04:00 夜间）返回 None。夜盘（夜间）不做识别：yfinance 的盘前盘后数据不含夜盘。"""
    et = _to_et(ts)
    if et is None:
        return None, None
    m = et.hour * 60 + et.minute
    if 4 * 60 <= m < 9 * 60 + 30:
        return "盘前", et
    if 16 * 60 <= m < 20 * 60:
        return "盘后", et
    return None, et


def _attach_extended(t, rec):
    """若当前有盘前/盘后价，另存 extended_price / ext_label（盘前或盘后）/ ext_change_pct（相对常规收盘）。
    price、change_pct 始终是常规时段（收盘，或盘中运行时的实时常规价）及其相对上一收盘的涨跌，
    买点缺口、止盈止损触及、距离、今日盈亏都只用它们，绝不使用盘外价。"""
    ext, label = None, None
    close = rec.get("close") or rec.get("price")
    last_daily = rec.get("date")  # 最后一根日线的日期（常规收盘日）
    # 1) 带盘前盘后的分钟线，取最新一根（必须是盘前/盘后时段，且与最后一根日线日期相符）
    try:
        h = t.history(period="1d", interval="5m", prepost=True, auto_adjust=False)
        if h is not None and len(h) and "Close" in h:
            h = h.dropna(subset=["Close"])
        if h is not None and len(h):
            last_ts, last_px = h.index[-1], float(h["Close"].iloc[-1])
            sess, et = _ext_session(last_ts)
            if last_px > 0 and sess and et is not None and last_daily:
                bar_day = et.strftime("%Y-%m-%d")
                # 盘后：必须是收盘当天；盘前：必须是收盘日之后的交易日
                if (sess == "盘后" and bar_day == last_daily) or (sess == "盘前" and bar_day > last_daily):
                    ext, label = last_px, sess
    except Exception:  # noqa: BLE001
        pass
    # 2) 回退：ticker.info 的盘前/盘后字段，必须与 marketState 对得上
    if ext is None:
        try:
            info = t.info or {}
            state = str(info.get("marketState") or "").upper()
            key = None
            if state == "PRE":
                key, label_ = "preMarketPrice", "盘前"
            elif state in ("POST", "POSTPOST", "CLOSED"):
                key, label_ = "postMarketPrice", "盘后"
            v = info.get(key) if key else None
            if v and float(v) > 0:
                ext, label = float(v), label_
        except Exception:  # noqa: BLE001
            pass
    if ext is None or not close:
        return rec
    # 与收盘几乎相同则不必显示
    if abs(ext - close) < 0.0005:
        return rec
    # 防呆：盘外价与常规收盘相差离谱（>1.8 倍或 <0.55 倍）多半是拆/并股或坏点，不采用
    if not (0.55 <= ext / close <= 1.8):
        rec = dict(rec)
        rec["range_note"] = (rec.get("range_note") or "") + "盘外价与收盘相差过大，已忽略。"
        return rec
    rec = dict(rec)
    rec["extended_price"] = round(ext, 4)
    rec["ext_label"] = label
    rec["ext_change_pct"] = round((ext / close - 1) * 100, 2)   # 相对常规收盘，不是相对前一日
    return rec


def _is_levered_yf(sym):
    s = str(sym or "").upper()
    return bool(LEVERED_RE.match(s.replace("-", ".")) or s in LEVERED)


def _sanitize_history(h, sym="", drop_bad_bars=True):
    """清洗日线：1) 发现拆股/并股式跳变（日收盘比 >1.8 倍或 <0.55 倍）时，
    有对应 Stock Splits 记录就按比例还原更早的价格，否则只保留跳变之后的数据；
    2) 丢掉明显的坏高低点（最高价 >1.8 倍开/收盘较大者，或最低价 <0.5 倍较小者）。
    返回 (清洗后的表, 说明列表)。Yahoo 一般已做拆股调整，但刚拆股后偶尔还没调整，或数据有坏点。"""
    notes = []
    if h is None or not len(h) or "Close" not in h.columns:
        return h, notes
    h = h.copy()
    has_splits = "Stock Splits" in h.columns
    # 1) 跳变处理（从最近的往前处理）
    for _ in range(4):
        close = h["Close"]
        r = (close / close.shift(1)).to_numpy()
        pos = [i for i in range(1, len(r)) if r[i] == r[i] and (r[i] > 1.8 or r[i] < 0.55)]
        if not pos:
            break
        i = pos[-1]
        split = None
        if has_splits:
            for j in range(max(0, i - 2), min(len(h), i + 3)):
                v = float(h["Stock Splits"].iloc[j] or 0)
                if v > 0 and v != 1 and 0.7 <= float(r[i]) * v <= 1.4:
                    split = v
                    break
        if split:
            for c in ("Open", "High", "Low", "Close"):
                if c in h.columns:
                    h.iloc[:i, h.columns.get_loc(c)] = h[c].iloc[:i] / split
            if "Volume" in h.columns:
                h.iloc[:i, h.columns.get_loc("Volume")] = h["Volume"].iloc[:i] * split
            notes.append(f"{h.index[i].strftime('%Y-%m-%d')} 有拆/并股（比例 {split:g}），更早的价格已按比例还原。")
        else:
            notes.append(f"{h.index[i].strftime('%Y-%m-%d')} 价格出现拆/并股式跳变（{float(r[i]):.2f} 倍）且无拆股记录，只用该日之后的数据。")
            h = h.iloc[i:]
    # 2) 坏高低点
    if drop_bad_bars and "High" in h.columns and "Low" in h.columns and len(h):
        top = h[[c for c in ("Open", "Close") if c in h.columns]].max(axis=1)
        bot = h[[c for c in ("Open", "Close") if c in h.columns]].min(axis=1)
        bad = (h["High"] > top * 1.8) | (h["Low"] < bot * 0.5)
        n_bad = int(bad.sum())
        if n_bad:
            h = h[~bad]
            notes.append(f"已排除 {n_bad} 根明显异常的高低点数据。")
    return h, notes


def _bars_from_history(h):
    """未复权日线。Open 缺失时用 Close 顶上，但 High/Low/Close 必须来自同一根，禁止只拿复权收盘去配原始高低。"""
    bars = []
    splits = []
    has_splits = "Stock Splits" in h.columns
    for ts, row in h.iterrows():
        try:
            day = ts.strftime("%Y-%m-%d")
            c = float(row["Close"])
            hi = float(row["High"])
            lo = float(row["Low"])
            op = float(row["Open"]) if "Open" in h.columns and row["Open"] == row["Open"] else c
        except (TypeError, ValueError, KeyError):
            continue
        if min(op, hi, lo, c) <= 0:
            continue
        bars.append({"date": day, "open": op, "high": hi, "low": lo, "close": c})
        if has_splits:
            try:
                v = float(row["Stock Splits"] or 0)
            except (TypeError, ValueError):
                v = 0
            if v > 0 and v != 1:
                splits.append({"date": day, "ratio": v})
    return bars, splits


def _fetch_issuer_notes(symbol):
    """不再作为核对依据。公告正文解析不能代替人工确认表。"""
    return [], []


def _public_audit(audit):
    """写入 data.json 的核对摘要。不带整段 K 线，也不带持仓。"""
    if not isinstance(audit, dict):
        return empty_hist_audit("")
    skip = {"_adjusted_count"}
    return {k: v for k, v in audit.items() if k not in skip}


def _attach_range(t, rec, sym=""):
    """附带复权校验和 Wilder ATR。未核实就不写技术参考用的 ATR / 高低点，也不再改用估算波动编价。"""
    rec = dict(rec)
    try:
        h = t.history(period="1y", auto_adjust=False)
        if h is None or len(h) == 0 or "Close" not in getattr(h, "columns", []):
            rec["hist_audit"] = empty_hist_audit(sym, "pending", "没有读到未复权日线，技术参考先隐藏。")
            rec["atr_suspect"] = True
            return rec
        need = [c for c in ("High", "Low", "Close") if c in h.columns]
        if len(need) < 3:
            rec["hist_audit"] = empty_hist_audit(sym, "failed", "日线缺少 High/Low/Close，不能做统一复权。")
            rec["atr_suspect"] = True
            return rec
        h = h.dropna(subset=need)
        bars, vendor_splits = _bars_from_history(h)
        canon = str(sym or "").upper().replace("-", ".")
        if canon in ("BRK.B",):
            canon = "BRK.B"
        quote = rec.get("close") or rec.get("price")
        # auto_adjust 保持 False：True 会把分红调进 OHLC。Adj Close 即使存在也不采用。
        audit = review_corporate_window(canon, bars, vendor_splits, quote)
        rec["hist_audit"] = _public_audit(audit)
        rec["atr_suspect"] = audit.get("status") != "verified"
        rec["range_note"] = (audit.get("note") or "")[:240]
        if audit.get("status") == "verified":
            rec["atr14"] = audit.get("atr14")
            rec["high_52w"] = audit.get("high_52w")
            rec["low_52w"] = audit.get("low_52w")
            rec["high_3m"] = audit.get("high_3m")
            rec["low_3m"] = audit.get("low_3m")
            rec["low_20d"] = audit.get("low_20d")
            rec["high_20d"] = None
    except Exception:  # noqa: BLE001
        rec["hist_audit"] = empty_hist_audit(sym, "pending", "复权校验没有完成，技术参考先隐藏。")
        rec["atr_suspect"] = True
    return rec


def _regular_session_open_now():
    """美股常规时段内（周一到周五 9:30–16:00 ET），最后一根日线是盘中价，不是收盘价。"""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= m < 16 * 60


def fetch_one(sym, want_extended=False):
    import yfinance as yf

    def go():
        t = yf.Ticker(sym)
        h = t.history(period="15d", auto_adjust=False)
        h = h.dropna(subset=["Close"])
        if len(h) < 1:
            raise RuntimeError("没有返回数据（代码可能写错，或接口暂时不可用）")
        h, hist_notes = _sanitize_history(h, sym, drop_bad_bars=False)
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
        # 常规时段涨跌：最后一根日线收盘 vs 上一根日线收盘（不含盘前盘后）
        pct = (last / prev - 1) * 100 if prev else None
        intraday = date == datetime.now(ET).strftime("%Y-%m-%d") and _regular_session_open_now()
        rec = {
            "price": round(last, 4),
            "close": round(last, 4),
            "prev_close": round(prev, 4) if prev else None,
            "change_pct": round(pct, 2) if pct is not None else None,
            "date": date,
            "currency": cur,
            "price_label": "盘中" if intraday else "收盘",
        }
        if hist_notes:
            rec["range_note"] = "".join(hist_notes)
        if want_extended:
            rec = _attach_extended(t, rec)
            rec = _attach_range(t, rec, sym)
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

    def dl(batch):
        d = yf.download(batch, period="5d", interval="1d", group_by="ticker",
                        auto_adjust=False, threads=True, progress=False, timeout=60)
        if d is None or d.empty:
            raise RuntimeError("没有返回数据")
        return d

    try:
        # 先整批一次下载（最快）；失败再回退到每 100 只一批
        try:
            frames = [(yf_list, dl(yf_list))]
        except Exception as e1:  # noqa: BLE001
            warn(f"标普500整批下载失败（{e1}），改为分批")
            frames = []
            for i in range(0, len(yf_list), 100):
                b = yf_list[i:i + 100]
                frames.append((b, retry(lambda b=b: dl(b), tries=2, delay=5, what="下载标普500分批")))
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
def _canon(s):
    u = str(s or "").strip().upper()
    return "BRK.B" if u in ("BRK-B", "BRKB", "BRK/B") else u


def _pos_levels(v):
    out = []
    for x in v or []:
        try:
            n = float(x)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.append(n)
    return out


def _days_between(a, b):
    try:
        return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days
    except Exception:  # noqa: BLE001
        return 9999


def _clean_fixed(v):
    """规范化一条固定止损/止盈：{"sl": 数, "tp": [数, ...最多两档], "as_of": 日期}；无效返回 None。"""
    if not isinstance(v, dict):
        return None
    out = {}
    try:
        sl = float(v.get("sl") if v.get("sl") is not None else v.get("stop") or 0)
    except (TypeError, ValueError):
        sl = 0
    if sl > 0:
        out["sl"] = sl
    raw = v.get("tp") if v.get("tp") is not None else v.get("take")
    if raw is None:
        raw = []
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    tps = []
    for x in raw:
        try:
            n = float(x)
        except (TypeError, ValueError):
            continue
        if n > 0:
            tps.append(n)
    tps = sorted(set(tps))[:2]
    if tps:
        out["tp"] = tps
    if not out:
        return None
    out["as_of"] = str(v.get("as_of") or v.get("asof") or "")[:10]
    return out


def load_fixed_sltp(cfg):
    """默认已定止损止盈 + config.json 的 agreed_sltp（同代码以 config 为准）。"""
    FIXED_SLTP.clear()
    FIXED_CANCELLED.clear()
    for sym, v in AGREED_SLTP_DEFAULT.items():
        c = _clean_fixed(v)
        if c:
            FIXED_SLTP[_canon(sym)] = c
    for sym, v in ((cfg or {}).get("agreed_sltp") or {}).items():
        if str(sym).startswith("_"):
            continue
        c = _clean_fixed(v)
        if c:
            FIXED_SLTP[_canon(sym)] = c
        else:
            FIXED_SLTP.pop(_canon(sym), None)   # 写成空对象表示取消这只的固定价
            FIXED_CANCELLED.add(_canon(sym))


def _fixed_for(spec_or_sym):
    """优先 config 里的已定价；没有就用 plan.active 里保存过的 sl/tp。"""
    if isinstance(spec_or_sym, dict):
        sym = _canon(spec_or_sym.get("symbol"))
        stored = _clean_fixed({"sl": spec_or_sym.get("sl"), "tp": spec_or_sym.get("tp"), "as_of": spec_or_sym.get("sltp_asof")})
    else:
        sym, stored = _canon(spec_or_sym), None
    return FIXED_SLTP.get(sym) or stored


def init_plan_state(prev_plan, today):
    """从上次 data.json 的 plan 里恢复当前计划；没有就用种子。只含代码、买点、名称。"""
    seed = {_canon(s["symbol"]): s for s in SEED_SPECS}
    dropped = []
    for x in (prev_plan or {}).get("dropped") or []:
        c = _canon(x)
        if c and c not in dropped:
            dropped.append(c)
    active, seen = [], set()
    prev_active = (prev_plan or {}).get("active") or []
    for e in prev_active:
        if not isinstance(e, dict) or not e.get("symbol") or not e.get("yf"):
            continue
        sym = _canon(e["symbol"])
        if sym in seen or sym in dropped:
            continue
        if sym in seed and not e.get("from"):
            spec = dict(seed[sym])  # 种子标的用代码里最新的梯子
        else:
            lv = _pos_levels(e.get("levels"))
            if not lv:
                continue
            kind = e.get("kind") if e.get("kind") in ("stock", "levered", "nvda", "brk") else "stock"
            spec = {"symbol": sym, "yf": str(e["yf"]).upper(), "name": _s(e.get("name"), 60) or sym, "levels": lv,
                    "kind": kind, "added": e.get("added") or "", "from": _canon(e.get("from")), "why": _s(e.get("why"), 300)}
        for k in ("sl", "tp", "sltp_asof"):
            if e.get(k) is not None and spec.get(k) is None and sym not in FIXED_CANCELLED:
                spec[k] = e[k]
        fx = FIXED_SLTP.get(sym)
        if fx:  # 已定止损/止盈合并进 plan.active（不覆盖页面「我的」改价；那只存在浏览器里）
            if fx.get("sl"):
                spec["sl"] = fx["sl"]
            if fx.get("tp"):
                spec["tp"] = list(fx["tp"])
            spec["sltp_asof"] = fx.get("as_of") or ""
        active.append(spec)
        seen.add(sym)
    if not active:
        active = [dict(s) for s in SEED_SPECS if _canon(s["symbol"]) not in dropped]
    else:
        for sym, s in seed.items():
            if sym not in seen and sym not in dropped:
                active.append(dict(s))
    for spec in active:
        fx = FIXED_SLTP.get(_canon(spec["symbol"]))
        if fx:
            if fx.get("sl"):
                spec["sl"] = fx["sl"]
            if fx.get("tp"):
                spec["tp"] = list(fx["tp"])
            spec["sltp_asof"] = fx.get("as_of") or ""
    retired = []
    for e in (prev_plan or {}).get("retired") or []:
        if not isinstance(e, dict) or not e.get("symbol") or not e.get("yf"):
            continue
        lv = _pos_levels(e.get("levels"))
        if not lv or _days_between(e.get("retired_on") or today, today) > RETIRED_KEEP_DAYS:
            continue
        kind = e.get("kind") if e.get("kind") in ("stock", "levered", "nvda", "brk") else "stock"
        retired.append({"symbol": _canon(e["symbol"]), "yf": str(e["yf"]).upper(), "name": _s(e.get("name"), 60) or e["symbol"],
                        "levels": lv, "kind": kind, "retired_on": e.get("retired_on") or today,
                        "replaced_by": _canon(e.get("replaced_by"))})
    log_ = [e for e in ((prev_plan or {}).get("swap_log") or []) if isinstance(e, dict) and e.get("in") and e.get("out")]
    PLAN_SPECS[:] = active
    RETIRED_SPECS[:] = retired[-8:]
    DROPPED[:] = dropped
    SWAP_LOG[:] = log_[-20:]


def effective_watchlist(base):
    """config.json 是种子；被换出的代码从自选里去掉，换进来的加上。"""
    out = []
    for s in base:
        if _canon(s) in DROPPED:
            continue
        if s not in out:
            out.append(s)
    have = {_canon(s) for s in out}
    for sp in PLAN_SPECS:
        if _canon(sp["symbol"]) not in have:
            out.append(sp["yf"])
    return out


def apply_auto_swap(opp, today, prices, errors):
    """AI 点名的机会只记成待复核，不改候选买点，也不用 ATR 生成新的可执行买档。"""
    if not opp or not opp.get("ticker"):
        return None
    tk, vs = _canon(opp.get("ticker")), _canon(opp.get("vs"))
    entry = {
        "date": today, "in": tk, "vs": vs, "applied": False,
        "reason": _s(opp.get("why"), 200),
        "url": _http_url(opp.get("url")),
        "note": "未确认。不改变候选买点，也不新增可执行买档。",
    }
    log(f"  机会 {tk or '—'} 只待复核，未改计划。")
    return entry


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
            gpct = (price - lv) / price * 100     # 现价还要再跌多少 % 才到该买点（以现价为分母）
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
        "ext_label": (rec or {}).get("ext_label") or "",
        "price_label": (rec or {}).get("price_label") or "收盘",
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
        "high_20d": _r((rec or {}).get("high_20d")),
        "low_20d": _r((rec or {}).get("low_20d")),
        "atr14": _r((rec or {}).get("atr14")),
        "atr_suspect": bool((rec or {}).get("atr_suspect")),
        "hist_audit": (rec or {}).get("hist_audit") or empty_hist_audit(spec.get("symbol"), "unknown", "这次行情没有复权校验记录，技术参考先隐藏。"),
        "data_note": ((rec or {}).get("range_note") or "")[:240],
        "ext_change_pct": _r((rec or {}).get("ext_change_pct"), 2),
        "fixed_sl": _r((_fixed_for(spec) or {}).get("sl"), 4),
        "fixed_tp": [round(x, 4) for x in ((_fixed_for(spec) or {}).get("tp") or [])],
        "fixed_asof": (_fixed_for(spec) or {}).get("as_of") or "",
        "swapped_in": bool(spec.get("from")),
        "swap_from": spec.get("from") or "",
        "added": spec.get("added") or "",
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


def _px(x):
    return round(float(x), 2) if abs(float(x)) >= 1 else round(float(x), 3)


# 发行人口径核对用的公开页面。这里只列地址，不写死任何拆/并股日期或比例。
# 页面没明确写出「代码 + 比例 + 除权日」时，状态保持 pending/unknown，不编造事件。
ISSUER_CHECK_URLS = {
    "TQQQ": [
        "https://www.proshares.com/press-releases/proshares-announces-etf-share-splits5",
        "https://www.proshares.com/press-releases/proshares-announces-etf-share-splits-122021",
        "https://www.proshares.com/press-releases/proshares-announces-etf-share-splits-10621",
    ],
    "SOXL": [
        "https://www.direxion.com/product/daily-semiconductor-bull-bear-3x-etfs",
        "https://www.direxion.com/press-releases",
    ],
    "KORU": [
        "https://www.direxion.com/press-release/direxion-to-split-nine-etfs",
        "https://www.direxion.com/press-release/direxion-announces-reverse-split-of-koru",
        "https://www.sec.gov/Archives/edgar/data/1424958/000119312526265925/d105555d497.htm",
    ],
}
_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def wilder_atr14(highs, lows, closes):
    """Wilder ATR(14)。

    TR = max(H-L, |H-前收|, |L-前收|)。
    种子：前 14 个 TR 的算术平均（因此至少要 15 根收盘，第一根没有前收）。
    之后每一根：ATR = (前一个 ATR × 13 + 当日 TR) / 14。
    返回最后一根的 ATR；数据不够则返回 None。种子所在的交易日是第 15 根 K 线。
    """
    n = min(len(highs or []), len(lows or []), len(closes or []))
    if n < 15:
        return None
    trs = []
    for i in range(1, n):
        try:
            h, lo, prev_c = float(highs[i]), float(lows[i]), float(closes[i - 1])
        except (TypeError, ValueError):
            return None
        trs.append(max(h - lo, abs(h - prev_c), abs(lo - prev_c)))
    if len(trs) < 14:
        return None
    atr = sum(trs[:14]) / 14.0
    for tr in trs[14:]:
        atr = (atr * 13.0 + tr) / 14.0
    return atr


def adjust_ohlc_to_current(bars, splits):
    """把未复权 OHLC 统一除以后来的拆/并股比例，得到当前每股口径。

    splits 用雅虎口径：前复权价 = 原始价 / 比例。20 合 1 的正股拆分比例是 20；
    10 合 1 的并股比例是 0.1。Open/High/Low/Close 用同一个因子，不允许只改收盘。
    bars 按日期从早到晚。返回新列表，不改入参。
    """
    events = []
    for sp in splits or []:
        try:
            ratio = float(sp.get("ratio"))
            day = str(sp.get("date") or "")[:10]
        except (TypeError, ValueError, AttributeError):
            continue
        if ratio > 0 and ratio != 1 and len(day) == 10:
            events.append((day, ratio))
    out = []
    for bar in bars or []:
        day = str(bar.get("date") or "")[:10]
        factor = 1.0
        for ex, ratio in events:
            if day < ex:
                factor *= ratio
        try:
            o, h, lo, c = (float(bar["open"]) / factor, float(bar["high"]) / factor,
                           float(bar["low"]) / factor, float(bar["close"]) / factor)
        except (TypeError, ValueError, KeyError):
            raise ValueError("OHLC 不完整，无法按同一口径复权")
        if not (h + 1e-9 >= max(o, c, lo) and lo <= min(o, c, h) + 1e-9 and c > 0):
            raise ValueError("复权后高低收盘关系不成立")
        out.append({"date": day, "open": o, "high": h, "low": lo, "close": c, "factor": factor})
    return out


def _parse_en_date(text):
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text or "")
    if not m:
        return None
    mon = _EN_MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    try:
        return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"
    except ValueError:
        return None


def parse_issuer_events(text, symbol):
    """从发行人页面正文里抽取「这个代码 + 比例 + 除权日」都写明的事件。缺任何一项就丢掉，不补。"""
    sym = str(symbol or "").upper()
    blob = text or ""
    events = []
    seen = set()
    for m in re.finditer(rf"(?:\({re.escape(sym)}\)|(?<![A-Z]){re.escape(sym)}(?![A-Z]))", blob, re.I):
        window = blob[m.start():m.start() + 280]
        rm = re.search(r"(\d+)\s*(?:for|:)\s*(\d+)", window, re.I)
        if not rm:
            continue
        a, b = int(rm.group(1)), int(rm.group(2))
        if a <= 0 or b <= 0 or a == b:
            continue
        wide = blob[max(0, m.start() - 200):m.start() + 900]
        dm = re.search(
            r"(?:split-adjusted basis on|prior to market open on|begin trading[^\n]{0,80}?\bon)\s+([A-Za-z]+\s+\d{1,2},\s*\d{4})",
            wide, re.I)
        if not dm and re.search(r"split-adjusted|market open|Ex-Date|Payable Date", wide, re.I):
            dm = re.search(r"([A-Za-z]+\s+\d{1,2},\s*\d{4})", wide)
        ex = _parse_en_date(dm.group(1) if dm else "")
        if not ex:
            continue
        ratio = a / float(b)
        kind = "forward" if a > b else "reverse"
        key = (ex, round(ratio, 6))
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "date": ex, "ratio": ratio, "kind": kind,
            "label": f"{a} for {b}", "symbol": sym,
        })
    events.sort(key=lambda e: e["date"])
    return events


def _events_match(issuer_events, vendor_events, slack_days=4):
    """窗口内的发行人事件必须和行情源拆股记录一一对应。对不上就是 failed，不是 verified。"""
    def near(a, b):
        try:
            da = datetime.strptime(a, "%Y-%m-%d")
            db = datetime.strptime(b, "%Y-%m-%d")
        except ValueError:
            return False
        return abs((da - db).days) <= slack_days

    used = set()
    for ev in issuer_events:
        hit = None
        for i, vd in enumerate(vendor_events):
            if i in used:
                continue
            if near(ev["date"], vd["date"]) and abs(float(ev["ratio"]) - float(vd["ratio"])) / float(ev["ratio"]) < 0.02:
                hit = i
                break
        if hit is None:
            return False
        used.add(hit)
    return len(used) == len(vendor_events)


def _split_gap_closed(raw_bars, adjusted, splits):
    """拆股日前应有明显跳空；复权后跳空应消失。若复权制造出新的大跳空，说明重复复权或方向错了。"""
    by_raw = {b["date"]: b for b in raw_bars}
    by_adj = {b["date"]: b for b in adjusted}
    dates = [b["date"] for b in raw_bars]
    for sp in splits or []:
        ex = str(sp.get("date") or "")[:10]
        i = next((k for k, d in enumerate(dates) if d >= ex), None)
        if i is None or i == 0:
            continue
        d0, d1 = dates[i - 1], dates[i]
        try:
            raw_jump = float(by_raw[d1]["close"]) / float(by_raw[d0]["close"])
            adj_jump = float(by_adj[d1]["close"]) / float(by_adj[d0]["close"])
        except (TypeError, ValueError, KeyError, ZeroDivisionError):
            return False
        if raw_jump <= 0 or adj_jump <= 0:
            return False
        raw_gap = abs(math.log(raw_jump))
        adj_gap = abs(math.log(adj_jump))
        if raw_gap > math.log(1.8) and adj_gap > math.log(1.8):
            return False
        if raw_gap <= math.log(1.8) and adj_gap > raw_gap + math.log(1.5):
            return False
    return True


def addon_conflicts(price, fixed_stop):
    """补充买价小于或等于有效的已定/我的止损，就是策略冲突。相等也算。不改价。"""
    try:
        return float(price) <= float(fixed_stop) + 1e-9
    except (TypeError, ValueError):
        return False


# 人工确认过的公司行为。比例用「新股数 / 旧股数」：20 for 1 是分子 20、分母 1 的正股拆分。
# 只收录这次对照过发行人原文的事件。没有写进表的，不能靠「表是空的」当成已核实。
# KORU 20-for-1 的依据是 Direxion 公告本身，不是一句没有具体网址的 “(sec)”。
CONFIRMED_ACTIONS = [
    {
        "symbol": "KORU",
        "ex_date": "2026-07-15",
        "numerator": 20,
        "denominator": 1,
        "direction": "forward",
        "source_url": "https://www.direxion.com/press-release/direxion-to-split-nine-etfs",
        "evidence": "Direxion 2026-06-10 公告表格：KORU forward split 20 for 1；2026-07-15 起按拆股后价格交易。",
        "status": "confirmed",
    },
    {
        "symbol": "KORU",
        "ex_date": "2025-02-10",
        "numerator": 1,
        "denominator": 10,
        "direction": "reverse",
        "source_url": "https://www.direxion.com/press-release/direxion-announces-reverse-split-of-koru",
        "evidence": "Direxion 2025-01-10 只针对 KORU 的公告：每 10 股换成 1 股；2025-02-10 起按并股后价格交易。",
        "status": "confirmed",
    },
    {
        "symbol": "TQQQ",
        "ex_date": "2025-11-20",
        "numerator": 2,
        "denominator": 1,
        "direction": "forward",
        "source_url": "https://www.proshares.com/press-releases/proshares-announces-etf-share-splits5",
        "evidence": "ProShares 2025-11-04 公告：TQQQ 2:1，2025-11-20 开盘前生效。只确认这一次，不是说 TQQQ 以后不再拆股。",
        "status": "confirmed",
    },
]
# 隔夜收盘比接近这些倍数才标成疑似拆/并股。1.5 或 2 这种杠杆波动不在此列，也不据此宣称已经证实。
_SUSPECT_RATIOS = (20.0, 10.0, 0.05, 0.1)


def _action_ratio(ev):
    return float(ev["numerator"]) / float(ev["denominator"])


def suspicious_overnight(bars, band=0.12):
    """补充检测。命中只表示疑似，不能代替确认表。"""
    found = []
    for i in range(1, len(bars or [])):
        prev = bars[i - 1].get("close")
        cur = bars[i].get("close")
        try:
            prev, cur = float(prev), float(cur)
        except (TypeError, ValueError):
            continue
        if prev <= 0 or cur <= 0:
            continue
        ratio = cur / prev
        near = None
        for target in _SUSPECT_RATIOS:
            if abs(ratio / target - 1) <= band:
                near = target
                break
        if near is not None:
            found.append({"date": bars[i]["date"], "observed": ratio, "near": near})
    return found


def _jump_at(bars, ex_date):
    dates = [b["date"] for b in bars]
    i = next((k for k, d in enumerate(dates) if d >= ex_date), None)
    if i is None or i == 0:
        return None
    prev, cur = float(bars[i - 1]["close"]), float(bars[i]["close"])
    if prev <= 0:
        return None
    return cur / prev


def _adjust_mode(jump, share_ratio):
    """share_ratio = 新股/旧股。未复权时价格大约变成 1/share_ratio。"""
    if jump is None or jump <= 0 or share_ratio <= 0:
        return "uncertain"
    expected = 1.0 / share_ratio
    if abs(jump / expected - 1) <= 0.25:
        return "raw"
    if 0.75 <= jump <= 1.35:
        return "already"
    return "uncertain"


def _ohlc_only(bars):
    """只用 OHLC。分红调整后的 Adj Close 即使附在旁边也不参与 ATR。"""
    out = []
    for b in bars or []:
        out.append({k: b[k] for k in ("date", "open", "high", "low", "close")})
    return out


def review_corporate_window(symbol, raw_bars, vendor_splits, quote_close=None, table=None, auto_adjust=False):
    """用人工确认表核对这一窗里的拆/并股。只做拆股复权，不做分红/全收益调整。

    表和行情源不一致、隔夜比疑似拆股但表里没有、或者这一窗两边都空，都隐藏技术参考。
    空窗只表示「这一窗没有已确认事件」，不是该品种永不拆股。
    """
    sym = str(symbol or "").upper()
    bars = _ohlc_only(raw_bars)
    base = {
        "symbol": sym,
        "basis": "split_only_current_share",
        "dividend_adjusted": False,
        "method": "uniform_ohlc_split_only",
        "atr_method": "wilder14",
        "hide_technical": True,
        "sources": [],
    }
    if auto_adjust:
        base.update(status="failed", note="auto_adjust=true 可能把分红算进 OHLC，不能用来做 ATR。")
        return base
    if not bars:
        base.update(status="pending", note="没有 OHLC，技术参考先隐藏。")
        return base
    first, last = bars[0]["date"], bars[-1]["date"]
    table = CONFIRMED_ACTIONS if table is None else table
    confirmed = []
    for ev in table:
        if str(ev.get("symbol") or "").upper() != sym or ev.get("status") != "confirmed":
            continue
        if not ev.get("source_url") or not str(ev.get("source_url")).startswith("http"):
            continue
        day = str(ev.get("ex_date") or "")[:10]
        if first < day <= last:
            confirmed.append(ev)
    vendor = []
    for sp in vendor_splits or []:
        day = str(sp.get("date") or "")[:10]
        if first < day <= last:
            try:
                vendor.append({"date": day, "ratio": float(sp["ratio"])})
            except (TypeError, ValueError, KeyError):
                base.update(status="failed", note="行情源拆股记录无法读取，技术参考隐藏。")
                return base
    confirmed_as_vendor = [{"date": e["ex_date"], "ratio": _action_ratio(e)} for e in confirmed]
    jumps = suspicious_overnight(bars)
    explained = {e["ex_date"] for e in confirmed}
    unexplained = [j for j in jumps if j["date"] not in explained and not any(abs((datetime.strptime(j["date"], "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days) <= 4 for d in explained)]
    if unexplained:
        base.update(status="unknown", note="隔夜价格比接近 10 或 20 倍（或相反），但确认表没有对应事件。只当作不确定并隐藏，不把这段波动写成已证实的拆股。")
        base["anomalies"] = unexplained
        return base
    if not _events_match(confirmed_as_vendor, vendor):
        base.update(status="failed", note="确认表和行情源在这一窗里的拆/并股不一致，技术参考隐藏。")
        base["confirmed"] = [{"date": e["ex_date"], "numerator": e["numerator"], "denominator": e["denominator"], "url": e["source_url"]} for e in confirmed]
        base["vendor_splits"] = vendor
        return base
    if not confirmed:
        base.update(status="unknown", note="这一窗的确认表和行情源都没有拆股。这只说明本窗没有已确认事件，不能当成该品种永不拆股。技术参考继续隐藏。")
        return base
    to_apply = []
    for ev in confirmed:
        jump = _jump_at(bars, ev["ex_date"])
        mode = _adjust_mode(jump, _action_ratio(ev))
        if mode == "uncertain":
            base.update(status="failed", note="除权日附近的价格跳空对不上「尚未复权」或「已经拆股复权」，为避免重复复权，技术参考隐藏。")
            return base
        if mode == "raw":
            to_apply.append({"date": ev["ex_date"], "ratio": _action_ratio(ev)})
    try:
        adjusted = adjust_ohlc_to_current(bars, to_apply) if to_apply else [dict(b, factor=1.0) for b in bars]
    except ValueError as e:
        base.update(status="failed", note=f"OHLC 无法按同一拆股因子复权（{e}）。分红调整不参与。")
        return base
    if suspicious_overnight(adjusted):
        base.update(status="failed", note="拆股复权之后仍有接近 10 或 20 倍的隔夜跳空，技术参考隐藏。")
        return base
    if quote_close:
        try:
            if not (0.85 <= float(adjusted[-1]["close"]) / float(quote_close) <= 1.15):
                base.update(status="failed", note="复权后的最后收盘和常规时段收盘对不上，技术参考隐藏。")
                return base
        except (TypeError, ValueError, ZeroDivisionError):
            base.update(status="failed", note="无法对照常规时段收盘，技术参考隐藏。")
            return base
    highs = [b["high"] for b in adjusted]
    lows = [b["low"] for b in adjusted]
    closes = [b["close"] for b in adjusted]
    atr = wilder_atr14(highs, lows, closes)
    if atr is None:
        base.update(status="failed", note="拆股复权后的 K 线不足 15 根，Wilder ATR 没有种子，技术参考隐藏。")
        return base
    last_c = float(closes[-1])
    base.update(
        status="verified",
        hide_technical=False,
        note="这一窗里的拆/并股与确认表一致，OHLC 只做了拆股复权，没有做分红调整。",
        cutoff=adjusted[-1]["date"],
        atr_seed_date=adjusted[14]["date"],
        atr14=round(float(atr), 4),
        atr_pct=round(float(atr) / last_c * 100, 2) if last_c else None,
        baseline=round(last_c, 4),
        high_3m=round(max(highs[-63:]), 4),
        low_3m=round(min(lows[-63:]), 4),
        low_20d=round(min(lows[-20:]), 4),
        high_52w=round(max(highs), 4),
        low_52w=round(min(lows), 4),
        source="未复权 OHLC（不含分红）+ 人工确认表",
        formula="TR=max(H-L,|H-前收|,|L-前收|)；种子=前14个TR的平均；其后 ATR=(前ATR×13+TR)/14",
        warmup="第 15 根 K 线才有第一颗 ATR。再往前不外推。",
        confirmed=[{"date": e["ex_date"], "numerator": e["numerator"], "denominator": e["denominator"],
                    "direction": e["direction"], "url": e["source_url"]} for e in confirmed],
        applied_splits=to_apply,
    )
    return base


def classify_adjustment(*_args, **_kwargs):
    """公告正文解析不再是核对依据。核实只走 review_corporate_window 和人工确认表。"""
    return {
        "status": "unknown",
        "hide_technical": True,
        "basis": "split_only_current_share",
        "note": "公告文本解析不再作为核实依据。",
    }



def empty_hist_audit(symbol, status="unknown", note=""):
    return {
        "symbol": str(symbol or "").upper(),
        "status": status,
        "hide_technical": True,
        "basis": "current_per_share",
        "note": note or "缺少复权校验，技术参考暂不显示。",
        "sources": [],
    }


def technical_allowed(row):
    """旧 data.json 没有 hist_audit 时视为未核实，不能拿 atr/高低点在本地编一个参考价。"""
    audit = (row or {}).get("hist_audit")
    if not isinstance(audit, dict):
        return False
    return audit.get("status") == "verified" and not audit.get("hide_technical")


def compute_tech_levels(price, levels, st, basis="ladder"):
    """本地用历史价格算技术参考：止损、止盈、补充批次。不调用模型，不是新闻判断。
    basis="ladder"：以买点梯子为基准（还没买）；basis="cost"：levels 传入 [你的成本]（已买/持有）。
    只有 hist_audit.status=verified 时才允许使用 ATR。ATR 缺失时不再用现价百分比编造。
    公式（与 reason 文字一致）：
      止损 = min( 基准最低价 − 1.5×ATR ，  近三月低点或 20 日低点（低于基准最低价时）− 0.5×ATR )
      止盈 = 基准均价 + 3×ATR；若近三月高点高出均价 5% 以上，改用「近三月高点，限制在均价+2~6 倍 ATR 内」；
             再保证不低于基准最高价的 103%。
    补充批次只是待复核提议，调用方不得把它写进可执行买点。"""
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if not price > 0:
        return None
    if not technical_allowed(st):
        return None
    atr = (st or {}).get("atr14")
    est = False
    try:
        cap = 0.20 if (st or {}).get("kind") == "levered" else 0.12
        if atr and float(atr) > 0 and not (float(atr) / price > cap):
            atr = float(atr)
        else:
            return None
    except (TypeError, ValueError):
        return None
    core = sorted([float(x) for x in (levels or []) if x and float(x) > 0], reverse=True)
    if not core:
        core = [price]
    low_core, high_core = min(core), max(core)
    avg_core = sum(core) / len(core)
    sl = low_core - 1.5 * atr
    sl_how = "基准最低价下 1.5×ATR"
    l3, h3 = (st or {}).get("low_3m"), (st or {}).get("high_3m")
    l20 = (st or {}).get("low_20d")
    for sw, nm in ((l3, "近三月低点"), (l20, "20日低点")):
        try:
            if sw and 0 < float(sw) < low_core and float(sw) - 0.5 * atr < sl:
                sl = float(sw) - 0.5 * atr
                sl_how = f"{nm}下 0.5×ATR"
        except (TypeError, ValueError):
            pass
    tp = avg_core + 3 * atr
    tp_how = "基准均价上 3×ATR"
    try:
        if h3 and float(h3) > avg_core * 1.05:
            tp = max(avg_core + 2 * atr, min(float(h3), avg_core + 6 * atr))
            tp_how = "近三月高点（限在均价上 2~6×ATR）"
    except (TypeError, ValueError):
        pass
    if tp < high_core * 1.03:
        tp, tp_how = high_core * 1.03, "基准最高价的 103%"
    sl = max(sl, 0.01)
    f = lambda v: (f"{v:.2f}" if v is not None else "—")
    base_txt = (f"你的成本 {f(core[0])}" if basis == "cost" and len(core) == 1
                else f"买点梯子（最深 {f(min(core))}）" if basis != "cost" else f"基准 {f(min(core))}")
    reason = (f"技术参考（本地按历史价格算，非 AI/新闻）：基准={base_txt}；ATR14≈{f(atr)}{'（估算）' if est else ''}；"
              f"近三月低/高 {f(l3)}/{f(h3)}。止损={f(sl)}（{sl_how}），止盈={f(tp)}（{tp_how}）。")
    return {
        "stop_loss": _px(sl),
        "take_profit": _px(tp),
        "extra_tranches": [],
        "executable_tranches": False,
        "reason": reason[:300],
        "url": "",
        "source": "technical",
    }


def _proposed_from_extras(row, extras):
    """补充价只作复核。小于等于已定/固定止损的标成冲突，不改价，也不进入可执行买点。"""
    stop = row.get("fixed_sl")
    out = []
    for x in extras or []:
        try:
            x = float(x)
        except (TypeError, ValueError):
            continue
        if x <= 0:
            continue
        conflict = addon_conflicts(x, stop) if stop else False
        out.append({"price": round(x, 4), "conflict": conflict, "executable": False})
    return out


def _merge_level_advice(row, ai_bits, prev_row):
    """技术参考只有在复权校验为 verified 时才写入。不覆盖用户既定核心买点，也不把补充批次变成可执行买档。
    未核实的旧参考价不沿用、不编造。"""
    row = dict(row)
    core = list(row.get("levels") or [])
    row["tranche_levels"] = list(core)
    if not technical_allowed(row):
        row["ref_sl"] = None
        row["ref_tp"] = None
        row["extra_tranches"] = []
        row["proposed_tranches"] = []
        note = ((row.get("hist_audit") or {}) if isinstance(row.get("hist_audit"), dict) else {}).get("note")
        row["sl_tp_reason"] = note or "技术参考未核实，暂不显示。已定和我的执行价不受影响。"
        row["sl_tp_stale"] = False
        return row
    advice = (ai_bits or {}).get("levels_advice") or {}
    a = advice.get(row["symbol"]) or advice.get((row.get("yf") or "").upper())
    if not a:
        return row
    sl, tp = a.get("stop_loss"), a.get("take_profit")
    if sl and sl > 0:
        row["ref_sl"] = _r(sl, 4)
    if tp and tp > 0:
        row["ref_tp"] = _r(tp, 4)
    row["sl_tp_reason"] = (a.get("reason") or "")[:300]
    row["sl_tp_url"] = a.get("url") or ""
    row["sl_tp_source"] = a.get("source") or row.get("sl_tp_source") or "technical"
    row["sl_tp_stale"] = bool(a.get("stale"))
    row["extra_tranches"] = []
    row["proposed_tranches"] = []
    row["tranche_levels"] = list(core)
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
        "ext_label": (rec or {}).get("ext_label") or "",
        "price_label": (rec or {}).get("price_label") or "收盘",
        "prev_close": _r((rec or {}).get("prev_close")),
        "change_pct": _r((rec or {}).get("change_pct"), 2),
        "ext_change_pct": _r((rec or {}).get("ext_change_pct"), 2),
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
    retired_rows = []
    prev_ret = {r.get("symbol"): r for r in (prev_plan or {}).get("retired_table") or [] if isinstance(r, dict)}
    for spec in RETIRED_SPECS:
        rec = prices.get(spec["symbol"]) or prices.get(spec["yf"])
        err = errors.get(spec["symbol"]) or errors.get(spec["yf"])
        r = build_buy_row(spec, rec, err, None, session, beijing_date)
        r.update({"retired": True, "at_buy": False, "newly_touched": [], "retired_on": spec.get("retired_on"),
                  "replaced_by": spec.get("replaced_by") or ""})
        retired_rows.append(r)
    # 技术参考始终单独计算，即使已有已定价或 AI 策略参考也不丢弃。
    adv_ai = dict((ai_bits or {}).get("levels_advice") or {})
    adv = {}
    for r in rows + retired_rows:
        if not r.get("price"):
            continue
        tech = compute_tech_levels(r["price"], r.get("levels"), r)
        if tech:
            adv[r["symbol"]] = tech
    ai_bits_tech = dict(ai_bits or {}, levels_advice=adv)
    rows = [_finalize_buy_row(r, ai_bits_tech, prev_rows.get(r["symbol"]), adv_ai) for r in rows]
    retired_rows = [_finalize_buy_row(r, ai_bits_tech, prev_ret.get(r["symbol"]), adv_ai) for r in retired_rows]
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
            "symbols": [s["symbol"] for s in PLAN_SPECS if s.get("kind") == "levered"],
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
        "opportunities": [],
        "retired_table": retired_rows,
        "active": [{k: s.get(k) for k in ("symbol", "yf", "name", "levels", "kind", "added", "from", "why", "sl", "tp", "sltp_asof") if s.get(k) not in (None, "", [])} for s in PLAN_SPECS],
        "retired": [dict(s) for s in RETIRED_SPECS],
        "swap_log": list(SWAP_LOG),
        "dropped": list(DROPPED),
        "levels_advice": (ai_bits_tech or {}).get("levels_advice") or {},
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
SYSTEM = ("你是谨慎的中文财经信息整理助手。只用联网搜索核实，只写搜到的内容；查不到就留空，不要编造数字、新闻或来源。"
          "不要给出买卖建议。你不知道也不要猜测用户的持仓。")

SCHEMA_TEXT = """{"sp500_mover_reasons":[{"ticker":"代码","reason":"一句话原因，查不到就不要列这一条"}],"big_news":[{"summary":"一句话","url":"https://来源"}],"opportunity":null}"""


def build_prompt(today_bj, movers):
    """极简提示词：只查涨跌榜原因 + 重大新闻（+可选一条明显更好的机会）。价格、买点、止盈止损都在本地算好，不让模型参与。"""
    lines = []
    if movers and not movers.get("error"):
        for tag, key in (("涨", "gainers"), ("跌", "losers")):
            for g in movers.get(key) or []:
                lines.append(f"{tag} {g['ticker']}（{g.get('name','')}）{g.get('change_pct')}%")
    movers_txt = "\n".join(lines) if lines else "（今日无涨跌榜，sp500_mover_reasons 输出空数组）"
    plan_syms = "、".join(s["symbol"] for s in PLAN_SPECS)
    return f"""北京时间 {today_bj}。联网搜索，只做两件事：
1. 为下面标普500上一交易日涨跌榜的每只股票各找一句原因（中文，不超过40字）。查不到可靠原因就不要输出那一条，不要写“未能核实”。
{movers_txt}
2. big_news：最多 3 条真正重大的新闻，每条一句中文摘要 + 一个 http(s) 来源链接。范围仅限：美联储和利率、英伟达、ADBE 财报（约北京时间 {ADBE_EARNINGS_BJ} 早上）、重要宏观数据、会影响长城短债的国内债市大波动。没有就输出空数组，不要凑数。
可选 opportunity：只有“极其明显”好于现有计划标的（{plan_syms}）的机会才填（流动性好、催化剂清晰、趋势或估值明显更优）。系统检查通过后会直接用它替换 vs 指定的现有标的，无需用户确认，所以门槛要高；大多数日子必须为 null。对象含 ticker、name、vs（必须是上面括号里的一个代码）、why（一句话）、url（可选）。不要给价格，系统会本地计算。
正文里不要出现网址或引用标记（url 字段除外）。只输出一个 JSON 对象，不要多余文字：
{SCHEMA_TEXT}"""


def _max_output_tokens():
    try:
        n = int(os.environ.get("AI_MAX_OUTPUT_TOKENS", "") or 2000)
    except ValueError:
        n = 2000
    return max(256, min(n, 8000))


_SECRET_RE = re.compile(r"(?i)\b(bearer\s+|xai-|sk-|api[_-]?key[^=\s]*[=:\s]+|AIza)[A-Za-z0-9_\-.]{6,}")


def preview_ai_text(text, n=400):
    """日志用的短预览：压缩空白、截断，并去掉像密钥的片段。"""
    raw = "" if text is None else str(text)
    s = _SECRET_RE.sub(r"\1***", raw)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > n:
        s = s[:n] + "…"
    return f"{len(raw)} 字：{s or '（空）'}"


def _normalize_ai_text(text):
    t = str(text or "").replace("\ufeff", "")
    t = t.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    t = t.replace("｛", "{").replace("｝", "}").replace("［", "[").replace("］", "]")
    return t.strip()


def _strip_trailing_commas(s):
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def _fenced_blocks(text):
    blocks = []
    for m in re.finditer(r"```(?:json|javascript|js)?\s*\r?\n?(.*?)```", text, flags=re.I | re.S):
        blocks.append(m.group(1).strip())
    return blocks


def _iter_balanced_json(text):
    """从文本中按括号匹配抽出完整的 {...} 或 [...]（忽略字符串里的括号）。"""
    in_str = False
    esc = False
    quote = ""
    start = None
    depth = 0
    opener = ""
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
            continue
        if ch in "{[":
            if depth == 0:
                start = i
                opener = ch
            depth += 1
        elif ch in "}]":
            if depth <= 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                if (opener == "{" and ch == "}") or (opener == "[" and ch == "]"):
                    yield text[start:i + 1]
                start = None
                opener = ""


def _load_json_candidate(raw):
    last_err = None
    for variant in (raw, _strip_trailing_commas(raw)):
        try:
            return json.loads(variant), None
        except json.JSONDecodeError as e:
            last_err = e
    return None, last_err


def extract_json(text):
    """从模型回复里抽出 JSON：支持 markdown 代码块、前后说明文字、第一个完整对象、多余逗号。"""
    if text is None or not str(text).strip():
        raise ValueError("AI 没有返回内容")
    t = _normalize_ai_text(text)
    blobs = []
    for block in _fenced_blocks(t) + [t]:
        if not block:
            continue
        found = list(_iter_balanced_json(block))
        if found:
            blobs.extend(found)
        else:
            blobs.append(block)
    seen = set()
    objects = []
    last_err = None
    had_blob = False
    for raw in blobs:
        raw = raw.strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        if raw[0] not in "{[":
            continue
        had_blob = True
        obj, err = _load_json_candidate(raw)
        if err:
            last_err = err
            continue
        if isinstance(obj, list):
            obj = next((x for x in obj if isinstance(x, dict)), None)
        if isinstance(obj, dict):
            objects.append(obj)
    if objects:
        def score(o):
            return sum(1 for k in ("sp500_mover_reasons", "big_news", "opportunity") if k in o)
        objects.sort(key=score, reverse=True)
        return objects[0]
    if not had_blob:
        raise ValueError("AI 返回的内容里找不到 JSON")
    raise ValueError("AI 返回的 JSON 无法解析" + (f"：{last_err}" if last_err else ""))


def build_repair_prompt(raw_text, today_bj, movers):
    """解析失败时用的短提示：要求只输出 JSON。有原文就改写，没有则带上原任务要点。"""
    head = ("只输出一个 JSON 对象，不要 markdown 代码块，不要解释或其它文字。"
            f"没有的内容用空数组或 null。格式：\n{SCHEMA_TEXT}\n")
    raw = _normalize_ai_text(raw_text)
    if raw:
        return head + "请把下面这份回复改写成上述 JSON：\n" + raw[:1500]
    return head + "请按下面的任务作答：\n" + build_prompt(today_bj, movers)




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


PLACEHOLDER_REASON = re.compile(r"^(未能核实|暂无可靠原因|暂无|无|未知|未核实|n/?a|none|null|—|-)+[。.!！]?$", re.I)


def _real_reason(s):
    s = _s(s, 400)
    return "" if (not s or PLACEHOLDER_REASON.match(s) or "未能核实" in s) else s


def validate_radar(obj, cfg, movers=None):
    """校验并整理 AI 输出（精简版）：只收涨跌榜原因、大新闻、至多一条机会。"""
    if not isinstance(obj, dict):
        raise ValueError("JSON 顶层不是对象")
    extra = []

    allowed = set()
    if movers:
        for row in (movers.get("gainers") or []) + (movers.get("losers") or []):
            if row.get("ticker"):
                tk = str(row["ticker"]).upper()
                allowed |= {tk, tk.replace(".", "-"), tk.replace("-", ".")}
    reasons = {}
    for it in obj.get("sp500_mover_reasons") or []:
        if not isinstance(it, dict) or not _s(it.get("ticker")):
            continue
        tk = _s(it["ticker"], 16).upper()
        reason, u = _clean_cites(it.get("reason"))
        extra += u
        reason = _real_reason(reason)
        if not reason:
            continue
        if allowed and tk not in allowed:
            continue
        reasons[tk] = reason

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
        return "BRK.B" if s in ("BRK-B", "BRKB") else s.replace("_", ".")

    known = {s.upper() for s in cfg["watchlist"]} | _plan_symbol_set() | {s["symbol"].upper() for s in DCA_SPECS}
    known |= {x.replace("-", ".") for x in known} | {x.replace(".", "-") for x in known}
    opp = obj.get("opportunity")
    if opp is None and isinstance(obj.get("opportunities"), list) and obj["opportunities"]:
        opp = obj["opportunities"][0]
    opp_out = None
    if isinstance(opp, dict):
        tk = _canon_sym(opp.get("ticker") or opp.get("symbol"))
        why, u = _clean_cites(opp.get("why") or opp.get("reason"))
        extra += u
        vs = _canon_sym(opp.get("vs") or opp.get("replace"))
        if vs and vs not in known:
            vs = ""
        if tk and _s(why) and tk not in _plan_symbol_set():
            opp_out = {"ticker": tk, "name": _s(opp.get("name"), 60), "vs": vs, "why": _s(why, 300),
                       "url": _http_url(opp.get("url") or (u[0] if u else ""))}

    return {
        "sp500_mover_reasons": reasons,
        "big_news": big,
        "opportunity": opp_out,
        "_extracted_urls": extra,
    }


def _attach_reasons(movers, reasons):
    """把原因并入涨跌榜；没有可靠原因就留空（页面只显示代码、名称和涨跌幅）。"""
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
        return {**row, "reason": _real_reason(reason)}

    out["gainers"] = [one(r) for r in (movers.get("gainers") or [])]
    out["losers"] = [one(r) for r in (movers.get("losers") or [])]
    return out


def _gemini_text(resp):
    """尽量从 Gemini 响应里取出可见正文（跳过 thought）。"""
    try:
        text = (resp.text or "").strip()
        if text:
            return text
    except Exception:  # noqa: BLE001
        pass
    parts = []
    try:
        for c in resp.candidates or []:
            content = getattr(c, "content", None)
            for p in getattr(content, "parts", None) or []:
                if getattr(p, "thought", False):
                    continue
                tx = getattr(p, "text", None)
                if tx:
                    parts.append(tx)
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(parts).strip()


def call_gemini(prompt, use_search=True):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("没有设置 GEMINI_API_KEY")
    from google import genai
    from google.genai import types

    model = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.8-flash"
    client = genai.Client(api_key=key)
    # 注意：使用 google_search 工具时，接口不允许同时指定 JSON 响应格式，所以在提示词里要求 JSON，再自行解析和校验。
    cfg = dict(
        system_instruction=SYSTEM,
        temperature=0.2,
        max_output_tokens=_max_output_tokens(),
    )
    if use_search:
        cfg["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(**cfg),
    )
    text = _gemini_text(resp)
    sources = []
    try:
        gm = resp.candidates[0].grounding_metadata
        for ch in (gm.grounding_chunks or []):
            if ch.web and ch.web.uri:
                sources.append({"title": ch.web.title or "", "url": ch.web.uri})
    except Exception:  # noqa: BLE001
        pass
    return text, sources, model


def _collect_xai_text(j):
    """从 xAI Responses / Chat Completions 响应里收集助手正文，避开 reasoning 和工具调用。"""
    if not isinstance(j, dict):
        return str(j or ""), []
    sources, messages = [], []

    def add_src(title, url):
        if url:
            sources.append({"title": title or "", "url": url})

    def from_parts(parts):
        if isinstance(parts, str):
            messages.append(parts)
            return
        for part in parts or []:
            if isinstance(part, str):
                messages.append(part)
                continue
            if not isinstance(part, dict):
                continue
            typ = (part.get("type") or "")
            if typ in ("output_text", "text", "input_text") or (part.get("text") and typ not in ("reasoning", "summary_text")):
                if part.get("text"):
                    messages.append(part["text"])
            for an in part.get("annotations", []) or []:
                if isinstance(an, dict):
                    add_src(an.get("title", ""), an.get("url"))

    for item in j.get("output") or []:
        if not isinstance(item, dict):
            continue
        typ = item.get("type") or ""
        if typ in ("reasoning", "web_search_call", "function_call", "custom_tool_call", "tool_call"):
            continue
        if typ in ("output_text", "text") and item.get("text"):
            messages.append(item["text"])
        if typ == "message" or item.get("content"):
            from_parts(item.get("content"))
    if not messages and isinstance(j.get("output_text"), str) and j["output_text"].strip():
        messages.append(j["output_text"])
    ch0 = (j.get("choices") or [None])[0] or {}
    msg = ch0.get("message") or {}
    if isinstance(msg.get("content"), str) and msg["content"].strip():
        messages.append(msg["content"])
    elif msg.get("content"):
        from_parts(msg.get("content"))
    for c in j.get("citations") or []:
        if isinstance(c, str):
            add_src("", c)
        elif isinstance(c, dict):
            add_src(c.get("title", ""), c.get("url") or c.get("uri"))
    status = j.get("status") or ""
    if status and status not in ("completed", "complete", "success"):
        detail = j.get("incomplete_details") or {}
        log(f"  xAI 回复状态：{status}" + (f"（{detail}）" if detail else "") + "，可能被截断")
    text = "\n".join(x for x in messages if x).strip()
    return text, sources


def call_grok(prompt, use_search=True):
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("没有设置 XAI_API_KEY")
    model = os.environ.get("XAI_MODEL", "").strip() or "grok-4.7"
    base = (os.environ.get("XAI_BASE_URL", "").strip() or "https://api.x.ai/v1").rstrip("/")
    # xAI Responses API + 内置 web_search 工具（见 docs.x.ai/developers/tools/web-search）
    body = {
        "model": model,
        "input": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        # 输出很短；上限同时覆盖推理 token，设太小会截断，所以留余量。可用 AI_MAX_OUTPUT_TOKENS 调整
        "max_output_tokens": _max_output_tokens(),
    }
    if use_search:
        body["tools"] = [{"type": "web_search"}]
    r = requests.post(f"{base}/responses", json=body, timeout=400,
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"xAI 接口返回 {r.status_code}：{r.text[:300]}")
    text, sources = _collect_xai_text(r.json())
    return text, sources, model


def _finalize_radar(radar, sources):
    extra = radar.pop("_extracted_urls", []) or []
    seen, src = set(), []
    pre = [{"title": "", "url": n["url"]} for n in (radar.get("big_news") or []) if n.get("url")]
    o = radar.get("opportunity")
    if o and o.get("url"):
        pre.append({"title": "", "url": o["url"]})
    for s in (pre + [{"title": "", "url": u} for u in extra] + list(sources or [])):
        u = _http_url((s or {}).get("url") if isinstance(s, dict) else s)
        if not u or u in seen:
            continue
        seen.add(u)
        src.append({"title": (s.get("title") or "") if isinstance(s, dict) else "", "url": u})
    radar["sources"] = src[:12]
    return radar


def run_ai(cfg, today_bj, movers):
    """主流程只调用一次模型（网络错误最多再试一次）。若返回无法解析为 JSON，再追加一次很短的「只输出 JSON」修复调用。"""
    provider = (os.environ.get("AI_PROVIDER", "") or "gemini").strip().lower()
    if provider in ("xai", "grok"):
        provider, fn = "grok", call_grok
    elif provider == "gemini":
        fn = call_gemini
    else:
        return None, {"provider": provider, "model": None, "ok": False,
                      "error": f"不认识的 AI_PROVIDER：{provider}（只能填 gemini 或 grok）"}
    prompt = build_prompt(today_bj, movers)
    model = (os.environ.get("XAI_MODEL", "").strip() or "grok-4.7") if provider == "grok" else (os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.8-flash")
    last, text, sources = None, "", []
    for i in range(2):
        try:
            text, sources, model = fn(prompt)
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  AI 调用失败（第 {i + 1}/2 次）：{e}")
            if "没有设置" in str(e):
                break
            time.sleep(5)
            continue
        log(f"  AI 原始回复预览（{preview_ai_text(text)}）")
        try:
            radar = validate_radar(extract_json(text), cfg, movers)
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  AI 返回内容解析失败：{e}")
            break
        return _finalize_radar(radar, sources), {"provider": provider, "model": model, "ok": True, "error": None}
    # 解析失败（已拿到回复）或第一次就没抽出 JSON：再试一次短修复提示，值得多花一次调用
    if last and "没有设置" not in str(last):
        repair = build_repair_prompt(text, today_bj, movers)
        log("  尝试一次「只输出 JSON」修复调用…")
        try:
            # 已有正文时不再联网，只让模型把原文改写成 JSON，省 token
            rtext, rsrc, model = fn(repair, use_search=not bool((text or "").strip()))
            log(f"  修复调用回复预览（{preview_ai_text(rtext)}）")
            radar = validate_radar(extract_json(rtext), cfg, movers)
            return _finalize_radar(radar, list(sources or []) + list(rsrc or [])), {"provider": provider, "model": model, "ok": True, "error": None}
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  修复调用仍未成功：{e}")
    return None, {"provider": provider, "model": model, "ok": False, "error": str(last)[:300] if last else "AI 调用失败"}


def local_market_summary(indices):
    """隔夜美股摘要改由行情接口数字在本地拼出（不调用模型）。"""
    parts = []
    for v in (indices or {}).values():
        if v.get("change_pct") is not None:
            parts.append(f"{v.get('name','')} {v['change_pct']:+.2f}%")
    return {"headline": "；".join(parts)[:80], "overview": "", "macro": []}


# ------------------------------------------------------------------ Server酱
def _md(s, n=60):
    """放进 Markdown 表格/行内的文本：去掉换行和竖线，截断。"""
    s = re.sub(r"\s+", " ", str(s or "")).replace("|", "／").strip()
    return s[:n]


def _fpx(v):
    if v is None:
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{v:,.2f}" if abs(v) >= 1 else f"{v:.4f}".rstrip("0").rstrip(".")


def _fpct(v, d=1):
    try:
        return f"{float(v):+.{d}f}%"
    except (TypeError, ValueError):
        return "—"


def _px_cell(r):
    px = r.get("price")
    if px is None:
        return "无报价"
    lab = r.get("price_label") or "收盘"
    return f"${_fpx(px)} {lab}" + ("·旧" if r.get("stale") else "")


def _ext_ref_text(src):
    """第二行「参考」：当前盘前/盘后价及相对常规收盘的涨跌；没有就返回空串。"""
    if not isinstance(src, dict) or src.get("extended_price") is None or not src.get("ext_label"):
        return ""
    ep = src.get("ext_change_pct")
    return f"{src['ext_label']} {_fpx(src['extended_price'])}" + (f" {_fpct(ep, 2)}" if ep is not None else "") + "（相对收盘，参考）"


def _fusd(v, signed=False):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if signed:
        return f"${v:+,.2f}"
    return f"${v:,.2f}"


def _lookup_quote(data, symbol):
    s = _canon(symbol)
    prices = (data or {}).get("prices") or {}
    plan = (data or {}).get("plan") or {}
    for key in (s, s.replace(".", "-"), s.replace("-", "."), symbol):
        rec = prices.get(key)
        if isinstance(rec, dict) and rec.get("price") is not None:
            return rec
    for r in list(plan.get("buy_table") or []) + list(plan.get("retired_table") or []) + list(plan.get("dca") or []):
        if isinstance(r, dict) and _canon(r.get("symbol")) == s and r.get("price") is not None:
            return r
    return None


def _plan_row_for(data, symbol):
    s = _canon(symbol)
    plan = (data or {}).get("plan") or {}
    for r in list(plan.get("buy_table") or []) + list(plan.get("retired_table") or []):
        if isinstance(r, dict) and _canon(r.get("symbol")) == s:
            return r
    return None


def _is_levered_sym(sym, data=None):
    s = _canon(sym)
    if LEVERED_RE.match(s):
        return True
    r = _plan_row_for(data, s) if data is not None else None
    return bool(r and r.get("kind") == "levered")


def _pos_num(v, positive=False):
    if v is None or v == "":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n:
        return None
    if positive and n <= 0:
        return None
    return n


def load_holdings_for_push():
    """只从环境变量读取，供推送使用。解析失败只打一句不含内容的日志，绝不打印原文。"""
    raw = os.environ.get("HOLDINGS_JSON")
    if raw is None or not str(raw).strip():
        return None
    try:
        obj = json.loads(str(raw).strip().lstrip("\ufeff"))
    except Exception:  # noqa: BLE001
        log("HOLDINGS_JSON 无法解析，本次推送不含持有仓。")
        return None
    if not isinstance(obj, dict):
        log("HOLDINGS_JSON 格式无效，本次推送不含持有仓。")
        return None
    src = obj.get("positions") or obj.get("hold") or obj.get("holdings")
    if src is None:
        src = []
    if not isinstance(src, list):
        log("HOLDINGS_JSON 格式无效，本次推送不含持有仓。")
        return None
    positions = []
    for it in src[:40]:
        if not isinstance(it, dict):
            continue
        sym = _canon(it.get("symbol") or it.get("ticker") or it.get("code") or "")
        if not sym:
            key = str(it.get("key") or "")
            if ":" in key:
                sym = _canon(key.split(":", 1)[-1])
        qty = _pos_num(it.get("qty") if it.get("qty") is not None else it.get("quantity") or it.get("shares"), True)
        if not sym or not qty:
            continue
        cost = _pos_num(it.get("cost") if it.get("cost") is not None else it.get("avg") or it.get("costPrice"))
        if cost is None:
            cost = 0.0
        acc = it.get("account") or it.get("acct") or ""
        if acc not in ("ZA", "盈立"):
            acc = "ZA" if sym in ("QQQM", "SCHD") else "盈立"
        sl = _pos_num(it.get("sl") if it.get("sl") is not None else it.get("stop") or it.get("stop_loss"), True)
        tp = _pos_num(it.get("tp") if it.get("tp") is not None else it.get("take") or it.get("take_profit"), True)
        positions.append({"symbol": sym, "qty": qty, "cost": cost, "account": acc, "sl": sl, "tp": tp,
                          "name": str(it.get("name") or "")[:40]})
    capital = _pos_num(obj.get("capital") if obj.get("capital") is not None else obj.get("capitalUsd") or obj.get("capital_usd"))
    if capital is None or capital <= 0:
        capital = 1500.0
    cash = _pos_num(obj.get("cash"))
    if cash is None:
        cash = capital - sum(p["qty"] * p["cost"] for p in positions)
    log("已读取持仓供推送使用。" if positions else "HOLDINGS_JSON 里没有有效持仓，本次推送不含持有仓。")
    if not positions:
        return None
    return {"capital": capital, "cash": cash, "positions": positions}


def _dist_to_stop(px, sl):
    """距止损：现价还要再跌多少 %（以现价为分母）。"""
    return (float(px) - float(sl)) / float(px) * 100


def _dist_to_tp(px, tp):
    """距止盈：现价还要再涨多少 %（以现价为分母）。"""
    return (float(tp) - float(px)) / float(px) * 100


def _session_px(*srcs):
    """常规时段价格（收盘或盘中最新）。不用盘前/盘后/夜盘价。"""
    for src in srcs:
        if not src or not isinstance(src, dict):
            continue
        try:
            v = src.get("price")
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            pass
    return None


def _official_close(rec, row=None):
    """已结束的常规时段收盘。盘中、盘前、盘后都不能触发止损止盈。"""
    rec = rec if isinstance(rec, dict) else {}
    row = row if isinstance(row, dict) else {}
    lab = str(rec.get("price_label") or row.get("price_label") or "")
    if lab in ("盘中", "即时报价", "盘前", "盘后"):
        return None
    if lab not in ("", "收盘"):
        return None
    for src in (rec, row):
        try:
            v = src.get("close")
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _auto_ref(row, px=None, cost=None):
    """最新自动参考。未核实的历史（含旧数据缺 hist_audit）不返回价位，也不在浏览器侧用估算补上。"""
    row = row or {}
    audit = row.get("hist_audit") if isinstance(row.get("hist_audit"), dict) else None
    if not technical_allowed(row):
        note = (audit or {}).get("note") or "缺少复权校验，技术参考暂不显示。"
        return {
            "sl": None, "tp": None, "tps": [], "src": "", "reason": note,
            "ai_sl": None, "ai_tp": None, "ai_reason": "",
            "hidden": True, "status": (audit or {}).get("status") or "unknown",
        }
    tech = None
    src = ""
    reason = ""
    if cost and cost > 0:
        base_px = px if px else _session_px(row)
        if base_px:
            tech = compute_tech_levels(base_px, [cost], row, basis="cost")
            if tech:
                src = "技术参考·按成本"
                reason = tech.get("reason") or ""
    sl = tp = None
    if tech and tech.get("stop_loss"):
        sl = float(tech["stop_loss"])
    elif row.get("ref_sl"):
        try:
            if float(row["ref_sl"]) > 0:
                sl = float(row["ref_sl"])
                src = src or "技术参考"
        except (TypeError, ValueError):
            pass
    if tech and tech.get("take_profit"):
        tp = float(tech["take_profit"])
    elif row.get("ref_tp"):
        try:
            if float(row["ref_tp"]) > 0:
                tp = float(row["ref_tp"])
                src = src or "技术参考"
        except (TypeError, ValueError):
            pass
    if not reason:
        reason = row.get("sl_tp_reason") or ""
    ai_sl = ai_tp = None
    try:
        if row.get("ai_ref_sl") and float(row["ai_ref_sl"]) > 0:
            ai_sl = float(row["ai_ref_sl"])
    except (TypeError, ValueError):
        pass
    try:
        if row.get("ai_ref_tp") and float(row["ai_ref_tp"]) > 0:
            ai_tp = float(row["ai_ref_tp"])
    except (TypeError, ValueError):
        pass
    return {
        "sl": sl,
        "tp": tp,
        "tps": [tp] if tp else [],
        "src": src,
        "reason": reason,
        "ai_sl": ai_sl,
        "ai_tp": ai_tp,
        "ai_reason": row.get("ai_ref_reason") or "",
        "hidden": False,
        "status": "verified",
    }


def _attach_price_layers(row):
    """在买点行上同时写出执行价（不含网页「我的」覆盖）和自动参考价。兼容旧字段 ref_sl/ref_tp/fixed_*。"""
    row = dict(row)
    rs = _resolve_sltp(row, _session_px(row))
    row["exec_sl"] = _r(rs["sl"], 4) if rs.get("sl") else None
    row["exec_tp"] = [round(float(x), 4) for x in (rs.get("tps") or [])]
    row["exec_sl_src"] = rs.get("sl_src") or ""
    row["exec_tp_src"] = rs.get("tp_src") or ""
    # 自动参考已在 ref_sl/ref_tp；这里再标来源，缺省则保持空，页面显示「暂无参考」
    ref = rs.get("ref") or {}
    if ref.get("src"):
        row["ref_src"] = ref["src"]
    return row


def _finalize_buy_row(row, ai_bits_tech, prev_row, adv_ai=None):
    """合并已核实的技术参考。AI 给出的价位和补充批次不能改候选买点，也不能变成新的可执行买档。"""
    row = _merge_level_advice(row, ai_bits_tech, prev_row)
    a = (adv_ai or {}).get(row["symbol"]) or (adv_ai or {}).get((row.get("yf") or "").upper())
    if a and technical_allowed(row):
        if a.get("stop_loss"):
            row["ai_ref_sl"] = _r(a.get("stop_loss"), 4)
        if a.get("take_profit"):
            row["ai_ref_tp"] = _r(a.get("take_profit"), 4)
        if a.get("reason"):
            row["ai_ref_reason"] = (a.get("reason") or "")[:300]
        row["proposed_tranches"] = []
    else:
        row["ai_ref_sl"] = None
        row["ai_ref_tp"] = None
    row["extra_tranches"] = []
    row["tranche_levels"] = list(row.get("levels") or [])
    return _attach_price_layers(row)


def _resolve_sltp(row, px=None, my_sl=None, my_tp=None, cost=None):
    """止损/止盈取值优先级（每一侧分别算）：
    我的（页面/密钥里填的）> 已定（config 里用户确认的固定价）> 技术参考。
    技术参考：已持有且知道成本时按成本算，否则按买点梯子算（云端 buy_table 里的 ref_sl/ref_tp）。
    返回执行价 {"sl","sl_src","tps","tp_src"}，以及始终单独给出的自动参考 {"ref": {...}}。
    参考价不会覆盖执行价。"""
    row = row or {}
    out = {"sl": None, "sl_src": "", "tps": [], "tp_src": ""}
    tech = None
    if cost and cost > 0:
        base_px = px if px else _session_px(row)
        if base_px:
            tech = compute_tech_levels(base_px, [cost], row, basis="cost")
    allow_tech = technical_allowed(row)
    if my_sl:
        out["sl"], out["sl_src"] = float(my_sl), "我的"
    elif row.get("fixed_sl"):
        out["sl"], out["sl_src"] = float(row["fixed_sl"]), "已定"
    elif allow_tech and tech and tech.get("stop_loss"):
        out["sl"], out["sl_src"] = float(tech["stop_loss"]), "技术参考·按成本"
    elif allow_tech and row.get("ref_sl"):
        out["sl"], out["sl_src"] = float(row["ref_sl"]), "技术参考"
    if my_tp:
        out["tps"], out["tp_src"] = [float(my_tp)], "我的"
    elif row.get("fixed_tp"):
        out["tps"], out["tp_src"] = [float(x) for x in row["fixed_tp"]], "已定"
    elif allow_tech and tech and tech.get("take_profit"):
        out["tps"], out["tp_src"] = [float(tech["take_profit"])], "技术参考·按成本"
    elif allow_tech and row.get("ref_tp"):
        out["tps"], out["tp_src"] = [float(row["ref_tp"])], "技术参考"
    out["ref"] = _auto_ref(row, px, cost)
    return out


def _sltp_for_position(pos, data):
    row = _plan_row_for(data, pos["symbol"]) or {}
    rec = _lookup_quote(data, pos["symbol"]) or {}
    px = _session_px(rec, row)
    return _resolve_sltp(row, px, pos.get("sl"), pos.get("tp"), pos.get("cost"))


def _tp_tier_text(tps, px):
    """止盈价位文字：单档「60」，两档「60 / 68」。"""
    return " / ".join(_fpx(t) for t in tps)


def _px_with_ext(rec, row, px, lab, pct):
    """现价单元格：主行=常规收盘价和相对上一收盘的涨跌；第二行（参考）=盘前/盘后价，相对收盘。"""
    if px is None:
        return "无报价"
    src = rec if rec.get("extended_price") is not None else row
    ref = _ext_ref_text(src)
    return f"{_fusd(px)} {_md(lab, 4)} {_fpct(pct, 2)}" + (f"<br>参考：{_md(ref, 40)}" if ref else "")


def _build_holdings_push(data, holdings):
    """持有仓 Markdown。holdings 已规范化；价格来自本次行情，不回写任何文件。"""
    if not holdings or not holdings.get("positions"):
        return "", [], []
    plan = data.get("plan") or {}
    hit_alerts = []
    held_syms = []
    accounts = {}
    for pos in holdings["positions"]:
        rec = _lookup_quote(data, pos["symbol"]) or {}
        row = _plan_row_for(data, pos["symbol"]) or {}
        px = _session_px(rec, row)
        prev = rec.get("prev_close") if rec.get("prev_close") is not None else row.get("prev_close")
        pct = rec.get("change_pct") if rec.get("change_pct") is not None else row.get("change_pct")
        qty, cost = pos["qty"], pos["cost"]
        mv = pl = day = plp = None
        if px is not None:
            mv = qty * float(px)
            pl = mv - qty * cost
            plp = (float(px) / cost - 1) * 100 if cost > 0 else None
            close_reg = rec.get("close") if rec.get("close") is not None else row.get("close")
            if prev is not None and close_reg is not None:
                day = qty * (float(close_reg) - float(prev))      # 常规时段今日盈亏；盘后波动不计入
            elif prev is not None:
                day = qty * (float(px) - float(prev))
            elif pct is not None:
                day = mv - mv / (1 + float(pct) / 100.0)
        rs = _sltp_for_position(pos, data)
        sl, tps = rs["sl"], rs["tps"]
        close_px = _official_close(rec, row)
        marks = []
        dist = []
        if close_px is None and (sl or tps):
            dist.append("尚未确认常规收盘，止损和止盈都不触发")
        if sl:
            if close_px is not None and float(close_px) <= float(sl) + 1e-9:
                marks.append("🚨收盘触及止损")
                hit_alerts.append(f"**{pos['symbol']}** 已确认常规收盘 {_fusd(close_px)}，触及{rs['sl_src']}止损 {_fusd(sl)}。这只是整笔卖出复核，不是下单，也不会自动改去看另一档。")
                dist.append(f"🚨止损 {_fpx(sl)}（{rs['sl_src']}）收盘已触及（分母：已确认的常规收盘）")
            elif close_px is not None:
                dist.append(f"止损 {_fpx(sl)}（{rs['sl_src']}）：再跌 {_dist_to_stop(close_px, sl):.1f}% 才到（分母：已确认的常规收盘）")
            else:
                dist.append(f"止损 {_fpx(sl)}（{rs['sl_src']}）")
        if tps:
            if close_px is not None and float(close_px) >= float(tps[0]) - 1e-9:
                marks.append("🚨收盘到止盈复核")
                both = f"两档 {_fusd(tps[0])} 和 {_fusd(tps[1])} 都是整笔卖出复核，先到的一档不会改去盯另一档。" if len(tps) > 1 else f"止盈 {_fusd(tps[0])} 是整笔卖出复核。"
                hit_alerts.append(f"**{pos['symbol']}** 已确认常规收盘 {_fusd(close_px)}，{both}不是下单。")
                dist.append(f"🚨止盈复核 {_tp_tier_text(tps, close_px)}（{rs['tp_src']}，两档都是整笔复核，分母：已确认的常规收盘）" if len(tps) > 1 else f"🚨止盈复核 {_tp_tier_text(tps, close_px)}（分母：已确认的常规收盘）")
            elif close_px is not None:
                tier = "；".join((f"第{'一二'[i]}档 " if len(tps) > 1 else "") + f"{_fpx(t)} 再涨 {_dist_to_tp(close_px, t):.1f}%（分母：已确认的常规收盘）" for i, t in enumerate(tps))
                dist.append(f"止盈复核（{rs['tp_src']}，两档各自整笔复核，先到的不切换目标）：{tier}" if len(tps) > 1 else f"止盈（{rs['tp_src']}）：{tier}")
            else:
                dist.append(f"止盈 {_tp_tier_text(tps, px)}（{rs['tp_src']}）")
        ref = rs.get("ref") or {}
        if ref.get("hidden"):
            dist.append("参考：暂不显示（复权未核实，不编造）")
        elif ref.get("sl") or ref.get("tp"):
            rsl = f"止损 {_fpx(ref['sl'])}" if ref.get("sl") else "止损 暂无参考"
            rtp = f"止盈 {_fpx(ref['tp'])}" if ref.get("tp") else "止盈 暂无参考"
            same_sl = (sl is not None and ref.get("sl") is not None and abs(float(sl) - float(ref["sl"])) < 1e-6)
            same_tp = (tps and ref.get("tp") is not None and len(tps) == 1 and abs(float(tps[0]) - float(ref["tp"])) < 1e-6)
            if not (same_sl and (same_tp or not tps)):
                dist.append(f"参考：{rsl} / {rtp}")
        elif sl or tps:
            dist.append("参考：暂无参考")
        conflicts = [p for p in (row.get("proposed_tranches") or []) if isinstance(p, dict) and p.get("conflict")]
        if conflicts:
            dist.append("补充价与有效止损冲突，不作为加仓提示")
        if not dist:
            dist.append("尚未设置止盈止损，也没有技术参考")
        acc = pos["account"]
        accounts.setdefault(acc, {"rows": [], "mv": 0.0, "cost": 0.0, "day": 0.0, "pl": 0.0})
        lab = rec.get("price_label") or row.get("price_label") or "收盘"
        accounts[acc]["rows"].append({
            "symbol": pos["symbol"], "qty": qty, "cost": cost, "px": px, "lab": lab, "pct": pct,
            "px_txt": _px_with_ext(rec, row, px, lab, pct),
            "day": day, "pl": pl, "plp": plp, "dist": "；".join(dist), "marks": marks,
        })
        if mv is not None:
            accounts[acc]["mv"] += mv
            accounts[acc]["pl"] += pl if pl is not None else 0
        accounts[acc]["cost"] += qty * cost
        if day is not None:
            accounts[acc]["day"] += day
        held_syms.append(pos["symbol"])
    lines = ["### 💼 持有仓"]
    if hit_alerts:
        lines += ["", "🚨 **卖出提醒**"] + [f"- {a}" for a in hit_alerts] + [""]
    lines += ["", "| 账户 | 标的 | 数量 | 成本 | 现价 | 今日盈亏 | 累计盈亏 | 止损/止盈 |", "|---|---|---|---|---|---|---|---|"]
    for acc in [a for a in ("ZA", "盈立") if a in accounts] + [a for a in accounts if a not in ("ZA", "盈立")]:
        b = accounts[acc]
        for r in b["rows"]:
            mk = (" " + " ".join(r["marks"])) if r["marks"] else ""
            qty_s = f"{r['qty']:.4f}".rstrip("0").rstrip(".")
            lines.append(
                f"| {acc} | **{_md(r['symbol'], 10)}**{mk} | {qty_s} | {_fusd(r['cost'])} | "
                f"{r['px_txt']} | "
                f"{_fusd(r['day'], True)} | {_fusd(r['pl'], True)}" + (f"（{_fpct(r['plp'])}）" if r["plp"] is not None else "") +
                f" | {_md(r['dist'], 80)} |"
            )
        lines.append(f"| {acc} 小计 |  |  | {_fusd(b['cost'])} | 市值 {_fusd(b['mv'])} | {_fusd(b['day'], True)} | {_fusd(b['pl'], True)} |  |")
    total_mv = sum(b["mv"] for b in accounts.values())
    total_day = sum(b["day"] for b in accounts.values())
    total_pl = sum(b["pl"] for b in accounts.values())
    capital = holdings["capital"]
    cash = holdings["cash"]
    assets = total_mv + (cash if cash is not None else 0)
    vs = assets - capital if capital else None
    vsp = (assets / capital - 1) * 100 if capital else None
    lines += ["", f"**合计**（起点 {_fusd(capital)}）：总资产 {_fusd(assets)}（持仓市值 {_fusd(total_mv)} + 现金 {_fusd(cash)}），相对起点 {_fusd(vs, True)}（{_fpct(vsp)}），今日 {_fusd(total_day, True)}。"]
    held_lev = [s for s in held_syms if _is_levered_sym(s, data)]
    return "\n".join(lines), held_syms, held_lev


def build_push_text(data, ai_ok, holdings=None):
    """微信推送（Server酱，Markdown）：持有仓（仅当提供 HOLDINGS_JSON）、观察仓买点与技术止盈止损、杠杆只拿一只、定投、涨跌榜、大新闻、自动换股、网页链接。
    持仓只来自环境变量，不会写入 data.json。未提供或无效则跳过持有仓。"""
    plan = data.get("plan") or {}
    today = (data.get("updated_at_beijing") or "")[:10]
    if holdings is None:
        holdings = load_holdings_for_push()
    held_sec, held_syms, held_lev = _build_holdings_push(data, holdings)
    held_set = {_canon(s) for s in (held_syms or [])}
    held_lev_set = {_canon(s) for s in (held_lev or [])}
    out = []
    ms = ((data.get("radar") or {}).get("market_summary") or {}).get("headline")
    out.append(f"**美股收盘**（北京时间 {data.get('updated_at_beijing') or ''} 更新）" + (f"\n{_md(ms, 90)}" if ms else ""))
    if held_sec:
        out.append(held_sec)

    rows = [r for r in (plan.get("buy_table") or []) if isinstance(r, dict) and r.get("symbol")]
    live = sorted([r for r in rows if not r.get("watch_only")],
                  key=lambda r: (r.get("sort_gap_pct") is None, r.get("sort_gap_pct") if r.get("sort_gap_pct") is not None else 0))
    ordered = live + [r for r in rows if r.get("watch_only")]
    if ordered:
        lines = ["### 👀 观察仓（按买点缺口排序）", "", "| 标的 | 现价 | 日涨跌 | 最近买点 | 距买点 | 止损/止盈 |", "|---|---|---|---|---|---|"]
        for r in ordered:
            s = _canon(r.get("symbol"))
            observe_only = bool(r.get("kind") == "levered" and held_lev_set and s not in held_lev_set)
            mark = ""
            if s in held_set:
                mark += " 已持有"
            if r.get("watch_only"):
                mark += " 👀只看不买"
            elif observe_only:
                mark += " 只观察"
            elif r.get("newly_touched"):
                mark += " 🔔到价待复核" + "/".join(_fpx(x) for x in r["newly_touched"])
            elif r.get("at_buy"):
                mark += " ⚠️到价待复核"
            if r.get("swapped_in"):
                mark += " 🆕换入"
            sltp = ""
            px0 = _session_px(r)
            rs = _resolve_sltp(r, px0)
            if rs["sl"] or rs["tps"]:
                dsl = dtp = ""
                if rs["sl"] and px0:
                    d_ = _dist_to_stop(px0, rs["sl"])
                    dsl = f"，再跌 {d_:.1f}%（分母：常规时段现价）" if d_ > 0 else "，常规时段现价已低于它"
                if rs["tps"] and px0:
                    d_ = _dist_to_tp(px0, rs["tps"][0])
                    dtp = f"，再涨 {d_:.1f}%（分母：常规时段现价）" if d_ > 0 else "，常规时段现价已高于它"
                sltp = (f"止损 {_fpx(rs['sl'])}（{rs['sl_src'] or '—'}{dsl}）" if rs["sl"] else "止损 —") + " / " + \
                       (f"止盈 {_tp_tier_text(rs['tps'], px0)}（{rs['tp_src'] or '—'}{dtp}）" if rs["tps"] else "止盈 —")
                ref = rs.get("ref") or {}
                if ref.get("hidden"):
                    sltp += "<br>参考：暂不显示（复权未核实，不编造）"
                elif ref.get("sl") or ref.get("tp"):
                    same_sl = (rs["sl"] is not None and ref.get("sl") is not None and abs(float(rs["sl"]) - float(ref["sl"])) < 1e-6)
                    same_tp = (bool(rs["tps"]) and ref.get("tp") is not None and len(rs["tps"]) == 1 and abs(float(rs["tps"][0]) - float(ref["tp"])) < 1e-6)
                    if not (same_sl and (same_tp or not rs["tps"])):
                        rsl = f"止损 {_fpx(ref['sl'])}" if ref.get("sl") else "止损 暂无参考"
                        rtp = f"止盈 {_fpx(ref['tp'])}" if ref.get("tp") else "止盈 暂无参考"
                        sltp += f"<br>参考：{rsl} / {rtp}"
                elif rs["sl"] or rs["tps"]:
                    sltp += "<br>参考：暂无参考"
                conflicts = [p for p in (r.get("proposed_tranches") or []) if isinstance(p, dict) and p.get("conflict")]
                if conflicts:
                    sltp += "<br>补充价与有效止损冲突，不作为加仓"
            gp = r.get("gap_pct")
            if r.get("watch_only") or observe_only:
                gap = "不提示"
            elif gp is None:
                gap = "—"
            elif gp <= 0:
                gap = "到价待复核"
            else:
                gap = f"再跌 {gp:.1f}%"
            ref_t = _ext_ref_text(r)
            ref_t = f"<br>参考：{_md(ref_t, 40)}" if ref_t else ""
            lines.append(f"| **{_md(r['symbol'], 10)}**{mark} | {_px_cell(r)}{ref_t} | {_fpct(r.get('change_pct'), 2)} | {_fpx(r.get('nearest_level'))} | {gap} | {sltp or '—'} |")
        notes = []
        for r in ordered:
            if r.get("kind") == "nvda" and r.get("watch_only"):
                notes.append(f"NVDA：{NVDA_EARNINGS} 财报前只看不买，财报后再按买点梯子提醒。")
            if r.get("kind") == "brk":
                notes.append(f"BRK.B：{BRK_DECIDE_BY} 前决定要不要买（已买入请忽略，网页里标记后会隐藏）。")
            if r.get("symbol") == "ADBE" and today and today <= ADBE_EARNINGS_BJ:
                notes.append(f"ADBE：财报约北京时间 {ADBE_EARNINGS_BJ} 早上。")
        if notes:
            lines += [""] + [f"- {n}" for n in notes]
        if any(r.get("ref_sl") or r.get("ref_tp") or r.get("fixed_sl") or r.get("fixed_tp") for r in ordered):
            lines.append("\n止盈止损分两层：执行价（我的＞已定＞技术参考，触发和距离都用它）和最新参考价（技术计算，有则另列，不会自动改执行价）。距离都以常规时段现价为分母。日涨跌是常规时段，盘后另标。不是下单指令。已持有的杠杆 ETF 和 NVDA 触及卖点时，按你已定的计划执行。")
        out.append("\n".join(lines))

    lev = plan.get("levered") or {}
    lev_syms = [s for s in (lev.get("symbols") or []) if any(_canon(r.get("symbol")) == _canon(s) for r in ordered)]
    if not lev_syms:
        lev_syms = [_canon(r.get("symbol")) for r in ordered if r.get("kind") == "levered"]
    if lev_syms:
        txt = f"### ♟️ 杠杆 ETF（{'、'.join(lev_syms)}）\n同一时间只拿一只，买了之后另外几只只观察。"
        if held_lev:
            others = [s for s in lev_syms if _canon(s) not in held_lev_set]
            txt += f"\n当前持有：**{'、'.join(held_lev)}**" + (f"，{_md('、'.join(others), 40)} 只观察、不提示买入。" if others else "。")
        else:
            reached = [s for s in (lev.get("reached_buy") or []) if _canon(s) in {_canon(x) for x in lev_syms}]
            if reached:
                txt += f"\n到价待复核：**{'、'.join(reached)}**" + ("。三只杠杆 ETF 最多同时持有一只，这不是买入指令。" if len(reached) > 1 else "。这不是买入指令。")
        out.append(txt)

    dca = [r for r in (plan.get("dca") or []) if isinstance(r, dict) and r.get("symbol")]
    if dca:
        cells = "｜".join(f"{_md(r['symbol'], 8)} {('$' + _fpx(r.get('price'))) if r.get('price') is not None else '—'} {_fpct(r.get('change_pct'), 2)}" + (f"（参考：{_md(_ext_ref_text(r), 40)}）" if _ext_ref_text(r) else "") for r in dca)
        out.append(f"### 📌 定投\n{cells}\n每批执行完请取消定投，{DCA_DEADLINE[:4]} 年底前做完。")

    mv = plan.get("sp500_movers") or {}
    mv_lines = []
    for tag, key in (("📈 涨", "gainers"), ("📉 跌", "losers")):
        for x in (mv.get(key) or []):
            reason = _real_reason(x.get("reason"))
            nm = _md(x.get("name"), 14)
            mv_lines.append(f"- {tag} **{_md(x.get('ticker'), 8)}** {_fpct(x.get('change_pct'))}" + (f" {nm}" if nm else "") + (f"：{_md(reason, 50)}" if reason else ""))
    if mv_lines:
        sd = mv.get("session_date")
        out.append(f"### 📊 标普 500 涨跌前 3{f'（{sd}）' if sd else ''}\n" + "\n".join(mv_lines))

    news = [n for n in (plan.get("big_news") or []) if isinstance(n, dict) and n.get("summary")][:3]
    if news:
        nl = []
        for n in news:
            title = _md(n["summary"], 70)
            url = _http_url(n.get("url"))
            nl.append(f"- [{title}]({url})" if url else f"- {title}")
        head = "### 🗞️ 大新闻" + ("（AI 今日未更新，沿用上次）" if (plan.get("ai_stale") or not ai_ok) else "")
        out.append(head + "\n" + "\n".join(nl))
    elif not ai_ok:
        out.append("（今日 AI 未更新：涨跌原因和大新闻暂无。）")

    swaps = [e for e in (plan.get("swap_log") or []) if e.get("date") == today]
    if swaps:
        sl = [f"- {_md(e.get('out'), 8)} → **{_md(e.get('in'), 8)}** {_md(e.get('in_name'), 20)}" + (f"：{_md(e.get('reason'), 50)}" if e.get("reason") else "") for e in swaps]
        out.append("### 🔁 今日自动换股\n" + "\n".join(sl))

    url = os.environ.get("PAGES_URL", "").strip() or DEFAULT_PAGES_URL
    out.append(f"[打开完整雷达页]({url})\n\n{PLAN_DISCLAIMER}")
    text = "\n\n".join(out)
    return text[:30000]


def push_title(data, holdings=None):
    """标题（Server酱限 32 字）：日期 + 新触及买点或卖点提醒（不含金额）。"""
    plan = data.get("plan") or {}
    d = (data.get("updated_at_beijing") or "")[5:10]
    title = f"每日雷达 {d}"
    if holdings is None:
        holdings = load_holdings_for_push()
    sell = []
    if holdings:
        for pos in holdings.get("positions") or []:
            rec = _lookup_quote(data, pos["symbol"]) or _plan_row_for(data, pos["symbol"]) or {}
            px = rec.get("price")
            rs = _sltp_for_position(pos, data)
            sl, tps = rs["sl"], rs["tps"]
            close_px = _official_close(rec, _plan_row_for(data, pos["symbol"]) or {})
            if close_px is None:
                continue
            if sl and float(close_px) <= float(sl) + 1e-9:
                sell.append(pos["symbol"])
            elif tps and float(close_px) >= float(tps[0]) - 1e-9:
                sell.append(pos["symbol"])
    if sell:
        title += " 🚨" + "、".join(sell[:3])
    else:
        hit = [r["symbol"] for r in (plan.get("buy_table") or []) if r.get("newly_touched") and not r.get("watch_only")]
        if hit:
            title += " 🔔" + "、".join(hit[:3])
    return title[:32]


def push_serverchan(data, radar_ok):
    """Server酱推送；任何失败都静默（只打一行日志，不让任务变红）。"""
    key = os.environ.get("SERVERCHAN_KEY", "").strip()
    if not key:
        log("未设置 SERVERCHAN_KEY，跳过微信推送。")
        return
    try:
        # 持仓只在这里读入推送正文，绝不写入 data 或日志
        holdings = load_holdings_for_push()
        r = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                          data={"title": push_title(data, holdings), "desp": build_push_text(data, radar_ok, holdings)}, timeout=15)
        log(f"Server酱推送：HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        log(f"Server酱推送未成功（已忽略）：{str(e)[:80]}")


def _alias_prices(prices, errors):
    """把 BRK-B / BRK.B 等别名统一写进 prices，方便页面查找。"""
    out = dict(prices)
    err = dict(errors)
    for spec in PLAN_SPECS + RETIRED_SPECS + DCA_SPECS:
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
    load_fixed_sltp(cfg)
    init_plan_state(prev_plan, beijing_date)

    # 自选 + 计划标的（计划标的拉盘后价）
    plan_yf = []
    for s in PLAN_SPECS + RETIRED_SPECS + DCA_SPECS:
        if s["yf"] not in plan_yf:
            plan_yf.append(s["yf"])
    watch = effective_watchlist(cfg["watchlist"])
    # 先拉自选（不含已在计划里的，避免重复且计划需要盘后价）
    watch_only = [s for s in watch if s not in plan_yf and s.replace(".", "-") not in plan_yf
                  and s.replace("-", ".") not in {x["symbol"] for x in PLAN_SPECS}]
    log("拉取自选行情…")
    prices, errors = fetch_prices(watch_only, prev_real.get("prices"), want_extended=False)
    log("拉取交易计划标的（含盘后价，如有）…")
    p2, e2 = fetch_prices(plan_yf, prev_real.get("prices"), want_extended=True)
    # 以展示代码写入（BRK.B 而不是 BRK-B）
    for spec in PLAN_SPECS + RETIRED_SPECS + DCA_SPECS:
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

    radar, ai = None, {"provider": None, "model": None, "ok": False, "error": None}
    if args.skip_ai:
        ai = {"provider": None, "model": None, "ok": False, "error": "已跳过 AI（调试）"}
        warn("已按 --skip-ai 跳过模型调用")
    else:
        log("调用 AI 一次（联网，只查涨跌榜原因和重大新闻）…")
        radar, ai = run_ai(cfg, today_bj, movers)
    radar_date = now.strftime("%Y-%m-%d")
    stale = False
    old_plan = prev_plan or {}
    if radar is None:
        if not args.skip_ai:
            warn(f"AI 调用失败：{ai['error']}（行情、买点和技术参考照常更新；原因/新闻沿用上次）")
        stale = True
        old_m = (old_plan.get("sp500_movers") or {})
        old_reasons = {}
        for row in (old_m.get("gainers") or []) + (old_m.get("losers") or []):
            if row.get("ticker") and row.get("reason"):
                old_reasons[str(row["ticker"]).upper()] = row["reason"]
        movers = _attach_reasons(movers, old_reasons)
        ai_bits = {"big_news": old_plan.get("big_news") or [], "opportunities": []}
        radar = {"sources": (prev_real.get("radar") or {}).get("sources") or []}
    else:
        movers = _attach_reasons(movers, radar.get("sp500_mover_reasons") or {})
        apply_auto_swap(radar.get("opportunity"), beijing_date, prices, errors)
        ai_bits = {"big_news": radar.get("big_news") or [], "opportunities": []}

    plan = build_plan(prices, errors, prev_plan, beijing_date, movers, ai_bits, stale_ai=stale)
    movers_pub, _cons = _strip_constituents(movers)
    plan["sp500_movers"] = {
        "session_date": movers_pub.get("session_date"),
        "gainers": movers_pub.get("gainers") or [],
        "losers": movers_pub.get("losers") or [],
        "error": movers_pub.get("error"),
    }

    # radar 字段保持旧结构（页面兼容）：隔夜摘要本地拼；自选逐只新闻已取消
    radar_out = {
        "market_summary": local_market_summary(indices),
        "watchlist_news": [],
        "unusual_movers": [],
        "big_news": plan.get("big_news") or [],
        "swap_proposal": None,
        "opportunities": [],
        "sources": radar.get("sources") or [],
    }
    stale = False  # 隔夜摘要是今天本地算的；原因/新闻是否旧见 plan.ai_stale

    data = {
        "example": False,
        "schema": 2,
        "updated_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at_beijing": today_bj,
        "watchlist": effective_watchlist(cfg["watchlist"]),
        "names": dict(cfg.get("names", {}), **{s["yf"]: s["name"] for s in PLAN_SPECS if s.get("from")}),
        "prices": prices,
        "price_errors": errors,
        "indices": indices,
        "fx": fx,
        "plan": plan,
        "radar": radar_out,
        "radar_date": radar_date,
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
