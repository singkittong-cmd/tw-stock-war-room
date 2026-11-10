# -*- coding: utf-8 -*-
"""以 `claude -p` 喚醒戰情室五位成員（dashboard 與盤中哨兵共用）。

成員：新聞達人（fundamental-analyst）、籌碼王（chip-monitor）、K線技術人（kline-analyst）、
巴董事長（主執行緒）、川大帝MAGA（us-semi-tracker）。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PYTHON = "C:/Users/singk/anaconda3/python.exe"

# 非互動模式預先授權的工具：Bash 整體開放（子分析員以完整直譯器路徑呼叫不再被拒），
# 另加 Glob/Grep 與派工用的 Agent/Task。
ALLOWED_TOOLS = "Bash,Read,Write,Edit,WebSearch,WebFetch,Glob,Grep,Agent,Task"

PREFETCH_NOTE = (
    "reports/data/ 已由 tools/batch_prefetch.py 預抓好 代號_fundamental.json、代號_chips.json、代號_kline.json，"
    "成員必須先 Read 這些檔案，fetched_at 為今日才可直接使用，缺檔或過期才自行執行 "
    f"{PYTHON} tools/finmind_cli.py。"
)
MEMORY_NOTE = (
    f"派工前先執行 {PYTHON} tools/memory_reflection.py 產生 reports/memory/ 覆盤檔，"
    "每位成員報告第一段必須是【🔄 昨日結論覆盤與思考修正】（讀 reports/memory/代號_reflection.json 與 market_reflection.json），"
    "並把新結論寫入 reports/memory/inbox/成員代稱.json；全部完成後再執行一次 "
    f"{PYTHON} tools/memory_reflection.py --snapshot 收錄新結論。"
)
QA_PATH = "reports/memory/war_room_qa_latest.json"


def sanitize_for_cmd(text):
    """shell=True 會經過 cmd.exe：移除換行與 cmd 特殊字元，避免指令被截斷或變數展開。"""
    text = text.replace("%", "pct").replace('"', "”")
    text = re.sub(r"[\r\n\t]+", " ", text)
    return re.sub(r"[\^&|<>]", " ", text).strip()


def build_research_prompt(stock_id, mode):
    mode_label = "盤中 intraday" if mode == "intraday" else "盤後 after_hours"
    return (
        f"請以巴董事長（主席）身份，依 CLAUDE.md 鐵律對 {stock_id} 進行{mode_label}完整研究："
        f"{PREFETCH_NOTE}{MEMORY_NOTE}"
        f"在同一則訊息中平行調用新聞達人 fundamental-analyst、籌碼王 chip-monitor（--mode {mode}）、K線技術人 kline-analyst，"
        f"完成 Red-Team 反向挑戰與安全優先矩陣仲裁，寫入 reports/stocks/{stock_id}_role4_chief.md "
        f"與 reports/stocks/{stock_id}_summary.json，最後以 10 行內繁體中文摘要裁決。"
    )


def build_portfolio_prompt(stock_ids):
    ids = "、".join(stock_ids)
    return (
        f"【持倉盤中診斷】請以巴董事長（主席）身份，依 CLAUDE.md 持倉裁決鐵律診斷我手上全部持股（{ids}）："
        f"先執行 {PYTHON} tools/portfolio_intraday.py 取得即時報價、五檔委買委賣比、內外盤方向與我的成本價、股數、帳面損益；"
        f"{PREFETCH_NOTE}{MEMORY_NOTE}"
        f"再對每一檔在同一則訊息平行調用新聞達人、籌碼王（--mode intraday）、K線技術人，"
        f"完成 Red-Team 反向挑戰與安全優先矩陣仲裁，更新各檔 reports/stocks/代號_role4_chief.md 與 代號_summary.json（含 position_advice），"
        f"並寫入 reports/portfolio_diagnosis.md。最後以繁體中文逐檔輸出具體部位控制建議（賣出或續抱的股數、價位、移動停利或停損價）。"
    )


def build_intraday_batch_prompt(stock_ids):
    ids = "、".join(stock_ids)
    return (
        f"【全持股盤中批次會診】請以巴董事長（主席）身份，依 CLAUDE.md 第 7 節全持股盤中批次會診模式處理我的全部持股（{ids}）："
        f"先執行 {PYTHON} tools/portfolio_intraday.py 與 {PYTHON} tools/memory_reflection.py；不調用新聞達人，"
        f"在同一則訊息平行派出籌碼王 chip-monitor 與 K線技術人 kline-analyst，"
        f"各自一次處理全部持股並分別寫入 reports/intraday_proposals_role2.json 與 reports/intraday_proposals_role3.json；"
        f"再由你逐檔仲裁衝突，寫入 reports/portfolio_intraday_decisions.json（以代號為 key，欄位依第 7 節），"
        f"最後執行 {PYTHON} tools/memory_reflection.py --snapshot，並逐檔一行輸出：代號 名稱、最終動作、執行價位。"
    )


def build_standby_review_prompt(stock_ids):
    ids = "、".join(stock_ids)
    return (
        f"【20 強備用計畫覆核】請以巴董事長（主席）身份，依 CLAUDE.md 第 8 節覆核 reports/watchlist_standby.json 中"
        f"目前已觸及或接近進場區的龍頭（{ids}）：在同一則訊息平行派出籌碼王 chip-monitor 與 K線技術人 kline-analyst，"
        f"各自一次 Read 這些代號的 reports/data/代號_chips.json、代號_kline.json、reports/memory/代號_reflection.json "
        f"與 reports/watchlist_radar_live.json 提出意見；"
        f"由你仲裁後直接修改 reports/watchlist_standby.json 對應代號的 entry_low、entry_high、stop_loss、target_1、"
        f"risk_reward、role4_note，並設 manual_override 為 true、reviewed_at 為現在時間。最後逐檔一行輸出調整結果。"
    )


def build_us_night_prompt():
    return (
        f"【美股夜盤更新】請以巴董事長（主席）身份，依 CLAUDE.md 第 9.1 節：先執行 {PYTHON} tools/us_semi_tracker.py --mode night_open，"
        f"只調用川大帝MAGA us-semi-tracker（不調用新聞達人、籌碼王、K線技術人），寫入 reports/us_market/us_night_report.md，"
        f"最後以 5 行內輸出費半與 TSM、NVDA、MU、VRT 開盤風向及對明日台股的初步想法。"
    )


def build_us_morning_prompt():
    return (
        f"【美股收盤盤前會診】請以巴董事長（主席）身份，依 CLAUDE.md 第 9.2 節完整執行步驟 A 到 E（含步驟 E 上鎖）："
        f"先執行 {PYTHON} tools/us_semi_tracker.py --mode check_status，若為 LOCKED_COMPLETED 則停止並回報；"
        f"否則執行 --mode morning_close 與 {PYTHON} tools/memory_reflection.py，調用川大帝MAGA 產出 reports/us_market/us_morning_close_report.md，"
        f"再於同一則訊息平行派出新聞達人、籌碼王、K線技術人，各自一次處理 reports/portfolio.json 持股"
        f"與 reports/sector_watchlist.json 20 大龍頭的美股連動議題（只讀 reports/data/ 預抓檔）；"
        f"由你反向挑戰後寫入 reports/us_market/tw_premarket_summary.md 與 tw_premarket_summary.json（含 us_top10），"
        f"最後執行 {PYTHON} tools/us_semi_tracker.py --mode complete 上鎖、{PYTHON} tools/memory_reflection.py --snapshot，"
        f"並以 10 行內輸出今日台股預判與開盤策略。"
    )


def build_news_brief_prompt():
    return (
        "【今日全市場財經新聞快報】請以巴董事長（主席）身份，調用新聞達人 fundamental-analyst 執行其定義檔的任務模式 B："
        "Read reports/news/daily_market_news_raw.json、reports/portfolio.json、reports/sector_watchlist.json、"
        "reports/memory/market_reflection.json，過濾業配與單純漲跌播報，寫入 reports/news/daily_news_brief.md 與 "
        "reports/news/daily_news_summary.json（含 top_news_10）；non_holding_opportunities 不得包含持股代號，"
        "報告不得寫入使用者的股數或成本。最後以 5 行內輸出今日新聞情緒與一句話總結。"
    )


def build_war_room_prompt(stock_ids, mode):
    ids = "、".join(stock_ids)
    mode_label = "盤中 intraday" if mode == "intraday" else "盤後 after_hours"
    return (
        f"【戰情室全員更新】請以巴董事長（主席）身份，依 CLAUDE.md 第 10 節，以{mode_label}模式更新新聞達人、籌碼王、K線技術人並由你拍板："
        f"資料已由 dashboard 預抓完成（batch_prefetch、portfolio_intraday、watchlist_intraday_radar、market_news_fetcher、memory_reflection），不得重抓；"
        f"川大帝MAGA 一律不調用，只 Read reports/us_market/tw_premarket_summary.json 作為美股背景。"
        f"每位成員第一段必須是【🔄 昨日結論覆盤與思考修正】，依據 reports/memory/代號_reflection.json 與 market_reflection.json。"
        f"在同一則訊息平行派出：新聞達人執行任務模式 B 寫入 reports/news/daily_news_brief.md 與 daily_news_summary.json；"
        f"籌碼王與 K線技術人依第 7 節各自一次處理全部持股（{ids}），寫入 reports/intraday_proposals_role2.json 與 role3.json。"
        f"三份回來後由你反向挑戰並逐檔仲裁，寫入 reports/portfolio_intraday_decisions.json，"
        f"最後執行 {PYTHON} tools/memory_reflection.py --snapshot，以 10 行內輸出裁決摘要。"
    )


def build_tactical_qa_prompt(question, scope, stock_id=None):
    """WAR ROOM 盤中戰術提問：單一 claude -p 呼叫，不派子代理（求快），由主席依各成員定義檔邏輯分別代答。"""
    target = {"market": "全市場", "holdings": "手上全部持股（reports/portfolio.json）"}.get(scope, f"個股 {stock_id}")
    files = ("reports/portfolio_live_status.json、reports/portfolio_intraday_decisions.json、" if scope == "holdings" else "")
    if stock_id:
        files += (f"reports/data/{stock_id}_chips.json、reports/data/{stock_id}_kline.json、"
                  f"reports/memory/{stock_id}_reflection.json、reports/intraday_snapshot.json、")
    return (
        f"【WAR ROOM 戰術提問】針對{target}，使用者問：「{question}」。"
        f"請以巴董事長（主席）身份快速回答，不要派出子代理，也不要執行任何爬蟲：只 Read {files}"
        f"reports/watchlist_radar_live.json、reports/market_close_summary.json、reports/news/daily_news_summary.json、"
        f"reports/us_market/tw_premarket_summary.json、reports/memory/market_reflection.json、reports/memory/reflection_index.json，"
        f"並依 .claude/agents/ 各成員定義檔的思考邏輯分別代答："
        f"新聞達人（消息面與基本面事實查核）、籌碼王（權證小哥：盤中五檔、大戶單、隔日沖、自營避險、融資券）、"
        f"K線技術人（朱家泓：趨勢方向、四線位階、關鍵轉折 K 與停損位），"
        f"最後巴董事長（美股大叔：預期差、機構部位、不對稱風報比）直接點出前三位的盲點或衝突，結合昨日覆盤落差，給出一錘定音的操作指令。"
        f"每位 3–6 行、每個數字必須來自上述檔案。把結果用 Write 寫入 {QA_PATH}，格式為 JSON："
        f"{{question, asked_at（YYYY-MM-DD HH:MM:SS）, scope, stock_id, news, chips, kline, chairman}}，寫完 Read 確認為合法 JSON，"
        f"最後只輸出巴董事長的最終指令一句話。"
    )


def run_prefetch(stock_ids, timeout=180):
    """以目前直譯器（sys.executable，即 Anaconda）執行批次預抓，回傳 (returncode, stdout, stderr)。"""
    cmd = [sys.executable, str(ROOT / "tools" / "batch_prefetch.py")]
    for sid in stock_ids:
        cmd += ["--stock", str(sid)]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            cmd,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=str(ROOT),
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"batch_prefetch 執行逾時（>{timeout} 秒）"
    except OSError as exc:
        return -1, "", f"無法啟動 batch_prefetch：{exc}"
    return result.returncode, result.stdout or "", result.stderr or ""


def run_claude(prompt, timeout=1200):
    """執行 claude -p，回傳 (returncode, stdout, stderr)。"""
    env = os.environ.copy()
    # 讓 claude 子行程中的 `python` 指向目前這個直譯器（例如 Anaconda）
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = ["claude", "-p", sanitize_for_cmd(prompt), "--allowedTools", ALLOWED_TOOLS, "--output-format", "text"]
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=str(ROOT),
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"claude -p 執行逾時（>{timeout} 秒）"
    except OSError as exc:
        return -1, "", f"無法啟動 claude：{exc}"
    return result.returncode, result.stdout or "", result.stderr or ""
