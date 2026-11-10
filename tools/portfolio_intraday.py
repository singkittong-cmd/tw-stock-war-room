# -*- coding: utf-8 -*-
"""盤中即時持倉報價、五檔力道與未實現損益 CLI。

用法：
    C:/Users/singk/anaconda3/python.exe tools/portfolio_intraday.py              # 掃描 reports/portfolio.json 全部持股
    C:/Users/singk/anaconda3/python.exe tools/portfolio_intraday.py --stock 2330 # 只查單一檔（非持股也可查報價與五檔）

資料來源：tools/realtime_quote_engine.py 雙引擎（Yahoo 1 分 K 成交價／VWAP／動能 + 證交所 MIS 五檔），
MIS 與 Yahoo 取時間較新的真實成交價，絕不以最佳委買充當現價。
結果印出 JSON 並寫入 reports/portfolio_live_status.json，同步合併進 reports/intraday_snapshot.json。
損益已扣除買進手續費、賣出手續費（各 0.1425%）與證交稅 0.3%。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import argparse
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# MIS 抓取函式由報價引擎提供；在此重新匯出，intraday_sentinel / dashboard 沿用原名稱
from tools.realtime_quote_engine import (  # noqa: E402,F401
    MISError,
    action_light,
    classify_side,
    fetch_mis_quotes,
    fetch_realtime,
    new_session,
    parse_quote,
    resolve_price,
)

REPORTS_DIR = ROOT / "reports"
DATA_DIR = REPORTS_DIR / "data"
TRADING_MINUTES = 270  # 09:00–13:30
PORTFOLIO_PATH = REPORTS_DIR / "portfolio.json"
STATUS_PATH = REPORTS_DIR / "portfolio_live_status.json"
SNAPSHOT_PATH = REPORTS_DIR / "intraday_snapshot.json"

STOCKS_DIR = REPORTS_DIR / "stocks"

FEE_RATE = 0.001425   # 券商手續費（買、賣各收一次，未計折讓）
TAX_RATE = 0.003      # 證交稅（一般股票賣出；ETF 0.1%、當沖 0.15% 未另外區分）
MIN_FEE = 20          # 整股最低手續費；零股最低收費依各券商規定


# ---------------------------------------------------------------- 雙引擎報價分析

def analyze_quote(q, yahoo=None):
    """補上雙引擎現價、漲跌幅、五檔力道、內外盤方向與 Yahoo VWAP。"""
    price, source, side, side_source, epoch = resolve_price(q, yahoo)
    bid_sum = sum(q["bid_vols"])
    ask_sum = sum(q["ask_vols"])
    y = yahoo if yahoo and not yahoo.get("error") else {}
    return {
        "current_price": price,
        "price_source": source,
        "price_age_sec": round(time.time() - epoch) if epoch else None,
        "mis_z": q["price"],
        "prev_close": q["prev_close"],
        "change_pct": round((price / q["prev_close"] - 1) * 100, 2) if price and q["prev_close"] else None,
        "open": q["open"],
        "high": q["high"],
        "low": q["low"],
        "tick_vol": q["tick_vol"],
        "acc_vol": q["acc_vol"],
        "quote_time": q["time"],
        "bids": q["bids"],
        "bid_vols": q["bid_vols"],
        "asks": q["asks"],
        "ask_vols": q["ask_vols"],
        "bid_vol_sum": bid_sum,
        "ask_vol_sum": ask_sum,
        "bid_ask_ratio": round(bid_sum / ask_sum, 3) if ask_sum else None,
        "bid_share": round(bid_sum / (bid_sum + ask_sum), 3) if (bid_sum + ask_sum) else None,
        "tick_side": side,
        "tick_side_source": side_source,
        "vwap": y.get("vwap"),
        "est_outer_lots": y.get("est_outer_lots"),
        "est_inner_lots": y.get("est_inner_lots"),
        "est_outer_inner_ratio": y.get("est_outer_inner_ratio"),
        "yahoo_price": y.get("price"),
        "yahoo_time": y.get("market_time"),
        "yahoo_error": yahoo.get("error") if yahoo else "未查詢",
    }


# ---------------------------------------------------------------- 盤中趨勢與量化初步建議

def _tick_size(price):
    for limit, step in ((10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1.0)):
        if price < limit:
            return step
    return 5.0


def round_tick_down(price):
    if not price or price <= 0:
        return None
    step = _tick_size(price)
    return round(math.floor(price / step + 1e-9) * step, 2)


def load_prefetch(stock_id, role):
    """讀 batch_prefetch 產生的 reports/data/<代號>_<role>.json 的 data 欄位；缺檔回傳空 dict。"""
    path = DATA_DIR / f"{stock_id}_{role}.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload.get("data") or {}


def load_summary(stock_id):
    """總分析員 reports/stocks/<代號>_summary.json（entry_zone、stop_loss、target_1 供行動燈號）。"""
    path = STOCKS_DIR / f"{stock_id}_summary.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def elapsed_fraction(now=None):
    """今日已開盤時間占全日比例；盤前回傳 None，收盤後與假日回傳 1.0。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return 1.0
    minutes = (now.hour * 60 + now.minute) - 9 * 60
    if minutes < 0:
        return None
    return min(max(minutes, 1), TRADING_MINUTES) / TRADING_MINUTES


def orderbook_pattern(price, vwap, ratio, tick_side, ask_vols):
    """五檔掛單型態：先看主動成交方向與均價線，再看五檔厚薄。"""
    if tick_side == "外盤主動買進" and price and vwap and price >= vwap:
        return "外盤主動掃貨"
    if tick_side == "內盤主動賣出" and price and vwap and price < vwap:
        return "內盤賣壓湧現"
    if ratio is not None and ratio >= 1.5:
        return "下檔委買厚實支撐"
    big_ask = len(ask_vols) >= 3 and max(ask_vols) >= 2 * (sum(ask_vols) / len(ask_vols))
    if ratio is not None and (ratio <= 0.67 or (big_ask and ratio < 1)):
        return "上檔委賣大單壓頂"
    return "掛單均衡"


def intraday_trend(pos, yahoo, kline):
    """VWAP 位置、開高低、近 15 分鐘動能、盤中量比、五檔掛單型態。"""
    y = yahoo if yahoo and not yahoo.get("error") else {}
    price, vwap = pos.get("current_price"), pos.get("vwap")
    day_open = pos.get("open") or y.get("day_open")
    day_high = pos.get("high") or y.get("bars_high")
    day_low = pos.get("low") or y.get("bars_low")
    if price and vwap:
        vwap_position = "站上均價線偏多" if price >= vwap else "跌破均價線偏空"
        vwap_gap_pct = round((price / vwap - 1) * 100, 2)
    else:
        vwap_position, vwap_gap_pct = "無 VWAP 資料", None
    acc = pos.get("acc_vol") or y.get("volume_lots") or 0
    frac = elapsed_fraction()
    vol_ma5 = kline.get("vol_ma5")
    est_total = acc / frac if frac else None
    vol_ratio = round(est_total / vol_ma5, 2) if est_total and vol_ma5 else None
    return {
        "vwap": vwap,
        "vwap_position": vwap_position,
        "vwap_gap_pct": vwap_gap_pct,
        "day_open": day_open,
        "day_high": day_high,
        "day_low": day_low,
        "momentum_5m": y.get("momentum_5m", "資料不足"),
        "momentum_5m_pct": y.get("momentum_5m_pct"),
        "momentum_15m": y.get("momentum_15m", "資料不足"),
        "momentum_15m_pct": y.get("momentum_15m_pct"),
        "acc_vol_lots": acc,
        "est_day_volume_lots": round(est_total, 0) if est_total else None,
        "vol_ma5_lots": round(vol_ma5, 0) if vol_ma5 else None,
        "volume_ratio": vol_ratio,
        "bid_vol_sum": pos.get("bid_vol_sum"),
        "ask_vol_sum": pos.get("ask_vol_sum"),
        "orderbook_ratio": pos.get("bid_ask_ratio"),
        "orderbook_pattern": orderbook_pattern(price, vwap, pos.get("bid_ask_ratio"), pos.get("tick_side"),
                                               pos.get("ask_vols") or []),
    }


def quant_suggestion(pos, trend, kline, chips):
    """Python 端量化初步建議（不需 AI）：依 CLAUDE.md 第 5 節順序判定，供 1 秒刷新時立即參考。"""
    price = pos.get("current_price")
    levels = kline.get("levels") or {}
    support, stop = levels.get("defense_support"), levels.get("stop_loss")
    r1, ma5, ma20 = levels.get("resistance_1"), kline.get("ma5"), kline.get("ma20")
    inst = chips.get("institutional") or {}
    f_streak = inst.get("foreign_streak_days") or 0
    t_streak = inst.get("trust_streak_days") or 0
    ratio, side = pos.get("bid_ask_ratio"), pos.get("tick_side")
    vwap, vol_ratio = trend.get("vwap"), trend.get("volume_ratio")
    held = pos.get("in_portfolio")
    net_pct = pos.get("net_return_pct")
    out = {"signal": "⚪ 續抱觀察", "reason": "", "trailing_stop": None,
           "intraday_support": None, "intraday_resistance": None,
           "inputs": {"defense_support": support, "stop_loss": stop, "resistance_1": r1, "ma5": ma5, "ma20": ma20,
                      "foreign_streak_days": f_streak, "trust_streak_days": t_streak,
                      "kline_date": kline.get("date"), "chips_date": inst.get("latest_date")}}
    if not price:
        out.update(signal="⚪ 續抱觀察", reason="無盤中報價，維持原計畫。")
        return out

    lows = [v for v in (support, trend.get("day_low")) if v and v <= price]
    highs = [v for v in (trend.get("day_high"), r1, ma20) if v and v > price]
    out["intraday_support"] = max(lows) if lows else stop
    out["intraday_resistance"] = min(highs) if highs else r1
    if held and pos.get("breakeven_price"):
        out["trailing_stop"] = round_tick_down(max(v for v in (support, pos["breakeven_price"], price * 0.95) if v))
    above_vwap = bool(vwap and price >= vwap)
    flow = f"五檔比 {ratio}、{side}、{trend.get('vwap_position')}、量比 {vol_ratio}、外資連{'買' if f_streak > 0 else '賣'} {abs(f_streak)} 日"

    if not kline:
        out.update(signal="⚪ 續抱觀察", reason="缺 K 線預抓檔，請先執行 batch_prefetch.py。" + flow)
    elif stop and price <= stop:
        out.update(signal="🔴 減碼停損", reason=f"現價 {price} 跌破絕對停損 {stop}，建議全數退場。")
    elif support and price < support:
        out.update(signal="🔴 減碼停損", reason=f"現價 {price} 跌破防守支撐 {support}（停損 {stop}），建議先減碼一半。")
    elif held and net_pct is not None and net_pct > 0 and (
            (r1 and price >= r1 * 0.985) or (ratio is not None and ratio < 0.8 and side == "內盤主動賣出")):
        out.update(signal="💰 分批獲利",
                   reason=f"帳面獲利且{'逼近第一壓力 ' + str(r1) if r1 and price >= r1 * 0.985 else '五檔賣壓湧現'}，先了結一半，移動停利 {out['trailing_stop']}。")
    elif held and net_pct is not None and net_pct > 0 and above_vwap and ma20 and price > ma20 \
            and f_streak > 0 and (vol_ratio or 0) >= 1:
        out.update(signal="🟢 順勢加碼", reason=f"獲利中、站上 VWAP 與 MA20 {ma20:.1f}、外資連買、量比 {vol_ratio}，可小量順勢加碼。")
    elif held and net_pct is not None and net_pct > 0:
        out.update(signal="⚪ 續抱觀察", reason=f"帳面獲利、未遇壓，續抱並以 {out['trailing_stop']} 移動停利。{flow}")
    elif held and support and price <= support * 1.02 and (ratio or 0) >= 1 and side != "內盤主動賣出" \
            and f_streak >= 0 and t_streak >= 0:
        out.update(signal="🟡 支撐攤平",
                   reason=f"貼近防守支撐 {support}（2% 內）、五檔委買撐盤、法人未連賣；條件式小量攤平（≤ 原部位 1/3），停損 {stop}。")
    elif held:
        out.update(signal="⚪ 續抱觀察", reason=f"帳面虧損但在支撐 {support} 之上，不攤平，停損 {stop}。{flow}")
    elif above_vwap and ma20 and price > ma20 and f_streak > 0:
        out.update(signal="🟢 順勢加碼", reason=f"非持股：站上 VWAP 與 MA20、外資連買，可留意。{flow}")
    else:
        out.update(signal="⚪ 續抱觀察", reason=f"非持股：未出現進場訊號。{flow}")
    return out


# ---------------------------------------------------------------- 損益計算

def compute_position(cost_price, shares, current_price):
    """台股未實現淨損益：市值 − 賣出手續費 − 證交稅 −（成本 + 買進手續費）。費用依券商慣例無條件捨去。"""
    cost = cost_price * shares
    buy_fee = max(MIN_FEE, math.floor(cost * FEE_RATE)) if shares > 0 else 0
    invested = cost + buy_fee
    result = {
        "total_cost": round(cost, 2),
        "buy_fee": buy_fee,
        "breakeven_price": round(invested / (shares * (1 - FEE_RATE - TAX_RATE)), 2) if shares > 0 else None,
        "market_value": None, "sell_fee": None, "tax": None,
        "gross_pnl": None, "net_pnl": None, "net_return_pct": None,
    }
    if current_price is None or shares <= 0:
        return result
    mv = current_price * shares
    sell_fee = max(MIN_FEE, math.floor(mv * FEE_RATE))
    tax = math.floor(mv * TAX_RATE)
    net = mv - sell_fee - tax - invested
    result.update({
        "market_value": round(mv, 2),
        "sell_fee": sell_fee,
        "tax": tax,
        "gross_pnl": round(mv - cost, 2),
        "net_pnl": round(net, 2),
        "net_return_pct": round(net / invested * 100, 2) if invested else None,
    })
    return result


# ---------------------------------------------------------------- 持倉檔讀寫

def load_portfolio(path=PORTFOLIO_PATH):
    """讀取並驗證持倉清單；格式錯誤的列略過並印出警告。"""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    rows = []
    for i, r in enumerate(raw if isinstance(raw, list) else []):
        try:
            stock_id = str(r["stock_id"]).strip()
            cost = float(r["cost_price"])
            shares = int(r["shares"])
        except (KeyError, TypeError, ValueError):
            print(f"[portfolio] 第 {i + 1} 筆格式錯誤，已略過：{r}", file=sys.stderr)
            continue
        if not stock_id or cost <= 0 or shares <= 0:
            print(f"[portfolio] 第 {i + 1} 筆數值不合法，已略過：{r}", file=sys.stderr)
            continue
        rows.append({"stock_id": stock_id, "name": str(r.get("name") or ""), "cost_price": cost,
                     "shares": shares, "note": str(r.get("note") or "")})
    return rows


def save_portfolio(rows, path=PORTFOLIO_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_status(path=STATUS_PATH):
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _totals(positions):
    held = [p for p in positions if p.get("in_portfolio") and p.get("market_value") is not None]
    invested = sum(p["total_cost"] + p["buy_fee"] for p in held)
    net = sum(p["net_pnl"] for p in held)
    return {
        "positions": len(held),
        "total_cost": round(sum(p["total_cost"] for p in held), 2),
        "total_market_value": round(sum(p["market_value"] for p in held), 2),
        "total_net_pnl": round(net, 2),
        "total_net_return_pct": round(net / invested * 100, 2) if invested else None,
    }


# ---------------------------------------------------------------- 主流程

def build_positions(targets, holdings, quotes, yahoo=None):
    yahoo = yahoo or {}
    positions = []
    for stock_id in targets:
        q = quotes.get(stock_id)
        h = holdings.get(stock_id)
        pos = {"stock_id": stock_id, "name": (h or {}).get("name") or (q["name"] if q else stock_id),
               "in_portfolio": h is not None}
        if h:
            pos.update({"shares": h["shares"], "cost_price": h["cost_price"], "note": h["note"]})
        if q is None:
            pos["error"] = "MIS 查無此代號報價"
            if h:
                pos.update(compute_position(h["cost_price"], h["shares"], None))
            positions.append(pos)
            continue
        pos.update(analyze_quote(q, yahoo.get(stock_id)))
        if h:
            pos.update(compute_position(h["cost_price"], h["shares"], pos["current_price"]))
        kline, chips = load_prefetch(stock_id, "kline"), load_prefetch(stock_id, "chips")
        pos["intraday_trend"] = intraday_trend(pos, yahoo.get(stock_id), kline)
        pos["quant_suggestion"] = quant_suggestion(pos, pos["intraday_trend"], kline, chips)
        pos["action_light"] = action_light(pos["current_price"], load_summary(stock_id))
        positions.append(pos)
    return positions


def update_snapshot(targets, quotes, positions):
    """把本次報價合併進 intraday_snapshot.json（與 intraday_sentinel.py 同格式），保留其他股票與哨兵記錄的大單。"""
    snap = {}
    if SNAPSHOT_PATH.exists():
        try:
            with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, json.JSONDecodeError):
            snap = {}
    stocks = snap.get("stocks") or {}
    by_id = {p["stock_id"]: p for p in positions}
    side_short = {"外盤主動買進": "外盤", "內盤主動賣出": "內盤"}
    for sid in targets:
        q, p = quotes.get(sid), by_id.get(sid)
        if not q or not p:
            continue
        old = stocks.get(sid) or {}
        entry = dict(q)
        entry.update({
            "price": p["current_price"],
            "price_source": p["price_source"],
            "price_is_estimate": p["price_source"] != "成交價",
            "pct_change": p["change_pct"],
            "bid_ratio": p["bid_share"],
            "last_side": side_short.get(p["tick_side"], p["tick_side"]),
            "last_side_source": p["tick_side_source"],
            "vwap": p["vwap"],
            "est_outer_lots": p["est_outer_lots"],
            "est_inner_lots": p["est_inner_lots"],
            "recent_large_ticks": old.get("recent_large_ticks", []),
            "writer": "tools/portfolio_intraday.py",
        })
        stocks[sid] = entry
    snap["stocks"] = stocks
    snap["updated_at"] = datetime.now().isoformat(timespec="seconds")
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SNAPSHOT_PATH)


def run(stock=None, save=True):
    """抓報價並計算損益，回傳狀態 dict。指定 stock 時只更新該檔並合併回既有狀態檔。"""
    holdings = {p["stock_id"]: p for p in load_portfolio()}
    targets = [stock] if stock else list(holdings)
    quotes, yahoo, _ = fetch_realtime(targets)
    positions = build_positions(targets, holdings, quotes, yahoo)
    if save:
        update_snapshot(targets, quotes, positions)

    if stock and save:
        previous = load_status().get("positions", [])
        merged = [p for p in previous if p.get("stock_id") != stock and p.get("stock_id") in holdings]
        merged.append(positions[0])
        order = {sid: i for i, sid in enumerate(holdings)}
        merged.sort(key=lambda p: order.get(p["stock_id"], len(order)))
        all_positions = merged
    else:
        all_positions = positions

    status = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fee_rate": FEE_RATE,
        "tax_rate": TAX_RATE,
        "min_fee": MIN_FEE,
        "positions": all_positions,
        "totals": _totals(all_positions),
    }
    if save:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATUS_PATH)
        try:  # 學習資料庫：依時段追加實盤快照並回溯驗算預測（失敗不影響報價）
            from tools import learning_engine
            learning_engine.on_price_update("portfolio_intraday")
        except Exception as exc:
            print(f"⚠️ 學習資料庫更新失敗：{exc!r}", file=sys.stderr)
    if stock:
        status = dict(status, positions=positions, totals=_totals(positions))
    return status


def main():
    parser = argparse.ArgumentParser(description="盤中即時持倉報價、五檔與未實現損益")
    parser.add_argument("--stock", help="只查單一代號；不帶則掃描 reports/portfolio.json")
    parser.add_argument("--no-save", action="store_true", help="不寫入 reports/portfolio_live_status.json")
    args = parser.parse_args()
    stock = args.stock.strip() if args.stock else None

    if not stock and not load_portfolio():
        print(json.dumps({"error": f"持倉清單為空，請編輯 {PORTFOLIO_PATH.relative_to(ROOT).as_posix()}"},
                         ensure_ascii=False, indent=2))
        sys.exit(1)
    try:
        status = run(stock, save=not args.no_save)
    except MISError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        sys.exit(2)
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
