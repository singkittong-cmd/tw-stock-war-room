# -*- coding: utf-8 -*-
"""🧪 SIMULATION ROOM：100 萬模擬基金帳本與交易引擎（0 Token）。

目標：2026-11-05 起以 NT$1,000,000 操作，2026-12-31 前總報酬 +10%～+20%（110 萬～120 萬）。

用法：
    C:/Users/singk/anaconda3/python.exe tools/sim_portfolio_engine.py --init      # 建立帳本（已存在則不覆蓋）
    C:/Users/singk/anaconda3/python.exe tools/sim_portfolio_engine.py --refresh   # 盤中盯盤：更新持倉現價、淨值與 20 強候選股掃描
    C:/Users/singk/anaconda3/python.exe tools/sim_portfolio_engine.py --status    # 顯示帳本摘要

檔案（reports/simulation/）：
- sim_account.json：現金、持倉、已實現／未實現損益、總淨值與報酬率。
- sim_trades.json：每筆模擬交易（含五人討論摘要與巴董事長拍板理由）。
- sim_nav_history.json：每日淨值（同一天多次更新只保留最新一筆）。
- sim_scan_live.json：最近一次盯盤結果（持倉警示＋20 強候選股）。
- latest_committee_meeting.json：最近一次五人投決會全紀錄（由 fast_team_runner 寫入）。

費用：買賣手續費 0.1425% × 0.6（電子下單折讓）、最低 20 元；賣出證交稅 0.3%。
與使用者真實持股（reports/portfolio.json）完全分開，不讀取也不寫入真實部位。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
import math
import os
import time
from contextlib import contextmanager
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORTS = ROOT / "reports"
SIM_DIR = REPORTS / "simulation"
ACCOUNT_PATH = SIM_DIR / "sim_account.json"
TRADES_PATH = SIM_DIR / "sim_trades.json"
NAV_PATH = SIM_DIR / "sim_nav_history.json"
SCAN_PATH = SIM_DIR / "sim_scan_live.json"
MEETING_PATH = SIM_DIR / "latest_committee_meeting.json"
LEDGER_LOCK_PATH = SIM_DIR / "sim_ledger.lock"
TIME_FMT = "%Y-%m-%d %H:%M:%S"

START_DATE = "2026-11-05"
TARGET_DATE = "2026-12-31"
INITIAL_CAPITAL = 1_000_000
TARGET_LOW_PCT, TARGET_HIGH_PCT = 10.0, 20.0
FEE_RATE = 0.001425 * 0.6
MIN_FEE = 20
TAX_RATE = 0.003
POSITION_MIN, POSITION_MAX = 150_000, 300_000   # 單一持股部位 15 萬～30 萬
CASH_RESERVE_PCT = 10.0                          # 至少保留總淨值 10% 現金彈性
LOT_UNIT = 100                                   # 下單單位：100 股（零股）或 1,000 股（整張）
BUY_ACTIONS, SELL_ACTIONS = ("BUY", "ADD"), ("TRIM", "EXIT_PROFIT", "EXIT_STOP")


BOARD_LOT = 1000                                 # 整張 1,000 股；不足一張的部分走盤中零股撮合池

# ---------------------------------------------------------------- 動態操盤規則（tools/strategy_optimizer.py 每日覆盤後更新）
RULES_PATH = SIM_DIR / "strategy_rules.json"
DEFAULT_RULES = {  # 預設值＝導入規則引擎前的行為（寬鬆上限），第一次高階覆盤後才依證據收緊
    "max_single_position_pct": 30.0,   # 單一持股最高占總淨值 %（30% ≈ 原本的 30 萬上限）
    "stop_loss_pct": 15.0,             # 硬性停損：持股虧損達 X% 系統直接出清；新買進的停損價不得低於買價 −X%
    "take_profit_pct": 40.0,           # 分批停利目標：目標價不得高於買價 +X%（觸及即召開投決會討論減碼）
    "cash_reserve_pct": 10.0,          # 平常最低現金水位 %
    "min_cash_reserve_pct": 10.0,      # 大盤偏弱（加權跌破月線或空頭趨勢）時強制保留的最低現金水位 %
    "avoid_chase_pct": 10.0,           # 禁止追高：當日已上漲 ≥ X% 不買進、不加碼（10% 即漲停附近）
    "min_hold_minutes": 0,             # 防過度交易：買進後至少持有 X 分鐘才可減碼／出清（虧損達硬性停損 % 除外）
    "disable_day_trading": False,      # 防過度交易：同一檔當日買進後不得當日賣出、當日賣出後不得當日再買（硬性停損除外）
    "cooldown_minutes_after_stop_loss": 0,  # 停損出清後，同一檔 X 分鐘內不得再買進（避免剛停損又追高）
    "agent_weights": {"news": 1.0, "chips": 1.0, "kline": 1.0, "maga": 1.0},
    "custom_disciplines": [],          # 巴董事長自訂鐵律（每場投決會強制注入 Prompt）
}
RULE_BOUNDS = {"max_single_position_pct": (5.0, 40.0), "stop_loss_pct": (2.0, 15.0), "take_profit_pct": (3.0, 40.0),
               "cash_reserve_pct": (0.0, 50.0), "min_cash_reserve_pct": (0.0, 80.0), "avoid_chase_pct": (1.0, 10.0),
               "min_hold_minutes": (0.0, 1440.0), "cooldown_minutes_after_stop_loss": (0.0, 4320.0)}
INT_RULES = ("min_hold_minutes", "cooldown_minutes_after_stop_loss")
WEIGHT_BOUNDS = (0.2, 2.0)
AGENT_KEYS = ("news", "chips", "kline", "maga")


def clamp_rules(raw):
    """把任何來源（預設、檔案、模型輸出）的規則轉成合法值：缺的補預設、超出範圍的夾到邊界、型別不對的用預設。"""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key, (lo, hi) in RULE_BOUNDS.items():
        try:
            out[key] = round(min(hi, max(lo, float(raw.get(key, DEFAULT_RULES[key])))), 2)
        except (TypeError, ValueError):
            out[key] = DEFAULT_RULES[key]
    out["min_cash_reserve_pct"] = max(out["min_cash_reserve_pct"], out["cash_reserve_pct"])  # 弱勢水位不得低於平常水位
    for key in INT_RULES:
        out[key] = int(round(out[key]))
    dt = raw.get("disable_day_trading", DEFAULT_RULES["disable_day_trading"])
    out["disable_day_trading"] = dt.strip().lower() in ("true", "1", "yes", "是", "啟用") if isinstance(dt, str) else bool(dt)
    weights = raw.get("agent_weights") if isinstance(raw.get("agent_weights"), dict) else {}
    out["agent_weights"] = {}
    for k in AGENT_KEYS:
        try:
            out["agent_weights"][k] = round(min(WEIGHT_BOUNDS[1], max(WEIGHT_BOUNDS[0], float(weights.get(k, 1.0)))), 2)
        except (TypeError, ValueError):
            out["agent_weights"][k] = 1.0
    disc = raw.get("custom_disciplines")
    disc = disc if isinstance(disc, list) else [disc] if isinstance(disc, str) and disc.strip() else []
    out["custom_disciplines"] = [str(x).strip()[:120] for x in disc if str(x).strip()][:8]
    return out


def load_strategy_rules():
    """讀取 reports/simulation/strategy_rules.json（不存在或損毀時用預設），回傳已驗證的規則＋版本資訊。"""
    data = _read(RULES_PATH, {}) if RULES_PATH.exists() else {}
    data = data if isinstance(data, dict) else {}
    rules = clamp_rules(data.get("rules") if isinstance(data.get("rules"), dict) else data)
    rules["_version"] = data.get("version", 0)
    rules["_updated_at"] = data.get("updated_at")
    return rules


def market_is_weak():
    """大盤偏弱：加權指數收盤跌破月線，或趨勢判讀含「空頭」。回傳 (是否偏弱, 說明)。"""
    t = (_read(REPORTS / "market_close_summary.json", {}) or {}).get("taiex") or {}
    close, ma20, trend = t.get("close"), t.get("ma20"), str(t.get("trend") or "")
    if "空頭" in trend:
        return True, f"加權趨勢 {trend}"
    if close and ma20 and close < ma20:
        return True, f"加權 {close:,.0f} 跌破月線 {ma20:,.0f}"
    return False, (f"加權 {close:,.0f} 站上月線 {ma20:,.0f}" if close and ma20 else "大盤資料不足，以平常水位計")


def nav_has(date_str):
    """sim_nav_history.json 是否已有該日的收盤結算紀錄（任何主機結算過、且已同步到這台都算）。"""
    nav = _read(NAV_PATH, []) if NAV_PATH.exists() else []
    return any(isinstance(n, dict) and n.get("date") == date_str for n in (nav if isinstance(nav, list) else []))


def meeting_date():
    """最近一場投決會的日期（YYYY-MM-DD），沒有時回傳空字串。"""
    m = _read(MEETING_PATH, {}) if MEETING_PATH.exists() else {}
    return str((m or {}).get("held_at") or "")[:10] if isinstance(m, dict) else ""


def cash_reserve_pct(rules=None):
    rules = rules or load_strategy_rules()
    return rules["min_cash_reserve_pct"] if market_is_weak()[0] else rules["cash_reserve_pct"]


def position_max(acct, rules=None):
    """單一持股上限（元）＝總淨值 × max_single_position_pct。"""
    rules = rules or load_strategy_rules()
    return float(acct.get("total_equity") or INITIAL_CAPITAL) * rules["max_single_position_pct"] / 100


def position_min(acct, rules=None):
    """新建倉下限（元）：原本 15 萬，但不得高於單一持股上限的 60%（上限調降時才買得進去）。"""
    return min(POSITION_MIN, position_max(acct, rules) * 0.6)


def rules_prompt(acct, rules=None):
    """投決會 Prompt 用：把最新操盤鐵律寫成條列，數字已換算成元。"""
    rules = rules or load_strategy_rules()
    weak, why = market_is_weak()
    reserve = cash_reserve_pct(rules)
    w = rules["agent_weights"]
    names = {"news": "新聞達人", "chips": "籌碼王", "kline": "K線技術人", "maga": "川大帝MAGA"}
    lines = [f"【巴董事長最新操盤鐵律（strategy_rules v{rules['_version']}，{rules['_updated_at'] or '預設值'}）— 系統會硬性檢查，違反即否決】",
             f"1. 單一持股上限 {rules['max_single_position_pct']:g}% 總淨值（約 {position_max(acct, rules):,.0f} 元）；"
             f"新建倉下限約 {position_min(acct, rules):,.0f} 元；股數為 100 的倍數。",
             f"2. 硬性停損 −{rules['stop_loss_pct']:g}%：新買進的停損價不得低於買價 −{rules['stop_loss_pct']:g}%，持股虧損達此幅度由系統直接出清。",
             f"3. 分批停利目標 +{rules['take_profit_pct']:g}%：目標價不得高於買價 +{rules['take_profit_pct']:g}%。",
             f"4. 最低現金水位 {reserve:g}%（{'大盤偏弱' if weak else '大盤正常'}：{why}）。",
             f"5. 禁止追高：當日已上漲 ≥ {rules['avoid_chase_pct']:g}% 的個股不買進、不加碼。",
             "6. 防過度交易：" + "；".join(x for x in (
                 f"買進後至少持有 {rules['min_hold_minutes']} 分鐘才可減碼或出清" if rules["min_hold_minutes"] else "最短持有時間未限制",
                 "禁止當沖（同一檔當日買進不得當日賣出、當日賣出不得當日再買）" if rules["disable_day_trading"] else "",
                 f"停損出清後同一檔 {rules['cooldown_minutes_after_stop_loss']} 分鐘內不得再買進"
                 if rules["cooldown_minutes_after_stop_loss"] else "停損後冷卻未限制") if x)
             + f"（虧損達硬性停損 −{rules['stop_loss_pct']:g}% 一律可立即出清）。",
             "7. 成員發言權重（依近期實盤準確率）：" + "、".join(f"{names[k]} {w[k]:g}" for k in AGENT_KEYS)
             + "；巴董事長拍板時權重高者意見優先，權重 < 1 者的多方論點需要其他成員佐證。"]
    lines += [f"{i}. {d}" for i, d in enumerate(rules["custom_disciplines"], 8)]
    return "\n".join(lines)


def _parse_ts(ts):
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def trade_block_reason(stock_id, action, rules=None, now=None, hard_stop=False, trades=None):
    """防過度交易鐵律（strategy_rules.json）：回傳否決理由字串，可交易時回傳 None。
    - 賣出（TRIM／EXIT_PROFIT／EXIT_STOP）：未滿 min_hold_minutes、或 disable_day_trading 且今日才買進 → 否決；hard_stop=True（虧損達硬性停損 %）一律放行。
    - 買進（BUY／ADD）：停損出清後 cooldown_minutes_after_stop_loss 內、或 disable_day_trading 且今日已賣出 → 否決。"""
    rules = rules or load_strategy_rules()
    now = now or datetime.now()
    action = str(action or "").upper()
    hist = [t for t in (trades if trades is not None else load_trades()) if isinstance(t, dict) and str(t.get("stock_id")) == str(stock_id)]
    today = now.strftime("%Y-%m-%d")
    if action in SELL_ACTIONS:
        if hard_stop:
            return None
        buys = [t for t in hist if t.get("action") in BUY_ACTIONS]
        last_buy = _parse_ts(buys[-1].get("timestamp")) if buys else None
        if last_buy is None:
            return None
        held_min = (now - last_buy).total_seconds() / 60
        if rules["disable_day_trading"] and last_buy.strftime("%Y-%m-%d") == today:
            return (f"【防過度交易】禁止當沖：{stock_id} 今日 {last_buy:%H:%M} 才買進，當日不得賣出"
                    f"（虧損達硬性停損 −{rules['stop_loss_pct']:g}% 除外）")
        if held_min < rules["min_hold_minutes"]:
            return (f"【防過度交易】{stock_id} 買進後才持有 {held_min:.0f} 分鐘，未滿最短持有 {rules['min_hold_minutes']} 分鐘"
                    f"（虧損達硬性停損 −{rules['stop_loss_pct']:g}% 除外）")
        return None
    if action in BUY_ACTIONS:
        sells = [t for t in hist if t.get("action") in SELL_ACTIONS]
        last = sells[-1] if sells else None
        when = _parse_ts(last.get("timestamp")) if last else None
        if when is None:
            return None
        if rules["disable_day_trading"] and when.strftime("%Y-%m-%d") == today:
            return f"【防過度交易】禁止當沖：{stock_id} 今日 {when:%H:%M} 已賣出，當日不得再買進"
        stopped = last.get("action") == "EXIT_STOP" or (_f(last.get("realized_pnl")) or 0) < 0
        wait = rules["cooldown_minutes_after_stop_loss"]
        gone = (now - when).total_seconds() / 60
        if stopped and gone < wait:
            return f"【防過度交易】{stock_id} {when:%m-%d %H:%M} 停損出清，冷卻 {wait} 分鐘內不得再買進（還剩 {wait - gone:.0f} 分鐘）"
    return None
READY_NOTE = "⏳ 模擬室已就緒（現金 $1,000,000），等待明日台股開盤 (09:00) 正式啟動盤中交易"
RUNNING_NOTE = "🟢 模擬室盤中交易運作中"


class SimError(ValueError):
    pass


def _now():
    return datetime.now().strftime(TIME_FMT)


def is_tw_market_trading_hours(now=None):
    """台灣時間開盤日（排除週末與證交所公告休市日）09:00:00–13:30:00 才允許模擬下單。"""
    from tools import tw_market_calendar as cal
    now = now or datetime.now()
    t = now.time()
    return cal.is_trading_day(now) and dtime(9, 0, 0) <= t <= dtime(13, 30, 0)


def lock_file(f):
    """對已開啟的檔案加非阻塞排他鎖（Windows msvcrt／其他平台 fcntl）；被占用時拋出 OSError。"""
    f.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_file(f):
    f.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def ledger_lock(timeout=120):
    """帳本寫入鎖（跨執行緒、跨行程）：背景守護執行緒與 dashboard 同時刷新或下單時，避免互相覆蓋 sim_account.json。"""
    SIM_DIR.mkdir(parents=True, exist_ok=True)
    f = open(LEDGER_LOCK_PATH, "a+b")
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock_file(f)
            break
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                raise SimError("模擬帳本忙碌中（背景投決會或盯盤進行中），請稍後再試")
            time.sleep(0.2)
    try:
        yield
    finally:
        unlock_file(f)
        f.close()


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def fee_of(amount):
    return max(MIN_FEE, math.floor(amount * FEE_RATE)) if amount > 0 else 0


def tax_of(amount):
    return math.floor(amount * TAX_RATE) if amount > 0 else 0


# ---------------------------------------------------------------- 帳本

def init_account(force=False):
    if ACCOUNT_PATH.exists() and not force:
        return load_account()
    acct = {"start_date": START_DATE, "target_date": TARGET_DATE, "initial_capital": INITIAL_CAPITAL,
            "target_return_low_pct": TARGET_LOW_PCT, "target_return_high_pct": TARGET_HIGH_PCT,
            "cash_balance": INITIAL_CAPITAL, "realized_pnl": 0, "unrealized_pnl": 0, "market_value": 0,
            "total_equity": INITIAL_CAPITAL, "total_return_pct": 0.0, "holdings": [], "status_note": READY_NOTE,
            "updated_at": _now()}
    _write(ACCOUNT_PATH, acct)
    _write(TRADES_PATH, [])
    _write(NAV_PATH, [])
    if force:
        _write(MEETING_PATH, {})
        _write(SCAN_PATH, {})
    record_nav(acct)
    return acct


_last_good_account = None


def load_account():
    """讀模擬帳本。檔案存在但暫時讀不到（例如正從 GitHub 同步覆寫中）時重試，並退回上一次讀到的版本，
    絕不因此重建一本新的 100 萬帳本。"""
    global _last_good_account
    for _ in range(5):
        acct = _read(ACCOUNT_PATH, None)
        if acct:
            _last_good_account = acct
            return acct
        if not ACCOUNT_PATH.exists():
            return init_account()
        time.sleep(0.2)
    if _last_good_account:
        return json.loads(json.dumps(_last_good_account))
    raise SimError("模擬帳本暫時無法讀取（同步中），請稍後再試")


def assert_trader():
    """單一帳本防衝突：只有被指定為交易執行者的主機能寫入模擬帳本（AUTOPILOT_MODE=cloud_master 時本機只看不下單）。"""
    from tools import github_memory_sync as ghsync
    if not ghsync.is_trader_here():
        who = "雲端（Streamlit Cloud／GitHub Actions）" if ghsync.cloud_mode() else "本機電腦"
        raise SimError(f"🔒 這台主機是觀看端（AUTOPILOT_MODE={ghsync.autopilot_mode()}），模擬交易只由{who}執行")


def load_trades():
    return _read(TRADES_PATH, [])


def load_nav():
    return _read(NAV_PATH, [])


def split_lots(shares):
    """拆成整張（1,000 股倍數）與盤中零股（餘數）兩個撮合池。"""
    return shares // BOARD_LOT * BOARD_LOT, shares % BOARD_LOT


def position_value(h):
    """持倉市值：整張部分用整張現價、零股部分用盤中零股報價（無零股價時退回整張價）。"""
    board, odd = split_lots(h["shares"])
    board_px = h.get("current_price") or h["avg_cost"]
    odd_px = h.get("odd_price") or board_px
    return round(board * board_px + odd * odd_px)


def recompute(acct):
    """依持倉現價重算市值、未實現淨損益（已預扣賣出手續費與證交稅）、總淨值與報酬率。"""
    mv = unreal = 0
    for h in acct["holdings"]:
        h["market_value"] = position_value(h)
        board, odd = split_lots(h["shares"])
        board_mv = round(board * (h.get("current_price") or h["avg_cost"]))
        odd_mv = h["market_value"] - board_mv
        sell_cost = sum(fee_of(v) + tax_of(v) for v in (board_mv, odd_mv) if v > 0)
        cost_basis = _cost_total(h)
        h["unrealized_pnl"] = round(h["market_value"] - sell_cost - cost_basis)
        h["unrealized_pct"] = round(h["unrealized_pnl"] / cost_basis * 100, 2) if cost_basis else 0.0
        mv += h["market_value"]
        unreal += h["unrealized_pnl"]
    acct["market_value"] = mv
    acct["unrealized_pnl"] = unreal
    acct["total_equity"] = round(acct["cash_balance"] + mv)
    acct["total_return_pct"] = round((acct["total_equity"] - acct["initial_capital"]) / acct["initial_capital"] * 100, 2)
    acct["updated_at"] = _now()
    return acct


def save_account(acct):
    recompute(acct)
    _write(ACCOUNT_PATH, acct)
    record_nav(acct)
    return acct


def record_nav(acct):
    nav = load_nav()
    today = date.today().strftime("%Y-%m-%d")
    row = {"date": today, "timestamp": _now(), "total_equity": acct["total_equity"], "cash": acct["cash_balance"],
           "market_value": acct.get("market_value", 0), "return_pct": acct["total_return_pct"]}
    nav = [n for n in nav if n["date"] != today] + [row]
    _write(NAV_PATH, nav)


def holding_of(acct, stock_id):
    return next((h for h in acct["holdings"] if h["stock_id"] == stock_id), None)


# ---------------------------------------------------------------- 即時報價

def latest_quotes(stock_ids):
    """MIS + Yahoo 雙引擎最新整張價（0 Token）：{代號: {price, change_pct, vwap, name, price_source}}。"""
    from tools import realtime_quote_engine as rq
    ids = list(dict.fromkeys(stock_ids))
    if not ids:
        return {}
    quotes, yahoo, _ = rq.fetch_realtime(ids, max_yahoo_age=20)
    out = {}
    for sid in ids:
        q, y = quotes.get(sid), yahoo.get(sid)
        if not q and not (y and y.get("price")):
            continue
        s = rq.summarize(sid, q, y)
        if s.get("price"):
            out[sid] = s
    return out


def odd_fill(stock_id, side, board_price):
    """盤中零股模擬成交價（getOddInfo.jsp）：z 優先，無成交時買用委賣一、賣用委買一，再不行以整張價加減 2 檔滑價。
    連不到證交所（海外雲端主機）時改用【海外零股估算價】：買進用整張委賣價或現價 +1 檔、賣出用整張委買價或現價 -1 檔。"""
    from tools import realtime_quote_engine as rq
    try:
        odd = rq.fetch_odd_lot_quote(stock_id)
    except rq.MISError:
        bid = ask = None
        try:  # 整張五檔若還連得到，用最佳一檔委買／委賣
            q = rq.fetch_mis_quotes([stock_id]).get(stock_id) or {}
            bid, ask = (q.get("bids") or [None])[0], (q.get("asks") or [None])[0]
        except rq.MISError:
            pass
        return rq.overseas_odd_estimate(side, board_price, bid, ask)
    return rq.odd_lot_fill_price(odd, side, board_price)


# ---------------------------------------------------------------- 交易

def _money(v):
    return f"${v:,.2f}".rstrip("0").rstrip(".") if v % 1 else f"${v:,.0f}"


def execute_trade(stock_id, action, shares, price=None, name=None, stop_loss=None, target_price=None,
                  thesis=None, team_discussion=None, buffett_verdict=None, odd_price=None, enforce_hours=True,
                  hard_stop=False):
    """以最新真實市場價執行模擬交割，回傳交易紀錄；不合規時丟出 SimError。

    - 只在台股盤中（週一至週五 09:00–13:30）允許交割（enforce_hours=False 僅供測試）。
    - 整張部分（1,000 股倍數）用整張價；零股部分（shares % 1000）強制用盤中零股真實報價，手續費依兩筆委託分別計算。
    - 防過度交易鐵律（最短持有、禁止當沖、停損冷卻）由 trade_block_reason 檢查；hard_stop=True 表示虧損已達硬性停損 %，一律放行。"""
    assert_trader()
    if enforce_hours and not is_tw_market_trading_hours():
        raise SimError("🔒 非台股盤中時段（週一至週五 09:00–13:30）禁止模擬下單交割")
    action = action.upper()
    if action not in BUY_ACTIONS + SELL_ACTIONS:
        raise SimError(f"不支援的動作：{action}")
    blocked = trade_block_reason(stock_id, action, hard_stop=hard_stop)
    if blocked:
        raise SimError(blocked)
    shares = int(shares or 0)
    if shares <= 0 or shares % LOT_UNIT:
        raise SimError(f"股數必須為 {LOT_UNIT} 股的倍數（收到 {shares}）")
    if price is None:
        q = latest_quotes([stock_id]).get(stock_id)
        if not q:
            raise SimError(f"{stock_id} 取不到即時報價，不交割")
        price, name = q["price"], name or q.get("name")
    board_price = float(price)
    acct = load_account()
    h = holding_of(acct, stock_id)
    side = "buy" if action in BUY_ACTIONS else "sell"
    if side == "sell":
        if h is None or h["shares"] < shares:
            raise SimError(f"{stock_id} 持股不足（持有 {h['shares'] if h else 0} 股，欲賣 {shares} 股）")
        if action in ("EXIT_PROFIT", "EXIT_STOP"):
            shares = h["shares"]
    board_shares, odd_shares = split_lots(shares)
    odd_src = None
    if odd_shares:
        if odd_price is None:
            odd_price, odd_src = odd_fill(stock_id, side, board_price)
        else:
            odd_src = "指定零股價"
        if not odd_price:
            raise SimError(f"{stock_id} 取不到盤中零股報價，不交割")
        odd_price = float(odd_price)
    board_gross = round(board_price * board_shares)
    odd_gross = round(odd_price * odd_shares) if odd_shares else 0
    gross = board_gross + odd_gross
    fee = sum(fee_of(v) for v in (board_gross, odd_gross) if v > 0)  # 整張、零股為兩筆委託
    note_parts = []  # 只組實際有的部分（整張無零股時 odd_price 為 None，不可先格式化）
    if board_shares:
        note_parts.append(f"【整張價 {_money(board_price)}】×{board_shares:,} 股")
    if odd_shares:
        note_parts.append(f"【零股價 {_money(odd_price)}（{odd_src}）】×{odd_shares:,} 股")
    price_note = "＋".join(note_parts)
    if side == "buy":
        net = gross + fee
        if net > acct["cash_balance"]:
            raise SimError(f"現金不足：需 ${net:,}，可用 ${acct['cash_balance']:,}")
        rules = load_strategy_rules()  # 最後一道：strategy_rules.json 的單檔上限與現金水位，任何來源的買單都要通過
        cap = position_max(acct, rules)
        held_after = (h["shares"] * board_price if h else 0) + gross
        if held_after > cap * 1.03:
            raise SimError(f"【操盤鐵律】單一持股上限 {rules['max_single_position_pct']:g}%（約 ${cap:,.0f}），"
                           f"本筆後將達 ${held_after:,.0f}")
        reserve = float(acct.get("total_equity") or INITIAL_CAPITAL) * cash_reserve_pct(rules) / 100
        if acct["cash_balance"] - net < reserve - 1:
            raise SimError(f"【操盤鐵律】最低現金水位 {cash_reserve_pct(rules):g}%（${reserve:,.0f}），"
                           f"本筆後現金只剩 ${acct['cash_balance'] - net:,.0f}")
        if h is not None and action == "BUY":
            action = "ADD"  # 已持有時一律記為加碼
        if h is None:
            action = "BUY"
            h = {"stock_id": stock_id, "name": name or stock_id, "shares": 0, "avg_cost": 0.0, "cost_total": 0,
                 "entry_date": _now()[:10]}
            acct["holdings"].append(h)
        h["cost_total"] = _cost_total(h) + net  # 總成本（元，含買進手續費），均價只供顯示
        h["shares"] += shares
        h["avg_cost"] = round(h["cost_total"] / h["shares"], 4)
        h["current_price"] = board_price
        if odd_shares:
            h["odd_price"] = odd_price
        for key, val in (("stop_loss", stop_loss), ("target_price", target_price)):
            if _f(val):
                h[key] = float(val)
        if thesis:
            h["buffett_thesis"] = thesis
        acct["cash_balance"] = round(acct["cash_balance"] - net)
        realized, fee_tax, net_amount = 0, fee, -net
    else:
        tax = sum(tax_of(v) for v in (board_gross, odd_gross) if v > 0)
        net = gross - fee - tax
        # 依比例沖銷總成本；全數出清時沖銷剩餘全部成本，累計損益與現金精確對帳
        cost_out = _cost_total(h) if shares == h["shares"] else round(_cost_total(h) * shares / h["shares"])
        realized = net - cost_out
        h["cost_total"] = _cost_total(h) - cost_out
        h["shares"] -= shares
        h["current_price"] = board_price
        if h["shares"] == 0:
            acct["holdings"].remove(h)
        acct["cash_balance"] = round(acct["cash_balance"] + net)
        acct["realized_pnl"] = round(acct.get("realized_pnl", 0) + realized)
        fee_tax, net_amount = fee + tax, net
    acct["status_note"] = RUNNING_NOTE
    trades = load_trades()
    trade = {"trade_id": f"T{len(trades) + 1:04d}", "timestamp": _now(), "stock_id": stock_id, "name": name or h.get("name"),
             "action": action, "price": round(gross / shares, 4), "shares": shares, "gross_amount": gross,
             "board_shares": board_shares, "board_price": board_price if board_shares else None,
             "odd_shares": odd_shares, "odd_price": odd_price if odd_shares else None, "odd_price_source": odd_src,
             "price_note": price_note, "fee_and_tax": fee_tax,
             "net_amount": net_amount, "realized_pnl": realized, "cash_after": acct["cash_balance"],
             "stop_loss": _f(stop_loss) or h.get("stop_loss"), "target_price": _f(target_price) or h.get("target_price"),
             "team_discussion": team_discussion or "", "buffett_verdict": buffett_verdict or ""}
    trades.append(trade)
    _write(TRADES_PATH, trades)
    acct["last_trade_timestamp"] = trade["timestamp"]  # 雲端防衝突：交易後推回 GitHub 前比對這個值
    acct["last_trade_id"] = trade["trade_id"]
    save_account(acct)
    return trade


def _cost_total(h):
    return h.get("cost_total") if h.get("cost_total") is not None else round(h["avg_cost"] * h["shares"])


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def update_levels(stock_id, stop_loss=None, target_price=None):
    """移動停利／調整停損（HOLD 時巴董事長也可上移防線）。"""
    acct = load_account()
    h = holding_of(acct, stock_id)
    if not h:
        return None
    if _f(stop_loss):
        h["stop_loss"] = _f(stop_loss)
    if _f(target_price):
        h["target_price"] = _f(target_price)
    save_account(acct)
    return h


# ---------------------------------------------------------------- 統計

def trade_stats(trades=None):
    trades = load_trades() if trades is None else trades
    sells = [t for t in trades if t["action"] in SELL_ACTIONS]
    wins = [t for t in sells if t["realized_pnl"] > 0]
    return {"trades": len(trades), "buys": sum(1 for t in trades if t["action"] in BUY_ACTIONS), "sells": len(sells),
            "wins": len(wins), "win_rate": round(len(wins) / len(sells) * 100, 1) if sells else None,
            "fees_total": sum(t["fee_and_tax"] for t in trades)}


def goal_status(acct):
    eq, cap = acct["total_equity"], acct["initial_capital"]
    low, high = cap * (1 + acct["target_return_low_pct"] / 100), cap * (1 + acct["target_return_high_pct"] / 100)
    days_left = (datetime.strptime(acct["target_date"], "%Y-%m-%d").date() - date.today()).days
    gain = eq - cap
    return {"low_target": round(low), "high_target": round(high), "gap_to_low": round(low - eq), "gap_to_high": round(high - eq),
            "low_progress_pct": round(max(0.0, gain / (low - cap) * 100), 1),
            "high_progress_pct": round(max(0.0, gain / (high - cap) * 100), 1),
            "days_left": days_left, "cash_pct": round(acct["cash_balance"] / eq * 100, 1) if eq else 0.0}


# ---------------------------------------------------------------- 盤中盯盤（0 Token）

def _zhu(stock_id):
    k = (_read(REPORTS / "data" / f"{stock_id}_kline.json", {}).get("data")) or {}
    z = k.get("zhu_analysis") or {}
    if not z and k.get("daily_30d"):
        from tools import kline_patterns
        z = kline_patterns.analyze_daily_rows(k["daily_30d"])
    return z


def hunt_score(r, zhu):
    """20 強買進評分：朱家泓多頭轉折／守支撐 ＋ 權證小哥法人買超／五檔有撐。"""
    score = {"HIT_ENTRY": 3, "APPROACHING": 2, "BELOW_ENTRY": 0, "WAITING": 0, "BROKEN": -3}.get(r.get("signal"), 0)
    score += r.get("chip_score") or 0
    score += {"多頭趨勢": 2, "盤整趨勢": 0, "空頭趨勢": -2}.get(zhu.get("trend"), 0)
    score += 1 if zhu.get("has_reversal_signal") else 0
    score += 1 if r.get("vwap_position") == "站上 VWAP" else 0
    score += 1 if (r.get("bid_ask_ratio") or 0) >= 1 else 0
    score += 1 if (r.get("risk_reward") or 0) >= 1.5 else 0
    return score


def refresh_sim_intraday(with_radar=True):
    """更新模擬持倉現價、VWAP 與總淨值，並掃描 20 強候選股（進場區＋朱家泓轉折訊號）。"""
    from tools import watchlist_intraday_radar as radar_mod
    started = datetime.now()
    acct = load_account()
    errors = []
    quotes = {}
    try:
        quotes = latest_quotes([h["stock_id"] for h in acct["holdings"]])
    except Exception as exc:  # 報價失敗沿用上一筆
        errors.append(f"持倉報價：{exc}")
    odd_ids = [h["stock_id"] for h in acct["holdings"] if h["shares"] % BOARD_LOT]
    odd_quotes = {}
    if odd_ids:
        from tools import realtime_quote_engine as rq
        try:
            odd_quotes = rq.fetch_odd_lot_quotes(odd_ids)
        except rq.MISError as exc:
            errors.append(f"零股報價：{exc}")
    alerts = []
    for h in acct["holdings"]:
        q = quotes.get(h["stock_id"])
        if q:
            h["current_price"], h["change_pct"], h["vwap"] = q["price"], q.get("change_pct"), q.get("vwap")
        if h["stock_id"] in odd_quotes:  # 零股部位以盤中零股可成交價（賣方）評價市值
            from tools import realtime_quote_engine as rq
            h["odd_price"], h["odd_price_source"] = rq.odd_lot_fill_price(odd_quotes[h["stock_id"]], "sell", h.get("current_price"))
        p = h.get("current_price")
        if p and h.get("stop_loss") and p <= h["stop_loss"]:
            alerts.append({"stock_id": h["stock_id"], "type": "STOP", "text": f"現價 {p:g} ≤ 停損 {h['stop_loss']:g}"})
        elif p and h.get("target_price") and p >= h["target_price"]:
            alerts.append({"stock_id": h["stock_id"], "type": "TARGET", "text": f"現價 {p:g} ≥ 目標 {h['target_price']:g}"})
    save_account(acct)
    radar = {}
    if with_radar:
        try:
            radar = radar_mod.run()
        except Exception as exc:
            errors.append(f"20 強建議買進價監控表：{exc}")
    radar = radar or radar_mod.load_radar()
    held = {h["stock_id"] for h in acct["holdings"]}
    prey = []
    for r in radar.get("results") or []:
        zhu = _zhu(r["stock_id"])
        bull_k = [p["pattern"] for p in zhu.get("patterns") or [] if p.get("type") == "bullish"]
        prey.append({"stock_id": r["stock_id"], "name": r.get("name"), "sector": r.get("sector"), "price": r.get("price"),
                     "change_pct": r.get("change_pct"), "signal": r.get("signal"), "signal_label": r.get("signal_label"),
                     "dist_to_entry_pct": r.get("dist_to_entry_pct"), "entry_low": r.get("entry_low"),
                     "entry_high": r.get("entry_high"), "stop_loss": r.get("stop_loss"), "target_1": r.get("target_1"),
                     "risk_reward": r.get("risk_reward"), "bid_ask_ratio": r.get("bid_ask_ratio"),
                     "vwap_position": r.get("vwap_position"), "chip_score": r.get("chip_score"), "chip_trend": r.get("chip_trend"),
                     "zhu_trend": zhu.get("trend"), "zhu_reversal": "、".join(bull_k) or None,
                     "held": r["stock_id"] in held, "score": hunt_score(r, zhu)})
    prey.sort(key=lambda x: -x["score"])
    scan = {"updated_at": _now(), "radar_updated_at": radar.get("updated_at"), "alerts": alerts, "prey": prey,
            "elapsed_sec": round((datetime.now() - started).total_seconds(), 2), "errors": errors}
    _write(SCAN_PATH, scan)
    return scan


def load_scan():
    return _read(SCAN_PATH, {})


def load_meeting():
    return _read(MEETING_PATH, {})


def main():
    parser = argparse.ArgumentParser(description="🧪 SIMULATION ROOM：100 萬模擬基金帳本")
    parser.add_argument("--init", action="store_true", help="建立帳本（已存在則不覆蓋）")
    parser.add_argument("--reset", action="store_true", help="清空帳本重新開始（會刪除所有模擬交易）")
    parser.add_argument("--refresh", action="store_true", help="盤中盯盤：更新持倉與 20 強候選股")
    parser.add_argument("--status", action="store_true", help="顯示帳本摘要")
    args = parser.parse_args()
    if args.init or args.reset:
        init_account(force=args.reset)
    if args.refresh:
        scan = refresh_sim_intraday()
        print(f"🔄 盯盤完成（{scan['elapsed_sec']} 秒）｜警示 {len(scan['alerts'])}｜候選股前 5："
              + "、".join(f"{p['stock_id']}{p['name']}({p['score']})" for p in scan["prey"][:5]))
    acct = load_account()
    g, s = goal_status(acct), trade_stats()
    print(f"💰 總淨值 {acct['total_equity']:,}（{acct['total_return_pct']:+.2f}%）｜現金 {acct['cash_balance']:,}（{g['cash_pct']}%）"
          f"｜距 110 萬 {g['gap_to_low']:,}｜剩 {g['days_left']} 天｜交易 {s['trades']} 筆、勝率 {s['win_rate']}")
    for h in acct["holdings"]:
        print(f"  {h['stock_id']} {h['name']} {h['shares']} 股 均價 {h['avg_cost']} 現價 {h.get('current_price')} "
              f"未實現 {h['unrealized_pnl']:+,}（{h['unrealized_pct']:+.2f}%）停損 {h.get('stop_loss')} 目標 {h.get('target_price')}")


if __name__ == "__main__":
    main()
