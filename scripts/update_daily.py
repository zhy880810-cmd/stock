#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日更新脚本：拉行情 + 汇率 + 让 AI 联网生成中文「每日雷达」，写入 data.json。

- 不含任何持仓数量/成本/金额（隐私默认）。
- AI 失败不影响行情：保留价格，并在 data.json 里写入错误说明。
- 仅供学习与记录参考，不构成投资建议。

环境变量：
  AI_PROVIDER      gemini（默认）或 grok
  GEMINI_API_KEY   / GEMINI_MODEL（默认 gemini-3.8-flash）
  XAI_API_KEY      / XAI_MODEL（默认 grok-4.3）/ XAI_BASE_URL（默认 https://api.x.ai/v1）
  SERVERCHAN_KEY   可选，Server酱推送（不含金额）
  PAGES_URL        可选，推送里附带的网页地址
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BJ = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISCLAIMER = "仅供学习与记录参考，不构成投资建议。"


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


# ------------------------------------------------------------------ 行情
def guess_currency(sym):
    s = sym.upper()
    if s.endswith(".HK"):
        return "HKD"
    if s.endswith(".SS") or s.endswith(".SZ"):
        return "CNY"
    return "USD"


def fetch_one(sym):
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
        return {
            "price": round(last, 4),
            "prev_close": round(prev, 4) if prev else None,
            "change_pct": round(pct, 2) if pct is not None else None,
            "date": date,
            "currency": cur,
        }

    return retry(go, tries=3, delay=3, what=f"获取 {sym}")


def fetch_prices(symbols, prev_prices):
    prices, errors = {}, {}
    for sym in symbols:
        try:
            prices[sym] = fetch_one(sym)
            log(f"  {sym}: {prices[sym]['price']} ({prices[sym]['change_pct']}%) {prices[sym]['date']}")
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


# ------------------------------------------------------------------ AI
SYSTEM = ("你是一名谨慎的中文市场信息整理助手。你必须通过联网搜索核实信息，只报道你在搜索结果里确实看到的内容；"
          "查不到或无法确认时，明确写“未能核实”，绝不编造数字、新闻或来源。你的输出仅供学习参考，不构成投资建议。")

SCHEMA_TEXT = """{
  "market_summary": {
    "headline": "一句话总结隔夜美股（不超过40字）",
    "overview": "隔夜美股三大指数表现及原因，100-250字",
    "macro": ["美联储/利率/通胀/就业等宏观要闻，每条一句话，2-4条"]
  },
  "watchlist_news": [
    {"symbol": "关注列表中的代码", "summary": "过去24小时与该标的相关的重要消息，没有则写“暂无重大消息”", "sentiment": "利好|利空|中性"}
  ],
  "unusual_movers": [
    {"ticker": "代码", "name": "公司名", "move": "涨跌幅，如 +12.3%，无法核实写“未核实”", "reason": "异动原因，一两句话", "relevance": "与关注列表的关联度，一句话"}
  ]
}"""


def build_prompt(cfg, today_bj, prices, indices, fx):
    wl = cfg["watchlist"]
    names = cfg.get("names", {})
    rcfg = cfg.get("radar", {})
    lo, hi = rcfg.get("movers_min", 3), rcfg.get("movers_max", 5)
    wl_lines = []
    for s in wl:
        p = prices.get(s)
        extra = f"，最近收盘 {p['price']} {p.get('currency','')}，涨跌 {p['change_pct']}%（{p['date']}）" if p and p.get("change_pct") is not None else ""
        wl_lines.append(f"- {s}（{names.get(s, '')}）{extra}")
    idx_lines = [f"- {n}（{k}）：{v['price']}，涨跌 {v['change_pct']}%（{v['date']}）"
                 for k, v in indices.items() for n in [v.get('name', k)] if v.get("change_pct") is not None]
    return f"""今天是北京时间 {today_bj}。请联网搜索，为我生成一份中文「每日盘前雷达」。

我的关注列表（只是代码，不涉及持仓）：
{chr(10).join(wl_lines)}

已核实的指数收盘数据（来自行情接口，请优先采用）：
{chr(10).join(idx_lines) if idx_lines else '（暂无）'}

要求：
1. market_summary：总结最近一个美股交易日（隔夜）的美股表现及主要原因，并列出美联储/宏观重要新闻。
2. watchlist_news：对关注列表里的每个代码，各给一条过去24小时的相关消息（ETF 可写其跟踪指数/成分股的相关消息）。
3. unusual_movers：找出过去24小时美股内成交额放大、盘前/盘后/盘中大幅涨跌的 {lo}-{hi} 只个股，必须是不在上述关注列表里的股票，并说明异动原因，以及与关注列表（如半导体/AI/大盘）的关联。
4. 全部用简体中文。不确定就写“未能核实”，不要编造。
5. 只输出一个 JSON 对象，不要任何多余文字、不要 Markdown 代码块。JSON 结构如下：
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


def validate_radar(obj, cfg):
    """校验并整理 AI 输出；不合格时抛出异常。"""
    if not isinstance(obj, dict):
        raise ValueError("JSON 顶层不是对象")
    ms = obj.get("market_summary")
    if not isinstance(ms, dict) or not _s(ms.get("overview")):
        raise ValueError("缺少 market_summary.overview")
    macro = ms.get("macro") or []
    if isinstance(macro, str):
        macro = [macro]
    wl = [s.upper() for s in cfg["watchlist"]]
    news = []
    for it in obj.get("watchlist_news") or []:
        if isinstance(it, dict) and _s(it.get("symbol")) and _s(it.get("summary")):
            sent = _s(it.get("sentiment"), 4)
            news.append({"symbol": _s(it["symbol"], 12).upper(), "summary": _s(it["summary"]),
                         "sentiment": sent if sent in ("利好", "利空", "中性") else "中性"})
    movers = []
    for it in obj.get("unusual_movers") or []:
        if not isinstance(it, dict) or not _s(it.get("ticker")):
            continue
        tk = _s(it["ticker"], 12).upper()
        if tk in wl:  # 要求是“未关注”的股票
            continue
        movers.append({"ticker": tk, "name": _s(it.get("name"), 60), "move": _s(it.get("move"), 20) or "未核实",
                       "reason": _s(it.get("reason")), "relevance": _s(it.get("relevance"))})
    hi = cfg.get("radar", {}).get("movers_max", 5)
    return {
        "market_summary": {"headline": _s(ms.get("headline"), 80), "overview": _s(ms.get("overview"), 1500),
                           "macro": [_s(m, 300) for m in macro if _s(m)][:6]},
        "watchlist_news": news,
        "unusual_movers": movers[:hi],
    }


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
    model = os.environ.get("XAI_MODEL", "").strip() or "grok-4.3"
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


def run_ai(cfg, today_bj, prices, indices, fx):
    provider = (os.environ.get("AI_PROVIDER", "") or "gemini").strip().lower()
    if provider in ("xai", "grok"):
        provider, fn = "grok", call_grok
    elif provider == "gemini":
        fn = call_gemini
    else:
        return None, {"provider": provider, "model": None, "ok": False,
                      "error": f"不认识的 AI_PROVIDER：{provider}（只能填 gemini 或 grok）"}
    prompt = build_prompt(cfg, today_bj, prices, indices, fx)
    model = (os.environ.get("XAI_MODEL", "").strip() or "grok-4.3") if provider == "grok" else (os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.8-flash")
    last = None
    for i in range(2):  # 最多尝试 2 次（含 JSON 格式不合格时重试）
        try:
            text, sources, model = fn(prompt)
            radar = validate_radar(extract_json(text), cfg)
            seen, src = set(), []
            for s in sources:
                if s["url"] not in seen:
                    seen.add(s["url"])
                    src.append(s)
            radar["sources"] = src[:12]
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
    radar = data.get("radar")
    if radar and radar_ok:
        if radar["market_summary"].get("headline"):
            lines.insert(0, radar["market_summary"]["headline"] + "\n")
        if radar["unusual_movers"]:
            lines.append("\n异动：" + "、".join(f"{m['ticker']} {m['move']}" for m in radar["unusual_movers"]))
    elif not radar_ok:
        lines.append("\n（今日 AI 雷达生成失败，只更新了行情）")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    url = os.environ.get("PAGES_URL", "").strip()
    if not url and "/" in repo:
        o, n = repo.split("/", 1)
        url = f"https://{o}.github.io/{n}/"
    if url:
        lines.append(f"\n查看详情：{url}")
    lines.append(f"\n{DISCLAIMER}")
    title = f"每日雷达 {data['updated_at_beijing'][:10]}"
    try:
        r = requests.post(f"https://sctapi.ftqq.com/{key}.send", data={"title": title, "desp": "\n".join(lines)}, timeout=15)
        log(f"Server酱推送：HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        warn(f"Server酱推送失败：{e}")


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data.json"))
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    cfg = load_json(args.config)
    if not cfg or not isinstance(cfg.get("watchlist"), list) or not cfg["watchlist"]:
        warn("config.json 读取失败或 watchlist 为空")
        return 1
    cfg["watchlist"] = [str(s).strip().upper() for s in cfg["watchlist"] if str(s).strip()]
    prev = load_json(args.out, {}) or {}
    prev_real = prev if not prev.get("example") else {}

    now = datetime.now(BJ)
    today_bj = now.strftime("%Y-%m-%d %H:%M")
    log(f"== 每日更新 {today_bj}（北京时间）==")

    log("拉取自选行情…")
    prices, errors = fetch_prices(cfg["watchlist"], prev_real.get("prices"))
    log("拉取指数…")
    idx_raw, idx_err = fetch_prices(list((cfg.get("indices") or {}).keys()), prev_real.get("indices"))
    indices = {k: dict(v, name=cfg["indices"].get(k, k)) for k, v in idx_raw.items()}
    log("拉取汇率…")
    fx = fetch_fx() or prev_real.get("fx", {})

    log("生成每日雷达（AI 联网）…")
    radar, ai = run_ai(cfg, today_bj, prices, indices, fx)
    radar_date = now.strftime("%Y-%m-%d")
    stale = False
    if radar is None:
        warn(f"AI 雷达生成失败：{ai['error']}（行情照常更新）")
        old = prev_real.get("radar")
        if old:  # 保留上一次的雷达内容，并标记为旧内容
            radar, stale = old, True
            radar_date = prev_real.get("radar_date") or (prev_real.get("updated_at_beijing") or "")[:10]
    data = {
        "example": False,
        "schema": 1,
        "updated_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at_beijing": today_bj,
        "watchlist": cfg["watchlist"],
        "names": cfg.get("names", {}),
        "prices": prices,
        "price_errors": errors,
        "indices": indices,
        "fx": fx,
        "radar": radar,
        "radar_date": radar_date if radar else None,
        "radar_stale": stale,
        "ai": ai,
        "disclaimer": DISCLAIMER,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log(f"已写入 {args.out}：{len(prices)} 个价格，AI {'成功' if ai['ok'] else '失败'}")

    if not args.no_push:
        push_serverchan(data, ai["ok"])
    if not prices:
        warn("一个价格都没拿到（网络或接口问题）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
