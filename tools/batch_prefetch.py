# -*- coding: utf-8 -*-
"""批次資料預抓引擎：一次抓齊持股與自選股的三角色數據，讓分析員直接讀檔、不再各自呼叫 FinMind。

用法：
    C:/Users/singk/anaconda3/python.exe tools/batch_prefetch.py               # portfolio.json 持股 + .env WATCHLIST
    C:/Users/singk/anaconda3/python.exe tools/batch_prefetch.py --stock 2408  # 單檔（可重複或以逗號分隔）

每檔輸出三個檔案到 reports/data/：
    <代號>_fundamental.json  近 6 月營收（MoM/YoY）、近 5 季損益與三率、近 14 日新聞
    <代號>_chips.json        三大法人 1/5/10/20 日張數、連買賣天數、外資急翻次數、近 10 日明細；
                             融資融券 1/5 日增減與券資比；外資持股 5/20 日變化；
                             逐筆內外盤/VWAP/大單（FinMind 逐筆，無權限時 Yahoo 1 分 K 推估並標註來源）
    <代號>_kline.json        MA/MACD/RSI/乖離、相對大盤強弱、防守支撐/絕對停損/第一第二壓力
另寫 reports/data/prefetch_manifest.json 記錄本次抓取時間、成功與失敗項目。
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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd

from tools.finmind_cli import (  # noqa: E402
    BIG_TICK_LOTS,
    DATA_DIR,
    FinMindError,
    _days_ago,
    fetch_dataset,
    load_snapshot,
    role_fundamental,
    role_kline,
    safe_fetch,
    save_json,
    to_native,
)
from tools.portfolio_intraday import load_portfolio  # noqa: E402
from tools.yahoo_quote import YahooError, fetch_intraday  # noqa: E402

MANIFEST_PATH = DATA_DIR / "prefetch_manifest.json"

# 依需求：外資只計 Foreign_Investor；自營商 = Dealer_self（自行買賣）+ Dealer_Hedging（權證避險），
# 兩者另外拆開輸出，供籌碼王（權證小哥邏輯）判讀權證造市避險動能。
INST_GROUP = {
    "Foreign_Investor": "foreign",
    "Investment_Trust": "trust",
    "Dealer_self": "dealer_self",
    "Dealer_Hedging": "dealer_hedging",
}
INST_KEYS = ("foreign", "trust", "dealer", "dealer_self", "dealer_hedging")
_tick_denied = threading.Event()


# ---------------------------------------------------------------- 籌碼王進階籌碼指標

def _streak(values):
    """連續買/賣超天數：正數為連買、負數為連賣、0 為最新一日無買賣。"""
    if not values or values[-1] == 0:
        return 0
    sign = 1 if values[-1] > 0 else -1
    n = 0
    for v in reversed(values):
        if v * sign > 0:
            n += 1
        else:
            break
    return n * sign


def _sign_flips(values):
    """正負號改變次數（略過 0）。"""
    signs = [1 if v > 0 else -1 for v in values if v != 0]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b)


def chips_institutional(stock_id):
    inst = fetch_dataset("TaiwanStockInstitutionalInvestorsBuySell", stock_id, _days_ago(60))
    if inst.empty:
        raise FinMindError(f"TaiwanStockInstitutionalInvestorsBuySell({stock_id}) 無資料")
    inst["buy"] = pd.to_numeric(inst["buy"], errors="coerce").fillna(0)
    inst["sell"] = pd.to_numeric(inst["sell"], errors="coerce").fillna(0)
    inst["group"] = inst["name"].map(INST_GROUP)
    inst = inst.dropna(subset=["group"])
    inst["net_lots"] = (inst["buy"] - inst["sell"]) / 1000.0
    pv = inst.pivot_table(index="date", columns="group", values="net_lots", aggfunc="sum").fillna(0.0).sort_index()
    for c in ("foreign", "trust", "dealer_self", "dealer_hedging"):
        if c not in pv.columns:
            pv[c] = 0.0
    pv["dealer"] = pv["dealer_self"] + pv["dealer_hedging"]
    out = {"latest_date": str(pv.index[-1]), "unit": "張"}
    for c in INST_KEYS:
        s = pv[c].round(0).tolist()
        out[c] = {
            "last_1d": s[-1],
            "sum_5d": round(sum(s[-5:]), 0),
            "sum_10d": round(sum(s[-10:]), 0),
            "sum_20d": round(sum(s[-20:]), 0),
            "streak_days": _streak(s),
        }
    out["foreign_streak_days"] = out["foreign"]["streak_days"]
    out["trust_streak_days"] = out["trust"]["streak_days"]
    out["foreign_flip_count_10d"] = _sign_flips(pv["foreign"].round(0).tail(10).tolist())
    out["daily_10d"] = [
        {"date": str(d), "foreign": round(r["foreign"], 0), "trust": round(r["trust"], 0),
         "dealer": round(r["dealer"], 0), "total": round(r["foreign"] + r["trust"] + r["dealer"], 0),
         "dealer_self": round(r["dealer_self"], 0), "dealer_hedging": round(r["dealer_hedging"], 0)}
        for d, r in pv.tail(10).iterrows()
    ]
    return out


def chips_margin(stock_id):
    mg = fetch_dataset("TaiwanStockMarginPurchaseShortSale", stock_id, _days_ago(45))
    if mg.empty:
        raise FinMindError(f"TaiwanStockMarginPurchaseShortSale({stock_id}) 無資料")
    cols = ("MarginPurchaseTodayBalance", "MarginPurchaseYesterdayBalance",
            "ShortSaleTodayBalance", "ShortSaleYesterdayBalance")
    for c in cols:
        mg[c] = pd.to_numeric(mg[c], errors="coerce").fillna(0)
    mg = mg.sort_values("date").reset_index(drop=True)
    last = mg.iloc[-1]
    first5 = mg.iloc[max(0, len(mg) - 5)]
    margin_bal = float(last["MarginPurchaseTodayBalance"])
    short_bal = float(last["ShortSaleTodayBalance"])
    daily = []
    for _, r in mg.tail(10).iterrows():
        mb, sb = float(r["MarginPurchaseTodayBalance"]), float(r["ShortSaleTodayBalance"])
        daily.append({
            "date": str(r["date"]),
            "margin_balance": mb,
            "margin_change": mb - float(r["MarginPurchaseYesterdayBalance"]),
            "short_balance": sb,
            "short_change": sb - float(r["ShortSaleYesterdayBalance"]),
            "short_margin_ratio_pct": round(sb / mb * 100, 2) if mb else None,
        })
    return {
        "latest_date": str(last["date"]),
        "unit": "張",
        "margin_balance": margin_bal,
        "margin_change_1d": margin_bal - float(last["MarginPurchaseYesterdayBalance"]),
        "margin_change_5d": margin_bal - float(first5["MarginPurchaseYesterdayBalance"]),
        "short_balance": short_bal,
        "short_change_1d": short_bal - float(last["ShortSaleYesterdayBalance"]),
        "short_change_5d": short_bal - float(first5["ShortSaleYesterdayBalance"]),
        "short_margin_ratio_pct": round(short_bal / margin_bal * 100, 2) if margin_bal else None,
        "daily_10d": daily,
    }


def chips_shareholding(stock_id):
    sh = fetch_dataset("TaiwanStockShareholding", stock_id, _days_ago(60))
    if sh.empty or "ForeignInvestmentSharesRatio" not in sh.columns:
        raise FinMindError(f"TaiwanStockShareholding({stock_id}) 無資料")
    sh["ratio"] = pd.to_numeric(sh["ForeignInvestmentSharesRatio"], errors="coerce")
    s = sh.sort_values("date").dropna(subset=["ratio"]).reset_index(drop=True)
    latest = float(s["ratio"].iloc[-1])

    def change(n):
        return round(latest - float(s["ratio"].iloc[-1 - n]), 2) if len(s) > n else None

    return {
        "latest_date": str(s["date"].iloc[-1]),
        "foreign_ratio_pct": round(latest, 2),
        "change_5d_pct_points": change(5),
        "change_20d_pct_points": change(20),
    }


def _daily_vwap(stock_id):
    """FinMind 日 K 的成交金額 ÷ 成交股數 = 當日精確 VWAP。"""
    px, err = safe_fetch("TaiwanStockPrice", stock_id, _days_ago(10))
    if err or px.empty or "Trading_money" not in px.columns:
        return None, None
    px = px.sort_values("date")
    money = pd.to_numeric(px["Trading_money"].iloc[-1], errors="coerce")
    vol = pd.to_numeric(px["Trading_Volume"].iloc[-1], errors="coerce")
    if not vol:
        return None, str(px["date"].iloc[-1])
    return round(float(money / vol), 2), str(px["date"].iloc[-1])


def chips_tick(stock_id, trade_date):
    """逐筆內外盤：FinMind TaiwanStockPriceTick 優先；免費版無權限時改用 Yahoo 1 分 K + 日 K VWAP + 哨兵大單。"""
    if _tick_denied.is_set():
        tick, err = pd.DataFrame(), "FinMind 逐筆需付費會員（本輪已確認無權限，不再重試）"
    else:
        tick, err = safe_fetch("TaiwanStockPriceTick", stock_id, trade_date)
        if err and "level is free" in err:
            _tick_denied.set()
    if not tick.empty:
        tick["volume"] = pd.to_numeric(tick["volume"], errors="coerce").fillna(0)
        tick["deal_price"] = pd.to_numeric(tick["deal_price"], errors="coerce")
        tick["TickType"] = pd.to_numeric(tick["TickType"], errors="coerce")
        outer = float(tick.loc[tick["TickType"] == 2, "volume"].sum())
        inner = float(tick.loc[tick["TickType"] == 1, "volume"].sum())
        total = float(tick["volume"].sum())
        big = tick[tick["volume"] >= BIG_TICK_LOTS]
        big_buy = float(big.loc[big["TickType"] == 2, "volume"].sum())
        big_sell = float(big.loc[big["TickType"] == 1, "volume"].sum())
        return {
            "date": trade_date,
            "source": "FinMind TaiwanStockPriceTick（逐筆實際資料）",
            "is_estimate": False,
            "outer_lots": outer,
            "inner_lots": inner,
            "outer_inner_ratio": round(outer / inner, 3) if inner else None,
            "vwap": round(float((tick["deal_price"] * tick["volume"]).sum() / total), 2) if total else None,
            "big_tick_threshold_lots": BIG_TICK_LOTS,
            "big_tick_count": int(len(big)),
            "big_buy_lots": big_buy,
            "big_sell_lots": big_sell,
            "big_net_lots": big_buy - big_sell,
        }

    notes = [f"FinMind 逐筆無資料：{err or '空資料'}"]
    out = {"date": trade_date, "is_estimate": True, "finmind_tick_error": err,
           "big_tick_threshold_lots": BIG_TICK_LOTS}
    try:
        y = fetch_intraday(stock_id)
        out.update({
            "source": "Yahoo Finance 1 分 K 推估",
            "yahoo_date": (y.get("market_time") or "")[:10],
            "outer_lots": y["est_outer_lots"],
            "inner_lots": y["est_inner_lots"],
            "outer_inner_ratio": y["est_outer_inner_ratio"],
            "vwap_intraday_1m": y["vwap"],
            "volume_lots": y["volume_lots"],
            "method": y["method"],
        })
    except YahooError as exc:
        out["source"] = "日 K 量價推估（Yahoo 亦失敗）"
        notes.append(f"Yahoo 失敗：{exc}")

    vwap, vwap_date = _daily_vwap(stock_id)
    out["vwap_daily_exact"] = vwap
    out["vwap_daily_date"] = vwap_date
    out["vwap"] = vwap if vwap is not None else out.get("vwap_intraday_1m")
    notes.append("VWAP 優先採 FinMind 日 K 成交金額 ÷ 成交股數（精確值）；vwap_intraday_1m 為 1 分 K 典型價加權")

    snap = load_snapshot(stock_id)
    large = (snap or {}).get("recent_large_ticks") or []
    snap_day = str((snap or {}).get("snapshot_updated_at") or "")[:10]
    if large and snap_day == datetime.now().strftime("%Y-%m-%d"):
        big_buy = float(sum(t.get("lots", 0) for t in large if t.get("side") == "外盤"))
        big_sell = float(sum(t.get("lots", 0) for t in large if t.get("side") == "內盤"))
        out.update({"big_tick_count": len(large), "big_buy_lots": big_buy, "big_sell_lots": big_sell,
                    "big_net_lots": big_buy - big_sell,
                    "big_tick_source": "intraday_sentinel.py 盤中輪詢記錄（僅含哨兵運行期間）"})
    else:
        out.update({"big_tick_count": None, "big_buy_lots": None, "big_sell_lots": None, "big_net_lots": None,
                    "big_tick_source": "無：免費版無逐筆、1 分 K 無法辨識單筆大單，且今日哨兵未記錄"})
    out["notes"] = notes
    return out


def build_chips(stock_id):
    """組合籌碼王全部籌碼指標；任一區塊失敗寫入 errors，不影響其他區塊。"""
    errors = []
    data = {"stock_id": stock_id, "mode": "prefetch", "unit": "張"}
    for key, func in (("institutional", chips_institutional), ("margin", chips_margin),
                      ("shareholding", chips_shareholding)):
        try:
            data[key] = func(stock_id)
        except FinMindError as exc:
            data[key] = None
            errors.append(str(exc))
    trade_date = (data.get("institutional") or {}).get("latest_date") or datetime.now().strftime("%Y-%m-%d")
    data["tick"] = chips_tick(stock_id, trade_date)
    data["errors"] = errors
    return "", data


ROLE_FUNCS = {
    "fundamental": role_fundamental,
    "chips": build_chips,
    "kline": role_kline,
}


def resolve_stocks(cli_stocks):
    """--stock 優先；否則合併 portfolio.json 持股（排前）與 .env WATCHLIST，保留順序去重。"""
    if cli_stocks:
        ids = []
        for item in cli_stocks:
            ids += [s.strip() for s in item.split(",") if s.strip()]
        return list(dict.fromkeys(ids))
    holdings = [p["stock_id"] for p in load_portfolio()]
    watch = [s.strip() for s in os.getenv("WATCHLIST", "").split(",") if s.strip()]
    return list(dict.fromkeys(holdings + watch))


def write_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(to_native(payload), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# 已有新鮮檔案（且上次無資料缺口）時不重抓，節省 FinMind 額度（未登入 Token 每小時約 300 次）。
# 籌碼 4 小時快取；K 線改走 Yahoo（不耗 FinMind 額度），30 分鐘即可重抓。
ROLE_TTL_MIN = {"fundamental": 360, "chips": 240, "kline": 30}
SECTOR_PATH = ROOT / "reports" / "sector_watchlist.json"


def load_sector_ids():
    if not SECTOR_PATH.exists():
        return []
    with open(SECTOR_PATH, "r", encoding="utf-8") as f:
        return [str(r["stock_id"]) for r in json.load(f) if r.get("stock_id")]
QUOTA_MARKERS = ("upper limit", "HTTP 402")
_quota_hit = threading.Event()


def _is_quota_error(text):
    return any(m in str(text) for m in QUOTA_MARKERS)


def existing_file(stock_id, role):
    """回傳 (fetched_at datetime 或 None, 上次是否無資料缺口)。"""
    path = DATA_DIR / f"{stock_id}_{role}.json"
    if not path.exists():
        return None, False
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        fetched = datetime.strptime(payload["fetched_at"], "%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None, False
    return fetched, not (payload.get("data") or {}).get("errors")


def fetch_one(stock_id, role, force=False):
    """抓單檔單角色並寫檔，回傳 dict(status=fetched/fresh/failed)。額度用完時保留舊檔不覆寫。"""
    started = time.perf_counter()

    def result(status, msg):
        return {"stock_id": stock_id, "role": role, "status": status, "message": msg,
                "elapsed_sec": round(time.perf_counter() - started, 2)}

    fetched, clean = existing_file(stock_id, role)
    if not force and fetched and clean and datetime.now() - fetched < timedelta(minutes=ROLE_TTL_MIN[role]):
        return result("fresh", f"沿用 {fetched:%H:%M:%S} 的檔案（{ROLE_TTL_MIN[role]} 分鐘內不重抓）")
    keep = f"，保留舊檔（{fetched:%Y-%m-%d %H:%M}）" if fetched else "，且無舊檔"
    if _quota_hit.is_set():
        return result("failed", "FinMind 額度已用完，本輪略過" + keep)
    try:
        _, data = ROLE_FUNCS[role](stock_id)
    except FinMindError as exc:
        if _is_quota_error(exc):
            _quota_hit.set()
        return result("failed", str(exc) + keep)
    except Exception as exc:  # 單檔失敗不影響其他檔
        return result("failed", f"{type(exc).__name__}: {exc}" + keep)
    warnings = data.get("errors") or []
    if any(_is_quota_error(w) for w in warnings):
        _quota_hit.set()
        return result("failed", "FinMind 額度用完導致資料不全，不寫檔" + keep)
    save_json(stock_id, role, data, source="tools/batch_prefetch.py")
    return result("fetched", "部分資料缺口：" + "；".join(warnings) if warnings else "OK")


def run(stocks, workers=8, force=False, roles=None):
    started = time.perf_counter()
    _quota_hit.clear()
    roles = [r for r in (roles or ROLE_FUNCS) if r in ROLE_FUNCS]
    jobs = [(s, r) for s in stocks for r in roles]
    results = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch_one, s, r, force) for s, r in jobs]
        for fut in as_completed(futures):
            results.append(fut.result())
    elapsed = time.perf_counter() - started

    order = {s: i for i, s in enumerate(stocks)}
    role_order = {r: i for i, r in enumerate(ROLE_FUNCS)}
    results.sort(key=lambda x: (order[x["stock_id"]], role_order[x["role"]]))
    for r in results:
        r["ok"] = r["status"] != "failed"
        r["file"] = f"reports/data/{r['stock_id']}_{r['role']}.json" if existing_file(r["stock_id"], r["role"])[0] else None
    manifest = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(elapsed, 2),
        "quota_exhausted": _quota_hit.is_set(),
        "stocks": stocks,
        "results": results,
    }
    write_atomic(MANIFEST_PATH, manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description="批次預抓持股與自選股的基本面、籌碼、K 線數據")
    parser.add_argument("--stock", action="append", help="股票代號，可重複或以逗號分隔；省略則用持股 + WATCHLIST")
    parser.add_argument("--workers", type=int, default=8, help="平行執行緒數（預設 8）")
    parser.add_argument("--force", action="store_true", help="忽略新鮮度快取，全部重抓")
    parser.add_argument("--roles", default="fundamental,chips,kline",
                        help="要抓的角色，逗號分隔（預設三者皆抓；20 強觀察池建議 chips,kline）")
    parser.add_argument("--sector", action="store_true", help="一併納入 reports/sector_watchlist.json 的 20 大龍頭")
    args = parser.parse_args()

    stocks = resolve_stocks(args.stock)
    if args.sector:
        stocks = list(dict.fromkeys(stocks + load_sector_ids()))
    roles = [r.strip() for r in args.roles.split(",") if r.strip() in ROLE_FUNCS]
    if not stocks:
        print("[錯誤] 沒有任何股票：portfolio.json 為空且 .env WATCHLIST 未設定，請用 --stock 指定。")
        sys.exit(1)

    print(f"預抓 {len(stocks)} 檔 × {len(roles)} 角色（{','.join(roles)}）：{', '.join(stocks)}")
    manifest = run(stocks, args.workers, args.force, roles)
    marks = {"fetched": "✅", "fresh": "♻️", "failed": "❌"}
    failed = 0
    for r in manifest["results"]:
        failed += 0 if r["ok"] else 1
        print(f"{marks[r['status']]} {r['stock_id']:<6} {r['role']:<12} {r['elapsed_sec']:>6.2f}s  {r['message']}")
    print(f"\n總耗時 {manifest['elapsed_sec']:.2f} 秒；成功/沿用 {len(manifest['results']) - failed}、失敗 {failed}。"
          f"清單：{MANIFEST_PATH.relative_to(ROOT).as_posix()}")
    if manifest["quota_exhausted"]:
        print("⚠️ FinMind 每小時請求額度已用完：失敗項目已保留舊檔，約 1 小時後再試；"
              "在 .env 填入免費註冊的 FINMIND_API_TOKEN 可提高額度。")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
