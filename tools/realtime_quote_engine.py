# -*- coding: utf-8 -*-
"""開源盤中即時報價雙引擎：Yahoo Finance 1 分 K（成交價、VWAP、動能）+ 證交所/櫃買 MIS（五檔、當盤量）。

用法：
    C:/Users/singk/anaconda3/python.exe tools/realtime_quote_engine.py --stock 2408 --stock 2330

現價決定規則（絕不拿委買價充當成交價）：
1. MIS `z` 有值 → 與 Yahoo 比較時間戳，取較新者（同秒以 MIS 為準）。
2. MIS `z` 為 '-' → 在 MIS 試撮價 `pz` 與 Yahoo 最新成交價之間取時間較新者。
3. 兩者皆無 → 退回昨收並標示「無成交」；不使用最佳委買。
Yahoo 台股盤中報價可能延遲，`price_age_sec` 記錄所採用報價距今秒數，供畫面標示。
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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

try:
    # 改用 Windows 憑證庫驗證 HTTPS（防毒軟體 / 代理攔截 TLS 時 certifi 會驗證失敗）
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from tools.yahoo_quote import TPE, fetch_many as fetch_yahoo_many  # noqa: E402

MIS_HOME = "https://mis.twse.com.tw/stock/index.jsp"
MIS_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": MIS_HOME,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.6,en;q=0.5",
}
MIS_TIMEOUT = 2.5                      # 海外雲端主機連證交所常卡住：2.5 秒即放棄，改用 Yahoo
MIS_COOLDOWN_SEC = 120                 # 連續失敗後 2 分鐘內不再嘗試 MIS，避免每輪都等逾時
MIS_FAIL_LIMIT = 2
OVERSEAS_ODD_LABEL = "【海外零股估算價】"

_session = None
_session_lock = threading.Lock()
_market_cache = {}
_mis_health = {"fails": 0, "down_until": 0.0, "last_error": None}


class MISError(Exception):
    pass


def mis_available():
    return time.time() >= _mis_health["down_until"]


def _mis_ok():
    _mis_health.update(fails=0, down_until=0.0)


def _mis_fail(exc):
    _mis_health["fails"] += 1
    _mis_health["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
    if _mis_health["fails"] >= MIS_FAIL_LIMIT:
        _mis_health["down_until"] = time.time() + MIS_COOLDOWN_SEC


def mis_health():
    return {"available": mis_available(), **_mis_health}


# ---------------------------------------------------------------- MIS 五檔

def _num(x):
    if x is None:
        return None
    try:
        v = float(str(x).strip())
    except ValueError:
        return None
    return v if v > 0 else None


def _levels(s):
    if not s:
        return []
    out = []
    for part in str(s).strip("_").split("_"):
        v = _num(part)
        if v is not None:
            out.append(v)
    return out


def new_session():
    """建立帶 Cookie 的 MIS session（先造訪首頁，否則 API 偶爾回空陣列）。"""
    session = requests.Session()
    session.headers.update(MIS_HEADERS)
    try:
        session.get(MIS_HOME, timeout=MIS_TIMEOUT)
    except requests.RequestException:
        pass
    return session


def shared_session(reset=False):
    """同一行程（例如 Streamlit 每 10 秒刷新）重用 MIS session，省去每次造訪首頁的時間。"""
    global _session
    with _session_lock:
        if reset or _session is None:
            _session = new_session()
        return _session


def parse_quote(item):
    """把 MIS msgArray 單筆轉成統一格式；成交價 `z` 為 '-' 時 price 為 None。"""
    tlong = item.get("tlong")
    return {
        "code": item.get("c"),
        "name": item.get("n", item.get("c")),
        "market": item.get("ex", "tse"),
        "price": _num(item.get("z")),
        "trial_price": _num(item.get("pz")),
        "tick_vol": int(_num(item.get("tv")) or 0),
        "acc_vol": int(_num(item.get("v")) or 0),
        "prev_close": _num(item.get("y")),
        "open": _num(item.get("o")),
        "high": _num(item.get("h")),
        "low": _num(item.get("l")),
        "bids": _levels(item.get("b")),
        "bid_vols": _levels(item.get("g")),
        "asks": _levels(item.get("a")),
        "ask_vols": _levels(item.get("f")),
        "time": item.get("t", ""),
        "tlong": tlong,
        "quote_epoch": int(tlong) / 1000 if str(tlong or "").isdigit() else None,
    }


def fetch_mis_quotes(stock_ids, session=None, market_cache=None):
    """抓多檔 MIS 即時報價，回傳 {代號: quote}。未知市場別時同時查上市 tse 與上櫃 otc。"""
    if not stock_ids:
        return {}
    if not mis_available():
        raise MISError(f"MIS 暫停使用（連線失敗冷卻中）：{_mis_health['last_error']}")
    session = session or shared_session()
    market_cache = market_cache if market_cache is not None else _market_cache
    channels = []
    for s in stock_ids:
        if s in market_cache:
            channels.append(f"{market_cache[s]}_{s}.tw")
        else:
            channels += [f"tse_{s}.tw", f"otc_{s}.tw"]
    params = {"ex_ch": "|".join(channels), "json": "1", "delay": "0", "_": str(int(time.time() * 1000))}
    try:
        resp = session.get(MIS_API, params=params, timeout=MIS_TIMEOUT)
        resp.encoding = "utf-8"
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        _mis_fail(exc)
        raise MISError(f"MIS 報價抓取失敗：{exc}") from exc
    _mis_ok()
    quotes = {}
    for item in payload.get("msgArray", []):
        code = item.get("c")
        if code not in stock_ids:
            continue
        market_cache[code] = item.get("ex", "tse")
        quotes[code] = parse_quote(item)
    return quotes


# ---------------------------------------------------------------- 盤中零股（獨立撮合池，與整張有價差）

MIS_ODD_API = "https://mis.twse.com.tw/stock/api/getOddInfo.jsp"


def fetch_odd_lot_quotes(stock_ids, session=None):
    """證交所／櫃買盤中零股 MIS 報價，回傳 {代號: {odd_price, odd_bid_1, odd_ask_1, odd_bids, odd_asks, odd_tick_shares, ...}}。
    odd_price 為零股最新成交價（z）；當秒無成交時為 None，由呼叫端改用零股委賣／委買第一檔。"""
    ids = list(dict.fromkeys(stock_ids))
    if not ids:
        return {}
    if not mis_available():
        raise MISError(f"零股 MIS 暫停使用（連線失敗冷卻中）：{_mis_health['last_error']}")
    session = session or shared_session()
    channels = []
    for s in ids:
        channels += [f"{_market_cache[s]}_{s}.tw"] if s in _market_cache else [f"tse_{s}.tw", f"otc_{s}.tw"]
    params = {"ex_ch": "|".join(channels), "json": "1", "delay": "0", "_": str(int(time.time() * 1000))}
    try:
        resp = session.get(MIS_ODD_API, params=params, timeout=MIS_TIMEOUT)
        resp.encoding = "utf-8"
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        _mis_fail(exc)
        raise MISError(f"零股 MIS 報價抓取失敗：{exc}") from exc
    _mis_ok()
    out = {}
    for item in payload.get("msgArray", []):
        code = item.get("c")
        if code not in ids:
            continue
        _market_cache.setdefault(code, item.get("ex", "tse"))
        bids, asks = _levels(item.get("b")), _levels(item.get("a"))
        tlong = item.get("tlong")
        out[code] = {
            "stock_id": code, "name": item.get("n"), "market": item.get("ex"),
            "odd_price": _num(item.get("z")), "odd_trial_price": _num(item.get("pz")),
            "odd_bid_1": bids[0] if bids else None, "odd_ask_1": asks[0] if asks else None,
            "odd_bids": bids, "odd_asks": asks,
            "odd_bid_vols": [int(v) for v in _levels(item.get("g"))], "odd_ask_vols": [int(v) for v in _levels(item.get("f"))],
            "odd_tick_shares": int(_num(item.get("tv")) or 0), "odd_acc_shares": int(_num(item.get("v")) or 0),
            "prev_close": _num(item.get("y")), "time": item.get("t"),
            "quote_epoch": int(tlong) / 1000 if str(tlong or "").isdigit() else None,
        }
    return out


def fetch_odd_lot_quote(stock_id):
    """單檔盤中零股報價（getOddInfo.jsp）；查無資料回傳 None。"""
    return fetch_odd_lot_quotes([stock_id]).get(stock_id)


def odd_lot_fill_price(odd, side, board_price=None, slip_ticks=2):
    """零股模擬成交價：優先用零股最新成交價 z；當秒無成交（z 為 "-"）時，買進用零股委賣一、賣出用零股委買一；
    零股五檔皆無時以整張價加減 slip_ticks 檔升降單位滑價。回傳 (價格, 來源說明)。"""
    from tools.finmind_cli import tick_size
    odd = odd or {}
    if odd.get("odd_price"):
        return odd["odd_price"], "零股成交價"
    key, label = ("odd_ask_1", "零股委賣一") if side == "buy" else ("odd_bid_1", "零股委買一")
    if odd.get(key):
        return odd[key], label
    if board_price:
        step = tick_size(board_price) * slip_ticks
        return round(board_price + step if side == "buy" else board_price - step, 2), f"整張價{'+' if side == 'buy' else '-'}{slip_ticks}檔滑價"
    return None, "無零股報價"


def overseas_odd_estimate(side, board_price, bid=None, ask=None):
    """海外主機連不到零股 MIS 時的零股估算價：買進用整張委賣價（無則現價 + 1 檔），
    賣出用整張委買價（無則現價 - 1 檔），並標註【海外零股估算價】。回傳 (價格, 來源說明)。"""
    from tools.finmind_cli import tick_size
    if not board_price:
        return None, "無報價"
    if side == "buy":
        if ask:
            return float(ask), f"{OVERSEAS_ODD_LABEL}整張委賣價"
        return round(board_price + tick_size(board_price), 2), f"{OVERSEAS_ODD_LABEL}現價+1檔"
    if bid:
        return float(bid), f"{OVERSEAS_ODD_LABEL}整張委買價"
    return round(board_price - tick_size(board_price), 2), f"{OVERSEAS_ODD_LABEL}現價-1檔"


def classify_side(price, bids, asks):
    """以同一快照的最佳一檔判斷當盤成交方向。"""
    if price is None:
        return "未成交"
    if asks and price >= asks[0]:
        return "外盤主動買進"
    if bids and price <= bids[0]:
        return "內盤主動賣出"
    if bids and asks:
        return "外盤主動買進" if price >= (bids[0] + asks[0]) / 2 else "內盤主動賣出"
    return "未明"


# ---------------------------------------------------------------- 雙引擎合併

def _yahoo_epoch(y):
    if not y or not y.get("market_time"):
        return None
    try:
        return datetime.strptime(y["market_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TPE).timestamp()
    except ValueError:
        return None


def resolve_price(q, yahoo=None):
    """現價：MIS 成交價與 Yahoo 取新；MIS 無成交時在試撮價與 Yahoo 間取新；絕不使用最佳委買。

    回傳 (現價, 來源說明, 當盤方向, 方向來源, 報價時間 epoch)。
    """
    y = yahoo if yahoo and not yahoo.get("error") and yahoo.get("price") else None
    y_epoch = _yahoo_epoch(y)
    mis_epoch = q.get("quote_epoch")
    y_side = y.get("last_bar_side") if y else None
    y_label = f"Yahoo 成交價（{y['market_time'][11:]}）" if y else None

    if q.get("price") is not None:
        if y and y_epoch and mis_epoch and y_epoch > mis_epoch + 1:
            return y["price"], y_label, y_side or "未明", "Yahoo 最後 1 分 K 推估", y_epoch
        return q["price"], "成交價", classify_side(q["price"], q["bids"], q["asks"]), "MIS 成交價對五檔", mis_epoch
    if q.get("trial_price") is not None and (not y or not y_epoch or (mis_epoch and mis_epoch >= y_epoch)):
        side = y_side if y_side and y_side != "未明" else classify_side(q["trial_price"], q["bids"], q["asks"])
        return q["trial_price"], "試撮價(pz)", side, "MIS 試撮價／Yahoo 1 分 K", mis_epoch
    if y:
        return y["price"], y_label, y_side or "未明", "Yahoo 最後 1 分 K 推估", y_epoch
    if q.get("prev_close"):
        return q["prev_close"], "昨收（今日尚無成交）", "未成交", "無成交資料", None
    return None, "無報價", "未成交", "無成交資料", None


def action_light(price, summary):
    """盤中行動燈號：依總分析員 summary 的 stop_loss、target_1、entry_zone 判斷現價位置（不涉及成本）。"""
    if price is None:
        return "⚪ 無報價"
    if not summary:
        return "⚪ 尚無裁決"
    try:
        stop = float(summary.get("stop_loss")) if summary.get("stop_loss") is not None else None
        t1 = float(summary.get("target_1")) if summary.get("target_1") is not None else None
    except (TypeError, ValueError):
        stop = t1 = None
    zone = summary.get("entry_zone")
    if stop and price <= stop:
        return "🔴 跌破停損"
    if t1 and price >= t1 * 0.985:
        return "🟠 逼近目標"
    if isinstance(zone, (list, tuple)) and len(zone) >= 2:
        try:
            lo, hi = float(zone[0]), float(zone[1])
        except (TypeError, ValueError):
            lo = hi = None
        if lo and hi and lo <= price <= hi:
            return "🟢 布局區間"
    return "🟡 續抱觀察"


def fetch_realtime(stock_ids, max_yahoo_age=0):
    """平行抓 MIS 與 Yahoo（v8 chart），回傳 (mis_quotes, yahoo_results, markets)。
    MIS 失敗時重建 session 重試一次；仍失敗（例如海外主機連不到證交所）時 mis_quotes 為空，現價無縫改用 Yahoo。"""
    stock_ids = list(dict.fromkeys(stock_ids))
    if not stock_ids:
        return {}, {}, {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        mis_future = pool.submit(fetch_mis_quotes, stock_ids)
        yahoo_future = pool.submit(fetch_yahoo_many, stock_ids, dict(_market_cache), max_yahoo_age)
        try:
            quotes = mis_future.result()
        except MISError:
            try:
                quotes = fetch_mis_quotes(stock_ids, shared_session(reset=True)) if mis_available() else {}
            except MISError:
                quotes = {}
        yahoo = yahoo_future.result()
    return quotes, yahoo, dict(_market_cache)


def summarize(stock_id, q, y):
    """單檔雙引擎合併結果（CLI 測試用）。"""
    y_only = "Yahoo 成交價（證交所 MIS 連不上，海外備援）" if not mis_available() else "Yahoo（MIS 無此代號）"
    price, source, side, side_source, epoch = resolve_price(q, y) if q else (
        (y or {}).get("price"), y_only, (y or {}).get("last_bar_side", "未明"), "Yahoo", _yahoo_epoch(y))
    bid_sum = sum((q or {}).get("bid_vols") or [])
    ask_sum = sum((q or {}).get("ask_vols") or [])
    prev = (q or {}).get("prev_close") or (y or {}).get("prev_close")
    return {
        "stock_id": stock_id,
        "name": (q or {}).get("name"),
        "price": price,
        "price_source": source,
        "price_age_sec": round(time.time() - epoch, 0) if epoch else None,
        "change_pct": round((price / prev - 1) * 100, 2) if price and prev else None,
        "mis_z": (q or {}).get("price"),
        "mis_time": (q or {}).get("time"),
        "yahoo_price": (y or {}).get("price"),
        "yahoo_time": (y or {}).get("market_time"),
        "vwap": (y or {}).get("vwap"),
        "momentum_5m": (y or {}).get("momentum_5m"),
        "bid_vol_sum": bid_sum,
        "ask_vol_sum": ask_sum,
        "orderbook_ratio": round(bid_sum / ask_sum, 3) if ask_sum else None,
        "tick_side": side,
        "tick_side_source": side_source,
    }


def main():
    parser = argparse.ArgumentParser(description="Yahoo + MIS 盤中即時報價雙引擎")
    parser.add_argument("--stock", action="append", required=True, help="股票代號，可重複")
    parser.add_argument("--odd", action="store_true", help="只顯示盤中零股報價（getOddInfo.jsp）")
    args = parser.parse_args()
    ids = [s.strip() for s in args.stock if s.strip()]
    if args.odd:
        for sid, o in fetch_odd_lot_quotes(ids).items():
            buy, buy_src = odd_lot_fill_price(o, "buy")
            sell, sell_src = odd_lot_fill_price(o, "sell")
            print(f"{sid} {o['name']}｜零股成交 {o['odd_price']}｜委買一 {o['odd_bid_1']}／委賣一 {o['odd_ask_1']}"
                  f"｜當盤 {o['odd_tick_shares']} 股、累計 {o['odd_acc_shares']:,} 股｜{o['time']}"
                  f"｜模擬買進 {buy}（{buy_src}）賣出 {sell}（{sell_src}）")
        return
    started = time.perf_counter()
    quotes, yahoo, _ = fetch_realtime(ids)
    out = {"elapsed_sec": round(time.perf_counter() - started, 2),
           "quotes": [summarize(s, quotes.get(s), yahoo.get(s)) for s in ids]}
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
