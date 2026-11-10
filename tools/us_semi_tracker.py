# -*- coding: utf-8 -*-
"""川大帝MAGA：美股半導體指標與台積電 ADR 折溢價抓取引擎（Yahoo Finance，免金鑰）。

用法：
    C:/Users/singk/anaconda3/python.exe tools/us_semi_tracker.py --mode night_open     # 22:00–24:00 美股開盤／盤中
    C:/Users/singk/anaconda3/python.exe tools/us_semi_tracker.py --mode morning_close  # 07:00–08:00 美股收盤定錨
    C:/Users/singk/anaconda3/python.exe tools/us_semi_tracker.py --mode check_status   # 檢查今日早盤匯報狀態鎖
    C:/Users/singk/anaconda3/python.exe tools/us_semi_tracker.py --mode complete       # 巴董事長完成盤前預判後上鎖
    C:/Users/singk/anaconda3/python.exe tools/us_semi_tracker.py --mode news           # 只抓美股半導體新聞
    加 --force：休眠鎖定中仍強制重抓報價與新聞（不改變鎖定狀態）

輸出：
    reports/us_market/us_semi_quotes.json   最新報價快照（每檔附 Yahoo Finance 報價頁 url 與報價時間 pub_date）
    reports/us_market/us_news_raw.json      美股半導體新聞（Yahoo Finance RSS + Google News US，每則附 url、source、pub_date）
    reports/us_market/us_daily_state.json   每日狀態鎖（以「對應的台股交易日」為單位，換日自動重置）
台股交易日判定：台股 14:00 收盤後至隔日 00:00 屬「下一個平日」；週末屬下週一。國定假日未納入。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import argparse
import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import requests

try:
    # 改用 Windows 憑證庫驗證 HTTPS（防毒軟體 / 代理攔截 TLS 時 certifi 會驗證失敗）
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from tools.yahoo_quote import TPE, YAHOO_HEADERS, YAHOO_URL  # noqa: E402

US_DIR = ROOT / "reports" / "us_market"
QUOTES_PATH = US_DIR / "us_semi_quotes.json"
STATE_PATH = US_DIR / "us_daily_state.json"
SUMMARY_JSON_PATH = US_DIR / "tw_premarket_summary.json"

TICKERS = [
    ("^SOX", "費城半導體指數", "指數與ETF"),
    ("SMH", "半導體 ETF", "指數與ETF"),
    ("^IXIC", "那斯達克", "指數與ETF"),
    ("^GSPC", "標普 500", "指數與ETF"),
    ("TSM", "台積電 ADR", "台股ADR與匯率"),
    ("UMC", "聯電 ADR", "台股ADR與匯率"),
    ("ASX", "日月光 ADR", "台股ADR與匯率"),
    ("TWD=X", "美元兌台幣", "台股ADR與匯率"),
    ("NVDA", "輝達", "AI與ASIC"),
    ("AMD", "超微", "AI與ASIC"),
    ("AVGO", "博通", "AI與ASIC"),
    ("MRVL", "邁威爾", "AI與ASIC"),
    ("MU", "美光", "記憶體"),
    ("WDC", "威騰", "記憶體"),
    ("ASML", "艾司摩爾", "設備"),
    ("AMAT", "應材", "設備"),
    ("LRCX", "科林研發", "設備"),
    ("VRT", "Vertiv", "AI伺服器與散熱"),
    ("DELL", "戴爾", "AI伺服器與散熱"),
    ("SMCI", "美超微", "AI伺服器與散熱"),
]
TSM_ADR_RATIO = 5  # 1 股 TSM ADR = 5 股台積電普通股


def _chart(symbol, interval, rng, pre_post=False):
    params = {"interval": interval, "range": rng}
    if pre_post:
        params["includePrePost"] = "true"
    resp = requests.get(YAHOO_URL.format(symbol=symbol), params=params, headers=YAHOO_HEADERS, timeout=10)
    payload = resp.json()
    chart = payload.get("chart") or {}
    if chart.get("error") or not chart.get("result"):
        raise ValueError(f"{symbol} 無資料：{chart.get('error')}")
    return chart["result"][0]


def _bars(res):
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        row = {}
        for k in ("open", "high", "low", "close", "volume"):
            series = quote.get(k) or []
            row[k] = series[i] if i < len(series) else None
        if row["close"] is not None:
            row["ts"] = ts
            out.append(row)
    return out


def fetch_ticker(symbol, name, group):
    """5 分 K（含盤前盤後）取最新價與盤後價；日 K 取前一正規盤收盤與 20 日均量。"""
    try:
        intraday = _chart(symbol, "5m", "5d", pre_post=True)
        daily = _chart(symbol, "1d", "1mo")
    except (requests.RequestException, ValueError) as exc:
        return {"symbol": symbol, "name": name, "group": group, "error": str(exc)}
    meta = intraday.get("meta") or {}
    price = meta.get("regularMarketPrice")
    market_ts = meta.get("regularMarketTime")
    market_day = datetime.fromtimestamp(market_ts, TPE).date() if market_ts else None

    dbars = _bars(daily)
    # 日 K 最後一根若與最新正規盤同一天，前一根才是「前一正規盤收盤」
    if dbars and market_ts and abs(dbars[-1]["ts"] - market_ts) < 86400 * 0.75 and len(dbars) >= 2:
        prev_close = dbars[-2]["close"]
        hist = dbars[:-1]
    else:
        prev_close = dbars[-1]["close"] if dbars else meta.get("chartPreviousClose")
        hist = dbars
    vols = [b["volume"] for b in hist[-20:] if b.get("volume")]
    avg_vol = sum(vols) / len(vols) if vols else None
    volume = meta.get("regularMarketVolume")

    ibars = _bars(intraday)
    period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    regular_end = period.get("end")
    post_price = post_time = None
    has_extended_hours = not symbol.startswith("^") and not symbol.endswith("=X")  # 指數與匯率無盤後交易
    if has_extended_hours and ibars and regular_end and ibars[-1]["ts"] >= regular_end:
        post_price = ibars[-1]["close"]
        post_time = datetime.fromtimestamp(ibars[-1]["ts"], TPE).strftime("%m-%d %H:%M")

    return {
        "symbol": symbol,
        "name": name,
        "group": group,
        "price": price,
        "prev_close": prev_close,
        "change_pct": round((price / prev_close - 1) * 100, 2) if price and prev_close else None,
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "volume": volume,
        "volume_ratio": round(volume / avg_vol, 2) if volume and avg_vol else None,
        "post_market_price": post_price,
        "post_market_time_tpe": post_time,
        "post_market_chg_pct": round((post_price / price - 1) * 100, 2) if post_price and price else None,
        "market_time_tpe": datetime.fromtimestamp(market_ts, TPE).strftime("%Y-%m-%d %H:%M") if market_ts else None,
        "market_day": str(market_day) if market_day else None,
        "currency": meta.get("currency"),
        # 報價出處：可驗證的 Yahoo Finance 報價頁與報價時間（台北）
        "url": quote_url(symbol),
        "pub_date": datetime.fromtimestamp(market_ts, TPE).strftime("%Y-%m-%d %H:%M") if market_ts else None,
        "source": "Yahoo Finance",
    }


def quote_url(symbol):
    return f"https://finance.yahoo.com/quote/{urllib.parse.quote(symbol, safe='')}"


# ---------------------------------------------------------------- 美股半導體新聞（Yahoo Finance RSS + Google News US）

NEWS_PATH = US_DIR / "us_news_raw.json"
NEWS_SYMBOLS = ["^SOX", "NVDA", "TSM", "MU", "VRT", "AVGO", "AMD", "ASML"]
GOOGLE_US_NEWS = ("https://news.google.com/rss/search?q=(Nvidia+OR+TSMC+OR+Micron+OR+Vertiv+OR+Broadcom+OR+"
                  "%22semiconductor+stocks%22)+when:2d&hl=en-US&gl=US&ceid=US:en")


def _rss_items(url, default_source, symbols=None):
    resp = requests.get(url, headers=YAHOO_HEADERS, timeout=10)
    root = ET.fromstring(resp.content)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip() or default_source
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        try:
            dt = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(TPE)
        except (TypeError, ValueError):
            continue
        if title and link.startswith(("https://", "http://")):
            out.append({"title": title, "source": source, "pub_date": dt.strftime("%Y-%m-%d %H:%M"),
                        "epoch": int(dt.timestamp()), "url": link, "symbols": symbols or []})
    return out


def fetch_us_news(limit=25, max_age_hours=48):
    """美股半導體新聞：Yahoo Finance 個股 RSS（SOX、NVDA、TSM、MU、VRT…）＋ Google News US，每則帶真實 url 與 pub_date。"""
    jobs = [(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={urllib.parse.quote(s)}&region=US&lang=en-US",
             "Yahoo Finance", [s]) for s in NEWS_SYMBOLS] + [(GOOGLE_US_NEWS, "Google News US", [])]
    errors, merged, seen = [], [], set()

    def one(job):
        try:
            return _rss_items(*job), None
        except (requests.RequestException, ET.ParseError) as exc:
            return [], f"{job[1]} {','.join(job[2])}：{exc}"

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(one, jobs))
    cutoff = datetime.now().timestamp() - max_age_hours * 3600
    for items, err in results:
        if err:
            errors.append(err)
        for n in items:
            key = re.sub(r"\W+", "", n["title"].lower())[:40]
            if key in seen or n["epoch"] < cutoff:
                continue
            seen.add(key)
            merged.append(n)
    merged.sort(key=lambda n: n["epoch"], reverse=True)
    out = {"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "errors": errors, "count": min(len(merged), limit),
           "news": merged[:limit]}
    _write(NEWS_PATH, out)
    return out


def tw_close_2330():
    res = _chart("2330.TW", "1d", "1mo")
    bars = _bars(res)
    last = bars[-1]
    return last["close"], datetime.fromtimestamp(last["ts"], TPE).strftime("%Y-%m-%d")


def tsm_premium(quotes):
    """TSM ADR 換算台股價 = TSM 價 / 5 × USD/TWD；溢價率對 2330 最近收盤。"""
    tsm = quotes.get("TSM") or {}
    fx = (quotes.get("TWD=X") or {}).get("price")
    try:
        tw_close, tw_date = tw_close_2330()
    except (requests.RequestException, ValueError, IndexError) as exc:
        return {"error": f"2330.TW 收盤取得失敗：{exc}"}
    if not tsm.get("price") or not fx:
        return {"error": "TSM 或匯率缺資料"}
    converted = tsm["price"] / TSM_ADR_RATIO * fx
    out = {
        "tsm_price_usd": tsm["price"],
        "usd_twd": round(fx, 4),
        "tsm_converted_twd": round(converted, 2),
        "tw_2330_close": tw_close,
        "tw_2330_close_date": tw_date,
        "premium_pct": round((converted - tw_close) / tw_close * 100, 2),
    }
    if tsm.get("post_market_price"):
        post_conv = tsm["post_market_price"] / TSM_ADR_RATIO * fx
        out["premium_pct_post_market"] = round((post_conv - tw_close) / tw_close * 100, 2)
    return out


# ---------------------------------------------------------------- 狀態鎖

def tw_trading_date(now=None):
    """此刻美股資訊所對應的台股交易日：台股 14:00 後屬下一個平日；週末屬下週一（國定假日未納入）。"""
    now = now or datetime.now()
    day = now.date()
    if now.hour >= 14:
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def _read(path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_state(now=None):
    """讀取狀態鎖；若記錄的台股交易日不是今天對應的交易日，回傳全新狀態（換日自動解鎖）。"""
    target = tw_trading_date(now)
    state = _read(STATE_PATH, {})
    if state.get("tw_trading_date") != target:
        state = {"tw_trading_date": target, "night_updated_at": None, "morning_close_fetched_at": None,
                 "morning_brief_completed": False, "morning_brief_completed_at": None}
    return state


def check_status(now=None):
    state = load_state(now)
    if state.get("morning_brief_completed"):
        return {"status": "LOCKED_COMPLETED",
                "message": "今日早上美股收盤匯報已完成，台股盤中與盤後時段川大帝MAGA 休眠中",
                "tw_trading_date": state["tw_trading_date"],
                "completed_at": state.get("morning_brief_completed_at")}
    if state.get("morning_close_fetched_at"):
        status, message = "MORNING_FETCHED", "美股收盤數據已抓取，等待川大帝MAGA 匯報與巴董事長盤前預判完成"
    elif state.get("night_updated_at"):
        status, message = "NIGHT_UPDATED", "已完成晚間美股開盤更新，等待早上 07:00–08:00 收盤匯報"
    else:
        status, message = "PENDING", "等待美股收盤匯報"
    return {"status": status, "message": message, "tw_trading_date": state["tw_trading_date"],
            "night_updated_at": state.get("night_updated_at")}


# ---------------------------------------------------------------- 主流程

def fetch_all(mode):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda t: fetch_ticker(*t), TICKERS))
    quotes = {r["symbol"]: r for r in results}
    snapshot = {
        "mode": mode,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tw_trading_date": tw_trading_date(),
        "tsm_adr": tsm_premium(quotes),
        "errors": [f"{r['symbol']}：{r['error']}" for r in results if r.get("error")],
        "quotes": quotes,
    }
    _write(QUOTES_PATH, snapshot)
    # 依模式另存一份，晚間與早盤數據可對照
    _write(US_DIR / f"us_semi_quotes_{mode}.json", snapshot)
    return snapshot


def run(mode, force=False):
    """force=True：使用者在川大帝MAGA 個人報告手動按「單獨更新」時，休眠鎖定中仍強制重抓（不解鎖、不改鎖定狀態）。"""
    if mode == "check_status":
        return check_status()
    state = load_state()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if mode == "complete":
        state.update(morning_brief_completed=True, morning_brief_completed_at=now)
        _write(STATE_PATH, state)
        return {"status": "LOCKED_COMPLETED", "tw_trading_date": state["tw_trading_date"], "completed_at": now}
    if mode == "news":
        return fetch_us_news()
    if state.get("morning_brief_completed") and not force:
        return {"status": "LOCKED_COMPLETED", "message": "今日早上美股收盤匯報已完成，川大帝MAGA 休眠中，不再抓取",
                "tw_trading_date": state["tw_trading_date"]}
    snapshot = fetch_all(mode)
    try:
        snapshot["news_count"] = fetch_us_news()["count"]
    except Exception as exc:  # 新聞失敗不影響報價
        snapshot["errors"].append(f"美股新聞：{exc}")
    if state.get("morning_brief_completed"):
        return snapshot  # 強制刷新只更新數據，不動鎖定狀態
    state["night_updated_at" if mode == "night_open" else "morning_close_fetched_at"] = now
    _write(STATE_PATH, state)
    return snapshot


def main():
    parser = argparse.ArgumentParser(description="川大帝MAGA：美股半導體指標與台積電 ADR 折溢價")
    parser.add_argument("--mode", required=True, choices=["night_open", "morning_close", "check_status", "complete", "news"])
    parser.add_argument("--force", action="store_true", help="狀態已鎖定時仍強制重抓（不解鎖）")
    args = parser.parse_args()
    out = run(args.mode, force=args.force)
    if args.mode == "news":
        print(f"美股新聞 {out['updated_at']}：{out['count']} 則" + (f"｜錯誤 {out['errors']}" if out["errors"] else ""))
        for n in out["news"][:10]:
            print(f"  [{n['pub_date']}] {n['source']}｜{n['title'][:70]}｜{n['url'][:60]}")
        return
    if "quotes" not in out:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    adr = out["tsm_adr"]
    print(f"美股快照 {out['fetched_at']}（{out['mode']}），對應台股交易日 {out['tw_trading_date']}")
    for q in out["quotes"].values():
        if q.get("error"):
            print(f"  {q['symbol']:<6} {q['name']}：{q['error']}")
            continue
        post = f"｜盤後 {q['post_market_price']}（{q['post_market_chg_pct']:+.2f}%）" if q.get("post_market_chg_pct") is not None else ""
        chg = f"{q['change_pct']:+.2f}%" if q.get("change_pct") is not None else "N/A"
        print(f"  {q['symbol']:<6} {q['name']:<8} {q['price']}（{chg}）量比 {q.get('volume_ratio')}{post}｜{q['market_time_tpe']}")
    if adr.get("error"):
        print(f"TSM ADR 溢價：{adr['error']}")
    else:
        print(f"TSM ADR 換算 {adr['tsm_converted_twd']} 元 vs 2330 收盤 {adr['tw_2330_close']}（{adr['tw_2330_close_date']}）"
              f" → 溢價 {adr['premium_pct']:+.2f}%（匯率 {adr['usd_twd']}）")
    if out["errors"]:
        print("缺資料：" + "；".join(out["errors"]))


if __name__ == "__main__":
    main()
