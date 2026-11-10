# -*- coding: utf-8 -*-
"""盤後備用進場價計算器：為 reports/sector_watchlist.json 的 20 大龍頭預先定錨進場區、停損與目標價。

用法：
    C:/Users/singk/anaconda3/python.exe tools/build_watchlist_standby.py            # 先預抓 K 線（Yahoo）與籌碼（FinMind，4 小時快取）再計算
    C:/Users/singk/anaconda3/python.exe tools/build_watchlist_standby.py --no-fetch # 只用 reports/data/ 既有檔案計算
    C:/Users/singk/anaconda3/python.exe tools/build_watchlist_standby.py --force    # 忽略快取重抓

輸出 reports/watchlist_standby.json：{"generated_at", "stocks": {代號: 備用計畫}}。
總分析員可直接修改某檔並設 "manual_override": true，之後重新計算時該檔保留不覆寫。
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
from datetime import datetime

from tools.batch_prefetch import run as prefetch_run  # noqa: E402
from tools.finmind_cli import round_tick  # noqa: E402
from tools.portfolio_intraday import load_prefetch  # noqa: E402

SECTOR_PATH = ROOT / "reports" / "sector_watchlist.json"
STANDBY_PATH = ROOT / "reports" / "watchlist_standby.json"


def load_sector():
    with open(SECTOR_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_standby():
    if not STANDBY_PATH.exists():
        return {}
    try:
        with open(STANDBY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def chip_assessment(chips):
    """法人籌碼評分（-4~+4）與簡評。"""
    inst = (chips or {}).get("institutional") or {}
    if not inst:
        return 0, "籌碼資料缺"
    f, t = inst.get("foreign") or {}, inst.get("trust") or {}
    f5, t5 = f.get("sum_5d") or 0, t.get("sum_5d") or 0
    fs, ts = inst.get("foreign_streak_days") or 0, inst.get("trust_streak_days") or 0
    score = sum(1 if v > 0 else -1 if v < 0 else 0 for v in (f5, t5, fs, ts))
    if ts >= 3:
        label = f"投信連買鎖碼（{ts} 日）"
    elif f5 > 0 and t5 > 0:
        label = "土洋合買"
    elif fs >= 3:
        label = f"外資連買（{fs} 日）"
    elif f5 < 0 and t5 < 0:
        label = "法人調節中"
    elif fs <= -3:
        label = f"外資連賣（{abs(fs)} 日）"
    else:
        label = "法人多空分歧"
    return score, f"{label}｜外資 5 日 {f5:+,.0f} 張、投信 5 日 {t5:+,.0f} 張"


def kline_assessment(k):
    close, ma5, ma10, ma20, ma60 = (k.get(x) for x in ("close", "ma5", "ma10", "ma20", "ma60"))
    if not all((close, ma10, ma20, ma60)):
        return "均線資料不足"
    if close > ma10 > ma20 > ma60:
        return f"多頭排列，站穩 10 日線（乖離 MA20 {k.get('bias20_pct', 0):+.1f}%）"
    if close > ma60 and ma20 > ma60 and close <= ma10:
        return "多頭回檔，跌破 10 日線但季線上"
    if close > ma60:
        return "季線之上整理"
    if ma20 < ma60:
        return "空頭排列，季線下"
    return "季線下整理"


def build_plan(item, k, chips):
    """依均線結構選戰術，算出進場區、停損、目標與風報比。"""
    close = k["close"]
    ma10, ma20, ma60 = k.get("ma10"), k.get("ma20"), k.get("ma60")
    low20, high20, high60 = k.get("low20"), k.get("high20"), k.get("high60")
    chip_score, chip_trend = chip_assessment(chips)

    if ma10 and ma20 and close > ma10 > ma20:
        strategy = "強勢整理回測10日線"
        entry_low, entry_high = ma10 * 0.985, ma10 * 1.005
        stop = ma20 * 0.98
    elif ma20 and ma60 and ma20 > ma60 and close >= ma20 * 0.98:
        strategy = "多頭拉回守月線低接"
        entry_low, entry_high = ma20 * 0.98, ma20 * 1.01
        stop = entry_low * 0.96
    elif ma60 and close >= ma60 * 0.97:
        strategy = "跌破月線回測季線"
        entry_low, entry_high = ma60 * 0.985, ma60 * 1.01
        stop = entry_low * 0.96
    else:
        strategy = "觀望等待底部確認"
        base = low20 or close
        entry_low, entry_high = base * 0.99, base * 1.02
        stop = base * 0.95

    entry_high = min(entry_high, close * 1.01)  # 不追高：進場區上緣不超過收盤 +1%
    entry_low = min(entry_low, entry_high * 0.985)  # 進場區至少保留 1.5% 寬度
    entry_low, entry_high = round_tick(entry_low, "down"), round_tick(entry_high, "down")
    stop = round_tick(min(stop, entry_low * 0.97), "down")  # 停損至少在進場下緣 3% 之下
    mid = (entry_low + entry_high) / 2
    # 第一目標：進場區上方 3% 以外最近的壓力（月線、季線、20 日高、60 日高）
    resistances = sorted(v for v in (ma20, ma60, high20, high60) if v and v > entry_high * 1.03)
    target_1 = round_tick(resistances[0] if resistances else entry_high * 1.08, "up")
    rr = round((target_1 - mid) / (mid - stop), 2) if mid > stop else None

    note = (f"回測 {entry_low:g}–{entry_high:g} 且五檔委買比 ≥ 0.7、盤中站回 VWAP 才分批進場；"
            f"跌破 {stop:g} 放棄暫不買進。")
    if strategy == "觀望等待底部確認":
        note = "均線偏空，僅在 20 日低點附近止穩（連兩日不破低、量縮）才試單；" + note
    if chip_score <= -2:
        note += "籌碼偏弱，進場量減半。"
    elif chip_score >= 2:
        note += "籌碼偏多，可依計畫正常部位。"
    if rr is not None and rr < 1.5:
        note += f"風報比僅 {rr}，未達 1.5，原則上不進場。"

    return {
        "stock_id": item["stock_id"],
        "name": item.get("name"),
        "sector": item.get("sector"),
        "basis_date": k.get("date"),
        "basis_close": close,
        "strategy_type": strategy,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": stop,
        "target_1": target_1,
        "risk_reward": rr,
        "chip_score": chip_score,
        "chip_trend": chip_trend,
        "kline_trend": kline_assessment(k),
        "role4_note": note,
        "manual_override": False,
    }


def build(fetch=True, force=False):
    sector = load_sector()
    ids = [r["stock_id"] for r in sector]
    fetch_report = None
    if fetch:
        manifest = prefetch_run(ids, workers=8, force=force, roles=["kline", "chips"])
        fetch_report = {"elapsed_sec": manifest["elapsed_sec"], "quota_exhausted": manifest["quota_exhausted"],
                        "failed": [f"{r['stock_id']}:{r['role']}" for r in manifest["results"] if not r["ok"]]}
    previous = (load_standby().get("stocks") or {})
    stocks, missing = {}, []
    for item in sector:
        sid = item["stock_id"]
        old = previous.get(sid) or {}
        if old.get("manual_override"):
            stocks[sid] = old
            continue
        k = load_prefetch(sid, "kline")
        if not k or not k.get("close"):
            missing.append(sid)
            if old:
                stocks[sid] = dict(old, stale=True)
            continue
        stocks[sid] = build_plan(item, k, load_prefetch(sid, "chips"))
    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "tools/build_watchlist_standby.py",
        "fetch": fetch_report,
        "missing_kline": missing,
        "stocks": stocks,
    }
    tmp = STANDBY_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STANDBY_PATH)
    return out


def main():
    parser = argparse.ArgumentParser(description="20 大龍頭盤後備用進場價計算")
    parser.add_argument("--no-fetch", action="store_true", help="不預抓，只用 reports/data/ 既有檔案")
    parser.add_argument("--force", action="store_true", help="忽略快取強制重抓")
    args = parser.parse_args()
    out = build(fetch=not args.no_fetch, force=args.force)
    print(f"盤後備用計畫 {out['generated_at']}，共 {len(out['stocks'])} 檔，缺 K 線：{out['missing_kline'] or '無'}")
    if out["fetch"]:
        print(f"預抓耗時 {out['fetch']['elapsed_sec']} 秒，失敗：{out['fetch']['failed'] or '無'}"
              + ("（FinMind 額度已用完，籌碼沿用舊檔）" if out["fetch"]["quota_exhausted"] else ""))
    for sid, p in out["stocks"].items():
        print(f"{sid:<5} {p.get('name', ''):<6} {p.get('sector', ''):<10} 收 {p.get('basis_close')}｜{p.get('strategy_type')}｜"
              f"進場 {p.get('entry_low')}–{p.get('entry_high')}｜停損 {p.get('stop_loss')}｜目標 {p.get('target_1')}｜"
              f"RR {p.get('risk_reward')}｜{p.get('chip_trend', '')[:12]}"
              + ("（人工覆寫）" if p.get("manual_override") else ""))


if __name__ == "__main__":
    main()
