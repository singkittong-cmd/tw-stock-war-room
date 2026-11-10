# -*- coding: utf-8 -*-
"""📅 台股開休市日判斷（2026／民國 115 年證交所公告休市日 + 盤中零成交量自動偵測）。

用法：
    C:/Users/singk/anaconda3/python.exe tools/tw_market_calendar.py              # 今天是否開盤
    C:/Users/singk/anaconda3/python.exe tools/tw_market_calendar.py --probe      # 盤中實測台積電與加權指數成交量

1. 週六、週日一律休市。
2. CLOSED_DAYS：證交所「115 年市場開休市日期」公告的休市日（含 2/12–2/13 僅辦理結算交割、市場無交易）。
   新年度請把證交所公告加進 CLOSED_DAYS；未列入的年份只看週末，並靠第 3 點自動偵測。
3. 盤中自動偵測：09:10 後若台積電（2330）與加權指數當日成交量都是 0（或 Yahoo 最後成交日不是今天），
   判定今天為休市日，寫入 reports/simulation/market_calendar_state.json，當天其餘時段直接休眠。
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
PROBE_AFTER = dtime(9, 10)             # 開盤 10 分鐘後才判斷，避免剛開盤尚未成交的誤判

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
    return f"盤中偵測零成交量（{detected}）" if detected else None


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
    """實測台積電與加權指數今日成交量。回傳 {"closed": True/False/None, "evidence": "..."}；None 表示查不到、不下結論。"""
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
            return {"closed": vol_tsmc == 0 and vol_idx == 0, "evidence": "；".join(evidence)}
    except Exception as exc:  # 海外主機連不到證交所：改用 Yahoo
        evidence.append(f"MIS 失敗：{type(exc).__name__}")
    try:
        from tools import yahoo_quote as yq
        res = yq._get_chart("2330.TW", interval="1d", rng="5d", timeout=5)
        meta = res.get("meta") or {}
        ts = meta.get("regularMarketTime")
        last_day = datetime.fromtimestamp(ts, yq.TPE).strftime("%Y-%m-%d") if ts else None
        evidence.append(f"Yahoo 2330 最後成交日 {last_day}")
        if last_day:
            return {"closed": last_day != today, "evidence": "；".join(evidence)}
    except Exception as exc:
        evidence.append(f"Yahoo 失敗：{type(exc).__name__}")
    return {"closed": None, "evidence": "；".join(evidence)}


def check_today_open(now=None):
    """盤中呼叫：今天是否真的有開盤。表定休市日直接回 False；09:10 後每天實測一次成交量並記錄結果。"""
    now = now or datetime.now()
    if not is_trading_day(now):
        return False
    if now.time() < PROBE_AFTER:
        return True
    today = now.strftime("%Y-%m-%d")
    state = dict(_state())
    if state.get("verified_open") == today:
        return True
    if time.monotonic() - _probe_at["t"] < PROBE_RETRY_SEC:
        return True  # 上次查不到報價：5 分鐘後再實測
    _probe_at["t"] = time.monotonic()
    r = probe_volume(now)
    if r["closed"] is None:
        return True  # 查不到報價時不誤判為休市
    if r["closed"]:
        state.setdefault("detected_closed", {})[today] = r["evidence"]
    else:
        state["verified_open"] = today
    state["last_probe"] = {"at": now.strftime("%Y-%m-%d %H:%M:%S"), **r}
    _save_state(state)
    return not r["closed"]


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
