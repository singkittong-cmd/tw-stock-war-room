# -*- coding: utf-8 -*-
"""台股戰情室（Streamlit）：🏠 清爽日常主畫面 ⇄ 🎖️ WAR ROOM 雙模式。

啟動：C:/Users/singk/anaconda3/python.exe -m streamlit run dashboard.py

以 st.session_state.in_war_room 切換兩個互不干擾的視圖：
- 🏠 日常主畫面：即時個股快查、今日財經新聞 TOP 10 vs 美股收盤 TOP 10、台股收盤大盤與資金總結。
  不顯示持股、不顯示 20 強清單。
- 🎖️ WAR ROOM：戰術會議桌（巴董事長主席位＋四位成員）、點選成員展開個人報告與昨日覆盤、
  持股盤中實戰表（密碼金庫 .env PRIVACY_PIN）、20 強建議買進價監控表、盤中戰術提問指揮台。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import json
import os
import re
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from tools import tw_clock  # noqa: E402
tw_clock.force_taipei_tz()  # Streamlit Cloud 主機是 UTC：整個行程改用台灣時間
from tools import batch_prefetch as prefetch_mod  # noqa: E402
from tools import build_watchlist_standby as standby_mod  # noqa: E402
from tools import fast_team_runner as ftr  # noqa: E402
from tools import finmind_cli as fm  # noqa: E402
from tools import github_memory_sync as ghsync  # noqa: E402
from tools import kline_patterns  # noqa: E402
from tools import learning_engine as le  # noqa: E402
from tools import llm_router  # noqa: E402
from tools import privacy_guard  # noqa: E402
from tools import market_news_fetcher as news_mod  # noqa: E402
from tools import memory_reflection as mem  # noqa: E402
from tools import portfolio_intraday as pi  # noqa: E402
from tools import quick_stock_lookup as qsl  # noqa: E402
from tools import sim_background_daemon as sim_daemon  # noqa: E402
from tools import sim_portfolio_engine as sim  # noqa: E402
from tools import ui_theme as ui  # noqa: E402
import importlib; importlib.reload(ui)  # noqa: E402,E702  每次重新整理強制載入最新版 ui_theme，避免舊模組快取
from tools import us_semi_tracker as us_mod  # noqa: E402
from tools import watchlist_intraday_radar as radar_mod  # noqa: E402
from tools.claude_runner import run_prefetch  # noqa: E402
from tools.privacy_mask import mask_sensitive_text, set_private_terms  # noqa: E402

load_dotenv(ROOT / ".env", encoding="utf-8")
llm_router.ensure_env_defaults()

# ⚡ AI 運算引擎：日常預設免費 Gemini；Claude 只在使用者主動選擇時才用（單次、無工具、不派子代理）
ENGINES = {
    "🌱 Gemini 3.5 Flash 免費引擎 (0 Claude Token・極速 3 秒)": "gemini",
    "⚡ Groq Llama-3.3 免費引擎 (0 Claude Token)": "groq",
    "👑 Claude 濃縮單核模式 (省 90% Token・僅巴董事長深潛用)": "claude_lite",
}
ENGINE_KEYS = {"gemini": ("GEMINI_API_KEY", "🔑 輸入 Google AI Studio 免費 Gemini API Key（輸入後自動存入 .env 永久生效）"),
               "groq": ("GROQ_API_KEY", "🔑 輸入 Groq 免費 API Key（輸入後自動存入 .env 永久生效）")}

REPORTS = ROOT / "reports"
STOCKS_DIR = REPORTS / "stocks"
DATA_DIR = REPORTS / "data"
US_DIR = REPORTS / "us_market"
NEWS_DIR = REPORTS / "news"
DIAGNOSIS_PATH = REPORTS / "portfolio_diagnosis.md"
DECISIONS_PATH = REPORTS / "portfolio_intraday_decisions.json"
SECTOR_PATH = REPORTS / "sector_watchlist.json"
STANDBY_PATH = REPORTS / "watchlist_standby.json"
CLOSE_PATH = REPORTS / "market_close_summary.json"
UP_COLOR, DOWN_COLOR = "#EF4444", "#10B981"
WATCHLIST = [s.strip() for s in os.getenv("WATCHLIST", "2330").split(",") if s.strip()]
PORTFOLIO_COLUMNS = ["stock_id", "name", "cost_price", "shares", "note"]
VAULT_PIN_KEY = "vault_pin"
QA_SCOPES = {"🌐 全市場": "market", "💼 手上持股": "holdings", "🎯 指定個股": "stock"}
SENTIMENT_KIND = {"偏多": "bull", "中立偏多": "bull", "分歧震盪": "amber", "偏空警戒": "bear"}
NEWS_NOISE = re.compile(r"盤中速報|漲停|跌停|今日熱門|直播|懶人包|一分鐘|抽獎|開箱")
BULL_NEWS = re.compile(r"創新高|成長|調升|擴產|大單|買超|看好|突破|上修|暢旺|漲價|獲利|增|超預期|強勁|回溫")
BEAR_NEWS = re.compile(r"下修|衰退|賣超|關稅|制裁|裁員|虧損|重挫|跌|疑慮|砍單|延後|示警|降評|減少|利空|崩")

st.set_page_config(page_title="台股戰情室｜WAR ROOM", page_icon="🎖️", layout="wide", initial_sidebar_state="collapsed")


@st.cache_resource(show_spinner=False)
def init_sim_background_daemon():
    """Streamlit 伺服器生命週期內只執行一次：任何頁面第一次被打開時，就在伺服器端啟動唯一一條 SimRoomDaemon
    背景執行緒（與瀏覽器分頁是否 Active、使用者停在哪個頁面無關）。"""
    return sim_daemon.start_daemon()


# 單一帳本（AUTOPILOT_MODE）：
# - 交易者（Streamlit Cloud 上的 cloud_master，或本機的 local_master）：啟動唯一一條 SimRoomDaemon 盯盤下單。
# - 觀看端（cloud_master 下的本機電腦）：只啟動 SimLedgerMirror，每 30 秒從 GitHub 拉回雲端帳本，禁止下單。
CLOUD_MODE = ghsync.cloud_mode()
IS_TRADER = ghsync.is_trader_here()
VIEWER = not IS_TRADER
init_sim_background_daemon()
if not (sim_daemon.running_thread() or sim_daemon.running_mirror()):  # 執行緒意外結束或模組重載時補啟動
    sim_daemon.start_daemon()
DAEMON_VIEW = sim_daemon.heartbeat_view(cloud=VIEWER)
DAEMON_FEEDS_RADAR = IS_TRADER and DAEMON_VIEW["alive"] and sim_daemon.phase_of(datetime.now()) == "TRADING"

# 💓 手動喚醒入口：在瀏覽器打開 ?keepalive=1 → 確保背景盯盤執行緒在跑、請它立即回應一次報價心跳（非阻塞），回傳狀態字串後結束
if st.query_params.get("keepalive"):
    _trader_running = IS_TRADER and sim_daemon.running_thread() is not None
    if _trader_running:
        sim_daemon.request_ping()
    st.write(f"KEEPALIVE_OK|trader={int(_trader_running)}|daemon={'running' if _trader_running else 'viewer' if VIEWER else 'stopped'}"
             f"|phase={sim_daemon.phase_of(datetime.now()).lower()}")
    st.stop()
st.markdown(ui.inject_css() + ui.avatar_css(), unsafe_allow_html=True)
st.session_state.setdefault("privacy_unlocked", False)
st.session_state.setdefault("in_war_room", False)
# 三種視圖：home 日常主畫面／war_room／sim_room（in_war_room 保留為舊版相容旗標）
st.session_state.setdefault("view", "war_room" if st.session_state.get("in_war_room") else "home")


def goto_view(view):
    st.session_state["view"] = view
    st.session_state["in_war_room"] = view == "war_room"
st.session_state.setdefault("member", None)


# ---------------------------------------------------------------- 共用工具

def read_text(path):
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def read_json(path, default=None):
    text = read_text(path)
    if not text:
        return {} if default is None else default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {} if default is None else default


def load_summary(stock_id):
    return read_json(STOCKS_DIR / f"{stock_id}_summary.json")


def load_prefetch(stock_id, role):
    return (read_json(DATA_DIR / f"{stock_id}_{role}.json").get("data")) or {}


def load_decisions():
    data = read_json(DECISIONS_PATH)
    return data if isinstance(data, dict) else {}


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cell(fn):
    return lambda v: "—" if v is None or (isinstance(v, float) and pd.isna(v)) else fn(v)


def money_table(df, money=(), signed_money=(), signed_pct=(), price=()):
    """表格格式化：金額 $千分位、股價 $兩位小數、損益與漲跌加 +／- 並套用台股紅正（#F87171）綠負（#34D399）。"""
    cols = set(df.columns)
    fmt = {c: _cell(ui.fmt_money) for c in money if c in cols}
    fmt.update({c: _cell(ui.fmt_price) for c in price if c in cols})
    fmt.update({c: _cell(lambda v: ui.fmt_money(v, signed=True)) for c in signed_money if c in cols})
    fmt.update({c: _cell(ui.fmt_pct) for c in signed_pct if c in cols})
    sty = df.style.format(fmt, na_rep="—")
    colored = [c for c in (*signed_money, *signed_pct) if c in cols]
    if colored:
        sty = sty.map(lambda v: f"color: {ui.sign_color(v)}; font-weight: 700" if to_float(v) is not None else "", subset=colored)
    return sty


def market_open_now(now=None):
    now = now or datetime.now()
    return now.weekday() < 5 and dtime(9, 0) <= now.time() <= dtime(13, 35)


def is_unlocked():
    return bool(st.session_state.get("privacy_unlocked"))


def mask(text):
    """隱私打碼＋把舊報告中的「角色 N」編號換成成員正式名稱。"""
    return ui.name_roles(mask_sensitive_text(text, is_unlocked()))


def html(markup):
    st.markdown(markup, unsafe_allow_html=True)


TS_FMT = "%Y-%m-%d %H:%M:%S"


def fmt_ts(value, fallback_path=None):
    """把 'YYYY-MM-DD HH:MM[:SS]'、'HH:MM:SS'（補檔案日期）或檔案修改時間統一成 YYYY-MM-DD HH:MM:SS。"""
    s = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", s):
        return s[:19]
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", s):
        return s + ":00"
    if fallback_path is not None and Path(fallback_path).exists():
        mtime = datetime.fromtimestamp(Path(fallback_path).stat().st_mtime)
        if re.match(r"^\d{2}:\d{2}(:\d{2})?$", s):
            return f"{mtime:%Y-%m-%d} {s}" + ("" if s.count(":") == 2 else ":00")
        return mtime.strftime(TS_FMT)
    return None


def stamp(value=None, path=None, note=""):
    """在區塊頂部顯示「🕒 資料最後更新」；超過 24 小時未更新標成琥珀色。"""
    ts = fmt_ts(value, path)
    stale = False
    if ts:
        try:
            stale = (datetime.now() - datetime.strptime(ts, TS_FMT)).total_seconds() > 86400
        except ValueError:
            pass
    html(ui.updated_tag(ts, note, stale))


def file_ts(path):
    return fmt_ts(None, path)


TASK_TEXT = {"war_room_sync": "戰情室同步完成！已成功為 {decided} 檔標的完成決策定錨",
             "daily_home_update": "今日早晚報更新完成！新聞 TOP {news} 則、美股 TOP {us} 則已定錨",
             "deep_dive": "巴董事長深潛完成！{stock_id} 裁決：{verdict}（風報比 {rr}）",
             "ask_war_room": "戰術提問完成！四位成員已回覆",
             "update_single_agent": "【{member_name}】最新報告已單獨更新完成！",
             "sim_trade_session": "投決會完成！巴董事長拍板成交 {executed} 筆、風控否決 {rejected} 筆，模擬總淨值 NT$ {total_equity:,}"}


def remember_run(label, result=None, error=None):
    """把任務結果整理成人看得懂的橫幅（不顯示 raw JSON），存到 session_state 供下一輪顯示。"""
    result = result or {}
    errors = list(result.get("errors") or [])
    if error is not None:
        errors.append(str(error))
    task = result.get("task")
    fields = {"decided": result.get("decided", 0), "news": (result.get("news") or {}).get("top_news", 0),
              "us": (result.get("us") or {}).get("us_top10", 0), "stock_id": result.get("stock_id", ""),
              "verdict": result.get("verdict", ""), "rr": result.get("rr", "N/A"), "member_name": result.get("member_name", ""),
              "executed": result.get("executed", 0), "rejected": result.get("rejected", 0),
              "total_equity": int(result.get("total_equity") or 0)}
    engine = result.get("provider") or (result.get("news") or {}).get("provider") or ""
    ok = error is None and not errors and not (task == "war_room_sync" and not fields["decided"])
    st.session_state["last_run"] = {
        "label": label, "ok": ok, "at": datetime.now().strftime(TS_FMT), "engine": engine,
        "elapsed": to_float(result.get("elapsed_sec")),
        "message": TASK_TEXT.get(task, f"{label} 完成").format(**fields), "errors": errors,
    }
    st.session_state["task_banner"] = st.session_state["last_run"]


def task_banner():
    """顯示最近一次任務結果：成功 → 綠色橫幅；有錯誤 → 紅色橫幅並條列細節。顯示一次後清除。"""
    run = st.session_state.pop("task_banner", None)
    if not run:
        return
    elapsed = f"（耗時 {run['elapsed']:.1f} 秒" + (f"・{run['engine']}" if run["engine"] else "") + "）" if run["elapsed"] else ""
    if run["ok"]:
        st.success(f"✅ {run['message']}{elapsed}")
    else:
        st.error(f"⚠️ {run['label']} 未完全成功{elapsed}，錯誤細節：\n" + "\n".join(f"- {mask(e)[:300]}" for e in run["errors"]
                                                                              or ["沒有產生任何決策（模型未回覆持股資料）"]))


def current_engine():
    return st.session_state.get("engine", "gemini")


def _sync_engine(widget_key):
    st.session_state["engine"] = ENGINES[st.session_state[widget_key]]


def engine_selector(view):
    """⚡ AI 運算引擎選擇器（兩個視圖各一個 widget，選擇存在 session_state.engine 共用）。"""
    labels = list(ENGINES)
    current = next(label for label, p in ENGINES.items() if p == current_engine())
    st.selectbox("⚡ AI 運算引擎", labels, index=labels.index(current), key=f"engine_{view}",
                 on_change=_sync_engine, args=(f"engine_{view}",), label_visibility="collapsed")


def engine_key_box(view):
    """選了免費引擎但 .env 沒金鑰時，在頁面上方顯示金鑰輸入框（不回顯金鑰）。"""
    provider = current_engine()
    if provider not in ENGINE_KEYS or llm_router.has_key(provider):
        if provider == "claude_lite":
            st.caption("👑 Claude 濃縮單核模式：單次 `claude -p --tools \"\"`，不派子代理、不讀原始 JSON，只送 Python 濃縮數據。")
        return
    env_name, label = ENGINE_KEYS[provider]
    with st.container(border=True):
        c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
        key_in = c1.text_input(label, type="password", key=f"key_input_{view}",
                               placeholder="貼上金鑰（只寫入本機 .env，畫面不顯示）")
        if c2.button("💾 儲存金鑰", width="stretch", key=f"btn_save_key_{view}"):
            try:
                llm_router.save_env_key(env_name, key_in)
                st.session_state["toast"] = f"💾 {env_name} 已寫入 .env，立即生效"
                st.session_state.pop(f"key_input_{view}", None)
                st.rerun()
            except ValueError as exc:
                st.error(f"❌ {exc}")
        st.caption("尚未設定金鑰時，AI 任務會自動改用另一個有金鑰的免費引擎；兩者皆無時只更新 Python 數據。"
                   " 免費金鑰：Google AI Studio https://aistudio.google.com/apikey｜Groq https://console.groq.com/keys")


def run_ai(label, fn, *args, **kwargs):
    """執行 fast_team_runner 任務，成功或失敗都記錄到 last_run。"""
    try:
        result = fn(*args, **kwargs)
        remember_run(label, result)
        return result
    except (llm_router.LLMError, LookupError) as exc:
        remember_run(label, error=exc)
        return None


def editor_to_rows(df):
    """把 data_editor 內容轉成 portfolio.json 格式，略過不完整的列。"""
    rows, skipped = [], 0
    for _, r in df.iterrows():
        stock_id = str(r.get("stock_id") or "").strip()
        cost = to_float(r.get("cost_price"))
        shares = to_float(r.get("shares"))
        if not stock_id or stock_id.lower() == "nan" or not cost or cost <= 0 or not shares or shares <= 0:
            if stock_id or cost or shares:
                skipped += 1
            continue
        name, note = r.get("name"), r.get("note")
        rows.append({"stock_id": stock_id,
                     "name": "" if name is None or pd.isna(name) else str(name).strip(),
                     "cost_price": round(cost, 4), "shares": int(shares),
                     "note": "" if note is None or pd.isna(note) else str(note).strip()})
    return rows, skipped


def refresh_live_status(rows):
    """抓即時報價並把 MIS 查到的名稱補回空白名稱欄。"""
    status = pi.run()
    names = {p["stock_id"]: p.get("name") for p in status.get("positions", [])}
    if any(not r["name"] and names.get(r["stock_id"]) for r in rows):
        for r in rows:
            if not r["name"]:
                r["name"] = names.get(r["stock_id"]) or ""
        pi.save_portfolio(rows)
    return status


@st.cache_data(ttl=600, show_spinner=False)
def load_price(stock_id, days):
    df = fm.compute_indicators(fm.kline_price(stock_id)[0])
    cutoff = pd.Timestamp(datetime.now()) - pd.Timedelta(days=days)
    return df[df["date"] >= cutoff].reset_index(drop=True)


@st.cache_data(ttl=600, show_spinner=False)
def load_institutional(stock_id, days):
    return fm.institutional_net(stock_id, days)


def kline_metrics(sid):
    """由預抓 K 線檔整理籌碼王判讀所需的量價指標與 K線技術人判讀（舊檔缺 zhu_analysis 時即時補算）。"""
    k = load_prefetch(sid, "kline")
    if not k:
        return {}
    rows = k.get("daily_30d") or []
    chg5 = ((k["close"] / rows[-6]["close"] - 1) * 100) if len(rows) > 5 and rows[-6].get("close") else None
    zhu = k.get("zhu_analysis") or kline_patterns.analyze_daily_rows(rows)
    return {"close": k.get("close"), "volume_lots": k.get("volume_lots"), "vol_ratio_20": k.get("vol_ratio_20"),
            "bias20_pct": k.get("bias20_pct"), "change_5d_pct": chg5, "date": k.get("date"), "zhu": zhu,
            "levels": k.get("levels") or {}}


# ---------------------------------------------------------------- 資料與狀態

portfolio = pi.load_portfolio()
portfolio_ids = [p["stock_id"] for p in portfolio]
portfolio_names = {p["stock_id"]: p["name"] for p in portfolio}
sector = read_json(SECTOR_PATH, default=[])
sector_ids = [r["stock_id"] for r in sector]
sector_names = {r["stock_id"]: r["name"] for r in sector}
extra_focus = st.session_state.setdefault("extra_focus", {})  # 快查或手動輸入帶進來的任意個股 {代號: 名稱}
all_names = {**extra_focus, **sector_names, **{k: v for k, v in portfolio_names.items() if v}}
us_status = us_mod.check_status()
us_locked = us_status["status"] == "LOCKED_COMPLETED"
# 只存在於持股（不在 20 大龍頭與 WATCHLIST）的代號與名稱，金庫鎖定時在成員文字中一律打碼
_public_ids = set(sector_ids) | set(WATCHLIST)
_private_ids = [sid for sid in portfolio_ids if sid not in _public_ids]
set_private_terms(_private_ids + [portfolio_names.get(sid, "").rstrip("*") for sid in _private_ids])

# 標的選單：未解鎖只列觀察池（20 大龍頭＋WATCHLIST＋自行查詢），解鎖後才併入持股，避免透露持倉
focus_options = list(dict.fromkeys((portfolio_ids if is_unlocked() else []) + sector_ids + WATCHLIST + list(extra_focus)))
_focus_pending = st.session_state.pop("focus_pending", None)
if _focus_pending in focus_options:
    st.session_state["focus_stock"] = _focus_pending
if st.session_state.get("focus_stock") not in focus_options:
    st.session_state["focus_stock"] = focus_options[0] if focus_options else "2330"
focus = st.session_state["focus_stock"]


def label_of(sid):
    tag = "💼 " if is_unlocked() and sid in portfolio_names else ""
    return f"{tag}{sid} {all_names.get(sid, '')}".strip()


def add_focus(sid, name):
    st.session_state["extra_focus"][sid] = name
    st.session_state["focus_pending"] = sid


toast = st.session_state.pop("toast", None)
if toast:
    st.toast(toast)


# ---------------------------------------------------------------- 一鍵任務（全部走 tools/fast_team_runner.py，預設免費 Gemini）

def daily_refresh():
    """🔄 日常一鍵更新：Python 預抓 20 強與建議買進價監控表 → fast_team_runner.daily_home_update（新聞 TOP 10＋美股 TOP 10＋收盤總結）。"""
    provider = current_engine()
    with st.status(f"🔄 更新今日早晚報與收盤數據（{provider}）…", expanded=True) as box:
        st.write("📦 預抓 20 強與自選股 K 線、籌碼（籌碼 4 小時快取）…")
        prefetch_mod.run(list(dict.fromkeys(sector_ids + WATCHLIST)), workers=8, roles=["kline", "chips"])
        st.write("🎯 刷新 20 強建議買進價監控表…")
        try:
            radar_mod.run()
        except pi.MISError as exc:
            st.write(f"⚠️ 建議買進價監控表失敗：{exc}")
        st.write("📰🦅 抓新聞與美股 → 收盤總結與覆盤 → 新聞達人＋川大帝MAGA 單次濃縮產出 TOP 10…")
        run_ai("一鍵更新今日早晚報", ftr.daily_home_update, provider)
        box.update(label="🔄 更新流程結束", state="complete")
    st.cache_data.clear()
    st.rerun()


def war_room_update():
    """🚨 全員同步：Python 預抓 → fast_team_runner.war_room_sync（報價、建議買進價監控表、覆盤 → 單次多人設請求全持股拍板）。"""
    provider = current_engine()
    with st.status(f"🚨 戰情室全員同步更新中（{provider}）…", expanded=True) as box:
        st.write("📦 批次預抓持股與自選股（籌碼 4 小時快取、K 線走 Yahoo）…")
        prefetch_mod.run(list(dict.fromkeys(portfolio_ids + WATCHLIST)), workers=8)
        st.write("💹 持股報價、20 強建議買進價監控表、昨日落差覆盤 → 籌碼王＋K線技術人提案、巴董事長 Challenge 拍板（單次請求）…")
        run_ai("戰情室全員同步更新", ftr.war_room_sync, provider)
        box.update(label="🚨 全員同步流程結束", state="complete")
    st.cache_data.clear()
    st.rerun()


def deep_research(sid):
    provider = current_engine()
    with st.spinner(f"巴董事長深入研究 {sid}（{provider}・單次濃縮會診）…"):
        run_prefetch([sid])
        run_ai(f"{sid} 巴董事長深潛", ftr.deep_dive, sid, provider)
    st.cache_data.clear()


def ai_tasks_popover():
    """⋯ 0 Token 工具與最近一次 AI 任務輸出。"""
    with st.popover("⋯", width="stretch"):
        st.markdown("**更多任務**")
        if st.button("🌙 盤後 20 強重新定錨（0 Token）", width="stretch"):
            with st.spinner("計算 20 強備用進場價…"):
                standby = standby_mod.build(fetch=True)
                radar_mod.run()
            st.session_state["toast"] = f"🌙 已重新定錨 {len(standby['stocks'])} 檔龍頭"
            st.rerun()
        if us_locked:
            st.caption("💤 川大帝MAGA 今日早報已定錨鎖定，一鍵更新不重抓美股、只補 TOP 10。")
        st.caption(f"目前 AI 引擎：{current_engine()}｜免費引擎金鑰：Gemini {'✅' if llm_router.has_key('gemini') else '❌'}"
                   f"／Groq {'✅' if llm_router.has_key('groq') else '❌'}")
        last_run = st.session_state.get("last_run")
        if last_run:
            st.caption(f"最近任務：{last_run['label']}（{last_run['at']}・{last_run['engine'] or 'N/A'}）")
            if last_run["ok"]:
                st.success(f"✅ {last_run['message']}")
            else:
                st.error("⚠️ 錯誤細節：\n" + "\n".join(f"- {mask(e)[:300]}" for e in last_run["errors"] or ["沒有產生任何結果"]))


# ================================================================ 🏠 視圖一：日常主畫面

def news_tag(text):
    bull, bear = len(BULL_NEWS.findall(text)), len(BEAR_NEWS.findall(text))
    return "利多" if bull > bear else "利空" if bear > bull else "中性"


def news_top10():
    """新聞達人 AI 精選（daily_news_summary.top_news_10）優先；否則由原始新聞過濾噪音後取前 10。"""
    summary = read_json(NEWS_DIR / "daily_news_summary.json")
    picked = summary.get("top_news_10") or []
    if picked:
        return [{"title": mask(n.get("title")), "point": mask(n.get("point")), "tag": n.get("tag", "中性"),
                 "url": n.get("url") or n.get("link"), "source": n.get("source"), "pub_date": n.get("pub_date")}
                for n in picked[:10]], "新聞達人 AI 精選"
    raw = read_json(NEWS_DIR / "daily_market_news_raw.json")
    items, seen = [], set()
    for n in raw.get("news") or []:
        title = n.get("title") or ""
        if NEWS_NOISE.search(title) or title[:14] in seen:
            continue
        seen.add(title[:14])
        point = (n.get("summary") or "").strip() or f"{n.get('source', '')}｜{n.get('category', '')}"
        items.append({"title": mask(title), "point": mask(point[:70] + ("…" if len(point) > 70 else "")),
                      "tag": news_tag(title + point), "url": n.get("url") or n.get("link"), "source": n.get("source"),
                      "pub_date": n.get("pub_date") or n.get("published_at")})
        if len(items) == 10:
            break
    return items, "關鍵字自動標籤（尚未 AI 精選）"


US_TOP = [
    ("^SOX", "費城半導體 SOX", "台股半導體與加權開盤方向"),
    ("TSM", "台積電 ADR", "2330 開盤跳空幅度"),
    ("NVDA", "輝達 NVDA", "AI 伺服器供應鏈（2317、2382、3231）"),
    ("MU", "美光 MU", "記憶體（2408、2344、8299）"),
    ("VRT", "Vertiv VRT", "AI 散熱（3017、3324）"),
    ("AVGO", "博通 AVGO", "ASIC 設計服務（3661、3443）"),
    ("ASML", "艾司摩爾 ASML", "CoWoS 與設備（3131、6187）"),
    ("^IXIC", "那斯達克指數", "科技股整體風險偏好"),
]


def us_top10():
    """川大帝MAGA 的 us_top10 優先；否則由美股報價（Yahoo 報價頁）＋美股新聞（原文網址）組成 10 條，每條都帶出處。"""
    pre = read_json(US_DIR / "tw_premarket_summary.json")
    if pre.get("us_top10"):
        return [{"title": mask(x.get("title")), "point": mask(x.get("point")), "tag": x.get("tag", "中性"),
                 "url": x.get("url"), "source": x.get("source"), "pub_date": x.get("pub_date")} for x in pre["us_top10"][:10]]
    snap = read_json(US_DIR / "us_semi_quotes.json")
    quotes, adr = snap.get("quotes") or {}, snap.get("tsm_adr") or {}
    items = []
    for sym, label, link in US_TOP:
        q = quotes.get(sym) or {}
        chg = to_float(q.get("change_pct"))
        if chg is None or to_float(q.get("price")) is None:
            continue
        point = f"收 {q.get('price'):,.2f}（{chg:+.2f}%）→ {link}"
        if sym == "TSM" and adr.get("premium_pct") is not None:
            point = (f"收 {q.get('price'):,.2f} 美元（{chg:+.2f}%），換算 {adr.get('tsm_converted_twd', 'N/A')} 元，"
                     f"對 2330 溢價 {adr['premium_pct']:+.2f}%")
        items.append({"title": label, "point": mask(point), "tag": "利多" if chg > 0.5 else "利空" if chg < -0.5 else "中性",
                      "url": q.get("url") or f"https://finance.yahoo.com/quote/{sym}", "source": "Yahoo Finance",
                      "pub_date": q.get("pub_date") or q.get("market_time_tpe")})
    for n in read_json(US_DIR / "us_news_raw.json").get("news") or []:
        if len(items) >= 10:
            break
        items.append({"title": n["title"][:80], "point": f"美股新聞（{n['source']}）", "tag": "中性",
                      "url": n["url"], "source": n["source"], "pub_date": n["pub_date"]})
    return items[:10]


def lookup_section():
    with st.container(border=True):
        st.markdown("#### 🔍 即時個股快查（輸入股票名稱或代號）")
        c1, c2 = st.columns([5, 1.3], vertical_alignment="bottom")
        query = c1.text_input("輸入台股名稱或代號（例：台積電、奇鋐、2408）即時分析目前狀況", key="lookup_query",
                              placeholder="台積電、鴻海、華邦電、奇鋐、2330、2408…")
        if c2.button("⚡ 立即快篩分析", type="primary", width="stretch", key="btn_lookup") and query.strip():
            try:
                with st.spinner(f"快篩 {query.strip()}（報價＋K線技術人趨勢＋籌碼王籌碼）…"):
                    result = qsl.lookup(query.strip())
                st.session_state["lookup_result"] = result
                mem.record_lookup(result)
            except LookupError as exc:
                st.warning(f"🔎 {exc}")
        r = st.session_state.get("lookup_result")
        if not r:
            st.caption("💡 0 Token 即時快篩：證交所 MIS + Yahoo 雙引擎報價、K線技術人趨勢判讀、籌碼王法人與融資判讀。")
            return
        html(ui.render_lookup_card(r))
        if r.get("errors"):
            st.caption("⚠️ 部分資料缺口：" + "；".join(r["errors"]))
        b1, b2, b3 = st.columns([2.2, 1.2, 1.6])
        if b1.button("🔥 請巴董事長召集團隊深度診斷此股（AI 深度重算）", type="primary", width="stretch", key="btn_lookup_deep"):
            deep_research(r["stock_id"])
            add_focus(r["stock_id"], r.get("name"))
            goto_view("war_room")
            st.session_state["member"] = "chairman"
            st.rerun()
        if b2.button("🔁 重新快篩", width="stretch", key="btn_lookup_again"):
            st.session_state["lookup_result"] = qsl.lookup(r["stock_id"])
            st.rerun()
        if b3.button("🎖️ 帶進 WAR ROOM 研究", width="stretch", key="btn_lookup_to_war"):
            add_focus(r["stock_id"], r.get("name"))
            goto_view("war_room")
            st.session_state["member"] = "chairman"
            st.rerun()


def render_daily():
    open_now = market_open_now()
    h1, h2, h6, h3, h5, h4 = st.columns([2.0, 1.5, 2.3, 1.8, 2.0, 0.4], vertical_alignment="center")
    with h1:
        badge = ui.pill("🟢 台股盤中", "bull") if open_now else ui.pill("🌙 台股盤後", "neutral")
        us_badge = ui.pill("✅ 美股早報已定錨", "info") if us_locked else ui.pill("🦅 等待美股收盤匯報", "amber")
        html(f'<div class="wr-title">📈 台股每日戰報</div><div style="margin-top:4px">{badge} {us_badge} '
             f'<span class="tl-time">{datetime.now():%Y-%m-%d %H:%M}</span></div>')
    with h2:
        if st.button("🎖️ 進入 WAR ROOM", width="stretch", key="btn_enter_war_room"):
            goto_view("war_room")
            st.rerun()
    with h6:
        if st.button("🧪 進入 SIMULATION ROOM", width="stretch", key="btn_enter_sim"):
            goto_view("sim_room")
            st.rerun()
    if h3.button("🔄 一鍵更新今日早晚報與收盤數據", width="stretch", key="btn_daily_refresh"):
        daily_refresh()
    with h5:
        engine_selector("daily")
    with h4:
        ai_tasks_popover()
    engine_key_box("daily")
    daemon_strip()
    task_banner()

    lookup_section()

    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            news_sum = read_json(NEWS_DIR / "daily_news_summary.json")
            sentiment = news_sum.get("market_sentiment")
            badge = ui.pill(sentiment, SENTIMENT_KIND.get(sentiment, "neutral")) if sentiment else ""
            items, source = news_top10()
            html(ui.render_station_head(1, f"📰 今日重要財經新聞 TOP 10｜{source}", badge))
            if news_sum.get("top_news_10"):
                stamp(news_sum.get("updated_at"), NEWS_DIR / "daily_news_summary.json", "新聞達人 TOP 10")
            else:
                raw_path = NEWS_DIR / "daily_market_news_raw.json"
                stamp(read_json(raw_path).get("updated_at"), raw_path, "原始新聞（尚未 AI 精選）")
            if news_sum.get("one_line_takeaway"):
                st.caption("💡 " + mask(news_sum["one_line_takeaway"]))
            html(ui.render_top10_list(items))
    with right:
        with st.container(border=True):
            lock_badge = ui.pill("✅ 今日早報已定錨鎖定", "info") if us_locked else ui.pill("🌙 等待收盤匯報", "amber")
            snap = read_json(US_DIR / "us_semi_quotes.json")
            pre = read_json(US_DIR / "tw_premarket_summary.json")
            html(ui.render_station_head(5, "🦅 美股收盤後 TOP 10 關鍵資訊", lock_badge))
            if pre.get("us_top10"):
                stamp(pre.get("us_top10_updated_at") or pre.get("generated_at"), US_DIR / "tw_premarket_summary.json",
                      f"美股報價 {snap.get('fetched_at', 'N/A')}")
            else:
                stamp(snap.get("fetched_at"), US_DIR / "us_semi_quotes.json", "美股報價快照")
            if pre.get("opening_strategy"):
                st.caption(f"🎯 開盤策略（{pre.get('date')}）：" + mask(pre["opening_strategy"])[:120])
            html(ui.render_top10_list(us_top10()))

    st.markdown("#### 📊 台股收盤後大盤與資金總結")
    summary = read_json(CLOSE_PATH)
    if summary:
        stamp(summary.get("bullets_updated_at") or summary.get("generated_at"), CLOSE_PATH,
              f"數據彙整 {summary.get('generated_at', 'N/A')}")
        bullets = summary.get("bullets") or ftr.fallback_close_bullets(summary)  # 尚未 AI 整理時用 Python 版重點
        card = dict(summary, bullets=[{"label": b.get("label"), "text": mask(b.get("text"))} for b in bullets],
                    bullets_engine=summary.get("bullets_engine") if summary.get("bullets") else "python")
        html(ui.render_market_close_card(card))
    else:
        st.info("尚無收盤總結，請按右上「🔄 一鍵更新今日早晚報與收盤數據」。")
    st.caption("資料來源：證交所 MIS、Yahoo Finance、FinMind、鉅亨網、Google News｜僅供研究參考，非投資建議。")


# ================================================================ 🎖️ 視圖二：WAR ROOM

def vault_pin():
    """金庫密碼只從 .env 或 Streamlit Secrets 的 PRIVACY_PIN 讀取；程式碼內沒有任何預設密碼。都沒設定時回傳空字串（金庫永久鎖定）。"""
    pin = (os.getenv("PRIVACY_PIN") or "").strip()
    if not pin:
        try:
            pin = str(st.secrets.get("PRIVACY_PIN", "") or "").strip()
        except Exception:  # 沒有 secrets.toml
            pin = ""
    return pin


def _vault_unlock():
    pin = vault_pin()
    if pin and st.session_state.get(VAULT_PIN_KEY, "").strip() == pin:
        st.session_state["privacy_unlocked"] = True
        st.session_state["vault_error"] = False
    else:
        st.session_state["vault_error"] = True
    st.session_state[VAULT_PIN_KEY] = ""


def _vault_lock():
    st.session_state["privacy_unlocked"] = False
    st.session_state["vault_error"] = False
    st.session_state[VAULT_PIN_KEY] = ""


def _leave_war_room():
    goto_view("home")
    st.session_state["member"] = None


def vault_locked_bar():
    with st.container(border=True):
        c1, c2, c3 = st.columns([3, 2.2, 0.9], vertical_alignment="center")
        c1.markdown('<div class="vault-bar">🔐 戰術金庫鎖定中：持股實戰表已隱藏</div>', unsafe_allow_html=True)
        pin_set = bool(vault_pin())
        c2.text_input("輸入金庫密碼", type="password", key=VAULT_PIN_KEY, label_visibility="collapsed",
                      placeholder="輸入密碼展開持股戰術表..." if pin_set else "尚未設定金庫密碼", disabled=not pin_set)
        c3.button("🔓 解鎖展開", width="stretch", on_click=_vault_unlock, disabled=not pin_set)
        if not pin_set:
            st.warning("🔒 尚未設定金庫密碼：請在 .env（本機）或 Streamlit Secrets（雲端）加入 PRIVACY_PIN，金庫在設定前永久鎖定。")
        elif st.session_state.get("vault_error"):
            st.error("❌ 密碼錯誤，金庫維持鎖定。")


RUN_EVERY = "10s" if st.session_state.get("auto_refresh", True) else None
LIVE_REFRESH = st.session_state.get("auto_refresh", True) and market_open_now()  # 只在實際盤中打外部報價 API


@st.fragment(run_every=RUN_EVERY)
def holdings_table():
    """💼 手上持股盤中實戰表：單一高密度戰術表（解鎖後才渲染）。"""
    if not is_unlocked():
        return
    if portfolio and LIVE_REFRESH:
        try:
            pi.run()
        except pi.MISError as exc:
            st.warning(f"報價刷新失敗，沿用上一筆：{exc}")
    live = pi.load_status()
    held = [p for p in live.get("positions", []) if p.get("in_portfolio")]
    if not held:
        st.info("尚無持股報價，請按「🔄 刷新持股報價」；清單為空時請於「✏️ 編輯持股清單」新增。")
        return
    decisions = load_decisions()
    engines = {d.get("engine") for d in decisions.values() if isinstance(d, dict) and d.get("engine")}
    stamp(live.get("updated_at"), pi.STATUS_PATH,
          f"🧠 巴董事長最近拍板 {file_ts(DECISIONS_PATH) or '尚未會診'}" + (f"（{'、'.join(engines)}）" if engines else ""))
    rows = []
    for p in held:
        trend = p.get("intraday_trend") or {}
        d = decisions.get(p["stock_id"]) or {}
        rows.append({
            "代號": p["stock_id"], "名稱": p.get("name"),
            "現價": to_float(p.get("current_price")), "漲跌%": to_float(p.get("change_pct")),
            "VWAP 位階": f"{trend.get('vwap_position', 'N/A')}（{ui.fmt_price(trend.get('vwap'))}）",
            "五檔買賣比": to_float(p.get("bid_ask_ratio")),
            "成本價": to_float(p.get("cost_price")), "持有股數": p.get("shares"),
            "未實現損益": to_float(p.get("net_pnl")), "損益%": to_float(p.get("net_return_pct")),
            "籌碼王提案": d.get("role2_action", "—"), "K線技術人提案": d.get("role3_action", "—"),
            "巴董事長拍板": d.get("role4_final_action") or f"量化：{(p.get('quant_suggestion') or {}).get('signal', '—')}",
            "昨日覆盤修正": mem.holding_hint(p["stock_id"]),
        })
    df = pd.DataFrame(rows)
    st.dataframe(money_table(df, price=["現價", "成本價"], signed_money=["未實現損益"], signed_pct=["漲跌%", "損益%"]),
                 width="stretch", hide_index=True, height=min(80 + 36 * len(df), 520), column_config={
                     "五檔買賣比": st.column_config.NumberColumn(format="%.2f"),
                     "持有股數": st.column_config.NumberColumn(format="%d"),
                     "昨日覆盤修正": st.column_config.TextColumn(width="large"),
                 })
    totals = live.get("totals", {})
    pct = totals.get("total_net_return_pct")
    html(f'<div class="tl-src">💰 總市值 {ui.fmt_money(totals.get("total_market_value", 0))}｜總成本 '
         f'{ui.fmt_money(totals.get("total_cost", 0))}｜未實現淨損益 {ui.signed_html(totals.get("total_net_pnl", 0), pct=pct)}'
         f'｜損益已扣手續費 0.1425%×2 與證交稅 0.3%｜{"盤中每 10 秒自動跳價" if LIVE_REFRESH else "非盤中或自動跳價關閉"}</div>')


def holdings_section():
    st.markdown("### 💼 戰術表一：手上持股盤中實戰表")
    if not is_unlocked():
        vault_locked_bar()
        return
    with st.container(border=True):
        t1, t2, t3 = st.columns([3, 1.1, 1.3], vertical_alignment="center")
        t1.caption("🔓 金庫已解鎖：成本、股數與損益僅在此表顯示，成員報告仍自動打碼保護。")
        if t2.button("🔄 刷新持股報價", width="stretch"):
            try:
                refresh_live_status(portfolio)
            except pi.MISError as exc:
                st.session_state["toast"] = f"⚠️ 報價刷新失敗：{exc}"
            st.rerun()
        t3.button("🔒 一鍵鎖上金庫", type="primary", width="stretch", on_click=_vault_lock)
        with st.expander("✏️ 編輯持股清單"):
            version = st.session_state.get("portfolio_editor_version", 0)
            edited = st.data_editor(
                pd.DataFrame(portfolio, columns=PORTFOLIO_COLUMNS), num_rows="dynamic", width="stretch",
                hide_index=True, key=f"portfolio_editor_{version}",
                column_config={
                    "stock_id": st.column_config.TextColumn("代號", required=True),
                    "name": st.column_config.TextColumn("名稱（留空自動帶入）"),
                    "cost_price": st.column_config.NumberColumn("成本價", min_value=0.0, step=0.05, format="%.2f", required=True),
                    "shares": st.column_config.NumberColumn("股數", min_value=1, step=1, format="%d", required=True),
                    "note": st.column_config.TextColumn("備註"),
                })
            if st.button("💾 儲存持股清單並刷新報價", type="primary"):
                rows, skipped = editor_to_rows(edited)
                pi.save_portfolio(rows)
                st.session_state["portfolio_editor_version"] = version + 1
                try:
                    refresh_live_status(rows)
                    st.session_state["toast"] = f"💾 已儲存 {len(rows)} 檔持股" + (f"（略過 {skipped} 筆）" if skipped else "")
                except pi.MISError as exc:
                    st.session_state["toast"] = f"⚠️ 已儲存，但報價刷新失敗：{exc}"
                st.rerun()
        holdings_table()
        diagnosis = read_text(DIAGNOSIS_PATH)
        if diagnosis:
            with st.expander("🩺 最近一次持股完整診斷報告"):
                st.markdown(diagnosis)


@st.fragment(run_every=RUN_EVERY)
def radar_table():
    """🎯 20 大龍頭建議買進價監控表：預設只顯示「達建議買進價」與「接近買進價 (差距1.5%內)」。"""
    if LIVE_REFRESH and not DAEMON_FEEDS_RADAR:  # 背景守護執行緒盤中每 15 秒已在更新監控表，不重複打 API
        try:
            radar_mod.run()
        except pi.MISError as exc:
            st.warning(f"建議買進價監控表刷新失敗，沿用上一筆：{exc}")
    radar = radar_mod.load_radar()
    results = radar.get("results") or []
    c1, c2 = st.columns([4, 1.4], vertical_alignment="center")
    show_all = c2.checkbox("強制展開全部 20 檔備用清單", key="radar_show_all")
    timing = [r for r in results if r.get("signal") in ("HIT_ENTRY", "APPROACHING")]
    with c1:
        stamp(radar.get("updated_at"), radar_mod.RADAR_PATH,
              f"備用計畫 {radar.get('standby_generated_at', 'N/A')}｜達建議買進價或接近 {len(timing)} / {len(results)} 檔")
    hits = [r for r in timing if r.get("signal") == "HIT_ENTRY"]
    if hits:
        html(ui.render_entry_alert_card(hits))
    shown = results if show_all else timing
    if not shown:
        html(ui.render_radar_banner("目前 20 大半導體龍頭沒有標的達建議買進價或接近買進價 (差距1.5%內)，不追高"
                                   "（右側可勾選「強制展開全部 20 檔備用清單」檢視）"))
        return
    df = pd.DataFrame([{
        "代號": r["stock_id"], "名稱": r.get("name"), "次產業": r.get("sector"), "燈號": radar_mod.SIGNAL_LABELS.get(r.get("signal"), r.get("signal_label")),
        "現價": to_float(r.get("price")), "漲跌%": to_float(r.get("change_pct")),
        "建議買進價": f"{ui.fmt_price(r.get('entry_low'))}–{ui.fmt_price(r.get('entry_high'))}",
        "距買進價%": to_float(r.get("dist_to_entry_pct")),
        "停損價": to_float(r.get("stop_loss")), "停利目標價": to_float(r.get("target_1")), "風報比": to_float(r.get("risk_reward")),
        "五檔比": to_float(r.get("bid_ask_ratio")), "VWAP": r.get("vwap_position"), "籌碼王": r.get("chip_trend"),
        "巴董事長備註": r.get("role4_note"),
    } for r in shown])
    st.dataframe(money_table(df, price=["現價", "停損價", "停利目標價"], signed_pct=["漲跌%", "距買進價%"]), width="stretch",
                 hide_index=True, height=min(80 + 36 * len(df), 560), column_config={
        "風報比": st.column_config.NumberColumn(format="%.2f"),
        "五檔比": st.column_config.NumberColumn(format="%.2f"),
        "巴董事長備註": st.column_config.TextColumn(width="large"),
    })
    p1, p2 = st.columns([3, 1.4], vertical_alignment="bottom")
    pick = p1.selectbox("帶入巴董事長深度研究", [r["stock_id"] for r in shown], key="radar_pick",
                        format_func=lambda s: f"{s} {sector_names.get(s, '')}")
    if p2.button("🔎 交給巴董事長研究", width="stretch", key="btn_radar_pick"):
        st.session_state["focus_pending"] = pick
        st.session_state["member"] = "chairman"
        st.rerun(scope="app")


# ---------------------------------------------------------------- 成員個人報告

def drop_section(text, keyword, placeholder):
    """移除標題含 keyword 的整個 `## ` 段落（到下一個 `## ` 為止），以 placeholder 取代。"""
    out, skipping = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            if keyword in line:
                skipping = True
                out += [line, "", placeholder, ""]
                continue
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out)


def report_tab(sid, suffix, height=520):
    text = read_text(STOCKS_DIR / f"{sid}_{suffix}.md")
    if text:
        with st.container(height=height):
            st.markdown(mask(text))
    else:
        st.caption(f"尚無 {sid}_{suffix}.md，可在巴董事長研究區按「🔥 召集團隊深度診斷」。")


def focus_picker(key_suffix):
    """研究標的選單＋任意個股輸入（名稱或代號）。"""
    c1, c2, c3 = st.columns([2.2, 2, 0.9], vertical_alignment="bottom")
    c1.selectbox("🎯 研究標的", focus_options, key="focus_stock", format_func=label_of)
    any_q = c2.text_input("或輸入任意台股名稱／代號", key=f"any_stock_{key_suffix}", placeholder="例：奇鋐、6669")
    if c3.button("切換", width="stretch", key=f"btn_any_{key_suffix}") and any_q.strip():
        try:
            sid, name = qsl.resolve(any_q)
            add_focus(sid, name)
            st.rerun()
        except LookupError as exc:
            st.warning(str(exc))


def reflection_section(member, sid):
    """該成員對本標的與大盤的「昨日結論 vs 今日實盤」覆盤紀錄。"""
    st.markdown("#### 🔄 昨日結論 vs 今日實盤落差覆盤")
    rows = []
    for target in (sid, mem.MARKET_ID):
        r = mem.load_reflection(target)
        for x in r.get("reviews") or []:
            if x["member"] == member:
                rows.append({"標的": "大盤" if target == mem.MARKET_ID else label_of(target), "來源": x["source"],
                             "結論時間": x["recorded_at"][5:16], "結果": x["verdict"], "覆盤": mask(x["gap_text"]),
                             "修正": mask(x["corrections"][0])})
    idx = (mem.load_index().get("members") or {}).get(member) or {}
    hidden = set(_private_ids) if not is_unlocked() else set()
    others = [g for g in idx.get("gap_items") or [] if g["stock_id"] not in hidden and g["stock_id"] != sid][:5]
    for g in others:
        rows.append({"標的": label_of(g["stock_id"]), "來源": "近期落差", "結論時間": g["recorded_at"][5:16], "結果": "落差",
                     "覆盤": mask(g["text"]), "修正": mask(g["correction"])})
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                     column_config={"覆盤": st.column_config.TextColumn(width="large"),
                                    "修正": st.column_config.TextColumn(width="medium")})
        st.caption(f"記憶庫：reports/memory/｜覆盤 {mem.load_index().get('generated_at', 'N/A')}｜"
                   f"{idx.get('name', '')} 累計 {idx.get('hits', 0)} 準／{idx.get('gaps', 0)} 落差")
    else:
        st.caption("尚無可驗證的昨日結論（記憶庫會在每次更新時自動收錄並於下一個交易日覆盤）。")


def panel_news(sid):
    summary = read_json(NEWS_DIR / "daily_news_summary.json")
    raw = read_json(NEWS_DIR / "daily_market_news_raw.json")
    left, right = st.columns([0.6, 0.4])
    with left:
        if summary:
            sentiment = summary.get("market_sentiment", "N/A")
            html(f'<div class="md-card"><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
                 f'<span class="pod-title">今日新聞情緒</span>{ui.pill(sentiment, SENTIMENT_KIND.get(sentiment, "neutral"), "big-badge")}'
                 f'<span class="tl-time">彙整於 {ui.esc(summary.get("updated_at", ""))}</span></div>'
                 f'<div class="say" style="margin-top:10px"><div class="who">💡 一句話市場核心焦點</div>'
                 f'{ui.esc(mask(summary.get("one_line_takeaway", "")))}</div><div style="margin-top:10px">🔥 '
                 + " ".join(ui.pill(mask(t), "amber") for t in summary.get("top_themes") or []) + "</div></div>")
            for r in summary.get("risk_warnings") or []:
                st.markdown(f"- ⚠️ {mask(r)}")
        tab_news, tab_stock = st.tabs(["📑 今日真假利多判讀報告", f"📊 {label_of(sid)} 基本面報告"])
        with tab_news:
            brief = read_text(NEWS_DIR / "daily_news_brief.md")
            with st.container(height=520):
                st.markdown(mask(brief) if brief else "尚無 daily_news_brief.md")
        with tab_stock:
            report_tab(sid, "role1_fundamental")
    with right:
        h, b = st.columns([2, 1], vertical_alignment="center")
        h.markdown("#### ⚡ 即時財經頭條流")
        if b.button("⚡ 更新 (0 Token)", width="stretch"):
            out = news_mod.run()
            st.session_state["toast"] = f"📰 已更新 {out['count']} 則（{out['elapsed_sec']} 秒）"
            st.rerun()
        items = (raw.get("news") or [])[:30]
        st.caption(f"更新於 {raw.get('updated_at', 'N/A')}｜最新 {len(items)} 則")
        with st.container(height=720):
            html(ui.render_news_timeline(items) if items else "<p class='tl-src'>尚無新聞清單。</p>")
    reflection_section("news", sid)


def panel_chips(sid):
    chips = load_prefetch(sid, "chips")
    inst, margin = chips.get("institutional") or {}, chips.get("margin") or {}
    radar_row = next((r for r in radar_mod.load_radar().get("results") or [] if r["stock_id"] == sid), {})
    verdict = qsl.chip_verdict(chips, kline_metrics(sid), {"bid_ask_ratio": radar_row.get("bid_ask_ratio"),
                                                           "tick_side": radar_row.get("tick_side")})
    html(f'<div class="say"><div class="who">{ui.mini_avatar(2, 30)}籌碼王判讀 '
         f'{ui.pill(verdict["label"], ui.tone_kind(verdict["label"]), "big-badge")}</div><ul>'
         + "".join(f"<li>⚠️ {ui.esc(t)}</li>" for t in verdict["alerts"])
         + "".join(f"<li>{ui.esc(t)}</li>" for t in verdict["points"]) + "</ul></div>")
    if inst:
        hedge = inst.get("dealer_hedging") or {}
        c = st.columns(6)
        c[0].metric("外資 5 日", f"{inst['foreign']['sum_5d']:+,.0f} 張", f"連{'買' if inst['foreign_streak_days'] > 0 else '賣'} {abs(inst['foreign_streak_days'])} 日", delta_color="off")
        c[1].metric("投信 5 日", f"{inst['trust']['sum_5d']:+,.0f} 張", f"連{'買' if inst['trust_streak_days'] > 0 else '賣'} {abs(inst['trust_streak_days'])} 日", delta_color="off")
        c[2].metric("外資 10 日急翻", f"{inst.get('foreign_flip_count_10d', 'N/A')} 次", "≥3 隔日沖嫌疑", delta_color="off")
        c[3].metric("自營避險 5 日", f"{hedge['sum_5d']:+,.0f} 張" if hedge else "N/A", "權證造市動能", delta_color="off")
        c[4].metric("融資 5 日", f"{margin.get('margin_change_5d', 0):+,.0f} 張" if margin else "N/A")
        c[5].metric("券資比", f"{margin.get('short_margin_ratio_pct', 'N/A')}%" if margin else "N/A")
        daily = pd.DataFrame(inst.get("daily_10d") or [])
        if not daily.empty:
            names = {"date": "日期", "foreign": "外資", "trust": "投信", "dealer_self": "自營自行", "dealer_hedging": "自營避險",
                     "dealer": "自營合計", "total": "合計"}
            st.dataframe(daily[[c for c in names if c in daily.columns]].rename(columns=names), width="stretch", hide_index=True)
        st.caption(f"法人資料日 {inst.get('latest_date')}（張）")
    else:
        st.info(f"{sid} 尚無籌碼預抓檔，請按「🚨 戰情室全員同步更新」。")
    if radar_row:
        st.markdown(f"**⚡ 盤中五檔**：委買/委賣比 {radar_row.get('bid_ask_ratio')}｜{radar_row.get('tick_side', '')}｜"
                    f"{radar_row.get('big_tick_flow', '')}｜{radar_row.get('vwap_position', '')}")
    radar = radar_mod.load_radar().get("results") or []
    if radar:
        with st.expander("🏦 20 大龍頭法人籌碼排行"):
            board = sorted(radar, key=lambda r: -(r.get("chip_score") or 0))
            st.dataframe(pd.DataFrame([{"代號": r["stock_id"], "名稱": r.get("name"), "次產業": r.get("sector"),
                                        "籌碼分數": r.get("chip_score"), "法人籌碼": r.get("chip_trend"),
                                        "五檔比": r.get("bid_ask_ratio")} for r in board]),
                         width="stretch", hide_index=True, height=420)
    st.markdown(f"#### 🎧 {label_of(sid)} 籌碼報告")
    report_tab(sid, "role2_chips", height=460)
    reflection_section("chips", sid)


def zhu_box(sid):
    km = kline_metrics(sid)
    zhu = km.get("zhu") or {}
    if not zhu or zhu.get("error"):
        st.caption(f"{sid} 尚無 K線技術人判讀（缺 K 線預抓檔）。")
        return
    patterns = "、".join(f'{p["pattern"]}（{p["date"][5:]}）' for p in zhu.get("patterns") or []) or "近 3 日無關鍵轉折 K"
    html(f'<div class="say"><div class="who">{ui.mini_avatar(3, 30)}K線技術人｜四步驟判讀'
         f'{ui.pill(zhu.get("trend"), ui.tone_kind(zhu.get("trend")), "big-badge")}</div><ul>'
         f'<li>① 趨勢定多空：{ui.esc(zhu.get("trend_rule"))}</li>'
         f'<li>② 四線排列：{ui.esc(zhu.get("ma_summary"))}｜{ui.esc(zhu.get("granville"))}</li>'
         f'<li>③ 關鍵轉折 K：{ui.esc(patterns)}</li>'
         f'<li>④ 紀律停損：{ui.esc(zhu.get("stop_basis"))}</li>'
         f'<li>👉 {ui.esc(zhu.get("action"))}</li></ul></div>')


def panel_kline(sid):
    summary = load_summary(sid)
    plan = (read_json(STANDBY_PATH).get("stocks") or {}).get(sid) or {}
    levels = summary or ({"entry_zone": [plan.get("entry_low"), plan.get("entry_high")], "stop_loss": plan.get("stop_loss"),
                          "target_1": plan.get("target_1")} if plan else {})
    zhu_box(sid)
    st.markdown(f"#### 📈 {label_of(sid)} 日 K 線（建議買進價與停損價：{'巴董事長裁決' if summary else '盤後備用計畫'}）")
    k_chart(sid, levels)
    st.markdown(f"#### ⚔️ {label_of(sid)} K 線報告")
    report_tab(sid, "role3_kline", height=460)
    reflection_section("kline", sid)


def k_chart(sid, levels, height=760):
    try:
        with st.spinner("載入日 K 線…"):
            price = load_price(sid, 150)
            try:
                inst = load_institutional(sid, 180)
            except Exception:
                inst = None
        st.plotly_chart(build_chart(price, inst, levels, height), width="stretch")
    except Exception as exc:
        st.error(f"行情載入失敗：{exc}")


def build_chart(price, inst, levels, height=760):
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.58, 0.17, 0.25],
                        subplot_titles=("日 K 線與均線", "成交量（張）", "三大法人買賣超（張）"))
    x = price["date"].dt.strftime("%Y-%m-%d")
    fig.add_trace(go.Candlestick(
        x=x, open=price["open"], high=price["high"], low=price["low"], close=price["close"], name="K 線",
        increasing=dict(line=dict(color=UP_COLOR), fillcolor=UP_COLOR),
        decreasing=dict(line=dict(color=DOWN_COLOR), fillcolor=DOWN_COLOR)), row=1, col=1)
    for col, color in (("ma5", "#F59E0B"), ("ma10", "#F472B6"), ("ma20", "#60A5FA"), ("ma60", "#C084FC")):
        fig.add_trace(go.Scatter(x=x, y=price[col], name=col.upper(), mode="lines", line=dict(color=color, width=1.4)),
                      row=1, col=1)
    zone = levels.get("entry_zone")
    if isinstance(zone, (list, tuple)) and len(zone) >= 2 and to_float(zone[0]) and to_float(zone[1]):
        fig.add_hrect(y0=float(zone[0]), y1=float(zone[1]), fillcolor="rgba(16, 185, 129, 0.18)", line_width=0,
                      annotation_text="建議買進價", annotation_position="top left", row=1, col=1)
    stop = to_float(levels.get("stop_loss"))
    if stop:
        fig.add_hline(y=stop, line_dash="dash", line_color="#EF4444", line_width=1.4,
                      annotation_text=f"停損 {stop:g}", annotation_position="bottom left", row=1, col=1)
    for key, label in (("target_1", "目標1"), ("target_2", "目標2")):
        val = to_float(levels.get(key))
        if val:
            fig.add_hline(y=val, line_dash="dot", line_color="#FBBF24", line_width=1,
                          annotation_text=f"{label} {val:g}", annotation_position="top left", row=1, col=1)
    vol_colors = [UP_COLOR if c >= o else DOWN_COLOR for o, c in zip(price["open"], price["close"])]
    fig.add_trace(go.Bar(x=x, y=price["volume_lots"], name="成交量", marker_color=vol_colors, showlegend=False),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=x, y=price["vol_ma20"], name="20 日均量", mode="lines",
                             line=dict(color="#60A5FA", width=1.2)), row=2, col=1)
    if inst is not None and not inst.empty:
        inst = inst.reindex(price["date"]).fillna(0.0)
        for col, label, color in (("foreign", "外資", "#3B82F6"), ("trust", "投信", "#F59E0B"), ("dealer", "自營商", "#A78BFA")):
            fig.add_trace(go.Bar(x=x, y=inst[col].values, name=label, marker_color=color), row=3, col=1)
    fig.update_layout(template="plotly_dark", height=height, barmode="group", xaxis_rangeslider_visible=False,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#0F172A", hovermode="x unified",
                      margin=dict(l=10, r=10, t=40, b=10), font=dict(family="Inter, Noto Sans TC"),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    fig.update_xaxes(type="category", nticks=12, gridcolor="#1F2937")
    fig.update_yaxes(gridcolor="#1F2937")
    return fig


def md_bullets(text, limit=3):
    """從成員報告擷取「核心論點」前幾點，回傳已遮罩、已 escape 的 <ul>。"""
    if not text:
        return '<p class="tl-src">尚無報告</p>'
    lines, capture = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            if capture:
                break
            capture = "核心論點" in line
            continue
        s = line.strip()
        if capture and s and (s[0].isdigit() or s.startswith(("-", "*"))):
            s = s.lstrip("-*0123456789.、 ").replace("**", "")
            lines.append(s[:110] + ("…" if len(s) > 110 else ""))
    if not lines:
        return '<p class="tl-src">報告未含核心論點段落</p>'
    return "<ul>" + "".join(f"<li>{ui.esc(mask(s))}</li>" for s in lines[:limit]) + "</ul>"


def premarket_card(include_holdings):
    pre = read_json(US_DIR / "tw_premarket_summary.json")
    if not pre:
        st.caption("尚無台股盤前預判。")
        return
    fresh = pre.get("date") == us_status["tw_trading_date"]
    html(f'<div class="md-card"><div class="pod-title" style="font-size:1.02rem">🌅 今日台股大盤走勢預判與開盤策略'
         f'（{ui.esc(pre.get("date"))}）{"" if fresh else " ⚠️ 非本交易日"}</div>'
         f'<div class="verdict-row" style="grid-template-columns:1fr 1fr;margin-top:10px">'
         f'<div class="say"><div class="who">📈 大盤預判</div>{ui.esc(mask(pre.get("tw_index_forecast", "")))}</div>'
         f'<div class="say"><div class="who">🎯 開盤策略</div>{ui.esc(mask(pre.get("opening_strategy", "")))}</div>'
         f'</div></div>')
    picks = pre.get("top_sector_picks") or {}
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**✅ 看好次產業**")
        for x in picks.get("favor") or []:
            st.markdown(f"- {mask(x)}")
    with c2:
        st.markdown("**⛔ 避開次產業**")
        for x in picks.get("avoid") or []:
            st.markdown(f"- {mask(x)}")
    alerts = privacy_guard.load_premarket_alert(pre) if include_holdings else []
    if alerts:
        with st.expander("💼 持股開盤特別叮嚀（金庫已解鎖）"):
            for a in alerts:
                st.markdown(f"- **{a.get('stock_id', '')}**：{ui.name_roles(a.get('alert', ''))}")


US_CARDS = [("^SOX", "費半 SOX"), ("TSM", "台積電 ADR"), (None, "TSM 溢價率"), ("NVDA", "輝達 NVDA"),
            ("MU", "美光 MU"), ("VRT", "Vertiv VRT"), ("ASML", "艾司摩爾 ASML")]


def panel_maga(sid):
    snapshot = read_json(US_DIR / "us_semi_quotes.json")
    quotes, adr = snapshot.get("quotes") or {}, snapshot.get("tsm_adr") or {}
    if us_locked:
        st.success(f"💤 今日 07:00–08:00 美股收盤匯報與台股預判已完成，川大帝MAGA 休眠鎖定中｜對應台股 {us_status['tw_trading_date']}")
    else:
        st.info(f"🌙 等待美股收盤匯報｜對應台股 {us_status['tw_trading_date']}｜{us_status['message']}")
    for col, (sym, label) in zip(st.columns(len(US_CARDS)), US_CARDS):
        if sym is None:
            prem = adr.get("premium_pct")
            col.metric(label, f"{prem:+.2f}%" if prem is not None else "N/A",
                       f"換算 {ui.fmt_price(adr.get('tsm_converted_twd'))}" if prem is not None else None, delta_color="off")
            continue
        q = quotes.get(sym) or {}
        chg = q.get("change_pct")
        # 指數（^開頭）是點數不是金額；個股與 ADR 顯示 $（美元）
        value = (ui.fmt_num(q["price"]) if sym.startswith("^") else ui.fmt_price(q["price"])) if q.get("price") else "N/A"
        col.metric(label, value,
                   f"{chg:+.2f}%" if chg is not None else None, delta_color="inverse")
    if snapshot:
        st.caption(f"快照 {snapshot.get('fetched_at')}（{snapshot.get('mode')}）｜美股時間 "
                   f"{(quotes.get('^SOX') or {}).get('market_time_tpe', 'N/A')}（台北）｜台股慣例紅漲綠跌")
    premarket_card(include_holdings=is_unlocked())
    tabs = st.tabs(["📊 美股收盤匯報", "🌅 盤前總結全文", "🌙 夜盤風向"])
    for tab, fname in zip(tabs, ("us_morning_close_report.md", "tw_premarket_summary.md", "us_night_report.md")):
        with tab:
            text = read_text(US_DIR / fname)
            if not text:
                st.caption(f"尚無 {fname}")
                continue
            if fname == "tw_premarket_summary.md" and not is_unlocked():
                text = drop_section(text, "持股開盤特別叮嚀", "> 🔐 持股開盤叮嚀已隱藏，解鎖金庫後可見。")
            with st.container(height=480):
                st.markdown(mask(text))
    reflection_section("maga", sid)


def panel_chairman(sid):
    """深入個股研究專區：任意個股 K 線大圖、三大法人圖、巴董事長裁決與五人辯論全紀錄。"""
    focus_picker("chair")
    summary = load_summary(sid)
    b1, b2 = st.columns([2.4, 3])
    if b1.button(f"🔥 巴董事長深入研究 {label_of(sid)}（{current_engine()}）", type="primary", width="stretch",
                 key="btn_chair_research"):
        deep_research(sid)
        st.rerun()
    b2.caption("巴董事長：先覆盤昨日落差，再把 350 字濃縮數據交給單次多人設請求（新聞達人→籌碼王→K線技術人→Red-Team 拍板），"
               "不派子代理。選「👑 Claude 濃縮單核模式」即為 Claude 終極深潛。")
    if summary:
        html(ui.render_md_verdict_card(summary, is_unlocked()))
    else:
        plan = (read_json(STANDBY_PATH).get("stocks") or {}).get(sid)
        st.info(f"尚無 {sid} 的巴董事長裁決，可按上方「🔥 召集團隊深度診斷」。")
        if plan:
            html(ui.render_agent_pod_card(4, "盤後備用進場計畫（Python 定錨）", plan.get("strategy_type", ""), "amber",
                                          "<ul>" + "".join(f"<li>{ui.esc(t)}</li>" for t in (
                                              f"進場 {plan.get('entry_low')} ~ {plan.get('entry_high')}｜停損 {plan.get('stop_loss')}｜"
                                              f"目標 {plan.get('target_1')}｜風報比 {plan.get('risk_reward')}",
                                              f"籌碼：{plan.get('chip_trend')}", f"技術：{plan.get('kline_trend')}",
                                              plan.get("role4_note", ""))) + "</ul>"))
    pods = st.columns(3)
    for col, (num, name, key, suffix) in zip(pods, ((1, "基本面", "role1_rating", "role1_fundamental"),
                                                    (2, "籌碼", "role2_rating", "role2_chips"),
                                                    (3, "K 線", "role3_rating", "role3_kline"))):
        rating = summary.get(key, "未評級")
        with col:
            html(ui.render_agent_pod_card(num, name, rating, ui.RATING_TYPES.get(rating, "neutral"),
                                          md_bullets(read_text(STOCKS_DIR / f"{sid}_{suffix}.md"))))
    plan = (read_json(STANDBY_PATH).get("stocks") or {}).get(sid) or {}
    levels = summary or ({"entry_zone": [plan.get("entry_low"), plan.get("entry_high")], "stop_loss": plan.get("stop_loss"),
                          "target_1": plan.get("target_1")} if plan else {})
    st.markdown(f"#### 📈 {label_of(sid)} K 線大圖＋三大法人買賣超")
    k_chart(sid, levels, height=820)
    st.markdown("#### 🗣️ 五人辯論全紀錄")
    tabs = st.tabs(["👑 巴董事長總裁決", "🕵️‍♂️ 新聞達人", "🎧 籌碼王", "⚔️ K線技術人", "🦅 川大帝MAGA 盤前定調", "🔄 覆盤紀錄"])
    for tab, suffix in zip(tabs[:4], ("role4_chief", "role1_fundamental", "role2_chips", "role3_kline")):
        with tab:
            report_tab(sid, suffix)
    with tabs[4]:
        premarket_card(include_holdings=is_unlocked())
    with tabs[5]:
        reflection_section("chairman", sid)


PANELS = {"news": panel_news, "chips": panel_chips, "kline": panel_kline, "maga": panel_maga}


# ---------------------------------------------------------------- 會議桌成員一句話戰況

def seat_status(sid):
    news = read_json(NEWS_DIR / "daily_news_summary.json")
    s_news = (f"{news.get('market_sentiment', '尚未彙整')}｜{mask(news.get('one_line_takeaway', ''))[:46]}"
              if news else "尚無新聞快報")
    km = kline_metrics(sid)
    chips = load_prefetch(sid, "chips")
    s_chips = f"{label_of(sid)}：{qsl.chip_verdict(chips, km)['one_line'][:60]}" if chips else f"{label_of(sid)} 籌碼待預抓"
    zhu = km.get("zhu") or {}
    s_kline = f"{label_of(sid)}：{zhu.get('one_line', '')[:60]}" if zhu.get("trend") else f"{label_of(sid)} K 線待預抓"
    if us_locked:
        s_maga = "💤 早報已定錨（休眠鎖定中）"
    else:
        q = (read_json(US_DIR / "us_semi_quotes.json").get("quotes")) or {}
        s_maga = (f"費半 {ui.fmt_pct((q.get('^SOX') or {}).get('change_pct'))}｜"
                  f"TSM ADR {ui.fmt_pct((q.get('TSM') or {}).get('change_pct'))}")
    pre = read_json(US_DIR / "tw_premarket_summary.json")
    summary = load_summary(sid)
    if summary.get("verdict"):
        directive = f"{label_of(sid)} 拍板「{summary['verdict']}」（信心 {summary.get('confidence', 'N/A')}）：{mask(summary.get('conflict_point', ''))[:60]}"
    elif pre.get("opening_strategy") and pre.get("date") == us_status["tw_trading_date"]:
        directive = mask(pre["opening_strategy"])[:90]
    else:
        directive = f"{label_of(sid)} 尚無裁決，可點主席進入深度研究"
    return {"news": s_news, "chips": s_chips, "kline": s_kline, "maga": s_maga, "chairman": directive}


def conference_table():
    hidden = _private_ids if not is_unlocked() else ()
    status = seat_status(focus)
    selected = st.session_state.get("member")
    acc = le.member_accuracy() or {}
    with st.container(key="war_table"):
        _, mid, _ = st.columns([1, 1.45, 1])
        with mid:
            html(ui.render_chairman_seat(status["chairman"], mem.member_status("chairman", hidden), selected == "chairman",
                                         accuracy=acc.get("chairman")))
            if st.button("🔎 點擊巴董事長：進入深入個股研究 & 總裁決報告", width="stretch", key="btn_chair"):
                st.session_state["member"] = "chairman"
                st.rerun()
        cols = st.columns(4)
        for col, num in zip(cols, (1, 2, 3, 5)):
            a = ui.AGENTS[num]
            with col:
                html(ui.render_member_seat(num, status[a["member"]], mem.member_status(a["member"], hidden),
                                           selected == a["member"], sleeping=(num == 5 and us_locked),
                                           accuracy=acc.get(a["member"])))
                if st.button(f"📂 {a['nick']} 個人報告", width="stretch", key=f"seat_btn_{a['member']}",
                             type="primary" if selected == a["member"] else "secondary"):
                    st.session_state["member"] = a["member"]
                    st.rerun()
        st.caption(f"🎯 會議桌焦點標的：{label_of(focus)}（點主席或成員報告可切換）｜🧠 覆盤記憶 "
                   f"{mem.load_index().get('generated_at', '尚未建立')}｜📊 對帳學習資料庫 "
                   f"{le.load_ledger().get('updated_at', '尚未建立')}（準確率只計已收盤結案的預測）")


def member_stamp(member, sid):
    """個人報告卡頂部：該成員報告的產出時間（缺報告時改顯示所依據資料的抓取時間）。"""
    def fetched(role):
        return read_json(DATA_DIR / f"{sid}_{role}.json").get("fetched_at") or "尚未預抓"

    if member == "news":
        path = NEWS_DIR / "daily_news_summary.json"
        report = file_ts(STOCKS_DIR / f"{sid}_role1_fundamental.md")
        stamp(read_json(path).get("updated_at"), path, f"{label_of(sid)} 基本面報告 {report or '尚無'}")
    elif member in ("chips", "kline"):
        suffix, role = ("role2_chips", "chips") if member == "chips" else ("role3_kline", "kline")
        report = STOCKS_DIR / f"{sid}_{suffix}.md"
        data_note = f"{'籌碼' if role == 'chips' else 'K 線'}資料抓取 {fetched(role)}"
        if report.exists():
            stamp(path=report, note=f"{label_of(sid)} 報告｜{data_note}")
        else:
            stamp(fetched(role) if fetched(role) != "尚未預抓" else None, note=f"尚無 {label_of(sid)} 報告，顯示{data_note}")
    elif member == "chairman":
        path = STOCKS_DIR / f"{sid}_summary.json"
        summary = read_json(path)
        stamp(summary.get("updated_at"), path, f"{label_of(sid)} 總裁決" + (f"（{summary['engine']}）" if summary.get("engine")
                                                                         else "") if summary else f"{label_of(sid)} 尚無裁決")
    else:
        path = US_DIR / "tw_premarket_summary.json"
        pre = read_json(path)
        stamp(pre.get("us_top10_updated_at") or pre.get("generated_at"), path,
              f"美股報價 {read_json(US_DIR / 'us_semi_quotes.json').get('fetched_at', 'N/A')}｜收盤匯報 "
              f"{file_ts(US_DIR / 'us_morning_close_report.md') or '尚無'}")


MEMBER_AGENT = {"news": "news_master", "chips": "chip_king", "kline": "kline_tech", "chairman": "chairman_buffett",
                "maga": "trump_maga"}


def member_panel(intraday_mode):
    member = st.session_state.get("member")
    if not member:
        return
    role = ui.MEMBER_ROLE[member]
    nick = ui.AGENTS[role]["nick"]
    with st.container(border=True):
        h1, h2, h3 = st.columns([3.6, 2.4, 1.3], vertical_alignment="center")
        h1.markdown(ui.render_panel_head(role, f"焦點標的：{label_of(focus)}｜{'⚡ 盤中' if intraday_mode else '🌙 盤後'}"),
                    unsafe_allow_html=True)
        with h2:
            if st.button(f"🔄 單獨更新【{nick}】最新報告", type="primary", width="stretch", key=f"btn_update_{member}"):
                with st.spinner(f"只更新{nick}（{current_engine()}）…"):
                    run_ai(f"{nick} 單獨更新", ftr.update_single_agent, MEMBER_AGENT[member],
                           None if member == "maga" else focus, current_engine())
                st.cache_data.clear()
                st.rerun()
            member_stamp(member, focus)
        if h3.button("✖️ 關閉個人報告返回戰術桌", width="stretch", key="btn_close_member"):
            st.session_state["member"] = None
            st.rerun()
        if member == "chairman":
            panel_chairman(focus)
        else:
            if member != "maga":
                focus_picker(member)
            PANELS[member](focus)
        learning_history(member)


LEARN_RESULT = {"CORRECT": "✅ CORRECT 準確", "DEVIATED": "⚠️ DEVIATED 有落差", "WRONG": "❌ WRONG 方向錯誤"}


def learning_history(member):
    """📊 歷史判斷準確率與學習修正紀錄：成員過去預測 vs 之後實際開盤/盤中/收盤表現。"""
    nick = ui.AGENTS[ui.MEMBER_ROLE[member]]["nick"]
    st.markdown(f"#### 📚 歷史判斷 vs 實盤對帳學習紀錄表｜{nick}")
    s = le.member_accuracy(member) or {}
    acc = f"{s['accuracy_pct']:.0f}%" if s.get("total") else "尚無"
    recent = f"{s['recent10_pct']:.0f}%" if s.get("recent10_pct") is not None else "N/A"
    stamp(le.load_ledger().get("updated_at"), le.LEDGER_PATH,
          f"🎯 歷史準確率 {acc} (已累積 {s.get('total', 0)} 次驗證)｜近 10 次 {recent}｜"
          f"CORRECT {s.get('correct', 0)}／DEVIATED {s.get('deviated', 0)}／WRONG {s.get('wrong', 0)}")
    hidden = _private_ids if not is_unlocked() else ()
    rows = le.member_history(member, limit=15, exclude_ids=hidden)
    if not rows:
        st.caption("尚無已驗算的預測。成員產出報告後，系統會在之後的開盤／盤中／收盤自動對帳。")
        return
    def actual(a, key):
        return to_float((a or {}).get(key))
    df = pd.DataFrame([{
        "預測時間": p["timestamp"][5:16], "時段": p["session"], "標的": f"{p['stock_id']} {p.get('name') or ''}".strip(),
        "預測時股價": to_float(p.get("price_at_prediction")), "當時結論": f"{p['stance']}／{p['action']}",
        "建議買進價": to_float(p.get("buy_zone")), "停損價": to_float(p.get("stop_loss")), "目標價": to_float(p.get("target_price")),
        "實際開盤": actual(p["grade"]["actual"], "open"), "盤中最高": actual(p["grade"]["actual"], "high"),
        "盤中最低": actual(p["grade"]["actual"], "low"),
        "收盤/最新": actual(p["grade"]["actual"], "close") or actual(p["grade"]["actual"], "last"),
        "預測後漲跌%": to_float(p["grade"].get("return_pct")),
        "系統判定": (LEARN_RESULT.get(p["grade"]["result"], p["grade"]["result"])
                    + ("（盤中暫判）" if p["status"] == "PROVISIONAL" else "")),
        "判定依據": "；".join(p["grade"].get("reasons") or []),
        "本次如何修正": mask("；".join(x["text"].replace("【歷史落差檢討與修正原則】", "")
                                   for x in p["grade"].get("principles") or []) or "判斷準確，維持原邏輯"),
    } for p in rows])
    st.dataframe(money_table(df, price=["預測時股價", "建議買進價", "停損價", "目標價", "實際開盤", "盤中最高", "盤中最低", "收盤/最新"],
                             signed_pct=["預測後漲跌%"]), width="stretch", hide_index=True,
                 column_config={"判定依據": st.column_config.TextColumn(width="medium"),
                                "本次如何修正": st.column_config.TextColumn(width="large")})
    if hidden:
        st.caption("🔐 戰術金庫鎖定中：持股相關的對帳紀錄已隱藏，解鎖後顯示。")


def qa_section():
    st.markdown("### 💬 盤中戰術提問指揮台（新聞達人／籌碼王／K線技術人 回答 ➔ 巴董事長 Challenge 總結）")
    with st.container(border=True):
        c1, c2 = st.columns([2.2, 2], vertical_alignment="bottom")
        scope_label = c1.radio("提問範圍", list(QA_SCOPES), horizontal=True, key="qa_scope")
        scope = QA_SCOPES[scope_label]
        stock_q = c2.text_input("指定個股（名稱或代號）", key="qa_stock", placeholder="例：2408、華邦電",
                                disabled=scope != "stock")
        question = st.text_area("戰術提問", key="qa_question", height=80,
                                placeholder="例：2408 現在盤中五檔賣壓重，該在這裡攤平還是等尾盤？")
        locked_holdings = scope == "holdings" and not is_unlocked()
        if locked_holdings:
            st.caption("🔐 針對手上持股提問需先解鎖上方戰術金庫。")
        if st.button("🚀 發送戰術提問", type="primary", key="btn_qa_send", disabled=locked_holdings):
            if not question.strip():
                st.warning("請先輸入問題。")
            else:
                sid = None
                try:
                    sid = qsl.resolve(stock_q)[0] if scope == "stock" else None
                except LookupError as exc:
                    st.warning(str(exc))
                if scope != "stock" or sid:
                    with st.spinner(f"WAR ROOM 討論中（{current_engine()}・單次四人設請求）…"):
                        run_ai("WAR ROOM 戰術提問", ftr.ask_war_room, question.strip(), sid, scope, current_engine())
                    st.rerun()
        qa = read_json(ftr.QA_PATH)
        if not qa:
            st.caption("尚無提問紀錄。送出後，四格戰術對話框會顯示在這裡。")
            return
        stamp(qa.get("asked_at"), ftr.QA_PATH, f"引擎 {qa.get('engine', 'claude')}")
        if qa.get("scope") == "holdings" and not is_unlocked():
            st.caption("🔐 最近一次為持股提問，金庫鎖定中不顯示內容。")
            return
        st.markdown(f"**Q（{ui.esc(qa.get('asked_at', ''))}・{ui.esc(qa.get('engine', 'claude'))}）：** {mask(qa.get('question', ''))}")
        html(ui.render_qa_board({k: mask(qa.get(k) or "") for k in ("news", "chips", "kline", "maga", "chairman")}))


def render_war_room():
    st.markdown(ui.war_room_css(), unsafe_allow_html=True)
    open_now = market_open_now()
    t1, t2, t7, t3, t6, t4, t5 = st.columns([1.5, 1.5, 1.6, 1.4, 1.9, 2.0, 0.4], vertical_alignment="center")
    t1.button("⬅️ 離開 WAR ROOM (返回日常主畫面)", width="stretch", on_click=_leave_war_room, key="btn_leave_war_room")
    t7.button("🧪 切換至 SIMULATION ROOM", width="stretch", on_click=goto_view, args=("sim_room",), key="btn_war_to_sim")
    with t2:
        html(f'<div class="wr-title">🎖️ WAR ROOM</div><div style="margin-top:2px">'
             f'{ui.pill("🟢 台股盤中", "bull") if open_now else ui.pill("🌙 台股盤後", "neutral")} '
             f'<span class="tl-time">{datetime.now():%Y-%m-%d %H:%M}</span></div>')
    t3.toggle("🟢 盤中每 10 秒自動更新報價", value=True, key="auto_refresh",
              help="僅在實際盤中（09:00–13:35）每 10 秒以 Yahoo + MIS 雙引擎刷新持股與建議買進價監控表（0 Token）")
    with t6:
        engine_selector("war")
    with t4:
        run_all = st.button("🚨 戰情室全員同步更新 (含昨日落差覆盤)", width="stretch", key="btn_war_sync")
    intraday_mode = st.session_state.get("intraday_mode", open_now)
    with t5:
        ai_tasks_popover()
    engine_key_box("war")
    daemon_strip()
    if run_all:
        war_room_update()
    task_banner()

    conference_table()
    member_panel(intraday_mode)
    holdings_section()
    st.markdown("### 🎯 戰術表二：半導體 20 大龍頭建議買進價監控表 — 僅顯示「達建議買進價」與「接近買進價 (差距1.5%內)」")
    with st.container(border=True):
        radar_table()
    qa_section()
    st.caption("資料來源：FinMind、證交所 MIS、Yahoo Finance、鉅亨網、Google News｜僅供研究參考，非投資建議。")


# ================================================================ 🧪 視圖三：SIMULATION ROOM

SIM_RUN_EVERY = "10s"  # 畫面每 10 秒重讀帳本；報價與下單由伺服器端背景守護執行緒負責
SIM_ACTION_LABEL = {"BUY": "🟢 新建倉", "ADD": "➕ 加碼", "TRIM": "✂️ 減碼", "EXIT_PROFIT": "💰 停利出清",
                    "EXIT_STOP": "🛑 停損出清", "HOLD": "⏸️ 續抱", "WATCHLIST_PLAN": "📝 明日預備劇本"}


def lot_note(shares, board_price, odd_price=None):
    """持倉／交易的整張與零股價別標註：【整張價 $X】×N 股＋【零股價 $Y】×M 股。"""
    board, odd = sim.split_lots(int(shares or 0))
    parts = []
    if board:
        parts.append(f"【整張價 {ui.fmt_price(board_price)}】×{board:,}")
    if odd:
        parts.append(f"【零股價 {ui.fmt_price(odd_price or board_price)}】×{odd:,}")
    return "＋".join(parts) or "—"


def daemon_pills(view):
    if view["cloud"]:  # 觀看端：雲端盤中靜默，只有成交與收盤才把心跳 commit 回 GitHub，看不到即時心跳屬正常
        alive = (ui.pill("☁️ 雲端盯盤運行中", "bull") if view["alive"] else
                 ui.pill("☁️ 雲端靜默盯盤（成交或收盤才同步）", "neutral") if view["phase"] == "TRADING" else ui.pill("🌙 雲端待命", "neutral"))
        age = f"（{view['age_sec'] // 60} 分鐘前）" if view["age_sec"] is not None else ""
    else:
        alive = ui.pill("🟢 背景常駐運行中", "bull") if view["alive"] else ui.pill("🔴 背景引擎未回應", "bear")
        age = f"（{view['age_sec']} 秒前）" if view["age_sec"] is not None else ""
    beat = f"🕒 背景心跳：{(view['last_heartbeat'] or '—')[11:]}{age}"
    scans = ui.pill(f"已掃描 {view['scans']} 次", "neutral")
    trig = ui.pill(f"自動觸發 {view['triggers']} 次・成交 {view['trades']} 筆", "amber")
    phase = ui.pill(view["phase_text"], "bull" if view["phase_text"].startswith("⚡") else "neutral")
    return f"{alive} {ui.pill(beat, 'info')} {scans} {trig} {phase}"


def daemon_strip():
    """頂部導航列下方的背景盯盤引擎狀態列（三個視圖共用）。"""
    title = ("☁️ <b>模擬室雲端自動盯盤（本機只看不下單）</b>" if VIEWER else "☁️ <b>模擬室雲端自動盯盤（Streamlit 主力）</b>") \
        if CLOUD_MODE else ("👀 <b>模擬室觀看端（交易由本機電腦執行）</b>" if VIEWER else "🤖 <b>模擬室背景自動盯盤引擎</b>")
    html(f'<div style="margin:2px 0 6px 0;font-size:0.82rem">{title}　{daemon_pills(DAEMON_VIEW)}</div>')


def cloud_sync_controls():
    """雲端主控：顯示 GitHub 帳本同步狀態與最近一次 GitHub Actions 排程摘要；觀看端提供立即同步按鈕。"""
    syn = ghsync.sync_status()
    run = (sim._read(ROOT / "reports" / "cloud_autopilot_state.json", {}) or {}).get("last_run") or {}
    info = [f"GitHub 帳本同步：{'✅' if syn.get('ok') else '⚠️'} {syn.get('synced_at') or syn.get('checked_at') or '尚未同步'}"
            + (f"（commit {syn['commit']}・{syn.get('method')}）" if syn.get("commit") else "")]
    if syn.get("error"):
        info.append(f"同步錯誤：{syn['error']}")
    last_sync = DAEMON_VIEW["raw"].get("last_sync") or {}
    if last_sync:
        info.append(f"最近推送：{(last_sync.get('at') or '')[11:]} {last_sync.get('what')} {'✅' if last_sync.get('ok') else '⚠️'}"
                    f" {last_sync.get('detail') or ''}".strip())
    if run:
        info.append(f"最近一次 GitHub Actions 排程：{run.get('finished_at')}"
                    + (f"｜錯誤 {len(run['errors'])} 筆" if run.get("errors") else ""))
    st.caption("　｜　".join(mask(i) for i in info))
    c1, c2 = st.columns([2, 1.6], vertical_alignment="center")
    if IS_TRADER:
        c1.caption("☁️ 本伺服器醒著時每 15 秒盯盤（GitHub Actions 每 30 分鐘也會檢查一次）：交易前先取回 GitHub 最新帳本，"
                   "只有實際成交與收盤結算才寫回 GitHub。"
                   "要暫停自動交易，請在 GitHub repo 的 Variables 把 AUTOPILOT_MODE 改為 local_master，並重新啟動 Streamlit app。")
        return
    c1.caption("👀 觀看端：背景每 30 秒從 GitHub 拉回雲端帳本，這台電腦不會發起任何模擬下單。"
               "要改回本機交易，請在 .env 設 AUTOPILOT_MODE=local_master。")
    if c2.button("☁️ 立即從 GitHub 同步最新雲端帳本", width="stretch", key="btn_cloud_pull"):
        with st.spinner("從 GitHub 拉取最新模擬帳本…"):
            r = ghsync.pull_latest(force=True, paths=ghsync.MIRROR_PATHS, scope="mirror")
        st.session_state["toast"] = (f"☁️ 已同步 commit {r.get('commit')}（更新 {r.get('files', 0)} 個檔案）" if r.get("ok")
                                     else f"⚠️ 同步失敗：{r.get('error')}")
        st.rerun()


def daemon_panel():
    """SIMULATION ROOM 內的【🤖 模擬室背景自動盯盤引擎狀態】：狀態徽章＋自動進出場開關＋連線測試（雲端模式改為同步狀態）。"""
    view = sim_daemon.heartbeat_view(cloud=CLOUD_MODE)
    with st.container(border=True):
        st.markdown("#### 【☁️ 模擬室雲端自動盯盤狀態】" if CLOUD_MODE else "#### 【🤖 模擬室背景自動盯盤引擎狀態】")
        html(f"<div>{daemon_pills(view)}</div>")
        raw = view["raw"]
        notes = [f"狀態：{raw.get('status', '尚未啟動')}"]
        if raw.get("last_trigger"):
            notes.append("最近觸發：" + raw["last_trigger"]["at"][11:] + "｜"
                         + "、".join(f"{i['label']} {i.get('stock_id') or ''}".strip() for i in raw["last_trigger"]["items"]))
        if raw.get("last_meeting"):
            m = raw["last_meeting"]
            notes.append(f"最近背景投決會：{m['at'][11:]}｜成交 {m.get('executed', 0)} 筆、否決 {m.get('rejected', 0)} 筆"
                         + (f"｜錯誤：{m['error']}" if m.get("error") else ""))
        if raw.get("last_settlement"):
            s = raw["last_settlement"]
            notes.append(f"最近收盤結算：{s['date']}（{s['at'][11:]}）總淨值 "
                         + (ui.fmt_money(s["total_equity"]) if s.get("total_equity") else "N/A"))
        if raw.get("last_error"):
            notes.append(f"⚠️ 最近錯誤（{raw['last_error']['at'][11:]}・{raw['last_error']['where']}）：{raw['last_error']['error']}")
        st.caption("　｜　".join(mask(n) for n in notes))
        if CLOUD_MODE or VIEWER:
            cloud_sync_controls()
            return
        c1, c2 = st.columns([2, 1.6], vertical_alignment="center")
        c1.toggle("🟢 啟用背景自動進出場 (至 13:30 收盤)", value=view["auto_trade_enabled"], key="daemon_auto_trade",
                  on_change=lambda: sim_daemon.save_config(auto_trade_enabled=st.session_state["daemon_auto_trade"]),
                  help="開啟：盤中觸及停損/停利、達建議買進價或每 20 分鐘巡檢時，背景自動召開五人投決會並交割。"
                       "關閉：背景仍每 15 秒更新報價與淨值，但不自動下單。設定存在 reports/simulation/daemon_config.json")
        if c2.button("🔍 立即測試背景心跳與掃描連線", width="stretch", key="btn_daemon_ping"):
            if not sim_daemon.running_thread():
                sim_daemon.start_daemon()
            with st.spinner("等待背景守護執行緒回應（最多 12 秒）…"):
                ping = sim_daemon.wait_ping(sim_daemon.request_ping())
            if ping is None:
                st.error("❌ 12 秒內沒有回應：背景守護執行緒未運行，請重新啟動 start_war_room.bat。")
            elif ping.get("ok"):
                st.success(f"✅ 背景引擎存活（PID {view['pid']}），{ping['acked_at'][11:]} 回應｜報價連線 {ping['latency_sec']} 秒｜{ping['probe']}")
            else:
                st.warning(f"⚠️ 背景引擎存活，但報價連線失敗：{ping['probe']}")


@st.fragment(run_every=SIM_RUN_EVERY)
def sim_live_board():
    """儀表板＋持倉表＋TOP 20 候選股掃描。盤中由背景守護執行緒每 15 秒更新；背景引擎沒回應時才由畫面代為刷新。"""
    # 觀看端：帳本由 SimLedgerMirror 每 30 秒從 GitHub 拉回，畫面只讀；交易者的背景引擎沒回應時才由畫面代為刷新報價
    if IS_TRADER and market_open_now() and not sim_daemon.heartbeat_view()["alive"]:
        try:
            with sim.ledger_lock(timeout=1):
                sim.refresh_sim_intraday()
        except Exception as exc:  # 盯盤失敗沿用上一筆
            st.warning(f"盯盤刷新失敗，沿用上一筆：{exc}")
    acct = sim.load_account()
    scan = sim.load_scan()
    st.markdown("### 🏆 年底目標儀表板")
    stamp(acct.get("updated_at"), sim.ACCOUNT_PATH, f"目標 {acct['target_date']} 達 +{acct['target_return_low_pct']:.0f}%～"
          f"+{acct['target_return_high_pct']:.0f}%（$1,100,000～$1,200,000）")
    if acct.get("status_note"):
        (st.info if acct["status_note"].startswith("⏳") else st.success)(acct["status_note"])
    html(ui.render_sim_kpis(acct, sim.goal_status(acct), sim.trade_stats()))
    for a in scan.get("alerts") or []:
        (st.error if a["type"] == "STOP" else st.success)(f"{'🛑' if a['type'] == 'STOP' else '🎯'} {a['stock_id']}：{a['text']}")
    left, right = st.columns([1.05, 1])
    with left:
        st.markdown("#### 💼 目前模擬持倉")
        if acct["holdings"]:
            df = pd.DataFrame([{
                "代號": h["stock_id"], "名稱": h.get("name"), "股數": h["shares"], "買進均價": h["avg_cost"],
                "現價": to_float(h.get("current_price")), "漲跌%": to_float(h.get("change_pct")),
                "價別": lot_note(h["shares"], h.get("current_price"), h.get("odd_price")),
                "市值": h.get("market_value"), "未實現損益": h.get("unrealized_pnl"), "未實現%": h.get("unrealized_pct"),
                "防守停損": h.get("stop_loss"), "目標停利": h.get("target_price"), "建倉理由": mask(h.get("buffett_thesis") or "")}
                for h in acct["holdings"]])
            st.dataframe(money_table(df, money=["市值"], price=["買進均價", "現價", "防守停損", "目標停利"],
                                     signed_money=["未實現損益"], signed_pct=["漲跌%", "未實現%"]),
                         width="stretch", hide_index=True, column_config={"建倉理由": st.column_config.TextColumn(width="large"),
                                                                           "價別": st.column_config.TextColumn(width="medium")})
        else:
            st.info("目前空手（100% 現金 $1,000,000）。台股盤中按「🧠 召開五人投決會」由巴董事長挑選建倉標的。")
    with right:
        st.markdown("#### 📡 TOP 20 半導體龍頭 — 建議買進價監控表")
        stamp(scan.get("updated_at"), sim.SCAN_PATH, f"監控表報價 {scan.get('radar_updated_at', 'N/A')}")
        prey = scan.get("prey") or []
        if prey:
            df = pd.DataFrame([{
                "代號": p["stock_id"], "名稱": p.get("name"), "買進評分": p.get("score"), "燈號": radar_mod.SIGNAL_LABELS.get(p.get("signal"), p.get("signal_label")),
                "現價": to_float(p.get("price")), "漲跌%": to_float(p.get("change_pct")), "距買進價%": to_float(p.get("dist_to_entry_pct")),
                "100股需": (to_float(p.get("price")) or 0) * 100,
                "籌碼王": f"{p.get('chip_score', 0):+d}｜{(p.get('chip_trend') or '')[:14]}",
                "K線技術人": f"{p.get('zhu_trend') or 'N/A'}" + (f"｜{p['zhu_reversal']}" if p.get("zhu_reversal") else ""),
                "五檔比": to_float(p.get("bid_ask_ratio")), "風報比": to_float(p.get("risk_reward")),
                "持有": "✅" if p.get("held") else ""} for p in prey])
            st.dataframe(money_table(df, money=["100股需"], price=["現價"], signed_pct=["漲跌%", "距買進價%"]), width="stretch",
                         hide_index=True, height=420, column_config={"五檔比": st.column_config.NumberColumn(format="%.2f"),
                                                                    "風報比": st.column_config.NumberColumn(format="%.2f")})
        else:
            st.caption("尚無掃描結果，請按「⚡ 1秒刷新」。")


SIM_STATUS = {"executed": "✅ 已成交", "rejected": "⛔ 系統風控否決", "hold": "⏸️ 續抱", "plan": "📝 明日預備劇本"}
SIM_MODE_LABEL = {"intraday_hunt": "盤中交易", "daily_rebalance": "每日再平衡", "watchlist_plan": "盤後觀察（明日預備劇本）"}


def sim_meeting_section():
    meeting = sim.load_meeting()
    st.markdown("### 🏛️ 最新一場「五人投決會討論 & 巴董事長最終拍板」全紀錄")
    if not meeting:
        st.info("尚未召開投決會。")
        return
    stamp(meeting.get("held_at"), sim.MEETING_PATH,
          f"{SIM_MODE_LABEL.get(meeting.get('mode'), meeting.get('mode'))}｜引擎 {meeting.get('engine', 'N/A')}")
    if meeting.get("trading_allowed") is False:
        st.warning("📝 本場在非盤中時段召開，只產出明日開盤預備劇本（WATCHLIST_PLAN），沒有執行任何交割。")
    cands = "、".join(f"{c['stock_id']}{c.get('name') or ''}({c.get('score')})" + ("💼" if c.get("held") else "")
                      for c in meeting.get("candidates") or [])
    st.caption(f"🎯 本場候選（Python 量化篩選，括號為買進評分）：{cands}")
    html(ui.render_committee({**meeting, "members": {k: mask(v) for k, v in (meeting.get("members") or {}).items()},
                              "chairman": {k: mask(v) for k, v in (meeting.get("chairman") or {}).items()},
                              "reflection": mask(meeting.get("reflection") or "")}))
    st.markdown("#### 👑 巴董事長拍板指令")
    rows = [{"代號": d.get("stock_id"),
             "提案": SIM_ACTION_LABEL.get(d.get("proposed_action") or d.get("action"), d.get("proposed_action")),
             "結果": SIM_STATUS.get(d.get("status"), d.get("status")), "股數": d.get("shares"), "價格": d.get("price"),
             "停損": d.get("stop_loss"), "目標": d.get("target_price"), "風報比": d.get("rr"),
             "巴董事長理由 / 風控結果": mask(" ｜ ".join(x for x in (d.get("reason"), d.get("reject_reason"), d.get("note")) if x)),
             "成員共識": mask(d.get("team_view") or "")} for d in meeting.get("decisions") or []]
    if rows:
        st.dataframe(money_table(pd.DataFrame(rows), price=["價格", "停損", "目標"]), width="stretch", hide_index=True,
                     column_config={"巴董事長理由 / 風控結果": st.column_config.TextColumn(width="large"),
                                    "成員共識": st.column_config.TextColumn(width="large")})
    else:
        st.caption("巴董事長本場未下任何指令（按兵不動）。")


def sim_history_section():
    st.markdown("### 📜 歷史交易流水帳 & 淨值走勢")
    trades, nav = sim.load_trades(), sim.load_nav()
    stamp(trades[-1]["timestamp"] if trades else None, sim.TRADES_PATH, f"共 {len(trades)} 筆")
    acct = sim.load_account()
    cap = acct["initial_capital"]
    fig = go.Figure()
    if nav:
        fig.add_trace(go.Scatter(x=[n["date"] for n in nav], y=[n["total_equity"] for n in nav], mode="lines+markers",
                                 name="總資產淨值", line=dict(color="#10B981", width=2.4)))
    for y, label, color, dash in ((cap, "$1,000,000 基準", "#94A3B8", "dot"),
                                  (cap * (1 + acct["target_return_low_pct"] / 100), "$1,100,000 低標", "#F59E0B", "dash"),
                                  (cap * (1 + acct["target_return_high_pct"] / 100), "$1,200,000 高標", "#EF4444", "dash")):
        fig.add_hline(y=y, line_dash=dash, line_color=color, line_width=1.3, annotation_text=label, annotation_position="top left")
    fig.update_layout(template="plotly_dark", height=340, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#0F172A",
                      margin=dict(l=10, r=10, t=30, b=10), xaxis=dict(range=[acct["start_date"], acct["target_date"]]),
                      yaxis=dict(tickformat="$,.0f"), font=dict(family="Inter, Noto Sans TC"))
    st.plotly_chart(fig, width="stretch")
    if trades:
        df = pd.DataFrame([{
            "編號": t["trade_id"], "時間": t["timestamp"], "代號": t["stock_id"], "名稱": t.get("name"),
            "動作": SIM_ACTION_LABEL.get(t["action"], t["action"]), "股數": t["shares"],
            "價別": t.get("price_note") or lot_note(t["shares"], t["price"]), "均價": t["price"],
            "成交金額": t["gross_amount"], "手續費+稅": t["fee_and_tax"], "實現損益": t["realized_pnl"], "交易後現金": t["cash_after"],
            "五人共識": mask(t.get("team_discussion") or ""), "巴董事長拍板": mask(t.get("buffett_verdict") or "")}
            for t in reversed(trades)])
        st.dataframe(money_table(df, money=["成交金額", "手續費+稅", "交易後現金"], price=["均價"], signed_money=["實現損益"]),
                     width="stretch", hide_index=True, column_config={
                         "價別": st.column_config.TextColumn(width="medium"),
                         "五人共識": st.column_config.TextColumn(width="large"),
                         "巴董事長拍板": st.column_config.TextColumn(width="large")})
    else:
        st.caption("尚無任何模擬交易。")


def render_sim_room():
    sim.init_account()
    trading = sim.is_tw_market_trading_hours()
    t1, t2, t3, t4, t5 = st.columns([1.4, 1.4, 2.0, 2.0, 2.1], vertical_alignment="center")
    t1.button("⬅️ 返回日常主畫面", width="stretch", on_click=goto_view, args=("home",), key="btn_sim_home")
    t2.button("🎖️ 切換至 WAR ROOM", width="stretch", on_click=goto_view, args=("war_room",), key="btn_sim_to_war")
    with t3:
        html('<div class="wr-title">🧪 SIMULATION ROOM</div>'
             f'<div style="margin-top:2px">{ui.pill("🟢 台股盤中・開放模擬下單", "bull") if trading else ui.pill("🔒 非盤中・禁止交易", "neutral")} '
             f'<span class="tl-time">{datetime.now():%Y-%m-%d %H:%M}</span></div>')
    with t4:
        engine_selector("sim")
    with t5:
        state = ("☁️ 雲端運行中" if CLOUD_MODE else "🟢 運行中") if DAEMON_VIEW["alive"] else \
            ("🌙 待命" if DAEMON_VIEW["phase"] != "TRADING" else "🔴 未回應")
        html(f'<div style="font-size:0.8rem">{"☁️ 雲端盯盤" if CLOUD_MODE else "🤖 背景盯盤"}：{state}<br>'
             f'<span class="tl-time">心跳 {(DAEMON_VIEW["last_heartbeat"] or "—")[11:]}｜已掃描 {DAEMON_VIEW["scans"]} 次</span></div>')
    engine_key_box("sim")
    daemon_panel()
    c1, c2 = st.columns([1.4, 2])
    lock_ui = CLOUD_MODE or VIEWER
    cloud_help = ("☁️ 單一帳本：模擬交易只由背景盯盤執行緒執行（交易前後與 GitHub 同步），網頁按鈕不寫入帳本，避免兩本帳"
                  if lock_ui else None)
    if c1.button("⚡ 1秒刷新模擬房淨值與 TOP 20 建議買進價監控表 (0 Token)", width="stretch", key="btn_sim_refresh",
                 disabled=lock_ui, help=cloud_help):
        with st.spinner("刷新模擬持倉與 20 強建議買進價監控表…"):
            try:
                with sim.ledger_lock(timeout=30):
                    scan = sim.refresh_sim_intraday()
                st.session_state["toast"] = f"⚡ 盯盤完成（{scan['elapsed_sec']} 秒）"
            except sim.SimError as exc:
                st.session_state["toast"] = f"⏳ {exc}"
        st.rerun()
    label = ("🧠 召開五人投決會 ➔ 巴董事長拍板下單 (搜尋 TOP 20 交易機會)" if trading else
             "🔒 非盤中時段禁止交易（明日 09:00 開盤後開放實盤模擬下單，目前僅供預覽明日選股計畫）")
    if c2.button(label, width="stretch", key="btn_sim_meeting", disabled=lock_ui, help=cloud_help):
        mode = "intraday_hunt" if trading else "watchlist_plan"
        with st.spinner(f"五人投決會進行中（{current_engine()}・{'盤中下單' if trading else '只產出明日預備劇本'}）…"):
            try:
                with sim.ledger_lock(timeout=120):  # 背景投決會進行中時排隊等候，避免兩場同時交割
                    run_ai("五人投決會", ftr.sim_trade_session, mode, current_engine())
            except sim.SimError as exc:
                remember_run("五人投決會", error=exc)
        st.rerun()
    if not trading:
        st.caption("🔒 交易時段：台股週一至週五 09:00–13:30。非盤中按下投決會只會產出「明日開盤預備劇本」，不會扣款交割（國定假日未納入判斷）。")
    task_banner()
    sim_live_board()
    sim_meeting_section()
    sim_history_section()
    st.caption("🧪 模擬交易以證交所 MIS 最新真實成交價交割：整張用整張價、零股（不足一張的部分）用盤中零股報價；"
               "手續費 0.1425%×0.6（每筆最低 $20）、證交稅 0.3%｜與真實持股完全分開，僅供研究，非投資建議。")


# ---------------------------------------------------------------- 進入點

VIEWS = {"home": render_daily, "war_room": render_war_room, "sim_room": render_sim_room}
VIEWS.get(st.session_state["view"], render_daily)()
