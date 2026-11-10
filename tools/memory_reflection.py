# -*- coding: utf-8 -*-
"""每日覆盤與自我修正記憶引擎：把五位成員上一次的結論與最新實盤逐項量化比對，產生落差警示與修正建議。

用法：
    C:/Users/singk/anaconda3/python.exe tools/memory_reflection.py              # 收錄新結論 → 全面覆盤
    C:/Users/singk/anaconda3/python.exe tools/memory_reflection.py --snapshot   # 只把最新結論收進記憶庫
    C:/Users/singk/anaconda3/python.exe tools/memory_reflection.py --stock 2408 # 只覆盤指定個股（可重複）

記憶庫 reports/memory/：
- prediction_ledger.json：所有成員的歷史結論（當時股價、方向、支撐/壓力/停損、籌碼判斷）。
  自動收錄來源：stocks/*_summary.json、portfolio_intraday_decisions.json、watchlist_standby.json、
  us_market/tw_premarket_summary.json、news/daily_news_summary.json、data/*_kline.json（朱家泓趨勢）、
  dashboard 即時快篩，以及成員自行寫入的 inbox/<member>.json。
- <代號>_reflection.json、market_reflection.json：上一次結論 vs 最新實盤（收盤/盤中現價、實際高低點、實際法人買賣超）。
- reflection_index.json：各成員最新準確／落差統計，供 dashboard 戰術桌顯示。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
import re
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORTS = ROOT / "reports"
MEMORY_DIR = REPORTS / "memory"
INBOX_DIR = MEMORY_DIR / "inbox"
LEDGER_PATH = MEMORY_DIR / "prediction_ledger.json"
INDEX_PATH = MEMORY_DIR / "reflection_index.json"
MARKET_PATH = MEMORY_DIR / "market_reflection.json"
DATA_DIR = REPORTS / "data"
STOCKS_DIR = REPORTS / "stocks"
MARKET_ID = "TAIEX"
LEDGER_KEEP_DAYS = 60
TIME_FMT = "%Y-%m-%d %H:%M:%S"

MEMBERS = {"news": "新聞達人", "chips": "籌碼王", "kline": "K線技術人", "chairman": "巴董事長", "maga": "川大帝MAGA"}
MEMBER_ICONS = {"news": "🕵️‍♂️", "chips": "🎧", "kline": "⚔️", "chairman": "👑", "maga": "🦅"}

BULL_WORDS = ("強多", "偏多", "強力買進", "分批布局", "加碼", "攤平", "多頭", "順勢加碼", "支撐攤平", "鎖碼", "小量試單")
BEAR_WORDS = ("強空", "偏空", "減碼", "出場", "停損", "空頭", "暫不買進", "散戶融資承接", "警戒", "倒貨")


# ---------------------------------------------------------------- 共用

def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    tmp.replace(path)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _now():
    return datetime.now().strftime(TIME_FMT)


def direction_of(text):
    """把裁決/評級/動作文字轉成 偏多／偏空／中性。"""
    t = str(text or "")
    if any(w in t for w in ("持有觀望", "續抱", "中性", "觀望", "獲利了結", "分批獲利", "分歧")):
        if not any(w in t for w in ("偏多", "偏空", "強多", "強空")):
            return "中性"
    if any(w in t for w in BEAR_WORDS):
        return "偏空"
    if any(w in t for w in BULL_WORDS):
        return "偏多"
    return "中性"


def target_date_of(recorded_at):
    """結論驗證日：交易時段（平日 13:30 前）記錄者驗證當日；盤後與假日記錄者驗證下一個平日。"""
    dt = datetime.strptime(recorded_at, TIME_FMT)
    if dt.weekday() < 5 and dt.time() < dtime(13, 30):
        return dt.strftime("%Y-%m-%d")
    d = dt.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 實盤資料

_TAIEX_ROWS = None
_ROWS_CACHE = {}


def daily_rows(stock_id):
    """回傳日 K list[dict(date, open, high, low, close)]，由舊到新（同一次執行內快取）。"""
    if stock_id not in _ROWS_CACHE:
        _ROWS_CACHE[stock_id] = _load_rows(stock_id)
    return _ROWS_CACHE[stock_id]


def _load_rows(stock_id):
    global _TAIEX_ROWS
    if stock_id == MARKET_ID:
        if _TAIEX_ROWS is None:
            try:
                from tools import finmind_cli as fm
                df = fm.fetch_price_yahoo("^TWII", "3mo")
                _TAIEX_ROWS = [{"date": r["date"].strftime("%Y-%m-%d"), "open": float(r["open"]), "high": float(r["high"]),
                                "low": float(r["low"]), "close": float(r["close"])} for _, r in df.iterrows()]
            except Exception:
                _TAIEX_ROWS = []
        return _TAIEX_ROWS
    data = _read_json(DATA_DIR / f"{stock_id}_kline.json").get("data") or {}
    return [r for r in data.get("daily_30d") or [] if r.get("date")]


def live_bar(stock_id):
    """盤中最新報價（持股快照或 20 強建議買進價監控表），回傳今日的暫時 K 棒或 None。"""
    today = date.today().strftime("%Y-%m-%d")
    status = _read_json(REPORTS / "portfolio_live_status.json")
    for p in status.get("positions") or [] if str(status.get("updated_at") or "")[:10] == today else []:
        if p.get("stock_id") == stock_id and p.get("current_price"):
            return {"date": today, "open": _f(p.get("open")), "high": _f(p.get("high")) or _f(p["current_price"]),
                    "low": _f(p.get("low")) or _f(p["current_price"]), "close": _f(p["current_price"]),
                    "stamp": str(status["updated_at"])}
    radar = _read_json(REPORTS / "watchlist_radar_live.json")
    if str(radar.get("updated_at") or "")[:10] == today:
        for r in radar.get("results") or []:
            if r.get("stock_id") == stock_id and r.get("price"):
                px = _f(r["price"])
                return {"date": today, "open": None, "high": px, "low": px, "close": px, "stamp": str(radar["updated_at"])}
    return None


def kline_fetched_at(stock_id):
    return str(_read_json(DATA_DIR / f"{stock_id}_kline.json").get("fetched_at") or "")


def price_on(stock_id, day):
    """day（含）以前最後一根 K 棒收盤價。"""
    rows = [r for r in daily_rows(stock_id) if r["date"] <= day]
    return _f(rows[-1]["close"]) if rows else None


def actual_since(stock_id, target_date):
    rows = [dict(r) for r in daily_rows(stock_id) if r["date"] >= target_date]
    live = live_bar(stock_id) if stock_id != MARKET_ID else None
    if live and live["date"] >= target_date:
        same = next((r for r in rows if r["date"] == live["date"]), None)
        if same is None:
            rows.append(live)
        elif live["stamp"] > kline_fetched_at(stock_id):  # 預抓檔的今日 K 棒是較舊的快照：以較新的報價更新
            same["close"] = live["close"]
            same["high"] = max(v for v in (_f(same.get("high")), live["high"]) if v is not None)
            same["low"] = min(v for v in (_f(same.get("low")), live["low"]) if v is not None)
    if not rows:
        return None
    now = datetime.now()
    if rows[-1]["date"] == now.strftime("%Y-%m-%d") and now.weekday() < 5 and now.time() < dtime(13, 35):
        rows[-1]["live"] = True
    lows = [_f(r["low"]) for r in rows if _f(r.get("low"))]
    highs = [_f(r["high"]) for r in rows if _f(r.get("high"))]
    out = {"dates": [r["date"] for r in rows], "close": _f(rows[-1]["close"]), "low": min(lows) if lows else None,
           "high": max(highs) if highs else None, "is_partial": bool(rows[-1].get("live")),
           "foreign_net": None, "trust_net": None}
    if stock_id != MARKET_ID:
        inst = (_read_json(DATA_DIR / f"{stock_id}_chips.json").get("data") or {}).get("institutional") or {}
        days = [d for d in inst.get("daily_10d") or [] if str(d.get("date")) >= target_date]
        if days:
            out["foreign_net"] = sum(d.get("foreign") or 0 for d in days)
            out["trust_net"] = sum(d.get("trust") or 0 for d in days)
            out["chips_dates"] = [d["date"] for d in days]
    else:
        mc = _read_json(REPORTS / "market_close_summary.json").get("institutional") or {}
        if mc.get("date", "") >= target_date:
            out["foreign_net"], out["trust_net"], out["chips_unit"] = mc.get("foreign"), mc.get("trust"), "億元"
    return out


# ---------------------------------------------------------------- 收錄結論

def _entry(member, stock_id, source, recorded_at, direction, price_at=None, support=None, resistance=None,
           stop_loss=None, chip_view=None, note=None, name=None, target_date=None):
    recorded_at = recorded_at or _now()
    if _f(price_at) is None:
        # 收盤前記錄的結論不能用當日收盤價（那是事後資料）：改用前一交易日收盤
        intraday = recorded_at[11:16] < "13:30"
        day = recorded_at[:10]
        rows = [r for r in daily_rows(stock_id) if (r["date"] < day if intraday else r["date"] <= day)]
        price_at = rows[-1]["close"] if rows else None
    return {
        "key": f"{member}|{stock_id}|{source}|{recorded_at}",
        "member": member, "member_name": MEMBERS[member], "stock_id": stock_id, "name": name, "source": source,
        "recorded_at": recorded_at, "target_date": target_date or target_date_of(recorded_at),
        "price_at": _f(price_at), "direction": direction,
        "support": _f(support), "resistance": _f(resistance), "stop_loss": _f(stop_loss),
        "chip_view": chip_view, "note": (str(note)[:160] if note else None),
    }


def _norm_stamp(text, fallback_date=None):
    """把 'YYYY-MM-DD HH:MM[:SS]' 或 'HH:MM:SS' 正規化成 TIME_FMT。"""
    s = str(text or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", s):
        return s
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", s):
        return s + ":00"
    if re.match(r"^\d{2}:\d{2}(:\d{2})?$", s) and fallback_date:
        return f"{fallback_date} {s}" + ("" if s.count(":") == 2 else ":00")
    return None


def harvest():
    """從各報告檔收錄最新結論，回傳 entry 清單（尚未去重）。"""
    out = []
    # 1) 個股深度研究 summary：巴董事長裁決＋三位成員評級
    for path in STOCKS_DIR.glob("*_summary.json"):
        s = _read_json(path)
        sid, stamp = s.get("stock_id"), _norm_stamp(s.get("updated_at"))
        if not sid or not stamp:
            continue
        zone = s.get("entry_zone") if isinstance(s.get("entry_zone"), list) else [None, None]
        common = dict(name=s.get("stock_name"), support=zone[0] if zone else None, resistance=s.get("target_1"),
                      stop_loss=s.get("stop_loss"))
        out.append(_entry("chairman", sid, "summary", stamp, direction_of(s.get("verdict")),
                          note=f"{s.get('verdict')}：{s.get('conflict_point', '')}", chip_view=s.get("role2_rating"), **common))
        out.append(_entry("news", sid, "summary", stamp, direction_of(s.get("role1_rating")), note=s.get("role1_rating"), name=s.get("stock_name")))
        out.append(_entry("chips", sid, "summary", stamp, direction_of(s.get("role2_rating")), chip_view=s.get("role2_rating"),
                          note=s.get("role2_rating"), name=s.get("stock_name")))
        out.append(_entry("kline", sid, "summary", stamp, direction_of(s.get("role3_rating")), note=s.get("role3_rating"), **common))
    # 2) 全持股盤中批次會診
    dec_path = REPORTS / "portfolio_intraday_decisions.json"
    if dec_path.exists():
        day = datetime.fromtimestamp(dec_path.stat().st_mtime).strftime("%Y-%m-%d")
        for sid, d in (_read_json(dec_path) or {}).items():
            if not isinstance(d, dict):
                continue
            stamp = _norm_stamp(d.get("updated_at"), day) or f"{day} 13:00:00"
            lv = dict(support=d.get("key_support_intraday"), resistance=d.get("key_resistance_intraday"), name=d.get("stock_name"))
            out.append(_entry("chairman", sid, "intraday_decision", stamp, direction_of(d.get("role4_final_action")),
                              note=d.get("role4_final_action"), chip_view=d.get("role2_action"), **lv))
            out.append(_entry("chips", sid, "intraday_decision", stamp, direction_of(d.get("role2_action")),
                              chip_view=d.get("role2_action"), note=d.get("role2_reason"), **lv))
            out.append(_entry("kline", sid, "intraday_decision", stamp, direction_of(d.get("role3_action")),
                              note=d.get("role3_reason"), **lv))
    # 3) 20 強盤後備用定錨（K線技術人價位＋籌碼王籌碼分數）
    standby = _read_json(REPORTS / "watchlist_standby.json")
    stamp = _norm_stamp(standby.get("generated_at"))
    for sid, p in (standby.get("stocks") or {}).items() if stamp else []:
        kdir = "中性" if "觀望" in str(p.get("strategy_type")) or "跌破" in str(p.get("strategy_type")) else "偏多"
        out.append(_entry("kline", sid, "standby", stamp, kdir, price_at=p.get("basis_close"), support=p.get("entry_low"),
                          resistance=p.get("target_1"), stop_loss=p.get("stop_loss"), note=p.get("strategy_type"), name=p.get("name")))
        score = p.get("chip_score") or 0
        out.append(_entry("chips", sid, "standby", stamp, "偏多" if score >= 2 else "偏空" if score <= -2 else "中性",
                          price_at=p.get("basis_close"), chip_view=p.get("chip_trend"), note=p.get("chip_trend"), name=p.get("name")))
    # 4) 川大帝MAGA 盤前預判＋巴董事長大盤劇本
    pre = _read_json(REPORTS / "us_market" / "tw_premarket_summary.json")
    stamp = _norm_stamp(pre.get("generated_at"))
    if stamp and pre.get("date"):
        sox = _f(pre.get("sox_chg_pct")) or 0
        maga_dir = "偏多" if sox > 1 else "偏空" if sox < -1 else "中性"
        out.append(_entry("maga", MARKET_ID, "premarket", stamp, maga_dir, target_date=pre["date"],
                          price_at=price_on(MARKET_ID, (datetime.strptime(pre["date"], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")),
                          note=f"費半 {sox:+.2f}%、TSM 溢價 {pre.get('tsm_premium_pct')}%", name="加權指數"))
        forecast = str(pre.get("tw_index_forecast") or "")
        fdir = "偏空" if re.search(r"開低|偏空|下跌|走弱|跳空向下", forecast) else "偏多" if re.search(r"開高|偏多|上漲|走強", forecast) else "中性"
        out.append(_entry("chairman", MARKET_ID, "premarket", stamp, fdir, target_date=pre["date"],
                          price_at=price_on(MARKET_ID, (datetime.strptime(pre["date"], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")),
                          note=forecast[:160], name="加權指數"))
    # 5) 新聞達人全市場情緒
    news = _read_json(REPORTS / "news" / "daily_news_summary.json")
    stamp = _norm_stamp(news.get("updated_at"))
    if stamp:
        out.append(_entry("news", MARKET_ID, "news_brief", stamp, direction_of(news.get("market_sentiment")),
                          note=f"{news.get('market_sentiment')}：{news.get('one_line_takeaway', '')}", name="加權指數"))
    # 6) K線技術人朱家泓趨勢（預抓檔，每檔每個資料日一筆）
    for path in DATA_DIR.glob("*_kline.json"):
        payload = _read_json(path)
        d = payload.get("data") or {}
        zhu = d.get("zhu_analysis") or {}
        if zhu.get("trend") and d.get("date"):
            levels = d.get("levels") or {}
            out.append(_entry("kline", d.get("stock_id") or path.name.split("_")[0], "zhu_auto", f"{d['date']} 14:00:00",
                              direction_of(zhu["trend"]), price_at=d.get("close"), support=levels.get("defense_support"),
                              resistance=levels.get("resistance_1"), stop_loss=zhu.get("discipline_stop"), note=zhu.get("one_line")))
    # 7) 成員自行寫入的 inbox
    for path in INBOX_DIR.glob("*.json"):
        box = _read_json(path)
        member = box.get("member") if box.get("member") in MEMBERS else path.stem if path.stem in MEMBERS else None
        stamp = _norm_stamp(box.get("recorded_at")) or _now()
        for c in box.get("conclusions") or [] if member else []:
            if c.get("stock_id"):
                out.append(_entry(member, str(c["stock_id"]), "inbox", _norm_stamp(c.get("recorded_at")) or stamp,
                                  c.get("direction") if c.get("direction") in ("偏多", "偏空", "中性") else direction_of(c.get("direction")),
                                  price_at=c.get("price_at"), support=c.get("support"), resistance=c.get("resistance"),
                                  stop_loss=c.get("stop_loss"), chip_view=c.get("chip_view"), note=c.get("note"), name=c.get("name")))
    return out


def load_ledger():
    ledger = _read_json(LEDGER_PATH)
    return ledger if isinstance(ledger.get("entries"), list) else {"entries": []}


def append_entries(entries):
    """去重後追加到 prediction_ledger.json，並刪除 60 天前的舊紀錄；回傳新增筆數。"""
    ledger = load_ledger()
    known = {e["key"] for e in ledger["entries"]}
    new = [e for e in entries if e["key"] not in known]
    cutoff = (datetime.now() - timedelta(days=LEDGER_KEEP_DAYS)).strftime(TIME_FMT)
    ledger["entries"] = [e for e in ledger["entries"] + new if e["recorded_at"] >= cutoff]
    ledger["updated_at"] = _now()
    _write_json(LEDGER_PATH, ledger)
    return len(new)


def snapshot(sync_learning=True):
    """收錄最新結論；預設同步交給 learning_engine 存成永久預測帳本並回溯驗算。"""
    added = append_entries(harvest())
    if sync_learning:
        try:
            from tools import learning_engine
            learning_engine.sync()
        except Exception as exc:  # 學習資料庫失敗不影響覆盤
            print(f"⚠️ 學習資料庫同步失敗：{exc!r}", file=sys.stderr)
    return added


def record_lookup(result):
    """dashboard 即時快篩結果也收進記憶庫（每檔每天每位成員只留第一筆）。"""
    if not result or not result.get("stock_id"):
        return 0
    sid, name = result["stock_id"], result.get("name")
    day = (result.get("looked_up_at") or _now())[:10]
    stamp = result.get("looked_up_at") or _now()
    ledger_keys = {e["key"].rsplit("|", 1)[0] + "|" + e["recorded_at"][:10] for e in load_ledger()["entries"]}
    price = (result.get("quote") or {}).get("price")
    zhu = (result.get("kline") or {}).get("zhu") or {}
    chip, boss = result.get("chip_verdict") or {}, result.get("chairman") or {}
    cands = [
        _entry("chairman", sid, "quick_lookup", stamp, direction_of(boss.get("action")), price_at=price,
               support=boss.get("support"), resistance=boss.get("resistance"), stop_loss=boss.get("stop"),
               chip_view=chip.get("label"), note=boss.get("line"), name=name),
        _entry("chips", sid, "quick_lookup", stamp, direction_of(chip.get("label")), price_at=price,
               chip_view=chip.get("label"), note=chip.get("one_line"), name=name),
        _entry("kline", sid, "quick_lookup", stamp, direction_of(zhu.get("trend")), price_at=price,
               stop_loss=zhu.get("discipline_stop"), note=zhu.get("one_line"), name=name),
    ]
    fresh = [e for e in cands if f"{e['member']}|{sid}|quick_lookup|{day}" not in ledger_keys]
    return append_entries(fresh) if fresh else 0


# ---------------------------------------------------------------- 量化比對

def evaluate(e, act):
    price, close, low, high = e.get("price_at"), act["close"], act["low"], act["high"]
    ret = (close / price - 1) * 100 if price and close else None
    tags, corrections, score = [], [], 0
    d = e["direction"]
    if ret is not None:
        if d == "偏多":
            hit = ret > 0.3
            miss = ret < -0.3
        elif d == "偏空":
            hit = ret < -0.3
            miss = ret > 0.3
        else:
            hit, miss = abs(ret) <= 2, abs(ret) > 2
        if hit:
            score += 1
        elif miss:
            score -= 1
            tags.append("方向落差")
            corrections.append("偏多判斷落空：下次先檢查隔日沖、Priced-in 與美股連動再給多方" if d == "偏多" else
                               "偏空過度：空方訊號需等收盤確認，避免錯殺" if d == "偏空" else
                               f"預期盤整但實際波動 {ret:+.2f}%：區間假設失效，改用趨勢方向判斷")
    sup, stop, res = e.get("support"), e.get("stop_loss"), e.get("resistance")
    if sup and low is not None and low < sup:
        if close >= sup:
            tags.append("支撐假跌破（盤中跌破收回）")
            corrections.append(f"{sup:g} 支撐盤中失守但收回：支撐有效，盤中跌破需等收盤確認")
        elif close >= sup * 0.98:
            tags.append("支撐微幅跌破落差警示")
            score -= 1
            corrections.append(f"支撐 {sup:g} 微幅跌破：改以實際低點 {low:g} 重新定錨，並降低進場量")
        else:
            tags.append("支撐失守落差警示")
            score -= 2
            corrections.append(f"支撐 {sup:g} 失守：原支撐轉為壓力，方向下修一級")
    elif sup and low is not None:
        tags.append("支撐守住")
    if stop and low is not None and low <= stop:
        tags.append("觸發停損")
        corrections.append(f"已觸及停損 {stop:g}：依紀律出場，不攤平")
    if res and high is not None and high >= res:
        if close >= res:
            tags.append("突破壓力")
            corrections.append(f"壓力 {res:g} 已突破：上修目標至下一個壓力")
        else:
            tags.append("觸及壓力回落")
    fn, tn = act.get("foreign_net"), act.get("trust_net")
    cv = e.get("chip_view")
    if cv and fn is not None and tn is not None and e["member"] in ("chips", "chairman"):
        cdir = direction_of(cv)
        net = fn + tn
        unit = act.get("chips_unit", "張")
        if (cdir == "偏多" and net < 0) or (cdir == "偏空" and net > 0):
            tags.append("籌碼判斷落差")
            score -= 1
            corrections.append(f"預判籌碼{cdir}，實際外資 {fn:+,.0f}、投信 {tn:+,.0f} {unit}：籌碼權重下調，留意假外資與隔日沖")
    gaps = [t for t in tags if "落差" in t or t in ("觸發停損",)]
    verdict = "落差" if gaps else ("準確" if score > 0 or tags else "持平")
    when = "今日盤中" if act["is_partial"] else ("今日" if act["dates"][-1] == date.today().strftime("%Y-%m-%d") else act["dates"][-1])
    view = f"守 {sup:g} 元支撐" if sup else ""
    text = (f"{e['recorded_at'][5:16]} 預判{view}{d}" + (f"（當時 {price:g}）" if price else "")
            + f"，{when}實際最低 {low:g}、{'現價' if act['is_partial'] else '收'} {close:g}"
            + (f"（{ret:+.2f}%）" if ret is not None else "")
            + (" -> 觸發【" + "、".join(tags) + "】" if tags else ""))
    return {"verdict": verdict, "score": score, "return_pct": round(ret, 2) if ret is not None else None, "tags": tags,
            "gap_text": text, "corrections": corrections or ["判斷準確，維持原邏輯"]}


def _pick(entries, latest_bar_date):
    """每位成員×來源取「最近一筆已可驗證」的結論；優先用今天以前的結論（昨日結論覆盤）。"""
    today = date.today().strftime("%Y-%m-%d")
    picked = {}
    for e in sorted(entries, key=lambda x: x["recorded_at"]):
        if e["target_date"] > latest_bar_date:
            continue
        k = (e["member"], e["source"])
        prev = picked.get(k)
        if prev is None or not (prev["recorded_at"][:10] < today <= e["recorded_at"][:10]):
            picked[k] = e
    return list(picked.values())


def reflect_one(stock_id, entries):
    rows = daily_rows(stock_id)
    live = live_bar(stock_id) if stock_id != MARKET_ID else None
    latest = max([r["date"] for r in rows] + ([live["date"]] if live else []), default=None)
    if not latest:
        return None
    reviews = []
    for e in _pick(entries, latest):
        act = actual_since(stock_id, e["target_date"])
        if not act:
            continue
        res = evaluate(e, act)
        reviews.append({"member": e["member"], "member_name": e["member_name"], "source": e["source"],
                        "recorded_at": e["recorded_at"], "target_date": e["target_date"],
                        "prediction": {k: e.get(k) for k in ("price_at", "direction", "support", "resistance", "stop_loss",
                                                             "chip_view", "note")},
                        "actual": act, **res})
    if not reviews:
        return None
    reviews.sort(key=lambda r: (list(MEMBERS).index(r["member"]), r["recorded_at"]))
    gaps = [r for r in reviews if r["verdict"] == "落差"]
    name = next((e.get("name") for e in entries if e.get("name")), None)
    headline = (f"{gaps[0]['member_name']}：{gaps[0]['gap_text']}" if gaps
                else f"{len(reviews)} 項結論全數無重大落差")
    lessons = list(dict.fromkeys(c for r in gaps for c in r["corrections"]))[:5]
    return {"stock_id": stock_id, "name": name, "generated_at": _now(), "latest_bar_date": latest,
            "hits": sum(1 for r in reviews if r["verdict"] == "準確"), "gaps": len(gaps), "total": len(reviews),
            "headline": headline, "lessons": lessons, "reviews": reviews}


def reflect(stock_ids=None):
    ledger = load_ledger()["entries"]
    by_stock = {}
    for e in ledger:
        by_stock.setdefault(e["stock_id"], []).append(e)
    targets = [s for s in (stock_ids or by_stock) if s in by_stock]
    results = {}
    for sid in targets:
        out = reflect_one(sid, by_stock[sid])
        if not out:
            continue
        results[sid] = out
        _write_json(MARKET_PATH if sid == MARKET_ID else MEMORY_DIR / f"{sid}_reflection.json", out)
    if not stock_ids:
        write_index(results)
    return results


def write_index(results):
    members = {k: {"name": v, "hits": 0, "gaps": 0, "total": 0, "gap_items": []} for k, v in MEMBERS.items()}
    for sid, res in results.items():
        for r in res["reviews"]:
            m = members[r["member"]]
            m["total"] += 1
            m["hits"] += r["verdict"] == "準確"
            if r["verdict"] == "落差":
                m["gaps"] += 1
                m["gap_items"].append({"stock_id": sid, "text": r["gap_text"], "correction": r["corrections"][0],
                                       "recorded_at": r["recorded_at"]})
    for m in members.values():
        m["gap_items"].sort(key=lambda x: x["recorded_at"], reverse=True)
        m["gap_items"] = m["gap_items"][:20]
    _write_json(INDEX_PATH, {"generated_at": _now(), "members": members,
                             "stocks": {sid: {"headline": r["headline"], "hits": r["hits"], "gaps": r["gaps"]}
                                        for sid, r in results.items()}})


def run(stock_ids=None):
    _ROWS_CACHE.clear()  # dashboard 長駐行程：每輪重新讀取最新日 K
    added = snapshot()
    results = reflect(stock_ids)
    return {"added": added, "reflected": len(results),
            "gaps": sum(r["gaps"] for r in results.values()), "generated_at": _now()}


# ---------------------------------------------------------------- dashboard 讀取輔助

def load_reflection(stock_id):
    return _read_json(MARKET_PATH if stock_id == MARKET_ID else MEMORY_DIR / f"{stock_id}_reflection.json")


def load_index():
    return _read_json(INDEX_PATH)


def member_status(member, exclude_ids=()):
    """戰術桌一句話覆盤狀態；exclude_ids（金庫鎖定時的持股代號）不會出現在文字裡。"""
    m = (load_index().get("members") or {}).get(member)
    if not m or not m.get("total"):
        return "🔄 尚無可驗證的昨日結論"
    items = [g for g in m.get("gap_items") or [] if g["stock_id"] not in set(exclude_ids)]
    head = f"🔄 覆盤 {m['hits']} 準／{m['gaps']} 落差"
    if items:
        g = items[0]
        return f"{head}｜{g['stock_id']}：{g['correction']}"
    return head + ("｜持股落差已隱藏" if m["gaps"] else "｜維持原邏輯")


def holding_hint(stock_id):
    r = load_reflection(stock_id)
    if not r:
        return "—"
    gaps = [x for x in r.get("reviews") or [] if x["verdict"] == "落差"]
    if not gaps:
        return f"✅ {r.get('hits', 0)}/{r.get('total', 0)} 準確"
    g = gaps[0]
    return f"⚠️ {g['member_name']}：{'、'.join(t for t in g['tags'] if '落差' in t or t == '觸發停損')}｜{g['corrections'][0]}"


def main():
    parser = argparse.ArgumentParser(description="每日覆盤與自我修正記憶引擎")
    parser.add_argument("--snapshot", action="store_true", help="只收錄最新結論，不覆盤")
    parser.add_argument("--stock", action="append", help="只覆盤指定代號（可重複；TAIEX 為大盤）")
    args = parser.parse_args()
    if args.snapshot:
        print(f"🧠 已收錄 {snapshot()} 筆新結論 → {LEDGER_PATH.relative_to(ROOT)}")
        return
    out = run(args.stock)
    print(f"🧠 新增 {out['added']} 筆結論｜覆盤 {out['reflected']} 檔｜落差 {out['gaps']} 項")
    market = load_reflection(MARKET_ID)
    if market:
        print(f"📈 大盤：{market['headline']}")
    idx = load_index().get("members") or {}
    for k, m in idx.items():
        print(f"  {MEMBER_ICONS[k]} {m['name']}：{m['hits']} 準／{m['gaps']} 落差／共 {m['total']} 項")


if __name__ == "__main__":
    main()
