# -*- coding: utf-8 -*-
"""09:00-13:35 盤中實時監控哨兵。

- 輪詢證交所 MIS 即時報價（上市 tse / 上櫃 otc）：成交價、當盤張數、五檔委買委賣
- 觸發條件：跌破 stop_loss、進入 entry_zone、單筆 >= 門檻張數內盤倒貨 / 外盤點火、
  跌幅 <= -3.5% 且五檔委買比 < 0.45
- 第一階段：LINE 閃電警報；第二階段：fast_team_runner 單次濃縮會診（預設免費 Gemini）後推播巴董事長裁決

用法：
    C:/Users/singk/anaconda3/python.exe intraday_sentinel.py            # 交易時段內持續監控
    C:/Users/singk/anaconda3/python.exe intraday_sentinel.py --once     # 只輪詢一次
    C:/Users/singk/anaconda3/python.exe intraday_sentinel.py --force    # 忽略交易時段限制（盤後測試用）
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from tools import fast_team_runner, llm_router  # noqa: E402
from tools.claude_runner import run_prefetch  # noqa: E402
from tools.line_notifier import is_line_configured, push_text  # noqa: E402
from tools.portfolio_intraday import (  # noqa: E402
    MISError, compute_position, fetch_mis_quotes, load_portfolio, new_session, resolve_price,
)
from tools.watchlist_intraday_radar import run as run_radar  # noqa: E402
from tools.yahoo_quote import YahooError, fetch_intraday  # noqa: E402

load_dotenv(ROOT / ".env", encoding="utf-8")

REPORTS_DIR = ROOT / "reports"
STOCKS_DIR = REPORTS_DIR / "stocks"
SNAPSHOT_PATH = REPORTS_DIR / "intraday_snapshot.json"
STATE_PATH = REPORTS_DIR / "sentinel_state.json"
LOG_DIR = REPORTS_DIR / "sentinel_logs"
ALERT_LOG_PATH = REPORTS_DIR / "intraday_alerts.log"

WATCHLIST = [s.strip() for s in os.getenv("WATCHLIST", "2330").split(",") if s.strip()]
INTERVAL_SEC = max(5, int(os.getenv("SENTINEL_INTERVAL_SEC", "20")))
LARGE_TICK_LOTS = int(os.getenv("LARGE_TICK_THRESHOLD_LOTS", "50"))
COOLDOWN_MIN = float(os.getenv("ALERT_COOLDOWN_MIN", "30"))
AUTO_WAKE = os.getenv("AUTO_WAKE_CLAUDE_ON_ALERT", "true").strip().lower() in ("1", "true", "yes", "y")

CRASH_PCT = -3.5
WEAK_BID_RATIO = 0.45
MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(13, 35)
CLOSING_AUCTION_TIME = "13:30:00"
CLAUDE_TIMEOUT_SEC = 900

ANSI_RESET = "\033[0m"
ANSI_STYLE = {
    "alert": "\033[1;97;41m",    # 白字紅底：第一階段閃電警報
    "verdict": "\033[1;30;43m",  # 黑字黃底：第二階段總分析員裁決
}

ALERT_LABEL = {
    "STOP_BREAK": "🚨 跌破絕對停損",
    "ENTRY_ZONE": "🎯 進入布局區間",
    "LARGE_SELL": "🔻 大單內盤倒貨",
    "LARGE_BUY": "🔺 大單外盤點火",
    "CRASH": "⚠️ 急殺且五檔失衡",
}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _num(x):
    if x is None:
        return None
    try:
        v = float(str(x).strip())
    except ValueError:
        return None
    return v if v > 0 else None


def notify(text, level="alert"):
    """有 LINE Token 就推播；沒有則在終端機印出醒目警報。兩種情況都寫入 intraday_alerts.log。"""
    ALERT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} [{level}] =====\n{text}\n\n")
    if is_line_configured() and push_text(text):
        return
    style = ANSI_STYLE.get(level, ANSI_STYLE["alert"])
    bar = "━" * 56
    print(f"\n{style}{bar}{ANSI_RESET}", flush=True)
    for line in text.splitlines():
        print(f"{style} {line} {ANSI_RESET}", flush=True)
    print(f"{style}{bar}{ANSI_RESET}\n", flush=True)


def parse_entry_zone(value):
    """支援 [low, high]、"1440-1470"、"1440 ~ 1470" 等格式。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        nums = [float(value[0]), float(value[1])]
    else:
        nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", str(value))]
    if len(nums) < 2:
        return None
    return min(nums[:2]), max(nums[:2])


def in_trading_hours(now=None):
    now = now or datetime.now()
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class Sentinel:
    def __init__(self, stocks, include_portfolio=True, radar=True):
        self.base_stocks = stocks
        self.include_portfolio = include_portfolio
        self.radar_enabled = radar
        self.radar_prev = {}
        self.holdings = {}
        self.stocks = list(stocks)
        self.session = None
        self.market = {}
        self.prev = {}
        self.large_ticks = {}
        self.summaries = {}
        self.consulting = set()
        self.lock = threading.Lock()
        self.state = self._load_state()
        self.refresh_portfolio()

    def refresh_portfolio(self):
        """每輪重讀持倉，讓 dashboard 編輯後的新持股自動納入監控。"""
        if not self.include_portfolio:
            return
        try:
            self.holdings = {p["stock_id"]: p for p in load_portfolio()}
        except (OSError, json.JSONDecodeError) as exc:
            log(f"portfolio.json 讀取失敗：{exc}")
        self.stocks = list(dict.fromkeys(list(self.holdings) + list(self.base_stocks)))

    # ------------------------------------------------------------ 狀態

    def _load_state(self):
        if STATE_PATH.exists():
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        return {"last_alert": {}}

    def _save_state(self):
        with self.lock:
            write_json_atomic(STATE_PATH, self.state)

    def _cooldown_ok(self, key):
        last = self.state.get("last_alert", {}).get(key)
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        return datetime.now() - last_dt >= timedelta(minutes=COOLDOWN_MIN)

    def _mark_alert(self, key):
        self.state.setdefault("last_alert", {})[key] = datetime.now().isoformat(timespec="seconds")
        self._save_state()

    def load_summary(self, code):
        path = STOCKS_DIR / f"{code}_summary.json"
        if not path.exists():
            return {}
        mtime = path.stat().st_mtime
        cached = self.summaries.get(code)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log(f"{code} summary 讀取失敗：{exc}")
            return cached[1] if cached else {}
        self.summaries[code] = (mtime, data)
        return data

    # ------------------------------------------------------------ 抓取

    def fetch_quotes(self):
        if self.session is None:
            self.session = new_session()
        try:
            return fetch_mis_quotes(self.stocks, self.session, self.market)
        except MISError as exc:
            log(str(exc))
            self.session = None
            return {}

    # ------------------------------------------------------------ 判斷

    @staticmethod
    def _side(price, ref):
        if price is None or ref is None:
            return "未明"
        bids, asks = ref.get("bids") or [], ref.get("asks") or []
        if asks and price >= asks[0]:
            return "外盤"
        if bids and price <= bids[0]:
            return "內盤"
        if bids and asks:
            return "外盤" if price >= (bids[0] + asks[0]) / 2 else "內盤"
        return "未明"

    def evaluate(self, q):
        code = q["code"]
        prev = self.prev.get(code)
        price = q["price"]
        q["price_source"] = "成交價"
        if price is None:
            # MIS z 為 '-'：試撮價 pz → Yahoo 真實成交價（60 秒快取）→ 前一輪現價 → 最佳委買
            yahoo = None
            if q.get("trial_price") is None:
                try:
                    yahoo = fetch_intraday(code, q.get("market"), max_age=60)
                except YahooError as exc:
                    yahoo = {"error": str(exc)}
            price, source, _, _, _ = resolve_price(q, yahoo)
            if source.startswith("昨收") and prev and prev.get("price"):
                price, source = prev["price"], "前一輪現價"
            q["price"] = price
            q["price_source"] = source
            q["price_is_estimate"] = True
        pct = (price / q["prev_close"] - 1) * 100 if price and q["prev_close"] else None
        bid_sum = sum(q["bid_vols"])
        ask_sum = sum(q["ask_vols"])
        bid_ratio = bid_sum / (bid_sum + ask_sum) if (bid_sum + ask_sum) > 0 else None
        q["pct_change"] = pct
        q["bid_ratio"] = bid_ratio

        new_trade = q["tick_vol"] > 0 and (prev is None or q["acc_vol"] > prev.get("acc_vol", 0))
        side = self._side(price, prev if prev else q) if new_trade else "無新成交"
        q["last_side"] = side

        alerts = []
        # 首次觀測無前一筆可比對；13:30 收盤集合競價為整批撮合量，皆不視為單筆大單
        is_auction = q["time"] >= CLOSING_AUCTION_TIME
        if (prev is not None and not is_auction and new_trade
                and q["tick_vol"] >= LARGE_TICK_LOTS and side in ("內盤", "外盤")):
            tick = {"time": q["time"], "price": price, "lots": q["tick_vol"], "side": side}
            self.large_ticks[code] = (self.large_ticks.get(code, []) + [tick])[-50:]
            if side == "內盤":
                alerts.append(("LARGE_SELL", f"單筆 {q['tick_vol']} 張內盤成交 @ {price}"))
            else:
                alerts.append(("LARGE_BUY", f"單筆 {q['tick_vol']} 張外盤成交 @ {price}"))
        q["recent_large_ticks"] = self.large_ticks.get(code, [])[-20:]

        summary = self.load_summary(code)
        stop = _num(summary.get("stop_loss")) if summary else None
        zone = parse_entry_zone(summary.get("entry_zone")) if summary else None
        if price and stop and price < stop:
            alerts.append(("STOP_BREAK", f"現價 {price} 跌破停損 {stop}"))
        if price and zone and zone[0] <= price <= zone[1]:
            alerts.append(("ENTRY_ZONE", f"現價 {price} 進入布局區 {zone[0]}~{zone[1]}"))
        if pct is not None and pct <= CRASH_PCT and bid_ratio is not None and bid_ratio < WEAK_BID_RATIO:
            alerts.append(("CRASH", f"跌幅 {pct:.2f}% 且五檔委買比 {bid_ratio:.2f} < {WEAK_BID_RATIO}"))

        self.prev[code] = q
        return alerts

    # ------------------------------------------------------------ 推播與會診

    def fire(self, q, alerts):
        code, name = q["code"], q["name"]
        fresh = [(t, d) for t, d in alerts if self._cooldown_ok(f"{code}:{t}")]
        if not fresh:
            return
        for t, _ in fresh:
            self._mark_alert(f"{code}:{t}")
        summary = self.load_summary(code)
        lines = [
            f"⚡【盤中閃電警報】{code} {name}",
            f"時間 {q['time']}｜現價 {q['price']}（{q['pct_change']:+.2f}%）" if q["pct_change"] is not None else f"時間 {q['time']}｜現價 {q['price']}",
            f"五檔委買比 {q['bid_ratio']:.2f}" if q["bid_ratio"] is not None else "五檔委買比 N/A",
        ]
        lines += [f"{ALERT_LABEL[t]}：{d}" for t, d in fresh]
        holding = self.holdings.get(code)
        if holding and q["price"]:
            pos = compute_position(holding["cost_price"], holding["shares"], q["price"])
            lines.append(f"💼 持倉 {holding['shares']} 股｜成本 {holding['cost_price']}｜"
                         f"帳面淨損益 {pos['net_pnl']:+,.0f} 元（{pos['net_return_pct']:+.2f}%）")
        if summary:
            lines.append(f"前次裁決：{summary.get('verdict', 'N/A')}｜停損 {summary.get('stop_loss', 'N/A')}｜進場區 {summary.get('entry_zone', 'N/A')}")
        if AUTO_WAKE:
            lines.append("🧠 已喚醒四位分析員會診，裁決稍後送出…")
        text = "\n".join(lines)
        log(text.replace("\n", " ｜ "))
        notify(text, "alert")

        if AUTO_WAKE:
            with self.lock:
                if code in self.consulting:
                    log(f"{code} 會診進行中，略過重複喚醒")
                    return
                self.consulting.add(code)
            threading.Thread(target=self.consult, args=(q, fresh), daemon=True).start()

    def consult(self, q, alerts):
        code, name = q["code"], q["name"]
        try:
            alert_desc = "；".join(f"{ALERT_LABEL[t]} {d}" for t, d in alerts)
            ratio = f"{q['bid_ratio']:.2f}" if q["bid_ratio"] is not None else "N/A"
            pct = f"{q['pct_change']:+.2f}" if q["pct_change"] is not None else "N/A"
            prompt = (f"【盤中緊急會診】{code} {name} 於 {q['time']} 觸發警報：{alert_desc}。"
                      f"現價 {q['price']}，漲跌幅 {pct} pct，五檔委買比 {ratio}。")
            prc, pout, perr = run_prefetch([code])
            log(f"{code} 批次預抓完成 rc={prc}" + ("" if prc == 0 else f"：{(perr or pout)[-200:]}"))
            # 改走 fast_team_runner 單次濃縮會診（預設免費 Gemini），不再以 claude -p 派出四位子代理
            provider = os.getenv("DEFAULT_LLM_PROVIDER", "gemini").strip() or "gemini"
            log(f"{code} 單次濃縮會診中（{provider}）…")
            started = datetime.now()
            try:
                result = fast_team_runner.deep_dive(code, provider, mode="intraday")
                rc, out, err = 0, (f"引擎 {result['provider']}｜裁決 {result['verdict']}｜風報比 {result['rr']}"
                                   f"｜耗時 {result['elapsed_sec']} 秒"), ""
            except (llm_router.LLMError, LookupError) as exc:
                rc, out, err = 1, "", str(exc)
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LOG_DIR / f"{code}_{started:%Y%m%d_%H%M%S}.log"
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"PROMPT:\n{prompt}\n\nRETURN CODE: {rc}\n\nSTDOUT:\n{out}\n\nSTDERR:\n{err}\n")

            self.summaries.pop(code, None)
            summary = self.load_summary(code)
            updated = summary.get("updated_at", "")
            lines = [f"🧠【總分析員盤中裁決】{code} {name}"]
            if summary:
                lines += [
                    f"裁決：{summary.get('verdict')}｜信心度 {summary.get('confidence')}",
                    f"進場區 {summary.get('entry_zone')}｜停損 {summary.get('stop_loss')}",
                    f"目標 {summary.get('target_1')} / {summary.get('target_2')}｜風報比 {summary.get('risk_reward_ratio')}",
                    f"Red-Team：{summary.get('red_team_warning')}",
                ]
                if summary.get("position_advice"):
                    lines.append(f"💼 部位建議：{summary.get('position_advice')}")
                lines.append(f"（summary 更新於 {updated}）")
            if rc != 0:
                lines.append(f"⚠️ 會診失敗：{(err or out)[-500:]}")
            elif out.strip():
                lines += ["", "— 會診摘要 —", out.strip()[-1800:]]
            notify("\n".join(lines), "verdict")
            log(f"{code} 會診完成（rc={rc}，耗時 {(datetime.now() - started).seconds} 秒），紀錄 {log_path.name}")
        finally:
            with self.lock:
                self.consulting.discard(code)

    # ------------------------------------------------------------ 主迴圈

    def poll_once(self):
        self.refresh_portfolio()
        quotes = self.fetch_quotes()
        if not quotes:
            log("本輪無報價")
            return
        snapshot = {"updated_at": datetime.now().isoformat(timespec="seconds"), "stocks": {}}
        for code in self.stocks:
            q = quotes.get(code)
            if not q:
                continue
            alerts = self.evaluate(q)
            snapshot["stocks"][code] = q
            pct = f"{q['pct_change']:+.2f}%" if q["pct_change"] is not None else "N/A"
            ratio = f"{q['bid_ratio']:.2f}" if q["bid_ratio"] is not None else "N/A"
            log(f"{code} {q['name']} {q['price']} {pct} 量 {q['acc_vol']} 單筆 {q['tick_vol']}({q['last_side']}) 委買比 {ratio}")
            if alerts:
                self.fire(q, alerts)
        write_json_atomic(SNAPSHOT_PATH, snapshot)
        if self.radar_enabled:
            self.scan_radar()

    def scan_radar(self):
        """20 大龍頭盤中建議買進價監控表（0 Token）：標的現價達建議買進價時印出【🎯 產業龍頭進場通知】並寫入 log。"""
        try:
            radar = run_radar()
        except Exception as exc:  # 建議買進價監控表失敗不影響持股監控
            log(f"龍頭建議買進價監控表本輪失敗：{exc!r}")
            return
        for r in radar["results"]:
            sid = r["stock_id"]
            entered = r["signal"] == "HIT_ENTRY" and self.radar_prev.get(sid) != "HIT_ENTRY"
            self.radar_prev[sid] = r["signal"]
            key = f"{sid}:RADAR_HIT"
            if not entered or not self._cooldown_ok(key):
                continue
            ratio = f"{r['bid_ask_ratio']:.2f}" if r["bid_ask_ratio"] is not None else "N/A"
            pct = f"{r['change_pct']:+.2f}%" if r["change_pct"] is not None else "N/A"
            text = "\n".join([
                f"🎯【產業龍頭進場通知】{sid} {r['name']}（{r['sector']}）",
                f"現價 {r['price']}（{pct}）已達建議買進價 {r['entry_low']}–{r['entry_high']}",
                f"五檔委買/委賣比 {ratio}｜{r['vwap_position']}｜{r['big_tick_flow']}",
                f"停損 {r['stop_loss']}｜目標 {r['target_1']}｜風報比 {r['risk_reward']}｜{r['strategy_type']}",
                f"提示：{r['signal_hint']}",
                f"籌碼：{r['chip_trend']}",
            ])
            notify(text, "alert")
            self._mark_alert(key)
        counts = radar["counts"]
        log(f"龍頭建議買進價監控表：達建議買進價 {counts.get('HIT_ENTRY', 0)}、接近買進價 {counts.get('APPROACHING', 0)}、"
            f"跌破停損價 {counts.get('BROKEN', 0)} 檔（{radar['elapsed_sec']}s）")

    def run(self, once=False, force=False):
        channel = "LINE 推播" if is_line_configured() else f"終端機彩色警報 + {ALERT_LOG_PATH.relative_to(ROOT).as_posix()}"
        log(f"哨兵啟動：{','.join(self.stocks)}｜持倉 {len(self.holdings)} 檔｜間隔 {INTERVAL_SEC}s｜"
            f"大單門檻 {LARGE_TICK_LOTS} 張｜冷卻 {COOLDOWN_MIN} 分｜自動會診 {AUTO_WAKE}｜通知 {channel}")
        current_day = datetime.now().date()
        while True:
            now = datetime.now()
            if now.date() != current_day:
                current_day = now.date()
                self.prev.clear()
                self.large_ticks = {}
            if force or in_trading_hours(now):
                try:
                    self.poll_once()
                except Exception as exc:  # 單輪失敗不中斷哨兵
                    log(f"本輪例外：{exc!r}")
                if once:
                    break
                time.sleep(INTERVAL_SEC)
            else:
                if once:
                    log("目前非交易時段（週一至週五 09:00-13:35），如需測試請加 --force")
                    break
                log("非交易時段，60 秒後再檢查…")
                time.sleep(60)
        if self.consulting:
            log("等待進行中的會診完成…")
            while self.consulting:
                time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="台股盤中實時監控哨兵")
    parser.add_argument("--once", action="store_true", help="只輪詢一次後結束")
    parser.add_argument("--force", action="store_true", help="忽略交易時段限制")
    parser.add_argument("--stocks", help="覆寫 WATCHLIST，逗號分隔（持倉股仍會自動納入）")
    parser.add_argument("--no-portfolio", action="store_true", help="不自動監控 reports/portfolio.json 持股")
    parser.add_argument("--no-radar", action="store_true", help="不掃描 reports/sector_watchlist.json 20 大龍頭建議買進價監控表")
    args = parser.parse_args()
    stocks = [s.strip() for s in args.stocks.split(",")] if args.stocks else WATCHLIST
    os.system("")  # 啟用 Windows 主控台 ANSI 色彩
    try:
        Sentinel(stocks, include_portfolio=not args.no_portfolio, radar=not args.no_radar).run(
            once=args.once, force=args.force)
    except KeyboardInterrupt:
        log("哨兵已手動停止")


if __name__ == "__main__":
    main()
