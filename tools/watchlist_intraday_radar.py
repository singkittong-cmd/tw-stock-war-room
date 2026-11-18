# -*- coding: utf-8 -*-
"""20 強建議買進價監控表：比對即時報價與盤後建議買進價區間，判定買進燈號（0 Token）。

用法：
    C:/Users/singk/anaconda3/python.exe tools/watchlist_intraday_radar.py

燈號（依序判定）：
    BROKEN       🚨 跌破停損價・暫不買進：price < stop_loss
    HIT_ENTRY    🎯 達建議買進價：entry_low <= price <= entry_high
    BELOW_ENTRY  ⬇️ 低於建議買進價、未破停損價：stop_loss <= price < entry_low（等止穩）
    APPROACHING  👀 接近買進價 (差距1.5%內)：0 < dist_to_entry_pct <= 1.5
    WAITING      ⏳ 高於買進價・等待回檔：dist_to_entry_pct > 1.5
dist_to_entry_pct = (price - entry_high) / entry_high * 100
結果寫入 reports/watchlist_radar_live.json；new_hits 為本次才由其他狀態轉為 HIT_ENTRY 的代號。
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
import time
from datetime import datetime

from tools.portfolio_intraday import analyze_quote  # noqa: E402
from tools.realtime_quote_engine import fetch_realtime  # noqa: E402

SECTOR_PATH = ROOT / "reports" / "sector_watchlist.json"
STANDBY_PATH = ROOT / "reports" / "watchlist_standby.json"
RADAR_PATH = ROOT / "reports" / "watchlist_radar_live.json"
APPROACH_PCT = 1.5
LARGE_TICK_LOTS = int(os.getenv("LARGE_TICK_THRESHOLD_LOTS", "50"))

SIGNAL_LABELS = {
    "BROKEN": "🚨 跌破停損價・暫不買進",
    "HIT_ENTRY": "🎯 達建議買進價",
    "BELOW_ENTRY": "⬇️ 低於建議買進價、未破停損價",
    "APPROACHING": "👀 接近買進價 (差距1.5%內)",
    "WAITING": "⏳ 高於買進價・等待回檔",
    "NO_PLAN": "⚪ 尚無備用計畫",
    "NO_QUOTE": "⚪ 無報價",
}


def _load(path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def classify(price, plan, ratio):
    """回傳 (燈號代碼, 距進場區上限 %, 補充提示)。"""
    if price is None:
        return "NO_QUOTE", None, ""
    if not plan or plan.get("entry_high") is None:
        return "NO_PLAN", None, "請先執行 build_watchlist_standby.py"
    lo, hi, stop = plan["entry_low"], plan["entry_high"], plan.get("stop_loss")
    dist = round((price - hi) / hi * 100, 2)
    if stop is not None and price < stop:
        return "BROKEN", dist, "跌破停損價・暫不買進"
    if lo <= price <= hi:
        if ratio is not None and ratio >= 0.7:
            hint = "🟢 買盤有撐，可掛單買進"
        elif ratio is not None and ratio < 0.5:
            hint = "⚠️ 達建議買進價但賣壓重，等止穩"
        else:
            hint = "達建議買進價，觀察五檔與 VWAP 再分批買進"
        rr = plan.get("risk_reward")
        if rr is not None and rr < 1.5:
            hint += f"（風報比 {rr} < 1.5，僅觀察不買進）"
        return "HIT_ENTRY", dist, hint
    if price < lo:
        return "BELOW_ENTRY", dist, "已低於建議買進價，等止穩再買進"
    if dist <= APPROACH_PCT:
        return "APPROACHING", dist, "準備掛單買進"
    return "WAITING", dist, "高於買進價・等待回檔"


def big_tick_flow(q, analysis):
    """單筆大單動向：MIS 當盤成交張數 ≥ 門檻時依當盤方向標示。"""
    tv = (q or {}).get("tick_vol") or 0
    if tv >= LARGE_TICK_LOTS:
        side = analysis.get("tick_side", "")
        tag = "外盤" if "外盤" in side else "內盤" if "內盤" in side else "未明"
        return f"大單{tag} {tv} 張"
    return f"當盤 {tv} 張"


def run(save=True):
    started = time.perf_counter()
    sector = _load(SECTOR_PATH, [])
    standby = _load(STANDBY_PATH, {})
    plans = standby.get("stocks") or {}
    previous = {r["stock_id"]: r.get("signal") for r in (_load(RADAR_PATH, {}).get("results") or [])}
    ids = [r["stock_id"] for r in sector]
    quotes, yahoo, _ = fetch_realtime(ids)

    results, new_hits = [], []
    for item in sector:
        sid = item["stock_id"]
        q, plan = quotes.get(sid), plans.get(sid) or {}
        a = analyze_quote(q, yahoo.get(sid)) if q else {}
        if not q and (yahoo.get(sid) or {}).get("price"):
            y = yahoo[sid]
            a = {"current_price": y["price"], "price_source": "Yahoo（MIS 無此代號）", "vwap": y.get("vwap"),
                 "change_pct": round((y["price"] / y["prev_close"] - 1) * 100, 2) if y.get("prev_close") else None}
        price = a.get("current_price")
        ratio = a.get("bid_ask_ratio")
        signal, dist, hint = classify(price, plan, ratio)
        if signal == "HIT_ENTRY" and previous.get(sid) != "HIT_ENTRY":
            new_hits.append(sid)
        results.append({
            "stock_id": sid,
            "name": item.get("name"),
            "sector": item.get("sector"),
            "pool": item.get("pool"),
            "pool_score": item.get("score"),
            "price": price,
            "price_source": a.get("price_source"),
            "price_age_sec": a.get("price_age_sec"),
            "change_pct": a.get("change_pct"),
            "open": a.get("open"),
            "high": a.get("high"),
            "low": a.get("low"),
            "prev_close": a.get("prev_close"),
            "acc_vol": a.get("acc_vol"),
            "vwap": a.get("vwap"),
            "vwap_position": ("站上 VWAP" if price and a.get("vwap") and price >= a["vwap"]
                              else "跌破 VWAP" if price and a.get("vwap") else "N/A"),
            "bid_ask_ratio": ratio,
            "tick_side": a.get("tick_side"),
            "big_tick_flow": big_tick_flow(q, a),
            "signal": signal,
            "signal_label": SIGNAL_LABELS[signal],
            "signal_hint": hint,
            "dist_to_entry_pct": dist,
            "entry_low": plan.get("entry_low"),
            "entry_high": plan.get("entry_high"),
            "stop_loss": plan.get("stop_loss"),
            "target_1": plan.get("target_1"),
            "risk_reward": plan.get("risk_reward"),
            "strategy_type": plan.get("strategy_type"),
            "chip_trend": plan.get("chip_trend"),
            "chip_score": plan.get("chip_score"),
            "kline_trend": plan.get("kline_trend"),
            "role4_note": plan.get("role4_note"),
        })
    out = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "standby_generated_at": standby.get("generated_at"),
        "elapsed_sec": round(time.perf_counter() - started, 2),
        "counts": {k: sum(1 for r in results if r["signal"] == k) for k in SIGNAL_LABELS},
        "new_hits": new_hits,
        "results": results,
    }
    if save:
        tmp = RADAR_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, RADAR_PATH)
        try:  # 學習資料庫：依時段追加實盤快照並回溯驗算預測（失敗不影響監控表）
            from tools import learning_engine
            learning_engine.on_price_update("watchlist_intraday_radar")
        except Exception as exc:
            print(f"⚠️ 學習資料庫更新失敗：{exc!r}", file=sys.stderr)
    return out


def load_radar():
    return _load(RADAR_PATH, {})


def main():
    parser = argparse.ArgumentParser(description="20 大龍頭建議買進價監控表（0 Token）")
    parser.add_argument("--no-save", action="store_true", help="不寫入 reports/watchlist_radar_live.json")
    args = parser.parse_args()
    out = run(save=not args.no_save)
    print(f"建議買進價監控表 {out['updated_at']}（備用計畫 {out['standby_generated_at']}），耗時 {out['elapsed_sec']} 秒")
    print("燈號統計：" + "、".join(f"{SIGNAL_LABELS[k]} {v}" for k, v in out["counts"].items() if v))
    for r in out["results"]:
        dist = f"{r['dist_to_entry_pct']:+.2f}%" if r["dist_to_entry_pct"] is not None else "N/A"
        print(f"{r['stock_id']:<5} {r['name']:<6} {r['price']}（{r['change_pct']}%）｜{r['signal_label']}｜"
              f"建議買進價 {r['entry_low']}–{r['entry_high']}｜距上限 {dist}｜五檔比 {r['bid_ask_ratio']}｜停損 {r['stop_loss']}"
              + (f"｜{r['signal_hint']}" if r["signal"] == "HIT_ENTRY" else ""))
    if out["new_hits"]:
        print(f"🎯 新達建議買進價：{', '.join(out['new_hits'])}")


if __name__ == "__main__":
    main()
