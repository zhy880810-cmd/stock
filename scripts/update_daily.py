#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日更新脚本：拉行情 + 汇率 + 让 AI 联网生成中文「每日雷达 / 交易计划」，写入 data.json。

- 不含任何持仓数量/成本/金额（隐私默认）。公开文件只写代码、价格、买点缺口和新闻。
- 买点表、定投收盘价、距买点跌幅、标普 500 涨跌幅、止损/止盈/补充批次（技术参考：ATR + 近期高低点）均在本地计算，不由模型编造。
- AI（Grok / Gemini）整个脚本只调用一次，只负责：标普 500 涨跌榜每只一句原因、1–3 条大新闻（各附一个链接）、至多一条明显更好的机会（只给代码和理由，价格仍由本地算）。
- 明显更好的机会会由脚本直接自动替换计划里被点名的标的（无需用户确认）：当前计划写在 data.json 的 plan.active（只有代码、买点、名称），config.json 只是首次的种子；替换记录写在 plan.swap_log。被换出的标的进入 plan.retired（网页上只在你标记已买入/持仓里有它时继续显示止损止盈）。
- 若设置了 SERVERCHAN_KEY，每次运行后推送一条简短中文摘要到微信（不含任何持仓金额）。
- AI 失败不影响行情：保留价格，并在 data.json 里写入错误说明。
- 仅供学习与记录参考，不构成投资建议。

环境变量：
  AI_PROVIDER      gemini（默认）或 grok
  GEMINI_API_KEY   / GEMINI_MODEL（默认 gemini-3.8-flash）
  XAI_API_KEY      / XAI_MODEL（默认 grok-4.7）/ XAI_BASE_URL（默认 https://api.x.ai/v1）
  SERVERCHAN_KEY   可选，Server酱推送（不含金额）
  PAGES_URL        可选，推送里附带的网页地址（默认 https://stock-6d3.pages.dev）
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
    """附带近一年/三个月/20 日高低和 ATR14，用于本地计算技术参考（不调用模型）。"""
    try:
        import pandas as pd
        h = t.history(period="1y", auto_adjust=False)
        if h is None or h.empty:
            return rec
        need = [c for c in ("High", "Low", "Close") if c in h.columns]
        if len(need) < 3:
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
        l20 = h.tail(20)
        rec["high_20d"] = round(float(l20["High"].max()), 4)
        rec["low_20d"] = round(float(l20["Low"].min()), 4)
        prev = h["Close"].shift(1)
        tr = pd.concat([h["High"] - h["Low"], (h["High"] - prev).abs(), (h["Low"] - prev).abs()], axis=1).max(axis=1)
        atr = tr.tail(14).mean()
        if atr == atr and atr > 0:
            rec["atr14"] = round(float(atr), 4)
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
        active.append(spec)
        seen.add(sym)
    if not active:
        active = [dict(s) for s in SEED_SPECS if _canon(s["symbol"]) not in dropped]
    else:
        for sym, s in seed.items():
            if sym not in seen and sym not in dropped:
                active.append(dict(s))
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
    """Grok 点名一个明显更好的机会：直接替换计划里被点名的标的（不需要用户确认）。
    买点梯子和止盈止损由本地按历史价格计算。返回替换记录，没有替换返回 None。"""
    if not opp or not opp.get("ticker"):
        return None
    tk, vs = _canon(opp["ticker"]), _canon(opp.get("vs"))
    if not vs:
        log("  机会没有指明要替换的现有标的，忽略。")
        return None
    idx = next((i for i, s in enumerate(PLAN_SPECS) if _canon(s["symbol"]) == vs), None)
    if idx is None:
        log(f"  机会要替换的 {vs} 不在当前计划里，忽略。")
        return None
    if not re.match(r"^[A-Z]{1,5}(\.[A-Z])?$", tk) or any(_canon(s["symbol"]) == tk for s in PLAN_SPECS) \
            or tk in {_canon(s["symbol"]) for s in DCA_SPECS}:
        log(f"  机会代码 {tk} 不适用，忽略。")
        return None
    recent = [e for e in SWAP_LOG if _days_between(e.get("date") or "2000-01-01", today) <= SWAP_NO_FLIPBACK_DAYS]
    if len(recent) >= SWAP_MAX_PER_14D:
        log("  近 14 天自动替换已达上限，今天不换。")
        return None
    if any(_canon(e.get("out")) == tk for e in recent):
        log(f"  {tk} 刚被换出，不换回。")
        return None
    out_spec = PLAN_SPECS[idx]
    if out_spec.get("added") and _days_between(out_spec["added"], today) < SWAP_COOLDOWN_DAYS:
        log(f"  {vs} 刚加入计划不到 {SWAP_COOLDOWN_DAYS} 天，不换。")
        return None
    yf_sym = tk.replace(".", "-")
    try:
        rec = fetch_one(yf_sym, want_extended=True)
    except Exception as e:  # noqa: BLE001
        warn(f"自动替换：{tk} 行情获取失败，不替换：{e}")
        return None
    price = rec.get("price")
    tech = compute_tech_levels(price, [], rec)
    if not tech:
        return None
    levels = sorted(set([_px(price)] + tech["extra_tranches"]), reverse=True)
    why = _s(opp.get("why"), 200)
    new_spec = {"symbol": tk, "yf": yf_sym, "name": _s(opp.get("name"), 60) or tk, "levels": levels,
                "kind": "levered" if LEVERED_RE.match(tk) else "stock", "added": today, "from": vs, "why": why}
    PLAN_SPECS[idx] = new_spec
    RETIRED_SPECS[:] = [r for r in RETIRED_SPECS if _canon(r["symbol"]) != vs]
    RETIRED_SPECS.append({"symbol": vs, "yf": out_spec["yf"], "name": out_spec.get("name") or vs,
                          "levels": list(out_spec["levels"]), "kind": out_spec.get("kind") or "stock",
                          "retired_on": today, "replaced_by": tk})
    del RETIRED_SPECS[:-8]
    if vs not in DROPPED:
        DROPPED.append(vs)
    entry = {"date": today, "out": vs, "in": tk, "in_name": new_spec["name"], "reason": why, "url": _http_url(opp.get("url"))}
    SWAP_LOG.append(entry)
    del SWAP_LOG[:-20]
    prices[tk] = rec
    prices[yf_sym] = rec
    log(f"  已自动替换：{vs} → {tk}（买点 {levels}）")
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
        "high_20d": _r((rec or {}).get("high_20d")),
        "low_20d": _r((rec or {}).get("low_20d")),
        "atr14": _r((rec or {}).get("atr14")),
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


def compute_tech_levels(price, levels, st):
    """本地用历史价格算技术参考：止损、止盈、补充批次。不调用模型，不是新闻判断。
    st 需包含 atr14、low_3m/high_3m、low_20d、low_52w/high_52w（缺哪项就少用哪项）。"""
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if not price > 0:
        return None
    atr = (st or {}).get("atr14")
    try:
        atr = float(atr) if atr and float(atr) > 0 else price * 0.04
    except (TypeError, ValueError):
        atr = price * 0.04
    core = sorted([float(x) for x in (levels or []) if x and float(x) > 0], reverse=True)
    extras = []
    if not core:
        # 没有既定梯子（例如新机会）：用现价和 1、2 倍 ATR 下方作为参考批次
        core = [price]
        extras = [price - atr, price - 2 * atr]
    low_core, high_core = min(core + extras), max(core)
    avg_core = sum(core + extras) / len(core + extras)
    sl = low_core - (1.5 if extras else 1.5) * atr
    l3, h3 = (st or {}).get("low_3m"), (st or {}).get("high_3m")
    l20 = (st or {}).get("low_20d")
    for sw in (l3, l20):
        try:
            if sw and 0 < float(sw) < low_core:
                sl = min(sl, float(sw) - 0.5 * atr)
        except (TypeError, ValueError):
            pass
    tp = avg_core + 3 * atr
    try:
        if h3 and float(h3) > avg_core * 1.05:
            tp = max(avg_core + 2 * atr, min(float(h3), avg_core + 6 * atr))
    except (TypeError, ValueError):
        pass
    tp = max(tp, high_core * 1.03)
    if not extras:
        e1 = low_core - atr
        cand = [e1]
        try:
            if l3 and 0 < float(l3) < low_core - 0.3 * atr:
                cand.append(float(l3))
        except (TypeError, ValueError):
            pass
        for c in sorted(set(round(c, 4) for c in cand), reverse=True)[:2]:
            if c > sl + 0.3 * atr and not any(_near(c, k) for k in core):
                extras.append(c)
    sl = max(sl, 0.01)
    f = lambda v: (f"{v:.2f}" if v is not None else "—")
    reason = (f"技术参考（本地按历史价格计算，非 AI/新闻）：ATR14≈{f(atr)}；近三月低/高 {f(l3)}/{f(h3)}；"
              f"近一年低/高 {f((st or {}).get('low_52w'))}/{f((st or {}).get('high_52w'))}。"
              f"止损=最深买点下约 1.5 倍 ATR（并避开近期低点），止盈=阶梯均价上方阻力或约 3 倍 ATR。")
    return {
        "stop_loss": _px(sl),
        "take_profit": _px(tp),
        "extra_tranches": [_px(x) for x in extras],
        "reason": reason[:300],
        "url": "",
        "source": "technical",
    }


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
            "source": prev_row.get("sl_tp_source") or "",
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
    row["sl_tp_source"] = a.get("source") or row.get("sl_tp_source") or "technical"
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
    retired_rows = []
    prev_ret = {r.get("symbol"): r for r in (prev_plan or {}).get("retired_table") or [] if isinstance(r, dict)}
    for spec in RETIRED_SPECS:
        rec = prices.get(spec["symbol"]) or prices.get(spec["yf"])
        err = errors.get(spec["symbol"]) or errors.get(spec["yf"])
        r = build_buy_row(spec, rec, err, None, session, beijing_date)
        r.update({"retired": True, "at_buy": False, "newly_touched": [], "retired_on": spec.get("retired_on"),
                  "replaced_by": spec.get("replaced_by") or ""})
        retired_rows.append(r)
    adv = dict((ai_bits or {}).get("levels_advice") or {})
    for r in rows + retired_rows:
        if r["symbol"] in adv or not r.get("price"):
            continue
        tech = compute_tech_levels(r["price"], r.get("levels"), r)
        if tech:
            adv[r["symbol"]] = tech
    ai_bits = dict(ai_bits or {}, levels_advice=adv)
    rows = [_merge_level_advice(r, ai_bits, prev_rows.get(r["symbol"])) for r in rows]
    retired_rows = [_merge_level_advice(r, ai_bits, prev_ret.get(r["symbol"])) for r in retired_rows]
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
        "active": [{k: s.get(k) for k in ("symbol", "yf", "name", "levels", "kind", "added", "from", "why") if s.get(k) not in (None, "")} for s in PLAN_SPECS],
        "retired": [dict(s) for s in RETIRED_SPECS],
        "swap_log": list(SWAP_LOG),
        "dropped": list(DROPPED),
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
            temperature=0.2,
            max_output_tokens=int(os.environ.get("AI_MAX_OUTPUT_TOKENS", "") or 2048),
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
        # 输出很短；上限同时覆盖推理 token，设太小会截断，所以留余量。可用 AI_MAX_OUTPUT_TOKENS 调整
        "max_output_tokens": int(os.environ.get("AI_MAX_OUTPUT_TOKENS", "") or 2000),
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


def run_ai(cfg, today_bj, movers):
    """整个脚本只调用一次模型（网络错误时最多再试一次；JSON 格式不对不重试，避免重复花费）。"""
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
    last = None
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
        try:
            radar = validate_radar(extract_json(text), cfg, movers)
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  AI 返回内容解析失败（不再重试）：{e}")
            break
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
        return radar, {"provider": provider, "model": model, "ok": True, "error": None}
    return None, {"provider": provider, "model": model, "ok": False, "error": str(last)[:300]}


def local_market_summary(indices):
    """隔夜美股摘要改由行情接口数字在本地拼出（不调用模型）。"""
    parts = []
    for v in (indices or {}).values():
        if v.get("change_pct") is not None:
            parts.append(f"{v.get('name','')} {v['change_pct']:+.2f}%")
    return {"headline": "；".join(parts)[:80], "overview": "", "macro": []}


# ------------------------------------------------------------------ Server酱
def build_push_text(data, ai_ok):
    """微信推送的简短中文摘要：新触及买点、自动替换、涨跌榜、大新闻标题、网页链接。不含任何持仓或金额。"""
    plan = data.get("plan") or {}
    today = (data.get("updated_at_beijing") or "")[:10]
    lines = []
    ms = ((data.get("radar") or {}).get("market_summary") or {}).get("headline")
    if ms:
        lines.append("美股收盘：" + ms)
    touched = []
    for r in plan.get("buy_table") or []:
        if r.get("watch_only") or not r.get("newly_touched"):
            continue
        touched.append(f"{r['symbol']} " + "/".join(str(x) for x in r["newly_touched"]))
    if touched:
        lines.append("🔔 新触及买点：" + "；".join(touched))
    reached = [s for s in ((plan.get("levered") or {}).get("reached_buy") or [])]
    if len(reached) > 1:
        lines.append("三只杠杆 ETF 只买一只，已到买点：" + "、".join(reached))
    swaps = [e for e in plan.get("swap_log") or [] if e.get("date") == today]
    if swaps:
        lines.append("🔁 已自动替换：" + "；".join(f"{e['out']}→{e['in']}（{(e.get('reason') or '')[:30]}）" for e in swaps))
    mv = plan.get("sp500_movers") or {}
    def fm(arr):
        return "、".join(f"{x['ticker']} {x['change_pct']:+.1f}%" for x in arr or [])
    if mv.get("gainers") or mv.get("losers"):
        lines.append(f"标普500 涨：{fm(mv.get('gainers'))}｜跌：{fm(mv.get('losers'))}")
    news = plan.get("big_news") or []
    if news and not plan.get("ai_stale"):
        lines.append("📰 大新闻：" + "；".join(n.get("summary", "")[:40] for n in news[:3] if n.get("summary")))
    if not ai_ok:
        lines.append("（今日 AI 未更新，涨跌原因/新闻沿用上次）")
    url = os.environ.get("PAGES_URL", "").strip() or DEFAULT_PAGES_URL
    lines.append("详情：" + url)
    lines.append(PLAN_DISCLAIMER)
    return "\n\n".join(lines)


def push_serverchan(data, radar_ok):
    """Server酱推送；任何失败都静默（只打一行日志，不让任务变红）。"""
    key = os.environ.get("SERVERCHAN_KEY", "").strip()
    if not key:
        log("未设置 SERVERCHAN_KEY，跳过微信推送。")
        return
    try:
        title = f"每日雷达 {(data.get('updated_at_beijing') or '')[:10]}"
        r = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                          data={"title": title[:32], "desp": build_push_text(data, radar_ok)}, timeout=15)
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
