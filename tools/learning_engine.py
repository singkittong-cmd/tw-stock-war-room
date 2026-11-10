# -*- coding: utf-8 -*-
"""開盤／盤中／收盤實盤數據 × 五位成員分析對帳學習資料庫（整合 memory_reflection.py 的結論收錄）。

用法：
    C:/Users/singk/anaconda3/python.exe tools/learning_engine.py              # 追加快照 → 收錄預測 → 回溯驗算 → 提煉修正原則
    C:/Users/singk/anaconda3/python.exe tools/learning_engine.py --snapshot   # 只依目前時段追加實盤快照
    C:/Users/singk/anaconda3/python.exe tools/learning_engine.py --grade      # 只收錄預測並驗算
    C:/Users/singk/anaconda3/python.exe tools/learning_engine.py --stock 2408 # 印出該股學習摘要（送進 Prompt 的那一行）

資料庫 reports/learning_db/（永久累積，不刪歷史）：
- market_snapshots_log.jsonl：持股＋20 大龍頭（＋收盤時的加權指數）實盤快照，一行一筆，只追加不覆寫。
  時段標籤：OPEN 09:00–09:15／INTRADAY 09:15–13:30（每 15 分鐘或價格變動 ≥1%、燈號轉變、穿越 VWAP 才追加）／
  CLOSE 13:30 後（每檔每日一筆，收盤價更新時再追加）。欄位：開高低、現價/收盤、零股價、VWAP、量比、五檔委買委賣比、法人買賣超。
- snapshot_state.json：每檔每日的開盤/盤中/收盤軌跡摘要（驗算用，保留 30 個交易日；原始數據以 jsonl 為準）。
- agent_predictions_ledger.json：五位成員每一次的預測（時段、預測當下股價、看多/看空/觀望、動作、建議買進價、停損價、目標價、依據）
  與自動驗算結果 CORRECT／DEVIATED／WRONG，以及每位成員歷史累計與近 10 次準確率。
- agent_learning_memory.json：驗算出落差時自動生成的【歷史落差檢討與修正原則】（依個股 × 成員），供每次 AI 提示詞注入。

隱私：只記錄市場數據與成員結論，不記錄使用者的股數、成本與損益金額。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import memory_reflection as mem  # noqa: E402

REPORTS = ROOT / "reports"
LEARN_DIR = REPORTS / "learning_db"
SNAP_LOG = LEARN_DIR / "market_snapshots_log.jsonl"
SNAP_STATE = LEARN_DIR / "snapshot_state.json"
LEDGER_PATH = LEARN_DIR / "agent_predictions_ledger.json"
MEMORY_PATH = LEARN_DIR / "agent_learning_memory.json"
STATUS_PATH = REPORTS / "portfolio_live_status.json"
RADAR_PATH = REPORTS / "watchlist_radar_live.json"
CLOSE_PATH = REPORTS / "market_close_summary.json"
MEETING_PATH = REPORTS / "simulation" / "latest_committee_meeting.json"
DATA_DIR = REPORTS / "data"
TIME_FMT = "%Y-%m-%d %H:%M:%S"
MARKET_ID = mem.MARKET_ID
MEMBERS = mem.MEMBERS

INTRADAY_EVERY_MIN = 15      # 盤中定時快照間隔
KEY_MOVE_PCT = 1.0           # 關鍵變盤：距上一筆快照漲跌 ≥ 1%
TRACK_KEEP_DAYS = 30
GRADE_EVERY_SEC = 120        # 盤中報價刷新觸發驗算的最短間隔
EXPIRE_DAYS = 10             # 預測日後 10 天仍無實盤資料 → EXPIRED

RESULT_LABELS = {"CORRECT": "✅ 準確", "DEVIATED": "⚠️ 有落差", "WRONG": "❌ 方向錯誤"}
STANCE_OF = {"偏多": "看多", "偏空": "看空", "中性": "觀望"}
_last_sync = 0.0


# ---------------------------------------------------------------- 共用

def _read(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def _write(path, payload, indent=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=indent, default=str)
    tmp.replace(path)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _now():
    return datetime.now().strftime(TIME_FMT)


def _prev_weekday(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def session_tag(now=None):
    """回傳 (時段標籤, 對應交易日)：OPEN 09:00–09:15、INTRADAY 09:15–13:30、CLOSE 13:30 後；盤前與假日記為前一交易日 CLOSE。"""
    now = now or datetime.now()
    if now.weekday() >= 5:  # 週六、週日 → 週五收盤
        return "CLOSE", _prev_weekday(now.date() + timedelta(days=1)).strftime("%Y-%m-%d")
    t = now.time()
    if t < dtime(9, 0):
        return "CLOSE", _prev_weekday(now.date()).strftime("%Y-%m-%d")
    day = now.strftime("%Y-%m-%d")
    if t < dtime(9, 15):
        return "OPEN", day
    if t < dtime(13, 30):
        return "INTRADAY", day
    return "CLOSE", day


def prediction_session(stamp):
    """預測時段：開盤前（平日 09:00 前）／盤中（09:00–13:30）／收盤後。"""
    dt = datetime.strptime(stamp, TIME_FMT)
    if dt.weekday() < 5 and dt.time() < dtime(9, 0):
        return "開盤前"
    if dt.weekday() < 5 and dt.time() < dtime(13, 30):
        return "盤中"
    return "收盤後"


# ---------------------------------------------------------------- 1) 全時段實盤快照（只追加）

def _inst_of(stock_id):
    inst = (_read(DATA_DIR / f"{stock_id}_chips.json").get("data") or {}).get("institutional") or {}
    last = (inst.get("daily_10d") or [None])[-1]
    if not last:
        return {}
    return {"inst_date": last.get("date"), "foreign_net": last.get("foreign"), "trust_net": last.get("trust"),
            "dealer_net": last.get("dealer"), "inst_unit": "張"}


def _vol_ratio(stock_id, acc_vol):
    from tools.portfolio_intraday import elapsed_fraction
    vol_ma5 = _f((_read(DATA_DIR / f"{stock_id}_kline.json").get("data") or {}).get("vol_ma5"))
    frac = elapsed_fraction()
    return round(acc_vol / frac / vol_ma5, 2) if acc_vol and frac and vol_ma5 else None


def _fresh(updated_at, tag, trade_date, now):
    """報價檔是否屬於這個時段：CLOSE 需 13:30 後（或之後日期）的報價，盤中需 10 分鐘內。"""
    s = str(updated_at or "")
    if len(s) < 19:
        return False
    if tag == "CLOSE":
        if s[:10] == trade_date:
            return s[11:16] >= "13:30"
        # 之後日期的報價：只接受假日或 08:30 前（08:30–09:00 證交所 MIS 是今日試撮價，不是前一日收盤）
        dt = datetime.strptime(s[:19], TIME_FMT)
        return s[:10] > trade_date and (dt.weekday() >= 5 or dt.time() < dtime(8, 30))
    return s[:10] == trade_date and (now - datetime.strptime(s[:19], TIME_FMT)).total_seconds() <= 600


def collect_live_rows(tag, trade_date, now=None):
    """持股（portfolio_live_status.json）＋20 大龍頭（watchlist_radar_live.json）目前的實盤數據，以代號合併。"""
    now = now or datetime.now()
    rows = {}
    status = _read(STATUS_PATH)
    if _fresh(status.get("updated_at"), tag, trade_date, now):
        for p in status.get("positions") or []:
            if not p.get("current_price"):
                continue
            trend = p.get("intraday_trend") or {}
            rows[p["stock_id"]] = {
                "stock_id": p["stock_id"], "name": p.get("name"), "groups": ["holding"],
                "open": _f(p.get("open")), "high": _f(p.get("high")), "low": _f(p.get("low")), "last": _f(p["current_price"]),
                "prev_close": _f(p.get("prev_close")), "change_pct": _f(p.get("change_pct")), "vwap": _f(p.get("vwap")),
                "volume_ratio": _f(trend.get("volume_ratio")), "acc_vol": p.get("acc_vol"),
                "bid_ask_ratio": _f(p.get("bid_ask_ratio")), "tick_side": p.get("tick_side"),
                "orderbook_pattern": trend.get("orderbook_pattern"), "quote_time": p.get("quote_time"),
                "price_source": p.get("price_source"), "data_updated_at": status["updated_at"]}
    radar = _read(RADAR_PATH)
    if _fresh(radar.get("updated_at"), tag, trade_date, now):
        for r in radar.get("results") or []:
            if not r.get("price"):
                continue
            row = rows.get(r["stock_id"])
            if row:
                row["groups"].append("top20")
                row["signal"] = r.get("signal")
                continue
            rows[r["stock_id"]] = {
                "stock_id": r["stock_id"], "name": r.get("name"), "groups": ["top20"],
                "open": _f(r.get("open")), "high": _f(r.get("high")), "low": _f(r.get("low")), "last": _f(r["price"]),
                "prev_close": _f(r.get("prev_close")), "change_pct": _f(r.get("change_pct")), "vwap": _f(r.get("vwap")),
                "volume_ratio": _vol_ratio(r["stock_id"], r.get("acc_vol")), "acc_vol": r.get("acc_vol"),
                "bid_ask_ratio": _f(r.get("bid_ask_ratio")), "tick_side": r.get("tick_side"), "signal": r.get("signal"),
                "price_source": r.get("price_source"), "data_updated_at": radar["updated_at"]}
    if tag == "CLOSE":
        mc = _read(CLOSE_PATH)
        t, inst = mc.get("taiex") or {}, mc.get("institutional") or {}
        if t.get("date") == trade_date and t.get("close"):
            close, chg = _f(t["close"]), _f(t.get("change"))
            rows[MARKET_ID] = {
                "stock_id": MARKET_ID, "name": "加權指數", "groups": ["market"], "open": None, "high": _f(t.get("high")),
                "low": _f(t.get("low")), "last": close, "prev_close": round(close - chg, 2) if chg is not None else None,
                "change_pct": _f(t.get("change_pct")), "volume_ratio": _f(t.get("vol_ratio_20")) or None,
                "foreign_net": inst.get("foreign") if inst.get("date") == trade_date else None,
                "trust_net": inst.get("trust") if inst.get("date") == trade_date else None,
                "dealer_net": inst.get("dealer") if inst.get("date") == trade_date else None,
                "inst_date": inst.get("date"), "inst_unit": "億元", "data_updated_at": mc.get("generated_at")}
    return rows


def _log_reason(tag, row, st, now):
    """這一筆是否要追加到 jsonl，回傳原因（None = 不追加）。"""
    if tag == "OPEN":
        return None if st.get("OPEN") else "開盤"
    if tag == "CLOSE":
        return None if st.get("close") == row["last"] else ("收盤" if st.get("close") is None else "收盤價更新")
    last_ts, last_px = st.get("last_log_ts"), st.get("last_log_price")
    if not last_ts or not st.get("INTRADAY"):
        return "盤中首筆"
    if last_px and abs(row["last"] / last_px - 1) * 100 >= KEY_MOVE_PCT:
        return f"關鍵變盤（距上筆 {(row['last'] / last_px - 1) * 100:+.2f}%）"
    if row.get("signal") and st.get("last_signal") and row["signal"] != st["last_signal"]:
        return f"燈號轉變 {st['last_signal']}→{row['signal']}"
    side = "上" if row.get("vwap") and row["last"] >= row["vwap"] else "下" if row.get("vwap") else None
    if side and st.get("vwap_side") and side != st["vwap_side"]:
        return f"{'站上' if side == '上' else '跌破'} VWAP"
    if (now - datetime.strptime(last_ts, TIME_FMT)).total_seconds() >= INTRADAY_EVERY_MIN * 60:
        return f"定時（{INTRADAY_EVERY_MIN} 分鐘）"
    return None


def _odd_prices(ids):
    """零股報價（getOddInfo.jsp）；失敗回傳空 dict，不影響快照。"""
    ids = [s for s in ids if s != MARKET_ID]
    if not ids:
        return {}
    try:
        from tools.realtime_quote_engine import fetch_odd_lot_quotes
        return fetch_odd_lot_quotes(ids) or {}
    except Exception:
        return {}


def archive_snapshot(source="manual", now=None):
    """依目前時段把持股＋20 大龍頭實盤數據追加寫入 market_snapshots_log.jsonl；回傳追加筆數。"""
    now = now or datetime.now()
    tag, trade_date = session_tag(now)
    rows = collect_live_rows(tag, trade_date, now)
    if not rows:
        return 0
    state = _read(SNAP_STATE)
    stocks = state.setdefault("stocks", {})
    stamp = now.strftime(TIME_FMT)
    to_log = {}
    for sid, row in rows.items():
        st = stocks.setdefault(sid, {}).setdefault(trade_date, {})
        reason = _log_reason(tag, row, st, now)
        if reason:
            to_log[sid] = reason
        # 每日軌跡摘要（每次都更新，供驗算使用）
        st["name"] = row.get("name")
        for k in ("prev_close", "vwap", "volume_ratio"):
            if row.get(k) is not None:
                st[k] = row[k]
        if row.get("open") and not st.get("open"):
            st["open"] = row["open"]
        st["high"] = max(v for v in (st.get("high"), row.get("high"), row["last"]) if v is not None)
        st["low"] = min(v for v in (st.get("low"), row.get("low"), row["last"]) if v is not None)
        st["last"], st["last_ts"] = row["last"], stamp
        if tag == "OPEN" and not st.get("open_snapshot"):
            st["open_snapshot"] = {"ts": stamp, "price": row["last"], "vwap": row.get("vwap"),
                                   "bid_ask_ratio": row.get("bid_ask_ratio"), "tick_side": row.get("tick_side")}
        if tag == "CLOSE":
            st["close"] = row["last"]
        if row.get("signal"):
            st["last_signal"] = row["signal"]
        if row.get("vwap"):
            st["vwap_side"] = "上" if row["last"] >= row["vwap"] else "下"
    odd = _odd_prices(list(to_log)) if to_log else {}
    lines = []
    for sid, reason in to_log.items():
        row, st = rows[sid], stocks[sid][trade_date]
        o = odd.get(sid) or {}
        rec = {"ts": stamp, "trade_date": trade_date, "session": tag, "reason": reason, "source": source,
               **{k: row.get(k) for k in ("stock_id", "name", "groups", "open", "high", "low", "last", "prev_close",
                                          "change_pct", "vwap", "volume_ratio", "bid_ask_ratio", "tick_side",
                                          "orderbook_pattern", "signal", "price_source", "data_updated_at")},
               "close": row["last"] if tag == "CLOSE" else None,
               "odd_lot": {"price": o.get("odd_price"), "bid_1": o.get("odd_bid_1"), "ask_1": o.get("odd_ask_1"),
                           "acc_shares": o.get("odd_acc_shares"), "time": o.get("time")} if o else None}
        inst = _inst_of(sid) if sid != MARKET_ID else {k: row.get(k) for k in ("inst_date", "foreign_net", "trust_net",
                                                                               "dealer_net", "inst_unit")}
        rec.update(inst)
        lines.append(json.dumps(rec, ensure_ascii=False, default=str))
        st[tag] = True
        st["last_log_ts"], st["last_log_price"] = stamp, row["last"]
        st.setdefault("points", []).append(["13:30:00" if tag == "CLOSE" else stamp[11:19], row["last"], tag])
        st["points"] = st["points"][-60:]
    if lines:
        LEARN_DIR.mkdir(parents=True, exist_ok=True)
        with open(SNAP_LOG, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    cutoff = (now.date() - timedelta(days=TRACK_KEEP_DAYS * 7 // 5 + 2)).strftime("%Y-%m-%d")
    for sid in stocks:
        stocks[sid] = {d: v for d, v in stocks[sid].items() if d >= cutoff}
    state["updated_at"] = stamp
    _write(SNAP_STATE, state, indent=None)
    return len(lines)


def rebuild_state():
    """由 jsonl 歷史快照重建 snapshot_state.json（state 檔遺失或需修復時使用）。"""
    stocks = {}
    for rec in load_snapshots():
        st = stocks.setdefault(rec["stock_id"], {}).setdefault(rec["trade_date"], {})
        st["name"] = rec.get("name")
        for k in ("prev_close", "vwap", "volume_ratio"):
            if rec.get(k) is not None:
                st[k] = rec[k]
        if rec.get("open") and not st.get("open"):
            st["open"] = rec["open"]
        st["high"] = max(v for v in (st.get("high"), rec.get("high"), rec["last"]) if v is not None)
        st["low"] = min(v for v in (st.get("low"), rec.get("low"), rec["last"]) if v is not None)
        st["last"], st["last_ts"] = rec["last"], rec["ts"]
        if rec["session"] == "OPEN" and not st.get("open_snapshot"):
            st["open_snapshot"] = {"ts": rec["ts"], "price": rec["last"], "vwap": rec.get("vwap"),
                                   "bid_ask_ratio": rec.get("bid_ask_ratio"), "tick_side": rec.get("tick_side")}
        if rec["session"] == "CLOSE":
            st["close"] = rec["last"]
        st[rec["session"]] = True
        st["last_log_ts"], st["last_log_price"] = rec["ts"], rec["last"]
        st.setdefault("points", []).append(["13:30:00" if rec["session"] == "CLOSE" else rec["ts"][11:19], rec["last"],
                                            rec["session"]])
    _write(SNAP_STATE, {"updated_at": _now(), "stocks": stocks}, indent=None)
    return sum(len(v) for v in stocks.values())


def load_snapshots(stock_id=None, limit=None):
    """讀回 jsonl 歷史快照（可指定代號）；limit 取最後 N 筆。"""
    if not SNAP_LOG.exists():
        return []
    out = []
    with open(SNAP_LOG, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if stock_id is None or rec.get("stock_id") == stock_id:
                out.append(rec)
    return out[-limit:] if limit else out


# ---------------------------------------------------------------- 2) 五位成員預測存檔

ACTIONS_OK = {"看多": ("買進", "加碼", "續抱"), "看空": ("減碼", "停損"), "觀望": ("續抱", "減碼")}


def _action_of(stance, text):
    """從結論文字推動作，且必須與立場相容（看多不會是停損、看空不會是買進）。"""
    t = str(text or "")
    for words, action in ((("停損", "出場", "EXIT_STOP"), "停損"), (("減碼", "獲利", "TRIM", "EXIT_PROFIT"), "減碼"),
                          (("加碼", "順勢", "ADD"), "加碼"), (("買進", "布局", "攤平", "試單", "BUY", "WATCHLIST_PLAN"), "買進"),
                          (("續抱", "持有", "觀望", "HOLD"), "續抱")):
        if any(w in t for w in words) and action in ACTIONS_OK.get(stance, (action,)):
            return action
    return {"看多": "買進", "看空": "減碼"}.get(stance, "續抱")


def _prediction(key, member, stock_id, stamp, stance, price, buy_zone=None, stop_loss=None, target_price=None,
                rationale=None, source="", name=None, target_date=None, action=None):
    stance = STANCE_OF.get(stance, stance) if stance not in ("看多", "看空", "觀望") else stance
    return {
        "id": key, "timestamp": stamp, "session": prediction_session(stamp), "stock_id": stock_id, "name": name,
        "member": member, "member_name": MEMBERS.get(member, member), "source": source,
        "price_at_prediction": round(_f(price), 2) if _f(price) is not None else None, "stance": stance, "action": action or _action_of(stance, rationale),
        "buy_zone": _f(buy_zone), "stop_loss": _f(stop_loss), "target_price": _f(target_price),
        "rationale": (str(rationale)[:200] if rationale else None),
        "target_date": target_date or mem.target_date_of(stamp), "status": "OPEN", "grade": None,
    }


def load_ledger():
    led = _read(LEDGER_PATH)
    return led if isinstance(led.get("predictions"), list) else {"predictions": [], "member_stats": {}}


def _from_memory_entries(known):
    """把 memory_reflection 收錄的結論（summary、盤中會診、20 強定錨、盤前預判、新聞、朱家泓趨勢、inbox、快篩）轉成預測。"""
    out = []
    for e in mem.load_ledger()["entries"]:
        if e["key"] in known:
            continue
        out.append(_prediction(e["key"], e["member"], e["stock_id"], e["recorded_at"], e["direction"], e.get("price_at"),
                               buy_zone=e.get("support"), stop_loss=e.get("stop_loss"), target_price=e.get("resistance"),
                               rationale=e.get("note"), source=e["source"], name=e.get("name"), target_date=e.get("target_date")))
    return out


def _from_sim_meeting(known):
    """🧪 SIMULATION ROOM 投決會：巴董事長每一檔拍板（含非盤中的明日預備劇本）。"""
    m = _read(MEETING_PATH)
    stamp = mem._norm_stamp(m.get("held_at"))
    if not stamp:
        return []
    prices = {c["stock_id"]: c.get("price") for c in m.get("candidates") or []}
    names = {c["stock_id"]: c.get("name") for c in m.get("candidates") or []}
    out = []
    for d in m.get("decisions") or []:
        sid = str(d.get("stock_id") or "")
        key = f"chairman|{sid}|sim_committee|{stamp}"
        if not sid or key in known:
            continue
        act = str(d.get("action") or d.get("proposed_action") or "").upper()
        stance = "看多" if act in ("BUY", "ADD", "WATCHLIST_PLAN") else "看空" if act in ("TRIM", "EXIT_PROFIT", "EXIT_STOP") else "觀望"
        out.append(_prediction(key, "chairman", sid, stamp, stance, d.get("price") or prices.get(sid),
                               buy_zone=d.get("price") if stance == "看多" else None, stop_loss=d.get("stop_loss"),
                               target_price=d.get("target_price"), rationale=d.get("reason"), source="sim_committee",
                               name=names.get(sid), action=_action_of(stance, act)))
    return out


def record_prediction(member, stock_id, stance, price=None, buy_zone=None, stop_loss=None, target_price=None,
                      rationale=None, source="direct", name=None, action=None, stamp=None):
    """成員報告產出時直接寫入一筆預測（stance 可用 偏多/偏空/中性 或 看多/看空/觀望 或 評級文字）。"""
    stamp = stamp or _now()
    if stance not in ("看多", "看空", "觀望", "偏多", "偏空", "中性"):
        stance = mem.direction_of(stance)
    if _f(price) is None:
        price = mem.price_on(stock_id, stamp[:10]) if stamp[11:16] >= "13:30" else None
    key = f"{member}|{stock_id}|{source}|{stamp}"
    led = load_ledger()
    if any(p["id"] == key for p in led["predictions"]):
        return 0
    led["predictions"].append(_prediction(key, member, stock_id, stamp, stance, price, buy_zone, stop_loss, target_price,
                                          rationale, source, name, action=action))
    led["updated_at"] = _now()
    _write(LEDGER_PATH, led)
    return 1


# ---------------------------------------------------------------- 3) 自動回溯驗算（Auto-Grading）

def _kline_row(stock_id, day):
    if stock_id == MARKET_ID:
        t = _read(CLOSE_PATH).get("taiex") or {}
        if t.get("date") == day:
            return {"date": day, "open": None, "high": _f(t.get("high")), "low": _f(t.get("low")), "close": _f(t.get("close")),
                    "final": True}
    rows = mem.daily_rows(stock_id)
    row = next((r for r in rows if r["date"] == day), None)
    if not row:
        return None
    now = datetime.now()
    fetched = mem.kline_fetched_at(stock_id) if stock_id != MARKET_ID else now.strftime(TIME_FMT)
    final = day < now.strftime("%Y-%m-%d") or (fetched[:16] >= f"{day} 13:35" and now.strftime("%H:%M") >= "13:35")
    prev = [r for r in rows if r["date"] < day]
    return {**{k: _f(row.get(k)) for k in ("open", "high", "low", "close")}, "date": day, "final": final,
            "prev_close": _f(prev[-1]["close"]) if prev else None}


def _inst_on(stock_id, day):
    if stock_id == MARKET_ID:
        inst = _read(CLOSE_PATH).get("institutional") or {}
        return (inst.get("foreign"), inst.get("trust"), "億元") if inst.get("date") == day else (None, None, None)
    inst = (_read(DATA_DIR / f"{stock_id}_chips.json").get("data") or {}).get("institutional") or {}
    d = next((x for x in inst.get("daily_10d") or [] if str(x.get("date")) == day), None)
    return (d.get("foreign"), d.get("trust"), "張") if d else (None, None, None)


def day_actual(stock_id, day, after_ts=None, state=None):
    """預測驗證日的實盤表現：開盤、預測後最高/最低、最新價/收盤、開盤跳空、開盤 VWAP 與五檔、法人買賣超。"""
    track = ((state or _read(SNAP_STATE)).get("stocks") or {}).get(stock_id, {}).get(day) or {}
    row = _kline_row(stock_id, day)
    if not track and not row:
        return None
    opens = [v for v in (track.get("open"), (row or {}).get("open")) if v]
    highs = [v for v in (track.get("high"), (row or {}).get("high")) if v]
    lows = [v for v in (track.get("low"), (row or {}).get("low")) if v]
    close = track.get("close") or ((row or {}).get("close") if (row or {}).get("final") else None)
    last = close or track.get("last") or (row or {}).get("close")
    if not last:
        return None
    high, low = max(highs + [last]), min(lows + [last])
    # 盤中做出的預測：只看預測之後的軌跡
    if after_ts and after_ts[:10] == day and "09:00" <= after_ts[11:16] < "13:30":
        pts = [p[1] for p in track.get("points") or [] if p[0] >= after_ts[11:19] and p[2] != "CLOSE"]
        if pts:  # 有預測後的盤中快照才縮小區間，否則沿用全日高低
            high, low = max(pts + [last]), min(pts + [last])
    prev_close = track.get("prev_close") or (row or {}).get("prev_close")
    op = opens[0] if opens else None
    osnap = track.get("open_snapshot") or {}
    fn, tn, unit = _inst_on(stock_id, day)
    return {"date": day, "open": op, "high": high, "low": low, "last": last, "close": close, "final": close is not None,
            "prev_close": prev_close, "gap_pct": round((op / prev_close - 1) * 100, 2) if op and prev_close else None,
            "open_below_vwap": bool(osnap.get("vwap") and osnap.get("price") and osnap["price"] < osnap["vwap"]),
            "open_bid_ask_ratio": osnap.get("bid_ask_ratio"), "open_tick_side": osnap.get("tick_side"),
            "close_at_low": bool(close and high and low and close <= low * 1.003 and high >= low * 1.01),
            "foreign_net": fn, "trust_net": tn, "inst_unit": unit}


def judge(p, act):
    """回傳 (CORRECT|DEVIATED|WRONG, 報酬%, 判定理由清單)。"""
    ref = p.get("price_at_prediction") or act.get("prev_close")
    px = act["close"] or act["last"]
    ret = round((px / ref - 1) * 100, 2) if ref and px else None
    reasons, stance = [], p["stance"]
    if ret is None:
        return "DEVIATED", None, ["缺預測當下股價，無法計算方向"]
    bz, stop = p.get("buy_zone"), p.get("stop_loss")
    stop_hit = bool(stop and act["low"] is not None and act["low"] <= stop)
    # 看空時建議買進價被跌破本來就符合預期，只對看多／觀望檢查
    support_lost = bool(stance != "看空" and bz and act["low"] is not None and act["low"] < bz and px < bz * 0.98)
    if stance == "看多":
        if ret <= -1.5 or stop_hit:
            result = "WRONG"
        elif ret >= 0.3 and not support_lost:
            result = "CORRECT"
        else:
            result = "DEVIATED"
    elif stance == "看空":
        result = "WRONG" if ret >= 1.5 else "CORRECT" if ret <= -0.3 else "DEVIATED"
    else:  # 觀望沒有方向，區間假設失效只算落差
        result = "CORRECT" if abs(ret) <= 1.5 else "DEVIATED"
    if result == "CORRECT" and support_lost:
        result = "DEVIATED"
    reasons.append(f"預測後{'收' if act['close'] else '最新'} {px:g}（{ret:+.2f}%）")
    if act.get("gap_pct") is not None and abs(act["gap_pct"]) >= 1:
        reasons.append(f"開盤跳空 {act['gap_pct']:+.2f}%")
    if act.get("open_below_vwap"):
        reasons.append("開盤跌破均價線" + ("且五檔賣壓" if (act.get("open_bid_ask_ratio") or 1) < 0.8 else ""))
    if support_lost:
        reasons.append(f"建議買進價 {bz:g} 失守（最低 {act['low']:g}）")
    if stop_hit:
        reasons.append(f"觸及停損價 {stop:g}")
    if act.get("close_at_low"):
        reasons.append("收在當日最低附近")
    return result, ret, reasons


def principle_for(p, act, result, ret):
    """針對落差生成具體的【歷史落差檢討與修正原則】。"""
    who, sid, stance = p["member_name"], p["stock_id"], p["stance"]
    when = f"{p['timestamp'][5:16]}{p['session']}"
    price = p.get("price_at_prediction")
    head = f"{when}{who}對 {sid} {stance}" + (f"（當時 {price:g}）" if price else "")
    gap, fn, unit = act.get("gap_pct"), act.get("foreign_net"), act.get("inst_unit") or "張"
    rules = []
    if p["member"] == "chips" and stance == "看多" and fn is not None and fn < 0:
        rules.append(("外資一日行情", f"隔日外資反手賣超 {fn:+,.0f} {unit}（一日行情）→ 此股外資單日買超評分減半，需連買 ≥2 日才計入多方"))
    if stance == "看多" and p["session"] != "盤中" and gap is not None and gap <= -1:
        extra = "且跌破均價線" if act.get("open_below_vwap") else ""
        extra += "、五檔賣壓湧現" if (act.get("open_bid_ask_ratio") or 1) < 0.8 else ""
        rules.append(("跳空低開", f"{act['date'][5:]} 開盤跳空 {gap:+.2f}%{extra} → 收盤後看多前先核對美股費半與 TSM ADR，"
                                 f"跳空風險高時降為觀望、首筆買進量減半，開盤 15 分鐘站回 VWAP 再買進"))
    if p["session"] == "盤中" and stance == "看多" and act.get("close_at_low"):
        rules.append(("尾盤收最低", f"盤中認為有撐，尾盤卻收最低 {act['last']:g} → 13:00 後未站回 VWAP 不加碼，支撐需收盤確認"))
    bz, stop = p.get("buy_zone"), p.get("stop_loss")
    if stance != "看空" and bz and act["low"] is not None and act["low"] < bz and (act["close"] or act["last"]) < bz * 0.98:
        rules.append(("建議買進價失守", f"建議買進價 {bz:g} 失守（最低 {act['low']:g}）→ 下次建議買進價下移至 {act['low']:g} 附近，買進量減半"))
    if stance != "看空" and stop and act["low"] is not None and act["low"] <= stop:
        rules.append(("觸及停損", f"觸及停損價 {stop:g} → 依紀律停損，不攤平"))
    if stance == "看空" and ret is not None and ret > 0:
        rules.append(("看空反漲", f"看空後反漲 {ret:+.2f}% → 空方訊號需收盤確認，低檔乖離過大時不追空"))
    if stance == "觀望" and ret is not None and abs(ret) > 1.5:
        rules.append(("盤整失效", f"預期盤整但實際波動 {ret:+.2f}% → 區間假設失效，改依趨勢方向判斷"))
    if not rules and stance == "看多":
        rules.append(("看多落空", f"看多後{'收' if act['close'] else '現'} {act['close'] or act['last']:g}（{ret:+.2f}%）→ "
                                 f"給多方前先檢查 Priced-in、隔日沖與量能"))
    if not rules:
        rules.append(("方向落差", f"實際 {ret:+.2f}% 與預期不符 → 降低此類訊號權重"))
    return [{"pattern": k, "text": f"【歷史落差檢討與修正原則】{head}，{v}。",
             "short": f"{who}({p['timestamp'][5:10]}{stance}){v}"} for k, v in rules]


def grade_all(now=None):
    """驗算所有未結案預測：當日收盤確定後結案（CLOSED），盤中先給暫判（PROVISIONAL，不計入準確率）。回傳本次結案的預測。"""
    now = now or datetime.now()
    led = load_ledger()
    state = _read(SNAP_STATE)
    today = now.strftime("%Y-%m-%d")
    closed = []
    for p in led["predictions"]:
        if p["status"] in ("CLOSED", "EXPIRED"):
            continue
        td = p["target_date"]
        if td > today:
            continue
        act = day_actual(p["stock_id"], td, after_ts=p["timestamp"], state=state)
        if not act:
            if (now.date() - datetime.strptime(td, "%Y-%m-%d").date()).days > EXPIRE_DAYS:
                p["status"] = "EXPIRED"
            continue
        result, ret, reasons = judge(p, act)
        p["grade"] = {"result": result, "return_pct": ret, "reasons": reasons, "graded_at": now.strftime(TIME_FMT),
                      "actual": {k: act.get(k) for k in ("date", "open", "high", "low", "last", "close", "gap_pct",
                                                         "open_below_vwap", "close_at_low", "foreign_net", "trust_net",
                                                         "inst_unit")}}
        if act["final"]:
            p["status"] = "CLOSED"
            if result != "CORRECT":
                p["grade"]["principles"] = principle_for(p, act, result, ret)
            closed.append(p)
        else:
            p["status"] = "PROVISIONAL"
    led["member_stats"] = member_stats(led["predictions"])
    led["updated_at"] = now.strftime(TIME_FMT)
    _write(LEDGER_PATH, led)
    return closed, led


def member_stats(predictions):
    stats = {}
    for m, name in MEMBERS.items():
        done = sorted((p for p in predictions if p["member"] == m and p["status"] == "CLOSED" and p.get("grade")),
                      key=lambda p: (p["target_date"], p["timestamp"]))
        res = [p["grade"]["result"] for p in done]
        recent = res[-10:]
        stats[m] = {"name": name, "total": len(res), "correct": res.count("CORRECT"), "deviated": res.count("DEVIATED"),
                    "wrong": res.count("WRONG"),
                    "accuracy_pct": round(res.count("CORRECT") / len(res) * 100, 1) if res else None,
                    "recent10_pct": round(recent.count("CORRECT") / len(recent) * 100, 1) if recent else None,
                    "last_result": (done[-1]["grade"]["result"] if done else None),
                    "last_wrong": next(({"stock_id": p["stock_id"], "date": p["target_date"], "stance": p["stance"]}
                                        for p in reversed(done) if p["grade"]["result"] == "WRONG"), None)}
    return stats


# ---------------------------------------------------------------- 4) 錯誤修正記憶庫

def learn(led):
    """從已結案的落差預測提煉修正原則：依個股 × 成員保留最新 3 條，並偵測重複出現的模式（如外資常一日行情）。"""
    memory = {"lessons": [], "by_stock": {}, "stock_patterns": {}}
    for p in sorted(led["predictions"], key=lambda x: x["timestamp"]):
        g = p.get("grade") or {}
        if p["status"] != "CLOSED" or g.get("result") == "CORRECT":
            continue
        for r in g.get("principles") or []:
            memory["lessons"].append({"prediction_id": p["id"], "member": p["member"], "member_name": p["member_name"],
                                      "stock_id": p["stock_id"], "name": p.get("name"), "target_date": p["target_date"],
                                      "result": g["result"], "pattern": r["pattern"], "principle": r["text"],
                                      "short": r.get("short") or r["text"]})
    seen_pid, days = set(), {}
    for ls in memory["lessons"]:
        if ls["prediction_id"] not in seen_pid:  # 每筆預測只取優先度最高（第一條）的原則進 Prompt
            seen_pid.add(ls["prediction_id"])
            bucket = memory["by_stock"].setdefault(ls["stock_id"], {}).setdefault(ls["member"], [])
            bucket.append(ls["short"])
            memory["by_stock"][ls["stock_id"]][ls["member"]] = bucket[-3:]
        # 重複模式以「不同交易日」計次，避免同一天多位成員重複計算
        days.setdefault(ls["stock_id"], {}).setdefault(ls["pattern"], set()).add(ls["target_date"])
    memory["stock_patterns"] = {sid: {k: len(v) for k, v in pats.items()} for sid, pats in days.items()}
    memory["stock_rules"] = {}
    for sid, pats in memory["stock_patterns"].items():
        rules = []
        if pats.get("外資一日行情", 0) >= 2:
            rules.append(f"外資常一日行情（已 {pats['外資一日行情']} 次）：籌碼王對外資單日買超評分減半")
        if pats.get("跳空低開", 0) >= 2:
            rules.append(f"收盤後看多常遇隔日跳空低開（{pats['跳空低開']} 次）：首筆買進量減半、等開盤 15 分鐘")
        if pats.get("建議買進價失守", 0) >= 2:
            rules.append(f"建議買進價常被跌破（{pats['建議買進價失守']} 次）：建議買進價下移一檔支撐")
        if pats.get("尾盤收最低", 0) >= 2:
            rules.append(f"盤中支撐常在尾盤失守（{pats['尾盤收最低']} 次）：13:00 前不加碼")
        if rules:
            memory["stock_rules"][sid] = rules
    memory["lessons"] = memory["lessons"][-500:]
    memory["member_stats"] = led.get("member_stats") or {}
    memory["updated_at"] = _now()
    _write(MEMORY_PATH, memory)
    return memory


def sync(now=None):
    """收錄最新預測 → 回溯驗算 → 提煉修正原則。回傳摘要。"""
    global _last_sync
    led = load_ledger()
    known = {p["id"] for p in led["predictions"]}
    new = _from_memory_entries(known) + _from_sim_meeting(known)
    if new:
        led["predictions"] += new
        led["updated_at"] = _now()
        _write(LEDGER_PATH, led)
    closed, led = grade_all(now)
    memory = learn(led)
    _last_sync = time.time()
    return {"added": len(new), "closed_now": len(closed), "total": len(led["predictions"]),
            "open": sum(1 for p in led["predictions"] if p["status"] in ("OPEN", "PROVISIONAL")),
            "lessons": len(memory["lessons"]), "member_stats": led["member_stats"]}


def on_price_update(source, force_grade=False):
    """報價刷新掛勾（portfolio_intraday／watchlist_intraday_radar／market_close_summary）：追加快照＋節流驗算。"""
    n = archive_snapshot(source)
    if force_grade or n or time.time() - _last_sync >= GRADE_EVERY_SEC:
        sync()
    return n


# ---------------------------------------------------------------- 讀取輔助（Prompt 注入與 dashboard）

def load_memory():
    return _read(MEMORY_PATH)


def member_accuracy(member=None):
    stats = load_ledger().get("member_stats") or {}
    return stats.get(member) if member else stats


def _g(v):
    return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, (int, float)) else "?"


def _fmt_pct(v):
    return f"{v:.0f}%" if v is not None else "N/A"


def team_accuracy_line():
    """送進每次 Prompt 的「五位成員近期準確率＋剛看錯的成員」。"""
    stats = member_accuracy() or {}
    parts, wrong = [], []
    for m, s in stats.items():
        if s.get("total"):
            parts.append(f"{s['name']}{_fmt_pct(s['accuracy_pct'])}({s['total']}次,近10次{_fmt_pct(s['recent10_pct'])})")
            if s.get("last_result") == "WRONG" and s.get("last_wrong"):
                wrong.append(f"{s['name']}({s['last_wrong']['stock_id']} {s['last_wrong']['date'][5:]}{s['last_wrong']['stance']}錯)")
    if not parts:
        return "【成員準確率】尚無已結案的對帳紀錄"
    return "【成員準確率】" + "｜".join(parts) + (f"｜剛看錯：{'、'.join(wrong)}" if wrong else "")


def stock_learning_line(stock_id, limit=3):
    """送進每次 Prompt 的個股學習摘要：近期開盤/盤中/收盤對帳結果＋修正原則＋重複模式規則。"""
    led = load_ledger()
    done = [p for p in led["predictions"] if p["stock_id"] == stock_id and p["status"] in ("CLOSED", "PROVISIONAL")
            and p.get("grade")]
    done.sort(key=lambda p: p["timestamp"], reverse=True)
    seen, recent = set(), []
    for p in done:  # 每位成員只取最近一筆，避免同一來源洗版
        if p["member"] in seen:
            continue
        seen.add(p["member"])
        g, a = p["grade"], p["grade"]["actual"]
        recent.append(f"{p['timestamp'][5:10]}{p['member_name']}{p['stance']}@{_g(p.get('price_at_prediction'))}→"
                      f"開{_g(a.get('open'))}高{_g(a.get('high'))}低{_g(a.get('low'))}{'收' if a.get('close') else '現'}"
                      f"{_g(a.get('close') or a.get('last'))} {g['result']}")
        if len(recent) >= limit:
            break
    memory = load_memory()
    rules = (memory.get("stock_rules") or {}).get(stock_id) or []
    principles = list(dict.fromkeys(ps[-1] for ps in ((memory.get("by_stock") or {}).get(stock_id) or {}).values()))
    if not recent and not rules:
        return "【學習對帳】此股尚無已驗算的預測"
    out = "【學習對帳】" + ("；".join(recent) if recent else "")
    if principles:
        out += "｜修正原則：" + "；".join(x[:90] for x in principles[:3])
    if rules:
        out += "｜重複模式：" + "；".join(rules)
    return out[:520]


def market_learning_line():
    return f"{team_accuracy_line()}\n{stock_learning_line(MARKET_ID, limit=2)}"


def member_history(member, limit=15, exclude_ids=()):
    """dashboard 個人報告「📚 歷史判斷 vs 實盤對帳學習紀錄表」資料列。"""
    rows = [p for p in load_ledger()["predictions"] if p["member"] == member and p.get("grade")
            and p["stock_id"] not in set(exclude_ids)]
    rows.sort(key=lambda p: p["timestamp"], reverse=True)
    return rows[:limit]


def main():
    parser = argparse.ArgumentParser(description="開盤/盤中/收盤 × 五位成員分析對帳學習資料庫")
    parser.add_argument("--snapshot", action="store_true", help="只依目前時段追加實盤快照")
    parser.add_argument("--grade", action="store_true", help="只收錄預測並驗算")
    parser.add_argument("--stock", action="append", help="印出該代號的學習摘要（TAIEX 為大盤）")
    args = parser.parse_args()
    if args.stock:
        print(team_accuracy_line())
        for sid in args.stock:
            print(stock_learning_line(sid))
        return
    tag, trade_date = session_tag()
    if not args.grade:
        n = archive_snapshot("learning_engine_cli")
        print(f"📸 時段 {tag}（交易日 {trade_date}）追加 {n} 筆快照 → {SNAP_LOG.relative_to(ROOT)}")
    if args.snapshot:
        return
    mem.snapshot(sync_learning=False)
    out = sync()
    print(f"🧠 新收錄 {out['added']} 筆預測｜本次結案 {out['closed_now']} 筆｜未結案 {out['open']}｜累計 {out['total']} 筆｜"
          f"修正原則 {out['lessons']} 條")
    for m, s in out["member_stats"].items():
        print(f"  {mem.MEMBER_ICONS[m]} {s['name']}：準確率 {_fmt_pct(s['accuracy_pct'])}（{s['total']} 次驗證；"
              f"CORRECT {s['correct']}／DEVIATED {s['deviated']}／WRONG {s['wrong']}）｜近 10 次 {_fmt_pct(s['recent10_pct'])}")


if __name__ == "__main__":
    main()
