# -*- coding: utf-8 -*-
"""🤖 SIMULATION ROOM 伺服器端背景常駐盯盤引擎（SimRoomDaemon）。

掛在 Streamlit 伺服器行程內（dashboard.py 以 @st.cache_resource 啟動），與瀏覽器分頁是否 Active、
使用者停在哪個頁面無關。也可單獨執行：

    C:/Users/singk/anaconda3/python.exe tools/sim_background_daemon.py --run      # 前景常駐（不開 dashboard 時用）
    C:/Users/singk/anaconda3/python.exe tools/sim_background_daemon.py --status   # 印出 daemon_heartbeat.json

誰會啟動交易執行緒（tools/github_memory_sync.py 的 AUTOPILOT_MODE）：
- 本機是交易者（local_master，或 Streamlit Cloud 上的 cloud_master）：啟動 SimRoomDaemon。
  cloud_master 時每次投決會前先從 GitHub 取回最新帳本；盤中靜默不推送心跳，只有實際成交才比對
  last_trade_timestamp 後推回帳本與學習資料庫，收盤結算後全部推送（減少 commit，避免 Streamlit Cloud 盤中重新載入）。
- 本機是觀看端（cloud_master 下的本機電腦）：只啟動 SimLedgerMirror，每 30 秒從 GitHub 拉回 reports/simulation，嚴禁下單。

時段（開盤日＝排除週末與 tools/tw_market_calendar.py 的證交所休市日，並在 09:10 實測成交量）：
- 09:00:00–13:30:30 盤中：每 15 秒一個 Tick
  A. 0 Token 更新模擬持倉整張價／盤中零股價／VWAP 與 20 強建議買進價監控表（含五檔比），重算總淨值。
  B. 觸發檢查（同一檔 10 分鐘冷卻）：持倉觸及停損價／停利目標價、20 強「達建議買進價」且五檔委買比 ≥ 0.75
     （可用現金足夠、該股未滿倉）、每 20 分鐘定時巡檢（09:15、09:35…）。
  C. 觸發時自動召開五人投決會（fast_team_runner.sim_trade_session，預設免費 Gemini），巴董事長拍板交割，
     並同步寫入 learning_engine 學習庫。13:30:00 之後只更新報價、不再下單。
- 13:30:30 後：當日尚未結算時，更新最後收盤價、寫入 sim_nav_history.json、learning_engine 收盤對帳，標記已結算。
- 其餘時段：每 30 秒更新心跳（STANDBY），不呼叫任何報價或模型 API，嚴禁下單。

單例：同一行程內以執行緒名稱判斷、跨行程以 daemon.lock 檔案鎖判斷，永遠只有一條 SimRoomDaemon。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import github_memory_sync as ghsync  # noqa: E402
from tools import sim_portfolio_engine as sim  # noqa: E402
from tools import tw_market_calendar as cal  # noqa: E402

SIM_DIR = sim.SIM_DIR
HEARTBEAT_PATH = SIM_DIR / "daemon_heartbeat.json"
CONFIG_PATH = SIM_DIR / "daemon_config.json"
COMMAND_PATH = SIM_DIR / "daemon_command.json"
PROC_LOCK_PATH = SIM_DIR / "daemon.lock"
TIME_FMT = sim.TIME_FMT

THREAD_NAME = "SimRoomDaemon"
MIRROR_THREAD_NAME = "SimLedgerMirror"
TICK_SEC = 15
PULL_ACTIONS_EVERY_SEC = 600           # Streamlit 主機每 10 分鐘取回 GitHub Actions 產生的早報、新聞、記憶庫
STANDBY_BEAT_SEC = 30
COOLDOWN_SEC = 600                     # 同一檔觸發後 10 分鐘內不重複觸發
PATROL_START, PATROL_EVERY_MIN = dtime(9, 15), 20
BID_ASK_MIN = 0.75
OPEN_T, TRADE_END, LOOP_END, SETTLE_END = dtime(9, 0), dtime(13, 30), dtime(13, 30, 30), dtime(13, 35)
ALIVE_MAX_AGE = 90                     # 心跳超過 90 秒視為停止（投決會進行中放寬到 300 秒）
MEETING_MAX_AGE = 300
CLOUD_ALIVE_MAX_AGE = 900              # 觀看端看到的雲端心跳：每 5 分鐘推送一次，容許 15 分鐘
DEFAULT_CONFIG = {"auto_trade_enabled": True, "provider": "gemini"}

STATUS_TEXT = {
    "HOLIDAY": "🌙 HOLIDAY (今日台股休市，背景守護待命，下個開盤日 09:00:00 自動啟動)",
    "TRADING": "⚡ TRADING (盤中背景自動盯盤中，每 15 秒更新報價並檢查進出場，一路跑到 13:30 收盤)",
    "MEETING": "🧠 MEETING (背景五人投決會進行中，巴董事長拍板進出場)",
    "SETTLING": "🧾 SETTLING (收盤結算中：更新收盤價、記錄當日淨值、五人準確率對帳)",
    "STANDBY": "STANDBY_AFTER_HOURS (背景守護待命中，將於下個交易日 09:00:00 自動啟動盤中盯盤至 13:30 收盤)",
}

_proc_lock_file = None                 # 持有跨行程鎖的檔案控制代碼（行程結束時由 OS 自動釋放）


def _now():
    return datetime.now()


def _ts(dt=None):
    return (dt or _now()).strftime(TIME_FMT)


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    for _ in range(5):  # Windows 上 dashboard 正在讀檔時 replace 可能短暫失敗
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- 設定與時段

def load_config():
    return {**DEFAULT_CONFIG, **_read(CONFIG_PATH, {})}


def save_config(**changes):
    cfg = {**load_config(), **changes, "updated_at": _ts()}
    _write(CONFIG_PATH, cfg)
    return cfg


def is_trading_day(now):
    return cal.is_trading_day(now)


def phase_of(now):
    """TRADING（09:00:00–13:30:30）／SETTLE（13:30:30 後、當日未結算才會執行）／STANDBY。"""
    if not is_trading_day(now):
        return "STANDBY"
    t = now.time()
    if OPEN_T <= t < LOOP_END:
        return "TRADING"
    if t >= LOOP_END:
        return "SETTLE"
    return "STANDBY"


def next_open(now):
    d = cal.next_trading_day(now.date(), include_today=now.time() < OPEN_T)
    return datetime.combine(d, OPEN_T)


def patrol_slot(now):
    """09:15 起每 20 分鐘一格（09:15、09:35…13:15），回傳格號；09:15 前為 None。"""
    start = datetime.combine(now.date(), PATROL_START)
    if now < start or now.time() > TRADE_END:
        return None
    return int((now - start).total_seconds() // (PATROL_EVERY_MIN * 60))


# ---------------------------------------------------------------- 觸發條件

def can_buy(acct, p):
    """依 strategy_rules.json：未達禁止追高門檻、扣最低現金水位後 ≥ 新建倉下限或 100 股、距單檔上限還放得下 100 股。"""
    rules = sim.load_strategy_rules()
    price = p.get("price") or 0
    cap = sim.position_max(acct, rules)
    if price <= 0 or price * sim.LOT_UNIT > cap:
        return False
    try:
        if float(p.get("change_pct") or 0) >= rules["avoid_chase_pct"]:
            return False
    except (TypeError, ValueError):
        pass
    reserve = acct["total_equity"] * sim.cash_reserve_pct(rules) / 100
    free = acct["cash_balance"] - reserve
    h = sim.holding_of(acct, p["stock_id"])
    if sim.trade_block_reason(p["stock_id"], "ADD" if h else "BUY", rules):
        return False  # 停損冷卻中或禁止當沖：不為它召開投決會
    held_value = h["shares"] * price if h else 0
    if cap - held_value < price * sim.LOT_UNIT:
        return False
    return free >= (price * sim.LOT_UNIT if h else sim.position_min(acct, rules))


def find_triggers(scan, acct, cooldowns, now):
    """回傳觸發清單：STOP／TARGET（持倉）與 HIT_ENTRY（20 強且五檔委買比 ≥ 0.75），已排除冷卻中的代號。"""
    out = []
    cool = {sid for sid, ts in cooldowns.items() if (now - datetime.strptime(ts, TIME_FMT)).total_seconds() < COOLDOWN_SEC}
    rules = sim.load_strategy_rules()
    for a in scan.get("alerts") or []:
        if a["type"] == "STOP":
            h = sim.holding_of(acct, a["stock_id"]) or {}
            try:
                hard = float(h.get("unrealized_pct")) <= -rules["stop_loss_pct"]
            except (TypeError, ValueError):
                hard = False
            if sim.trade_block_reason(a["stock_id"], "EXIT_STOP", rules, now=now, hard_stop=hard):
                continue  # 未滿最短持有／禁止當沖：小幅震盪不召開停損投決會
        if a["stock_id"] not in cool:
            out.append({"type": a["type"], "stock_id": a["stock_id"], "text": a["text"]})
    for p in scan.get("prey") or []:
        if p["stock_id"] in cool or p.get("signal") != "HIT_ENTRY":
            continue
        ratio = p.get("bid_ask_ratio")
        if ratio is not None and ratio >= BID_ASK_MIN and can_buy(acct, p):
            out.append({"type": "HIT_ENTRY", "stock_id": p["stock_id"],
                        "text": f"{p.get('name') or ''} 現價 {p.get('price')} 達建議買進價、五檔委買比 {ratio:.2f}"})
    return out


TRIGGER_LABEL = {"STOP": "🚨 觸及停損價", "TARGET": "💰 觸及停利目標價", "HIT_ENTRY": "🎯 達建議買進價", "PATROL": "🕒 定時巡檢",
                 "MANUAL": "👑 管理者手動召開"}


# ---------------------------------------------------------------- 守護執行緒

class SimRoomDaemon(threading.Thread):
    def __init__(self):
        super().__init__(name=THREAD_NAME, daemon=True)
        self.stop_event = threading.Event()
        old = _read(HEARTBEAT_PATH, {})
        today = _now().strftime("%Y-%m-%d")
        same_day = old.get("date") == today
        self.hb = {
            "thread_name": THREAD_NAME, "pid": os.getpid(), "host": os.getenv("AUTOPILOT_HOST") or ghsync.where(),
            "autopilot_mode": ghsync.autopilot_mode(),
            "started_at": _ts(), "date": today,
            "scans_today": old.get("scans_today", 0) if same_day else 0,
            "triggers_today": old.get("triggers_today", 0) if same_day else 0,
            "meetings_today": old.get("meetings_today", 0) if same_day else 0,
            "trades_today": old.get("trades_today", 0) if same_day else 0,
            "cooldowns": old.get("cooldowns", {}) if same_day else {},
            "patrol_slot_done": old.get("patrol_slot_done") if same_day else None,
            "daily_close_settled_date": old.get("daily_close_settled_date"),
            "last_trigger": old.get("last_trigger") if same_day else None,
            "last_meeting": old.get("last_meeting") if same_day else None,
            "last_settlement": old.get("last_settlement"),
            "last_ping": old.get("last_ping"),
            "last_sync": old.get("last_sync"),
            "last_error": None,
        }
        self._last_beat = 0.0
        self._next_tick = 0.0
        self.cloud_sync = ghsync.ledger_sync_active()   # cloud_master 的交易主機：交易前後與 GitHub 同步
        self._last_pull_actions = 0.0

    # ---- 心跳
    def beat(self, phase, status_key=None, **extra):
        now = _now()
        today = now.strftime("%Y-%m-%d")
        if self.hb["date"] != today:  # 換日：重置當日計數
            self.hb.update(date=today, scans_today=0, triggers_today=0, meetings_today=0, trades_today=0,
                           cooldowns={}, patrol_slot_done=None, last_trigger=None, last_meeting=None)
        cfg = load_config()
        self.hb.update(pid=os.getpid(), last_heartbeat=_ts(now), phase=phase,
                       status=STATUS_TEXT[status_key or phase], auto_trade_enabled=cfg["auto_trade_enabled"],
                       provider=cfg["provider"], next_open=_ts(next_open(now)), **extra)
        _write(HEARTBEAT_PATH, self.hb)
        self._last_beat = time.monotonic()

    def error(self, where, exc):
        self.hb["last_error"] = {"at": _ts(), "where": where, "error": f"{type(exc).__name__}: {exc}"[:300]}
        print(f"⚠️ [{THREAD_NAME}] {where}：{exc!r}", file=sys.stderr)

    # ---- 主迴圈
    def run(self):
        self.beat("TRADING" if phase_of(_now()) == "TRADING" else "STANDBY")
        while not self.stop_event.is_set():
            try:
                self.loop_once()
            except Exception as exc:  # 任何錯誤都不能讓守護執行緒死掉
                self.error("主迴圈", exc)
                traceback.print_exc()
            self.stop_event.wait(1)

    def loop_once(self):
        self.handle_command()
        now = _now()
        phase = phase_of(now)
        if phase == "TRADING" and time.monotonic() >= self._next_tick and not cal.check_today_open(now):
            phase = "STANDBY"  # 表定休市日，或 09:30 後 MIS 連續兩次回報台積電零成交量
        if self.cloud_sync:
            self.cloud_housekeeping(phase)
        if phase == "TRADING":
            if time.monotonic() >= self._next_tick:
                self._next_tick = time.monotonic() + TICK_SEC
                self.tick(now)
            return
        # 收盤結算補課：13:30 後任何時間醒來，只要心跳與 sim_nav_history.json 都還沒有今天的結算就補做
        if (phase == "SETTLE" and self.hb.get("daily_close_settled_date") != now.strftime("%Y-%m-%d")
                and not sim.nav_has(now.strftime("%Y-%m-%d"))):
            self.settle(now)
            return
        if time.monotonic() - self._last_beat >= STANDBY_BEAT_SEC:
            self.beat("STANDBY" if cal.is_trading_day(now) else "HOLIDAY")

    # ---- 雲端交易主機：定期推送與取回
    def sync_note(self, what, r):
        self.hb["last_sync"] = {"at": _ts(), "what": what, "ok": bool(r.get("pushed") or r.get("ok")),
                                "detail": r.get("commit") or r.get("reason") or r.get("error")}
        if r.get("conflict") or (r.get("reason") and "失敗" in str(r.get("reason"))):
            self.error(what, RuntimeError(r.get("reason")))

    def cloud_housekeeping(self, phase):
        """盤中靜默：不推送心跳（只有成交與收盤結算才 commit）；Streamlit 主機每 10 分鐘只「拉取」早報與新聞（拉取不產生 commit）。"""
        mono = time.monotonic()
        if ghsync.where() == "streamlit_cloud" and mono - self._last_pull_actions >= PULL_ACTIONS_EVERY_SEC:
            self._last_pull_actions = mono
            # 管理者在別台主機（本機或 GitHub）更新的操盤規則也一併拉回，下一場投決會即生效
            r = ghsync.pull_latest(force=True, paths=ghsync.ACTIONS_OWNED + ghsync.STRATEGY_PATHS, scope="actions")
            if not r.get("ok"):
                self.sync_note("取回早報與新聞", r)

    # ---- 盤中 Tick
    def prefetch_once(self, now):
        """雲端主機（reports/data 不進 git）：每個交易日第一輪 Tick 前預抓 20 強 K 線與籌碼（新鮮檔自動沿用）。"""
        key = f"{ghsync.where()}|{now:%Y-%m-%d}"   # 依主機記錄：Streamlit 預抓過不代表 GitHub Actions 的容器也有檔案
        if ghsync.where() == "local" or self.hb.get("prefetch_done") == key:
            return
        self.hb["prefetch_done"] = key
        try:
            from tools import batch_prefetch as bp
            ids = [r["stock_id"] for r in sim._read(sim.REPORTS / "sector_watchlist.json", [])]
            bp.run(ids, workers=8, roles=["kline", "chips"])
        except Exception as exc:
            self.error("20 強預抓", exc)

    def tick(self, now):
        self.prefetch_once(now)
        started = time.perf_counter()
        try:
            with sim.ledger_lock(timeout=10):
                scan = sim.refresh_sim_intraday(with_radar=True)  # A：持倉整張/零股價、VWAP、20 強監控表、總淨值
        except sim.SimError as exc:  # 帳本被手動投決會占用：本輪略過
            self.error("Tick 帳本鎖", exc)
            self.beat("TRADING")
            return
        self.hb["scans_today"] += 1
        acct = sim.load_account()
        tick_info = {"last_tick_at": _ts(now), "last_tick_elapsed_sec": round(time.perf_counter() - started, 2),
                     "last_scan_errors": (scan.get("errors") or [])[:3], "total_equity": acct["total_equity"],
                     "holdings_count": len(acct["holdings"])}
        delay = cal.quote_delay_note(now)
        tick_info["quote_delay"] = delay
        self.beat("TRADING", **tick_info)
        if delay:
            return  # 報價還停在昨天：照常每 15 秒抓價，取得今日報價前不召開投決會（避免用舊價下單）
        cfg = load_config()
        if not cfg["auto_trade_enabled"] or not sim.is_tw_market_trading_hours(now):
            return  # 13:30:00 後只更新報價，不再下單
        triggers = find_triggers(scan, acct, self.hb["cooldowns"], now)
        slot = patrol_slot(now)
        if not triggers and slot is not None and slot != self.hb.get("patrol_slot_done"):
            triggers = [{"type": "PATROL", "stock_id": None, "text": f"{now:%H:%M} 定時巡檢盤面強勢突破機會"}]
        if triggers:
            if slot is not None:
                self.hb["patrol_slot_done"] = slot  # 本格已開過會，定時巡檢不重複
            self.meeting(triggers, cfg, now)

    # ---- 背景五人投決會
    def meeting(self, triggers, cfg, now):
        for t in triggers:
            if t["stock_id"]:
                self.hb["cooldowns"][t["stock_id"]] = _ts(now)
        self.hb["triggers_today"] += 1
        self.hb["last_trigger"] = {"at": _ts(now), "items": [{**t, "label": TRIGGER_LABEL[t["type"]]} for t in triggers]}
        self.beat("TRADING", "MEETING")
        from tools import fast_team_runner as ftr
        started = time.perf_counter()
        result = {"at": _ts(now), "trigger": "、".join(f"{TRIGGER_LABEL[t['type']]} {t['stock_id'] or ''}".strip() for t in triggers)}
        try:
            with sim.ledger_lock(timeout=60):
                pre = ghsync.pull_ledger_before_trade()  # 單一帳本：交易前先取回 GitHub 最新帳本
                if not pre.get("ok"):
                    raise RuntimeError(f"交易前無法確認 GitHub 最新帳本，本輪不下單：{pre.get('error')}")
                out = ftr.sim_trade_session("intraday_hunt", cfg["provider"])
                self.learning_sync()  # 學習資料庫：把投決會決策收錄成預測並驗算（寫回 GitHub 前完成）
                executed = out.get("executed", 0)
                result.update(executed=executed, rejected=out.get("rejected", 0), engine=out.get("provider"),
                              total_equity=out.get("total_equity"), errors=(out.get("errors") or [])[:3],
                              elapsed_sec=round(time.perf_counter() - started, 1))
                self.hb["meetings_today"] += 1
                self.hb["trades_today"] += executed
                self.hb["last_meeting"] = result
                self.beat("TRADING")
                # 盤中靜默：只有實際成交才寫回 GitHub（避免每次 commit 讓 Streamlit Cloud 重新載入）；
                # 寫回前確認 GitHub 帳本沒被別人搶先更新，被搶先時本次成交作廢、改用 GitHub 版本
                if self.cloud_sync and executed > 0:
                    post = ghsync.publish_ledger_after_trade(
                        pre.get("base_ts"), f"🤖 sim 成交 {executed} 筆 {_ts(now)}：{result['trigger']}",
                        paths=ghsync.TRADE_PUSH_PATHS)
                    self.sync_note("成交後寫回帳本", post)
                    if post.get("conflict"):
                        self.hb["trades_today"] -= executed
                        result.update(executed=0, errors=[post["reason"]] + result["errors"])
                        self.beat("TRADING")
        except Exception as exc:  # 模型或報價失敗：記錄後下一輪再試
            self.error("背景投決會", exc)
            result.update(executed=0, error=f"{type(exc).__name__}: {exc}"[:200],
                          elapsed_sec=round(time.perf_counter() - started, 1))
            self.hb["last_meeting"] = result
            self.beat("TRADING")

    def learning_sync(self):
        try:
            from tools import learning_engine
            learning_engine.sync()
        except Exception as exc:
            self.error("學習庫同步", exc)

    # ---- 收盤結算
    def settle(self, now):
        today = now.strftime("%Y-%m-%d")
        self.beat("SETTLING")
        info = {"date": today, "at": _ts(now), "late": now.time() > SETTLE_END}
        try:
            with sim.ledger_lock(timeout=60):
                scan = sim.refresh_sim_intraday(with_radar=True)  # 最後收盤價（save_account 內已寫入 sim_nav_history）
            acct = sim.load_account()
            sim.record_nav(acct)
            info.update(total_equity=acct["total_equity"], return_pct=acct["total_return_pct"],
                        errors=(scan.get("errors") or [])[:3])
        except Exception as exc:
            self.error("收盤結算報價", exc)
            info["error"] = str(exc)[:200]
            if now.time() <= SETTLE_END:  # 結算窗口內失敗：30 秒後重試
                self.beat("STANDBY")
                self.stop_event.wait(30)
                return
        try:
            from tools import portfolio_intraday as pi
            pi.run()  # 持股收盤快照（學習資料庫 CLOSE 用）
        except Exception as exc:
            self.error("持股收盤報價", exc)
        try:
            from tools import learning_engine
            learning_engine.on_price_update("sim_background_daemon", force_grade=True)
            s = learning_engine.sync()
            info["learning"] = {"closed_now": s["closed_now"], "open": s["open"], "lessons": s["lessons"]}
        except Exception as exc:
            self.error("收盤對帳", exc)
        self.hb["daily_close_settled_date"] = today
        self.hb["last_settlement"] = info
        self.beat("STANDBY")
        if self.cloud_sync:  # 收盤結算後推送帳本、淨值與學習資料庫
            self.sync_note("收盤推送", ghsync.publish(f"🤖 sim 收盤結算 {today}", ghsync.TRADER_OWNED))
            self.beat("STANDBY")

    # ---- 連線測試指令
    def handle_command(self):
        cmd = _read(COMMAND_PATH, {})
        ping_id = cmd.get("ping_id")
        if not ping_id or ping_id == (self.hb.get("last_ping") or {}).get("ping_id"):
            return
        started = time.perf_counter()
        ack = {"ping_id": ping_id, "requested_at": cmd.get("requested_at"), "acked_at": _ts()}
        try:
            q = sim.latest_quotes(["2330"]).get("2330") or {}
            ack.update(ok=bool(q.get("price")), probe=f"2330 {q.get('name') or '台積電'} ${q.get('price')}（{q.get('price_source', 'N/A')}）")
        except Exception as exc:
            ack.update(ok=False, probe=f"報價連線失敗：{type(exc).__name__}: {exc}"[:200])
        ack["latency_sec"] = round(time.perf_counter() - started, 2)
        self.hb["last_ping"] = ack
        self.beat(self.hb.get("phase") if self.hb.get("phase") in STATUS_TEXT else "STANDBY")


# ---------------------------------------------------------------- 單例啟動

class SimLedgerMirror(threading.Thread):
    """觀看端（cloud_master 下的本機電腦）：每 30 秒從 GitHub 拉回 reports/simulation（帳本、成交紀錄、雲端心跳），
    完全不寫入帳本、不下單。沒有 git 也沒有 GITHUB_TOKEN 時，GitHub API 每小時限 60 次，改為每 120 秒一次。"""

    def __init__(self):
        super().__init__(name=MIRROR_THREAD_NAME, daemon=True)
        self.stop_event = threading.Event()
        fast = not ghsync.use_api() or bool(ghsync._token())
        self.every = ghsync.MIRROR_EVERY_SEC if fast else 120

    def run(self):
        while not self.stop_event.is_set():
            try:
                ghsync.pull_latest(force=True, paths=ghsync.MIRROR_PATHS, scope="mirror")
            except Exception as exc:  # 網路錯誤：下一輪再試
                print(f"⚠️ [{MIRROR_THREAD_NAME}] {exc!r}", file=sys.stderr)
            self.stop_event.wait(self.every)


def running_thread():
    return next((t for t in threading.enumerate() if t.name == THREAD_NAME and t.is_alive()), None)


def running_mirror():
    return next((t for t in threading.enumerate() if t.name == MIRROR_THREAD_NAME and t.is_alive()), None)


def _acquire_process_lock():
    """跨行程單例：鎖住 daemon.lock（其他行程已持有時回傳 False）。"""
    global _proc_lock_file
    if _proc_lock_file is not None:
        return True
    SIM_DIR.mkdir(parents=True, exist_ok=True)
    f = open(PROC_LOCK_PATH, "a+b")
    try:
        sim.lock_file(f)
    except OSError:
        f.close()
        return False
    _proc_lock_file = f
    return True


def start_daemon():
    """依 AUTOPILOT_MODE 啟動：交易者 → 唯一一條 SimRoomDaemon；觀看端 → SimLedgerMirror（只拉帳本、不下單）。
    已在本行程或其他行程運行時不重複啟動。"""
    if not ghsync.is_trader_here():
        if running_mirror():
            return {"started": False, "reason": "viewer_mirror_running", "pid": os.getpid()}
        if not ghsync.can_sync():
            return {"started": False, "reason": "viewer_no_repo", "pid": os.getpid()}
        SimLedgerMirror().start()
        return {"started": True, "reason": "viewer_mirror", "pid": os.getpid()}
    t = running_thread()
    if t:
        return {"started": False, "reason": "already_running", "pid": os.getpid()}
    if not _acquire_process_lock():
        return {"started": False, "reason": "other_process", "pid": _read(HEARTBEAT_PATH, {}).get("pid")}
    if ghsync.ledger_sync_active() and ghsync.where() == "streamlit_cloud":
        # 雲端主機剛開機（或重新部署）：先取回 GitHub 上全部最新資料，作為之後只推送「有改動檔案」的基準
        r = ghsync.pull_latest(force=True, scope="boot")
        if not r.get("ok"):
            print(f"⚠️ 開機同步失敗：{r.get('error')}", file=sys.stderr)
    t = SimRoomDaemon()
    t.start()
    return {"started": True, "reason": "started", "pid": os.getpid()}


def stop_daemon(timeout=5):
    for t in (running_thread(), running_mirror()):
        if t:
            t.stop_event.set()
            t.join(timeout)


# ---------------------------------------------------------------- dashboard 讀取

def load_heartbeat():
    return _read(HEARTBEAT_PATH, {})


def heartbeat_view(hb=None, now=None, cloud=False):
    """把心跳整理成 dashboard 徽章用的欄位。cloud=True：本機是觀看端，心跳是從 GitHub 拉回的雲端心跳（每 5 分鐘推送，容許 15 分鐘）。"""
    hb = load_heartbeat() if hb is None else hb
    now = now or _now()
    age = None
    if hb.get("last_heartbeat"):
        age = (now - datetime.strptime(hb["last_heartbeat"], TIME_FMT)).total_seconds()
    if cloud:
        max_age = CLOUD_ALIVE_MAX_AGE
    else:
        max_age = MEETING_MAX_AGE if hb.get("status", "").startswith("🧠") or hb.get("phase") == "SETTLING" else ALIVE_MAX_AGE
    alive = age is not None and age <= max_age
    today = now.strftime("%Y-%m-%d")
    same_day = hb.get("date") == today
    phase = phase_of(now)
    holiday = cal.holiday_name(now)
    if phase == "TRADING":
        phase_text = ("☁️ 雲端盤中自動盯盤至 13:30 收盤" if cloud else "⚡ 盤中自動監控至 13:30 收盤") \
            if hb.get("auto_trade_enabled", True) else "⏸️ 盤中只更新報價（自動進出場已關閉）"
        delay = cal.quote_delay_note(now)
        if delay:
            phase_text += f"｜{delay}"
    elif holiday and holiday != "週末":
        phase_text = f"🌙 今日休市（{holiday}），下個開盤日 09:00 自動啟動"
    else:
        phase_text = "🌙 盤後待命，下個開盤日 09:00 由雲端自動啟動" if cloud else "🌙 盤後待命中，下個開盤日 09:00 自動啟動"
    return {"alive": alive, "cloud": cloud, "host": hb.get("host"), "phase": phase,
            "age_sec": None if age is None else int(age), "last_heartbeat": hb.get("last_heartbeat"),
            "pid": hb.get("pid"), "scans": hb.get("scans_today", 0) if same_day else 0,
            "triggers": hb.get("triggers_today", 0) if same_day else 0,
            "meetings": hb.get("meetings_today", 0) if same_day else 0,
            "trades": hb.get("trades_today", 0) if same_day else 0, "phase_text": phase_text,
            "status": hb.get("status"), "settled_today": hb.get("daily_close_settled_date") == today,
            "auto_trade_enabled": load_config()["auto_trade_enabled"], "raw": hb}


def request_ping():
    ping_id = uuid.uuid4().hex[:12]
    _write(COMMAND_PATH, {"ping_id": ping_id, "requested_at": _ts()})
    return ping_id


def wait_ping(ping_id, timeout=12):
    """等背景執行緒回應連線測試（守護執行緒每秒檢查一次指令檔）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ping = load_heartbeat().get("last_ping") or {}
        if ping.get("ping_id") == ping_id:
            return ping
        time.sleep(0.5)
    return None


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", encoding="utf-8")
    parser = argparse.ArgumentParser(description="🤖 SIMULATION ROOM 背景常駐盯盤引擎")
    parser.add_argument("--run", action="store_true", help="前景常駐執行（Ctrl+C 結束）")
    parser.add_argument("--seconds", type=float, help="搭配 --run：執行 N 秒後自動結束（測試用）")
    parser.add_argument("--status", action="store_true", help="印出 daemon_heartbeat.json")
    parser.add_argument("--ping", action="store_true", help="請正在運行的守護執行緒回應連線測試")
    args = parser.parse_args()
    if args.run:
        r = start_daemon()
        print(f"🤖 {r}")
        if not r["started"]:
            return
        try:
            end = time.monotonic() + args.seconds if args.seconds else None
            while (running_thread() or running_mirror()) and (end is None or time.monotonic() < end):
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        stop_daemon()
    if args.ping:
        print(wait_ping(request_ping()) or "⚠️ 12 秒內沒有回應：背景守護執行緒可能未啟動")
    if args.status or not (args.run or args.ping):
        print(json.dumps(load_heartbeat(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
