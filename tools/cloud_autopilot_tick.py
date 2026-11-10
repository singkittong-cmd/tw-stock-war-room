# -*- coding: utf-8 -*-
"""☁️ 雲端單次排程腳本（GitHub Actions 每次只跑一輪 `--once`，不跑長迴圈、不裝瀏覽器，節省免費額度）。

用法：
    C:/Users/singk/anaconda3/python.exe tools/cloud_autopilot_tick.py --once

依台灣時間（Asia/Taipei）判斷要做什麼；證交所休市日與週末完全不呼叫任何 API：
- 每次執行：設有 STREAMLIT_APP_URL 時送一次輕量 HTTP GET（timeout 10 秒）。
- 07:30–09:00 盤前：今日川大帝MAGA 早報尚未完成時，抓美股收盤 → 美股 TOP 10 與台股開盤預判 → 鎖定早報 → commit。
- 09:00–13:30 盤中：**本腳本就是主力**，每次排程都執行一輪盯盤 Tick（沿用 SimRoomDaemon）：
  更新模擬持倉整張價／盤中零股價與 20 強建議買進價監控表 → 觸及停損價／停利目標價、達建議買進價且五檔委買比 ≥ 0.75、
  或進入新的 20 分鐘巡檢時段時召開五人投決會。交易前先取回 GitHub 最新帳本；
  **只有實際成交才 commit**（帳本、成交紀錄、學習資料庫），沒成交不 commit，避免 Streamlit Cloud 盤中重新載入。
- 13:30 後：當天還沒有人結算時執行模擬室收盤結算；新聞 TOP 10、明日 20 強建議買進價、學習對帳 → commit（每天一次）。
Streamlit Cloud 醒著時，伺服器內的 SimRoomDaemon 也會每 15 秒盯盤；兩邊都遵守同一套帳本防衝突規則。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
import os
import time
import traceback
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import tw_clock  # noqa: E402
tw_clock.force_taipei_tz()  # 必須在其他模組使用 datetime.now() 之前

import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", encoding="utf-8")
IN_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
os.environ.setdefault("AUTOPILOT_HOST", "github_actions" if IN_ACTIONS else "cloud_tick_cli")

from tools import github_memory_sync as ghsync  # noqa: E402
from tools import llm_router  # noqa: E402
from tools import sim_background_daemon as daemon  # noqa: E402
from tools import sim_portfolio_engine as sim  # noqa: E402
from tools import tw_market_calendar as cal  # noqa: E402

llm_router.ensure_env_defaults()

STATE_PATH = ROOT / "reports" / "cloud_autopilot_state.json"
PRE_START, OPEN_T = dtime(7, 30), dtime(9, 0)
PHASE_TEXT = {"PREMARKET": "🌅 盤前（美股收盤匯報與台股開盤預判）", "TRADING": "⚡ 盤中（檢查報價與進出場）",
              "CLOSE": "🧾 收盤後（結算、新聞 TOP 10、明日建議買進價）", "IDLE": "🌙 非交易時段（不呼叫任何 API）",
              "HOLIDAY": "🌙 今日休市（不呼叫任何 API）"}


def phase_now(now):
    if not cal.is_trading_day(now):
        return "HOLIDAY"
    if PRE_START <= now.time() < OPEN_T:
        return "PREMARKET"
    p = daemon.phase_of(now)
    return {"TRADING": "TRADING", "SETTLE": "CLOSE"}.get(p, "IDLE")


def _read_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def ping_streamlit():
    """對 Streamlit Cloud 網址送一次輕量 HTTP GET（不帶任何個人資訊）。只能碰觸網站，不保證 app 會從休眠中開機。"""
    url = (os.getenv("STREAMLIT_APP_URL") or "").strip()
    if not url:
        return None
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 tw-stock-war-room-autopilot"})
        return f"HTTP {r.status_code}"
    except requests.RequestException as exc:
        return f"失敗：{type(exc).__name__}"


class Autopilot:
    def __init__(self, provider, push):
        self.provider = provider
        self.push = push
        self.state = _read_state()
        self.log = []
        self.errors = []
        self.trader_role = ghsync.is_trader_here()  # cloud_master 的 GitHub Actions，或 local_master 的本機
        # 本機 dashboard 的 SimRoomDaemon 持有行程鎖時，本腳本不下單（同一台電腦不重複交易）
        self.can_trade = self.trader_role and daemon._acquire_process_lock()
        self.d = daemon.SimRoomDaemon()
        self.push_paths = None                       # None = 本次不 commit

    def say(self, text):
        line = f"[{tw_clock.now_tw():%H:%M:%S}] {text}"
        print(line, flush=True)
        self.log.append(line)

    def step(self, name, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # 單一步驟失敗不中斷整輪
            self.errors.append(f"{name}：{type(exc).__name__}: {exc}"[:300])
            self.say(f"❌ {name}失敗：{exc!r}")
            traceback.print_exc()
            return None

    def save_state(self, **extra):
        self.state.update(extra)
        sim._write(STATE_PATH, self.state)

    def may_trade(self, what):
        if not self.trader_role:
            self.say(f"👀 AUTOPILOT_MODE={ghsync.autopilot_mode()}：這台主機不是交易執行者，不{what}")
            return False
        if not self.can_trade:
            self.say(f"⚠️ 本機已有 SimRoomDaemon 背景執行緒在運行（dashboard 開著），本次不{what}，避免重複交易")
            return False
        return True

    # ---- 盤前：川大帝MAGA 早報
    def premarket(self, now):
        from tools import us_semi_tracker as us_mod
        today = now.strftime("%Y-%m-%d")
        status = us_mod.check_status(now)
        if status["status"] == "LOCKED_COMPLETED":
            if self.state.get("morning_done_date") != today:
                self.say(f"🦅 今日早報已完成（{status.get('completed_at')}），川大帝MAGA 休眠")
                self.save_state(morning_done_date=today)
            return
        from tools import fast_team_runner as ftr
        from tools import memory_reflection as mem
        self.say("🦅 川大帝MAGA：抓取美股收盤（費半、TSM ADR 折溢價、NVDA/MU/VRT、美股新聞）")
        self.step("美股收盤抓取", us_mod.run, "morning_close")
        self.step("記憶庫覆盤", mem.run)
        r = self.step("美股 TOP 10 與開盤預判", ftr._us_brief, self.provider, us_mod.check_status())
        if r:
            us_mod.run("complete")
            self.step("記憶庫收錄", mem.snapshot)
            self.say(f"✅ 早報完成：美股 TOP {r['us_top10']}（{r['provider']}），已鎖定今日早報")
            self.save_state(morning_done_date=today)
            self.push_paths = ghsync.ACTIONS_OWNED  # 早報完成才 commit

    # ---- 盤中：主力檢查一次報價與進出場
    def trading(self, now):
        if not cal.check_today_open(now):
            self.say("🌙 09:10 實測台積電與加權指數零成交量：判定今日休市，直接休眠")
            return
        if not self.may_trade("下單"):
            return
        before = self.d.hb.get("trades_today", 0)
        self.step("盤中 Tick（含 20 強預抓、交易前取回 GitHub 帳本）", self.d.tick, now)
        hb = self.d.hb
        m = hb.get("last_meeting") or {}
        met_now = (m.get("at") or "")[:16] == f"{now:%Y-%m-%d %H:%M}"
        self.say(f"⚡ Tick 完成：總淨值 {hb.get('total_equity')}｜持倉 {hb.get('holdings_count')} 檔｜耗時 {hb.get('last_tick_elapsed_sec')} 秒"
                 + (f"｜投決會（{m.get('trigger')}）成交 {m.get('executed', 0)} 筆、否決 {m.get('rejected', 0)} 筆" if met_now else "｜未觸發投決會"))
        if hb.get("trades_today", 0) > before:
            self.say(f"☁️ 成交後已寫回 GitHub：{(hb.get('last_sync') or {}).get('detail')}")
        else:
            self.say("🤫 沒有成交：盤中靜默，不 commit")

    # ---- 收盤後
    def close(self, now):
        today = now.strftime("%Y-%m-%d")
        settled_here = False
        if self.d.hb.get("daily_close_settled_date") == today:
            self.say(f"🧾 今日已由 {self.d.hb.get('host')} 完成收盤結算")
        elif self.may_trade("結算"):
            self.say("🧾 模擬室收盤結算：更新收盤價、寫入 sim_nav_history、五人準確率對帳")
            self.d.cloud_sync = False  # 結算結果由本腳本最後統一 commit 一次
            self.step("收盤結算", self.d.settle, now)
            settled_here = True
            s = self.d.hb.get("last_settlement") or {}
            self.say(f"✅ 結算完成：總淨值 {s.get('total_equity')}（{s.get('return_pct')}%）")
        if self.state.get("close_reports_date") != today:
            from tools import build_watchlist_standby as standby_mod
            from tools import fast_team_runner as ftr
            self.say("📰 今日財經新聞 TOP 10、收盤重點、明日 20 強建議買進價定錨")
            r = self.step("新聞 TOP 10 與收盤重點", ftr.daily_home_update, self.provider, True)
            if r:
                self.errors += [f"每日更新：{e}"[:300] for e in r.get("errors") or []]
            self.step("20 強建議買進價重新定錨", standby_mod.build)
            self.save_state(close_reports_date=today)
            self.push_paths = ghsync.ACTIONS_OWNED
        if settled_here:
            self.push_paths = ghsync.SYNC_PATHS   # 結算過：帳本、淨值、學習資料庫一起 commit

    def idle(self, now):
        pass

    def holiday(self, now):
        self.say(f"🌙 今日休市（{cal.holiday_name(now)}），下一個開盤日 {cal.next_trading_day(now.date(), include_today=False)}")

    def push_now(self, label):
        if not self.push or not self.push_paths:
            return
        r = ghsync.publish(f"🤖 autopilot {label} {tw_clock.now_tw():%Y-%m-%d %H:%M}", self.push_paths)
        self.say(f"☁️ GitHub 同步：{'已 push ' + r.get('commit', '') if r.get('pushed') else r.get('reason')}")

    def run(self):
        now = tw_clock.now_tw()
        phase = phase_now(now)
        self.say(f"☁️ 雲端單次排程（{os.environ['AUTOPILOT_HOST']}・{ghsync.autopilot_mode()}・{self.provider}）｜目前 {PHASE_TEXT[phase]}")
        if phase not in ("HOLIDAY", "IDLE"):
            ping = ping_streamlit()
            if ping:
                self.say(f"💓 Streamlit 輕量 Ping：{ping}")
        started = time.monotonic()
        getattr(self, phase.lower())(now)
        summary = {"finished_at": f"{tw_clock.now_tw():%Y-%m-%d %H:%M:%S}", "phase": phase,
                   "elapsed_sec": round(time.monotonic() - started, 1), "host": os.environ["AUTOPILOT_HOST"],
                   "trades_today": self.d.hb.get("trades_today"), "errors": self.errors[-10:], "log_tail": self.log[-15:]}
        self.save_state(last_run=summary)
        self.push_now(PHASE_TEXT[phase].split("（")[0])
        return summary


def main():
    parser = argparse.ArgumentParser(description="☁️ 雲端單次排程（GitHub Actions 用）")
    parser.add_argument("--once", action="store_true", help="依目前台灣時間執行一輪後結束（預設行為）")
    parser.add_argument("--provider", default=os.getenv("AUTOPILOT_PROVIDER") or daemon.load_config()["provider"])
    parser.add_argument("--push", dest="push", action="store_true", default=IN_ACTIONS,
                        help="commit + push 資料檔回 GitHub（GitHub Actions 中預設開啟）")
    parser.add_argument("--no-push", dest="push", action="store_false")
    args = parser.parse_args()
    summary = Autopilot(args.provider, args.push).run()
    print(json.dumps({k: v for k, v in summary.items() if k != "log_tail"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
