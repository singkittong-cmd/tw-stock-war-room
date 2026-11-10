# -*- coding: utf-8 -*-
"""台股收盤後大盤與資金總結（日常主畫面區塊 C，0 Token）。

用法：
    C:/Users/singk/anaconda3/python.exe tools/market_close_summary.py [--force]

內容：
- 加權指數收盤結構：Yahoo ^TWII 日 K（收盤、漲跌、MA5/MA20 位置、20 日高低）＋朱家泓趨勢判定。
- 三大法人買賣超金額：FinMind TaiwanStockTotalInstitutionalInvestors（億元）。
- 全市場融資餘額變化：FinMind TaiwanStockTotalMarginPurchaseShortSale（億元，查不到則略過）。
- 強弱族群輪動：reports/watchlist_radar_live.json 20 大龍頭依次產業平均漲跌排序。
- 昨日預測覆盤：reports/memory/market_reflection.json。
輸出 reports/market_close_summary.json；30 分鐘內重跑直接沿用（--force 強制重算）。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import finmind_cli as fm  # noqa: E402
from tools import kline_patterns  # noqa: E402

REPORTS = ROOT / "reports"
OUT_PATH = REPORTS / "market_close_summary.json"
RADAR_PATH = REPORTS / "watchlist_radar_live.json"
REFLECTION_PATH = REPORTS / "memory" / "market_reflection.json"
CACHE_MIN = 30
INST_NAMES = {"Foreign_Investor": "foreign", "Foreign_Dealer_Self": "foreign", "Investment_Trust": "trust",
              "Dealer_self": "dealer", "Dealer_Hedging": "dealer"}


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def taiex_structure():
    df = fm.compute_indicators(fm.fetch_price_yahoo("^TWII", "6mo"))
    last, prev = df.iloc[-1], df.iloc[-2]
    close = float(last["close"])
    zhu = kline_patterns.analyze(df)
    return {
        "date": last["date"].strftime("%Y-%m-%d"),
        "close": round(close, 2),
        "change": round(close - float(prev["close"]), 2),
        "change_pct": round((close / float(prev["close"]) - 1) * 100, 2),
        "high": round(float(last["high"]), 2), "low": round(float(last["low"]), 2),
        "ma5": round(float(last["ma5"]), 2), "ma20": round(float(last["ma20"]), 2), "ma60": round(float(last["ma60"]), 2),
        "high20": round(float(df["high"].tail(20).max()), 2), "low20": round(float(df["low"].tail(20).min()), 2),
        "vol_ratio_20": round(float(last["volume"]) / float(df["volume"].tail(21).head(20).mean()), 2)
        if "volume" in df.columns and float(df["volume"].tail(21).head(20).mean() or 0) > 0 else None,
        "trend": zhu.get("trend"), "ma_order": zhu.get("ma_order"), "granville": zhu.get("granville"),
        "patterns": [p["pattern"] for p in zhu.get("patterns") or []],
    }


def institutional_amounts():
    """三大法人買賣超（億元）：回傳最新一日與近 5 日合計。"""
    df = fm.fetch_dataset("TaiwanStockTotalInstitutionalInvestors", None, fm._days_ago(14))
    df["group"] = df["name"].map(INST_NAMES)
    df = df.dropna(subset=["group"])
    df["net"] = (pd.to_numeric(df["buy"], errors="coerce") - pd.to_numeric(df["sell"], errors="coerce")) / 1e8
    pv = df.pivot_table(index="date", columns="group", values="net", aggfunc="sum").fillna(0.0).sort_index()
    latest = pv.iloc[-1]
    out = {"date": str(pv.index[-1]), "unit": "億元"}
    for c in ("foreign", "trust", "dealer"):
        out[c] = round(float(latest.get(c, 0.0)), 2)
        out[f"{c}_5d"] = round(float(pv[c].tail(5).sum()) if c in pv else 0.0, 2)
    out["total"] = round(out["foreign"] + out["trust"] + out["dealer"], 2)
    return out


def margin_amount():
    df = fm.fetch_dataset("TaiwanStockTotalMarginPurchaseShortSale", None, fm._days_ago(14))
    money = df[df["name"] == "MarginPurchaseMoney"].sort_values("date")
    if money.empty:
        return None
    last = money.iloc[-1]
    today, yes = float(last["TodayBalance"]), float(last["YesBalance"])
    return {"date": str(last["date"]), "balance_100m": round(today / 1e8, 1), "change_100m": round((today - yes) / 1e8, 2)}


def sector_rotation():
    radar = _read_json(RADAR_PATH)
    groups = defaultdict(list)
    for r in radar.get("results") or []:
        if r.get("change_pct") is not None:
            groups[r.get("sector") or "其他"].append(float(r["change_pct"]))
    ranked = sorted(((s, round(sum(v) / len(v), 2)) for s, v in groups.items()), key=lambda x: -x[1])
    return {"as_of": radar.get("updated_at"), "strong": ranked[:3], "weak": ranked[-3:][::-1] if len(ranked) > 3 else []}


def build(force=False):
    cached = _read_json(OUT_PATH)
    if not force and cached.get("generated_at"):
        age = datetime.now() - datetime.strptime(cached["generated_at"], "%Y-%m-%d %H:%M:%S")
        if age < timedelta(minutes=CACHE_MIN):
            return cached
    out, errors = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, []
    for key, func in (("taiex", taiex_structure), ("institutional", institutional_amounts), ("margin", margin_amount)):
        try:
            out[key] = func()
        except Exception as exc:  # 任一區塊失敗不影響其他區塊
            out[key] = None
            errors.append(f"{key}：{type(exc).__name__}: {exc}"[:200])
    out["rotation"] = sector_rotation()
    reflection = _read_json(REFLECTION_PATH)
    out["reflection"] = {"generated_at": reflection.get("generated_at"), "headline": reflection.get("headline"),
                         "lessons": (reflection.get("lessons") or [])[:3]}
    out["errors"] = errors
    out["one_line"] = one_line(out)
    REPORTS.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    tmp.replace(OUT_PATH)
    try:  # 學習資料庫：收盤時追加大盤＋個股快照並驗算預測
        from tools import learning_engine
        learning_engine.on_price_update("market_close_summary", force_grade=True)
    except Exception as exc:
        print(f"⚠️ 學習資料庫更新失敗：{exc!r}", file=sys.stderr)
    return out


def one_line(s):
    parts = []
    t = s.get("taiex")
    if t:
        pos = "站上" if t["close"] >= t["ma20"] else "跌破"
        parts.append(f"加權 {t['close']:,.0f}（{t['change_pct']:+.2f}%）{pos}月線，{t['trend']}")
    inst = s.get("institutional")
    if inst:
        parts.append(f"外資 {inst['foreign']:+.1f} 億、投信 {inst['trust']:+.1f} 億")
    rot = s.get("rotation") or {}
    if rot.get("strong"):
        parts.append("強勢：" + "、".join(n for n, _ in rot["strong"][:2]))
    return "｜".join(parts) or "尚無收盤資料"


def main():
    parser = argparse.ArgumentParser(description="台股收盤大盤與資金總結")
    parser.add_argument("--force", action="store_true", help="忽略 30 分鐘快取")
    args = parser.parse_args()
    out = build(force=args.force)
    print(out["one_line"])
    for e in out.get("errors") or []:
        print(f"⚠️ {e}")


if __name__ == "__main__":
    main()
