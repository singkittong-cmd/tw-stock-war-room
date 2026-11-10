# -*- coding: utf-8 -*-
"""Yahoo Finance 1 分 K 盤中報價：證交所 MIS 成交價為 '-' 時的真實成交價來源，並推算 VWAP 與主動買賣量。

用法：
    C:/Users/singk/anaconda3/python.exe tools/yahoo_quote.py --stock 2408

說明：
- 端點 https://query1.finance.yahoo.com/v8/finance/chart/<代號>.TW（上櫃為 .TWO）?interval=1m&range=1d
- range=1d 回傳最近一個交易日；收盤後即為當日完整 1 分 K。
- Yahoo 台股成交量單位為「股」，本模組一律換算為「張」（÷1000）。
- VWAP = Σ(1 分 K 收盤價 × 成交量) ÷ Σ成交量。
- 主動買賣量以 1 分 K tick rule 推估：收盤價高於前一根 → 計外盤，低於 → 計內盤，持平各半。屬估算值，非逐筆資料。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

try:
    # 改用 Windows 憑證庫驗證 HTTPS（防毒軟體 / 代理攔截 TLS 時 certifi 會驗證失敗）
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}
TPE = timezone(timedelta(hours=8))
METHOD_NOTE = "Yahoo 1 分 K tick rule 推估（收盤價較前一根上漲計外盤、下跌計內盤、持平各半）"

_cache = {}
_suffix = {}
_lock = threading.Lock()


class YahooError(Exception):
    pass


def _get_chart(symbol, interval="1m", rng="1d", timeout=3.5):
    try:
        resp = requests.get(YAHOO_URL.format(symbol=symbol), params={"interval": interval, "range": rng},
                            headers=YAHOO_HEADERS, timeout=timeout)
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise YahooError(f"{symbol} 連線失敗：{exc}") from exc
    chart = payload.get("chart") or {}
    if chart.get("error") or not chart.get("result"):
        raise YahooError(f"{symbol} 無資料：{chart.get('error')}")
    return chart["result"][0]


def _parse(stock_id, symbol, res):
    meta = res.get("meta") or {}
    stamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    def col(key, i):
        values = quote.get(key) or []
        return values[i] if i < len(values) else None

    bars = []
    for i, ts in enumerate(stamps):
        o, h, lo, c, v = (col(k, i) for k in ("open", "high", "low", "close", "volume"))
        if c is None or v is None:
            continue
        bars.append({"ts": ts, "open": o if o is not None else c, "high": h if h is not None else c,
                     "low": lo if lo is not None else c, "close": c, "volume": v})

    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    total_vol = sum(b["volume"] for b in bars)
    vwap = (sum(b["close"] * b["volume"] for b in bars) / total_vol) if total_vol else None

    outer = inner = 0.0
    ref = prev_close
    for b in bars:
        if ref is None or b["close"] == ref:
            outer += b["volume"] / 2
            inner += b["volume"] / 2
        elif b["close"] > ref:
            outer += b["volume"]
        else:
            inner += b["volume"]
        ref = b["close"]

    last_dir = "未明"
    if len(bars) >= 2:
        last, before = bars[-1], bars[-2]
        if last["close"] > before["close"] or (last["close"] == before["close"] and last["close"] > last["open"]):
            last_dir = "外盤主動買進"
        elif last["close"] < before["close"] or (last["close"] == before["close"] and last["close"] < last["open"]):
            last_dir = "內盤主動賣出"

    # 近 15 分鐘動能：最後 15 根 1 分 K 與更早盤勢比較
    momentum, momentum_pct = "資料不足", None
    if len(bars) >= 16:
        window, earlier = bars[-15:], bars[:-15]
        base = bars[-16]["close"]
        momentum_pct = (window[-1]["close"] / base - 1) * 100 if base else None
        earlier_high = max(b["high"] for b in earlier)
        earlier_low = min(b["low"] for b in earlier)
        if max(b["high"] for b in window) >= earlier_high and (momentum_pct or 0) > 0:
            momentum = "拉抬創高"
        elif min(b["low"] for b in window) <= earlier_low and (momentum_pct or 0) < 0:
            momentum = "壓回測低"
        else:
            momentum = "橫盤整理"

    # 近 5 分鐘動能：最後一根收盤相對 5 分鐘前收盤，±0.2% 為門檻
    momentum_5m, momentum_5m_pct = "資料不足", None
    if len(bars) >= 6 and bars[-6]["close"]:
        momentum_5m_pct = (bars[-1]["close"] / bars[-6]["close"] - 1) * 100
        momentum_5m = "拉抬" if momentum_5m_pct > 0.2 else "壓回" if momentum_5m_pct < -0.2 else "盤整"

    market_time = meta.get("regularMarketTime")
    return {
        "stock_id": stock_id,
        "symbol": symbol,
        "price": meta.get("regularMarketPrice"),
        "prev_close": prev_close,
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "volume_lots": round(meta["regularMarketVolume"] / 1000, 1) if meta.get("regularMarketVolume") else None,
        "market_time": datetime.fromtimestamp(market_time, TPE).strftime("%Y-%m-%d %H:%M:%S") if market_time else None,
        "bar_count": len(bars),
        "last_bar_time": datetime.fromtimestamp(bars[-1]["ts"], TPE).strftime("%H:%M") if bars else None,
        "vwap": round(vwap, 2) if vwap else None,
        "est_outer_lots": round(outer / 1000, 1),
        "est_inner_lots": round(inner / 1000, 1),
        "est_outer_inner_ratio": round(outer / inner, 3) if inner else None,
        "last_bar_side": last_dir,
        "day_open": bars[0]["open"] if bars else None,
        "bars_high": max(b["high"] for b in bars) if bars else None,
        "bars_low": min(b["low"] for b in bars) if bars else None,
        "momentum_5m": momentum_5m,
        "momentum_5m_pct": round(momentum_5m_pct, 2) if momentum_5m_pct is not None else None,
        "momentum_15m": momentum,
        "momentum_15m_pct": round(momentum_pct, 2) if momentum_pct is not None else None,
        "method": METHOD_NOTE,
    }


def fetch_intraday(stock_id, market=None, max_age=0):
    """抓單檔 Yahoo 1 分 K 統計。market 為 MIS 的 'tse' / 'otc'；max_age 秒內重複呼叫直接回快取。"""
    now = time.time()
    with _lock:
        cached = _cache.get(stock_id)
        if cached and max_age and now - cached[0] <= max_age:
            return cached[1]
    if market == "otc":
        suffixes = [".TWO"]
    elif market == "tse":
        suffixes = [".TW"]
    elif stock_id in _suffix:
        suffixes = [_suffix[stock_id]]
    else:
        suffixes = [".TW", ".TWO"]
    last_err = None
    for sfx in suffixes:
        symbol = f"{stock_id}{sfx}"
        try:
            result = _parse(stock_id, symbol, _get_chart(symbol))
        except YahooError as exc:
            last_err = exc
            continue
        if result["price"] is None and not result["bar_count"]:
            last_err = YahooError(f"{symbol} 無成交資料")
            continue
        with _lock:
            _suffix[stock_id] = sfx
            _cache[stock_id] = (time.time(), result)
        return result
    raise last_err or YahooError(f"{stock_id} 無資料")


def fetch_daily(stock_id, rng="6mo"):
    """Yahoo 日 K（免金鑰、不耗 FinMind 額度）。回傳 [{date, open, high, low, close, volume(股)}]，由舊到新。

    stock_id 可為台股代號（自動試 .TW / .TWO）或完整 Yahoo 代號（例如 ^TWII 加權指數）。
    """
    if stock_id.startswith("^") or "." in stock_id:
        symbols = [stock_id]
    elif stock_id in _suffix:
        symbols = [stock_id + _suffix[stock_id]]
    else:
        symbols = [f"{stock_id}.TW", f"{stock_id}.TWO"]
    last_err = None
    for symbol in symbols:
        try:
            res = _get_chart(symbol, interval="1d", rng=rng, timeout=8)
        except YahooError as exc:
            last_err = exc
            continue
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        rows = []
        for i, ts in enumerate(res.get("timestamp") or []):
            vals = {}
            for k in ("open", "high", "low", "close", "volume"):
                series = quote.get(k) or []
                vals[k] = series[i] if i < len(series) else None
            if vals["close"] is None:
                continue
            rows.append({"date": datetime.fromtimestamp(ts, TPE).strftime("%Y-%m-%d"),
                         "open": vals["open"] if vals["open"] is not None else vals["close"],
                         "high": vals["high"] if vals["high"] is not None else vals["close"],
                         "low": vals["low"] if vals["low"] is not None else vals["close"],
                         "close": vals["close"], "volume": vals["volume"] or 0})
        if rows:
            if not stock_id.startswith("^") and "." not in stock_id:
                with _lock:
                    _suffix[stock_id] = symbol[len(stock_id):]
            return rows
        last_err = YahooError(f"{symbol} 無日 K 資料")
    raise last_err or YahooError(f"{stock_id} 無日 K 資料")


def fetch_many(stock_ids, markets=None, max_age=0):
    """平行抓多檔，回傳 {代號: 結果}；失敗者回傳 {"error": 訊息}。"""
    markets = markets or {}

    def one(sid):
        try:
            return sid, fetch_intraday(sid, markets.get(sid), max_age)
        except YahooError as exc:
            return sid, {"error": str(exc)}

    if not stock_ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(stock_ids))) as pool:
        return dict(pool.map(one, stock_ids))


def main():
    parser = argparse.ArgumentParser(description="Yahoo Finance 1 分 K 盤中報價與 VWAP")
    parser.add_argument("--stock", required=True, help="股票代號，例如 2408")
    args = parser.parse_args()
    try:
        print(json.dumps(fetch_intraday(args.stock.strip()), ensure_ascii=False, indent=2))
    except YahooError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        sys.exit(2)


if __name__ == "__main__":
    main()
