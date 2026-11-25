# -*- coding: utf-8 -*-
"""📅 台股開休市日判斷（2026／民國 115 年證交所公告休市日 + 盤中零成交量自動偵測）。

用法：
    C:/Users/singk/anaconda3/python.exe tools/tw_market_calendar.py              # 今天是否開盤
    C:/Users/singk/anaconda3/python.exe tools/tw_market_calendar.py --probe      # 盤中實測台積電與加權指數成交量

1. 週六、週日一律休市。
2. CLOSED_DAYS：證交所「115 年市場開休市日期」公告的休市日（含 2/12–2/13 僅辦理結算交割、市場無交易）。
   新年度請把證交所公告加進 CLOSED_DAYS；未列入的年份只看週末，並靠第 3 點自動偵測。
3. 週一至週五且不在 CLOSED_DAYS → 預設有開盤。盤中動態偵測只認「證交所 MIS 明確回報台積電零成交量」，
   且 09:30 後、間隔 ≥ 5 分鐘連續兩次都是零才判定休市（寫入 reports/simulation/market_calendar_state.json）。
4. MIS 失敗、Yahoo 最後成交日還停在昨天 → 只是「報價延遲／抓價失敗」，維持開盤，狀態列顯示
   「報價延遲中，等待重試」，背景引擎每 15 秒照常抓價，抓到今天的報價就自動恢復投決會。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE_PATH = ROOT / "reports" / "simulation" / "market_calendar_state.json"
PROBE_AFTER = dtime(9, 10)             # 開盤 10 分鐘後才實測報價
CLOSE_CONFIRM_AFTER = dtime(9, 30)     # 判定休市至少要等到 09:30（MIS 零成交量連續兩次）
DETECT_METHOD = "mis_zero_volume_x2"   # 只有這種偵測結果才算休市；舊版（含 Yahoo 延遲誤判）的紀錄一律忽略

# 證交所「115 年市場開休市日期」（https://www.twse.com.tw/holidaySchedule/holidaySchedule）
CLOSED_DAYS = {
    "2026-01-01": "中華民國開國紀念日",
    "2026-02-12": "春節前市場無交易（僅辦理結算交割）",
    "2026-02-13": "春節前市場無交易（僅辦理結算交割）",
    "2026-02-16": "農曆除夕及春節",
    "2026-02-17": "農曆除夕及春節",
    "2026-02-18": "農曆除夕及春節",
    "2026-02-19": "農曆除夕及春節",
    "2026-02-20": "農曆春節補假",
    "2026-02-27": "和平紀念日補假",
    "2026-04-03": "兒童節及民族掃墓節補假",
    "2026-04-06": "兒童節及民族掃墓節補假",
    "2026-05-01": "勞動節",
    "2026-06-19": "端午節",
    "2026-09-25": "中秋節",
    "2026-09-28": "孔子誕辰紀念日／教師節",
    "2026-10-09": "國慶日補假",
    "2026-10-26": "臺灣光復暨金門古寧頭大捷紀念日補假",
    "2026-12-25": "行憲紀念日",
}

PROBE_RETRY_SEC = 300
DELAY_RETRY_SEC = 60                   # 報價延遲時每 60 秒重新實測一次是否已取得今日報價

_lock = threading.Lock()
_state_cache = {"mtime": None, "data": {}}
_probe_at = {"t": -1e9}


def _d(day):
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, str):
        return datetime.strptime(day[:10], "%Y-%m-%d").date()
    return day


def _state():
    try:
        mtime = STATE_PATH.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        if _state_cache["mtime"] != mtime:
            try:
                _state_cache["data"] = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                _state_cache["data"] = {}
            _state_cache["mtime"] = mtime
        return _state_cache["data"]


def holiday_name(day):
    """休市原因；開盤日回傳 None。"""
    d = _d(day)
    if d.weekday() >= 5:
        return "週末"
    key = d.strftime("%Y-%m-%d")
    if key in CLOSED_DAYS:
        return CLOSED_DAYS[key]
    detected = (_state().get("detected_closed") or {}).get(key)
    if isinstance(detected, dict) and detected.get("method") == DETECT_METHOD:
        return f"盤中偵測零成交量（{detected.get('evidence')}）"
    return None  # 舊版紀錄（例如 MIS 失敗＋Yahoo 延遲的誤判）不算休市


def is_trading_day(day):
    return holiday_name(day) is None


def next_trading_day(day, include_today=True):
    d = _d(day)
    if not include_today:
        d += timedelta(days=1)
    for _ in range(30):
        if is_trading_day(d):
            return d
        d += timedelta(days=1)
    return d


def _save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(f"{STATE_PATH.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def probe_volume(now=None):
    """實測台積電今日成交量。回傳 {"closed": True/False/None, "evidence": "..."}。
    closed=True 只來自 MIS 明確回報台積電零成交量（加權指數 MIS 量常為 0，不採用）；
    Yahoo 最後成交日是今天 → False；Yahoo 還停在昨天或全部失敗 → None（報價延遲，不下結論）。"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    evidence = []
    try:
        from tools import realtime_quote_engine as rq
        q = rq.fetch_mis_quotes(["2330", "t00"])
        tsmc, idx = q.get("2330"), q.get("t00")
        if tsmc is not None and idx is not None:
            vol_tsmc, vol_idx = tsmc.get("acc_vol") or 0, idx.get("acc_vol") or 0
            evidence.append(f"MIS 2330 量 {vol_tsmc}、加權指數量 {vol_idx}")
            return {"closed": vol_tsmc == 0, "evidence": "；".join(evidence), "source": "mis"}
    except Exception as exc:  # 海外主機連不到證交所：改用 Yahoo
        evidence.append(f"MIS 失敗：{type(exc).__name__}")
    try:
        from tools import yahoo_quote as yq
        res = yq._get_chart("2330.TW", interval="1d", rng="5d", timeout=5)
        meta = res.get("meta") or {}
        ts = meta.get("regularMarketTime")
        last_day = datetime.fromtimestamp(ts, yq.TPE).strftime("%Y-%m-%d") if ts else None
        evidence.append(f"Yahoo 2330 最後成交日 {last_day}")
        if last_day == today:
            return {"closed": False, "evidence": "；".join(evidence), "source": "yahoo"}
        # Yahoo 停在昨天：可能只是報價延遲，不能當成休市證據
    except Exception as exc:
        evidence.append(f"Yahoo 失敗：{type(exc).__name__}")
    return {"closed": None, "evidence": "；".join(evidence)}


def check_today_open(now=None):
    """盤中呼叫：今天是否開盤。週末與表定休市日回 False；其餘預設開盤。
    報價抓不到或停在昨天只記為「報價延遲」（仍回 True）；MIS 零成交量要 09:30 後連續兩次才判定休市。"""
    now = now or datetime.now()
    if not is_trading_day(now):
        return False
    if now.time() < PROBE_AFTER:
        return True
    today = now.strftime("%Y-%m-%d")
    state = dict(_state())
    if state.get("verified_open") == today:
        return True
    delayed = (state.get("quote_delay") or {}).get("date") == today
    wait = DELAY_RETRY_SEC if delayed else PROBE_RETRY_SEC
    if time.monotonic() - _probe_at["t"] < wait:
        return True
    _probe_at["t"] = time.monotonic()
    r = probe_volume(now)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    state["last_probe"] = {"at": stamp, **r}
    if r["closed"] is None:  # 報價延遲／抓價失敗：維持開盤，等待重試
        state["quote_delay"] = {"date": today, "at": stamp, "evidence": r["evidence"]}
        _save_state(state)
        return True
    state.pop("quote_delay", None)
    if r["closed"]:
        zero = state.get("zero_volume_seen") or {}
        prev = zero.get("at") if zero.get("date") == today else None
        if now.time() >= CLOSE_CONFIRM_AFTER and prev and (now - datetime.strptime(prev, "%Y-%m-%d %H:%M:%S")).total_seconds() >= 300:
            state.setdefault("detected_closed", {})[today] = {"method": DETECT_METHOD, "evidence": r["evidence"], "at": stamp}
            _save_state(state)
            return False
        if not prev:
            state["zero_volume_seen"] = {"date": today, "at": stamp}
        _save_state(state)
        return True  # 第一次看到零成交量：先不下結論，5 分鐘後再確認
    state["verified_open"] = today
    state.pop("zero_volume_seen", None)
    _save_state(state)
    return True


def quote_delay_note(now=None):
    """今天報價延遲中（MIS 失敗且 Yahoo 還停在昨天）時回傳狀態列文字，否則回傳 None。"""
    now = now or datetime.now()
    state = _state()
    today = now.strftime("%Y-%m-%d")
    d = state.get("quote_delay") or {}
    if d.get("date") != today or state.get("verified_open") == today or not is_trading_day(now):
        return None
    return f"⏳ 報價延遲中，等待重試（{d.get('at', '')[11:16]} 實測：{d.get('evidence', '')}）"


def main():
    parser = argparse.ArgumentParser(description="📅 台股開休市日判斷")
    parser.add_argument("--date", help="YYYY-MM-DD（預設今天）")
    parser.add_argument("--probe", action="store_true", help="實測台積電與加權指數今日成交量")
    args = parser.parse_args()
    day = _d(args.date) if args.date else date.today()
    name = holiday_name(day)
    print(f"{day} {'🌙 休市：' + name if name else '🟢 開盤日'}｜下一個開盤日 {next_trading_day(day, include_today=False)}")
    if args.probe:
        print(json.dumps(probe_volume(), ensure_ascii=False))


if __name__ == "__main__":
    main()
