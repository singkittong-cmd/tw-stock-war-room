# -*- coding: utf-8 -*-
"""🎯 母體池（0050 核心＋高潛力動能，約 50 檔）→ 量化評分 → 動態精選 Potential TOP 20。

流程（refresh_top20）：
1. 預抓母體池日 K（Yahoo，不耗 FinMind 額度；4 小時內的新鮮檔沿用）。
2. 量化評分（只用公開市場資料）：
   - 趨勢：多頭趨勢 +3、盤整 +1、空頭 −2（見轉折 K 回補 +1）；四線多頭排列 +2、空頭排列 −1；站上月線／季線各 +1。
   - 量能：量比 1.0–2.5 溫和放大 +1.5、> 2.5 爆量 +0.5、< 0.6 量縮 −0.5；逼近或突破 20 日高 +1。
   - 風險：月線乖離 > +10% −2（乖離過大不追）、< −10% −0.5；RSI > 80 −1。
   - 籌碼（只用已快取的籌碼檔，不為評分另耗額度）：外資 5 日買超 +1、投信連買 ≥ 2 日 +1、外資 5 日賣超 −0.5。
   - 接近建議買進價（既有定錨在 −2%～+3% 內）+1.5。
3. 取分數最高的 20 檔；模擬基金目前持有的個股一律保留在名單內（不會因洗牌而失去監控）。
4. 寫入 reports/sector_watchlist.json（stock_id、name、sector、pool=0050核心／高潛力動能、score、reasons）
   與 reports/universe_pool_scores.json（全部母體分數），接著重建 20 強建議買進價定錨與監控表。

0050 對標：benchmark_0050(start_date) 以 Yahoo 日 K 計算模擬基金起始日以來 0050 的同期報酬。

用法：
    C:/Users/singk/anaconda3/python.exe tools/universe_pool.py              # 預抓＋評分＋寫入＋重建定錨與監控表
    C:/Users/singk/anaconda3/python.exe tools/universe_pool.py --dry-run    # 只印評分結果，不寫檔
    C:/Users/singk/anaconda3/python.exe tools/universe_pool.py --no-fetch   # 只用既有預抓檔
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORTS = ROOT / "reports"
DATA_DIR = REPORTS / "data"
SECTOR_PATH = REPORTS / "sector_watchlist.json"
SCORES_PATH = REPORTS / "universe_pool_scores.json"
STANDBY_PATH = REPORTS / "watchlist_standby.json"
BENCH_CACHE = DATA_DIR / "benchmark_0050.json"
TOP_N = 20
BENCHMARK_ID = "0050"
CORE, MOMENTUM = "0050核心", "高潛力動能"

# 母體池：(代號, 名稱, 次產業, 分組)。分組依使用者指定的分類，並非證交所官方 0050 成分股清單。
UNIVERSE = [
    # AI 與半導體權值
    ("2330", "台積電", "晶圓代工", CORE), ("2454", "聯發科", "IC設計", CORE), ("3711", "日月光投控", "封測", CORE),
    ("2303", "聯電", "晶圓代工", CORE), ("3034", "聯詠", "IC設計", CORE), ("2379", "瑞昱", "IC設計", CORE),
    ("3661", "世芯-KY", "ASIC設計服務", CORE), ("3443", "創意", "ASIC設計服務", CORE), ("6669", "緯穎", "AI伺服器", CORE),
    ("2382", "廣達", "AI伺服器", CORE), ("2317", "鴻海", "AI伺服器", CORE), ("3231", "緯創", "AI伺服器", CORE),
    ("2357", "華碩", "品牌與伺服器", CORE), ("2395", "研華", "工業電腦", CORE), ("2308", "台達電", "電源與散熱", CORE),
    ("2345", "智邦", "網通交換器", CORE), ("3017", "奇鋐", "AI散熱", CORE), ("3324", "雙鴻", "AI散熱", CORE),
    # 0050 電子／傳產／重電／航運／金融代表
    ("1519", "華城", "重電", CORE), ("1503", "士電", "重電", CORE), ("2603", "長榮", "航運", CORE),
    ("2609", "陽明", "航運", CORE), ("2615", "萬海", "航運", CORE), ("3008", "大立光", "光學鏡頭", CORE),
    ("2327", "國巨", "被動元件", CORE), ("3037", "欣興", "ABF載板", CORE), ("8046", "南電", "ABF載板", CORE),
    ("2881", "富邦金", "金融", CORE), ("2882", "國泰金", "金融", CORE), ("2891", "中信金", "金融", CORE),
    ("2886", "兆豐金", "金融", CORE), ("2884", "玉山金", "金融", CORE), ("1216", "統一", "食品", CORE),
    ("2002", "中鋼", "鋼鐵", CORE),
    # 中小型高潛力（CoWoS／CPO／IC 設計／測試／記憶體／PCB）
    ("3131", "弘塑", "CoWoS設備", MOMENTUM), ("6187", "萬潤", "CoWoS設備", MOMENTUM), ("3583", "辛耘", "CoWoS設備", MOMENTUM),
    ("3450", "聯鈞", "CPO光通訊", MOMENTUM), ("4979", "華星光", "CPO光通訊", MOMENTUM), ("6442", "光聖", "CPO光通訊", MOMENTUM),
    ("5274", "信驊", "IC設計", MOMENTUM), ("3529", "力旺", "矽智財", MOMENTUM), ("6223", "旺矽", "探針卡", MOMENTUM),
    ("2449", "京元電子", "測試", MOMENTUM), ("2368", "金像電", "AI伺服器PCB", MOMENTUM), ("6488", "環球晶", "矽晶圓", MOMENTUM),
    ("1560", "中砂", "再生晶圓與耗材", MOMENTUM), ("2344", "華邦電", "記憶體", MOMENTUM), ("2408", "南亞科", "記憶體", MOMENTUM),
    ("8299", "群聯", "記憶體控制", MOMENTUM),
]
UNIVERSE_IDS = [u[0] for u in UNIVERSE]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _payload(sid, role):
    d = _read(DATA_DIR / f"{sid}_{role}.json", {}) or {}
    d = d.get("data", d) if isinstance(d, dict) else {}
    return d if isinstance(d, dict) else {}


def score_stock(sid, standby=None):
    """回傳 (分數, 理由清單, 收盤價)。沒有日 K 預抓檔時回傳 (None, [...], None)。"""
    k = _payload(sid, "kline")
    close = _f(k.get("close"))
    if not close:
        return None, ["缺日 K 資料"], None
    zhu = k.get("zhu_analysis") if isinstance(k.get("zhu_analysis"), dict) else {}
    score, why = 0.0, []

    def add(points, reason):
        nonlocal score
        score += points
        why.append(f"{reason}({points:+g})")

    trend = str(zhu.get("trend") or "")
    if trend == "多頭趨勢":
        add(3, "多頭趨勢")
    elif "盤整" in trend:
        add(1, "盤整")
    elif trend == "空頭趨勢":
        add(-2, "空頭趨勢")
        if zhu.get("has_reversal_signal"):
            add(1, "見轉折K")
    order = str(zhu.get("ma_order") or k.get("ma_alignment") or "")
    if "多頭排列" in order:
        add(2, "四線多頭排列")
    elif "空頭排列" in order:
        add(-1, "四線空頭排列")
    for n, label in ((20, "站上月線"), (60, "站上季線")):
        ma = _f(k.get(f"ma{n}"))
        if ma and close > ma:
            add(1, label)
    vr = _f(k.get("vol_ratio_20"))
    if vr is not None:
        if 1.0 <= vr <= 2.5:
            add(1.5, f"量比{vr:.1f}溫和放大")
        elif vr > 2.5:
            add(0.5, f"量比{vr:.1f}爆量")
        elif vr < 0.6:
            add(-0.5, "量縮")
    high20 = _f(k.get("high20"))
    if high20 and close >= high20 * 0.99:
        add(1, "逼近或突破20日高")
    bias = _f(k.get("bias20_pct"))
    if bias is not None and bias > 10:
        add(-2, f"月線乖離{bias:+.1f}%過大")
    elif bias is not None and bias < -10:
        add(-0.5, f"月線乖離{bias:+.1f}%")
    rsi = _f(k.get("rsi14"))
    if rsi is not None and rsi > 80:
        add(-1, f"RSI{rsi:.0f}過熱")
    inst = _payload(sid, "chips").get("institutional")
    if isinstance(inst, dict):
        fo, tr = inst.get("foreign") or {}, inst.get("trust") or {}
        f5 = _f(fo.get("sum_5d") if isinstance(fo, dict) else None)
        if f5 is not None:
            add(1, "外資5日買超") if f5 > 0 else add(-0.5, "外資5日賣超")
        streak = _f(tr.get("streak_days") if isinstance(tr, dict) else None)
        if streak is not None and streak >= 2:
            add(1, f"投信連買{streak:.0f}日")
    plan = ((standby or {}).get("stocks") or {}).get(sid) or {}
    lo, hi = _f(plan.get("entry_low")), _f(plan.get("entry_high"))
    if lo and hi and lo * 0.98 <= close <= hi * 1.03:
        add(1.5, "接近建議買進價")
    return round(score, 2), why, close


def _sim_holding_ids():
    try:
        from tools import sim_portfolio_engine as sim
        acct = sim.load_account()
        return [str(h.get("stock_id")) for h in (acct.get("holdings") or []) if isinstance(h, dict) and h.get("stock_id")]
    except Exception:
        return []


def select_top20_potential(fetch=True, top_n=TOP_N, save=True):
    """評分母體池並挑出 Potential TOP 20；save=True 時寫入 sector_watchlist.json 與 universe_pool_scores.json。"""
    started = time.perf_counter()
    fetch_note = None
    if fetch:
        from tools import batch_prefetch
        manifest = batch_prefetch.run(UNIVERSE_IDS, workers=8, roles=["kline"])
        fetch_note = {"elapsed_sec": manifest.get("elapsed_sec"),
                      "failed": [r["stock_id"] for r in manifest.get("results") or [] if not r.get("ok")]}
    standby = _read(STANDBY_PATH, {}) or {}
    meta = {u[0]: u for u in UNIVERSE}
    rows = []
    for sid, name, sector, pool in UNIVERSE:
        score, why, close = score_stock(sid, standby)
        rows.append({"stock_id": sid, "name": name, "sector": sector, "pool": pool, "score": score,
                     "close": close, "reasons": why})
    ranked = sorted((r for r in rows if r["score"] is not None), key=lambda r: (-r["score"], r["stock_id"]))
    held = [s for s in _sim_holding_ids()]
    picked = [r for r in ranked if r["stock_id"] in held]  # 模擬基金持有中的個股一律保留監控
    for r in ranked:
        if len(picked) >= top_n:
            break
        if r not in picked:
            picked.append(r)
    for sid in held:  # 持有中但不在母體池（或缺日 K）的個股也保留
        if sid not in {p["stock_id"] for p in picked}:
            m = meta.get(sid)
            picked.append({"stock_id": sid, "name": m[1] if m else sid, "sector": m[2] if m else "模擬持倉",
                           "pool": m[3] if m else "模擬持倉", "score": None, "close": None, "reasons": ["模擬基金持有中"]})
    for p in picked:
        if p["stock_id"] in held and "模擬基金持有中" not in p["reasons"]:
            p["reasons"] = ["模擬基金持有中"] + p["reasons"]
    stamp = _now()
    watchlist = [{"stock_id": p["stock_id"], "name": p["name"], "sector": p["sector"], "pool": p["pool"],
                  "score": p["score"], "reasons": p["reasons"][:6], "selected_at": stamp} for p in picked]
    result = {"generated_at": stamp, "source": "tools/universe_pool.py", "universe_size": len(UNIVERSE),
              "scored": len(ranked), "missing_kline": [r["stock_id"] for r in rows if r["score"] is None],
              "kept_sim_holdings": [s for s in held if s in {p["stock_id"] for p in picked}],
              "pool_counts": {CORE: sum(1 for p in picked if p["pool"] == CORE),
                              MOMENTUM: sum(1 for p in picked if p["pool"] == MOMENTUM)},
              "selected": [p["stock_id"] for p in picked], "fetch": fetch_note,
              "elapsed_sec": round(time.perf_counter() - started, 2), "scores": rows}
    if save and len(watchlist) >= 5:  # 評分資料嚴重不足時不覆蓋既有名單
        _write(SECTOR_PATH, watchlist)
        _write(SCORES_PATH, result)
    elif save:
        result["error"] = f"只有 {len(watchlist)} 檔有日 K 資料，未覆蓋既有 20 強名單"
    return result


def refresh_top20(fetch=True, rebuild=True):
    """重新篩選 TOP 20 → 重建建議買進價定錨（日 K＋籌碼）→ 刷新監控表。回傳摘要。"""
    result = select_top20_potential(fetch=fetch)
    summary = {k: result.get(k) for k in ("generated_at", "selected", "pool_counts", "kept_sim_holdings",
                                         "missing_kline", "elapsed_sec", "error")}
    if rebuild and not result.get("error"):
        from tools import build_watchlist_standby as standby_mod
        from tools import watchlist_intraday_radar as radar_mod
        standby = standby_mod.build(fetch=fetch)
        summary["standby_missing"] = standby.get("missing_kline")
        try:
            radar = radar_mod.run()
            summary["radar_counts"] = radar.get("counts")
        except Exception as exc:  # 報價失敗不影響名單
            summary["radar_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return summary


def load_scores():
    return _read(SCORES_PATH, {}) or {}


def benchmark_0050(start_date, max_age_sec=600):
    """模擬基金起始日以來 0050 的同期報酬：基準＝起始日前一個交易日收盤，現值＝Yahoo 最新日 K（盤中為當日即時價）。
    10 分鐘快取在 reports/data/benchmark_0050.json（不進 git）。失敗時回傳含 error 的 dict。"""
    cache = _read(BENCH_CACHE, {}) or {}
    if (cache.get("start_date") == start_date and not cache.get("error")
            and time.time() - (cache.get("ts") or 0) < max_age_sec):
        return cache
    try:
        from tools import yahoo_quote
        rows = yahoo_quote.fetch_daily(BENCHMARK_ID, rng="1y")
        before = [r for r in rows if r["date"] < start_date]
        base = before[-1] if before else next(r for r in rows if r["date"] >= start_date)
        last = rows[-1]
        out = {"stock_id": BENCHMARK_ID, "name": "元大台灣50", "start_date": start_date, "base_date": base["date"],
               "base_close": round(base["close"], 2), "last_date": last["date"], "last_close": round(last["close"], 2),
               "return_pct": round((last["close"] / base["close"] - 1) * 100, 2), "source": "Yahoo Finance 日 K",
               "updated_at": _now(), "ts": time.time()}
    except Exception as exc:
        out = {"stock_id": BENCHMARK_ID, "start_date": start_date, "error": f"{type(exc).__name__}: {exc}"[:200],
               "updated_at": _now(), "ts": time.time()}
    try:
        _write(BENCH_CACHE, out)
    except OSError:
        pass
    return out


def main():
    ap = argparse.ArgumentParser(description="0050 母體池 → Potential TOP 20")
    ap.add_argument("--dry-run", action="store_true", help="只印評分，不寫檔")
    ap.add_argument("--no-fetch", action="store_true", help="只用既有預抓檔")
    ap.add_argument("--no-rebuild", action="store_true", help="不重建定錨與監控表")
    args = ap.parse_args()
    if args.dry_run:
        r = select_top20_potential(fetch=not args.no_fetch, save=False)
    else:
        r = refresh_top20(fetch=not args.no_fetch, rebuild=not args.no_rebuild)
        r = {**load_scores(), **r}
    scores = {s["stock_id"]: s for s in r.get("scores") or []}
    print(f"母體池 {len(UNIVERSE)} 檔｜有效評分 {r.get('scored', len(scores))} 檔｜缺日 K：{r.get('missing_kline') or '無'}")
    print(f"Potential TOP {len(r.get('selected') or [])}：{r.get('pool_counts')}｜保留模擬持倉：{r.get('kept_sim_holdings') or '無'}")
    for i, sid in enumerate(r.get("selected") or [], 1):
        s = scores.get(sid) or {}
        print(f"{i:>2}. {sid} {s.get('name', '')}［{s.get('pool', '')}］分數 {s.get('score')}｜{'、'.join(s.get('reasons') or [])[:70]}")
    for k in ("standby_missing", "radar_counts", "radar_error", "error"):
        if r.get(k):
            print(f"{k}：{r[k]}")


if __name__ == "__main__":
    main()
