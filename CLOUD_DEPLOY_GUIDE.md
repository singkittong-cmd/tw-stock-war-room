# ☁️ 雲端上線指南（GitHub Actions 主力檢查 + Streamlit Cloud 看盤）

## 架構：只有一本模擬帳本

| 角色 | 在哪裡 | 做什麼 |
|---|---|---|
| **主力** | GitHub Actions，每次排程只跑一輪 `--once`（不裝瀏覽器） | 盤中 09:00–13:25 每 30 分鐘檢查一次報價與進出場（Streamlit 休眠也照跑）；07:35 川大帝MAGA 早報；13:35 收盤結算、新聞 TOP 10、明日建議買進價 |
| **加強盯盤** | Streamlit Cloud 伺服器內的背景執行緒 | 有人打開網頁、伺服器醒著時，額外每 15 秒盯盤 |
| **觀看端** | 你的電腦（`.env` 設 `AUTOPILOT_MODE=cloud_master`） | 每 30 秒從 GitHub 拉回雲端帳本顯示，禁止任何模擬下單 |

- **交易前**：一律先從 GitHub 取回最新帳本。
- **成交後**：比對 `last_trade_timestamp`。如果有人搶先交易，本次成交作廢、改用 GitHub 版本。
- **盤中靜默**：沒成交不 commit，只有「實際成交」「13:35 收盤結算」「07:35 早報完成」才 commit，避免 Streamlit Cloud 盤中因新 commit 重新載入。
- **證交所休市日**：內建在 `tools/tw_market_calendar.py`，休市日完全不呼叫 API；09:10 實測台積電與加權指數零成交量時，當天休眠。
- **免費額度**：每個交易日 12 次排程 × 約 1–2 分鐘，每月約 300–550 分鐘，在私人 repo 每月 2,000 分鐘的免費額度內。

---

## 步驟 1：推上 Private repo

`.gitignore` 已排除真實持股與金鑰（`reports/portfolio*.json`、`reports/portfolio_diagnosis.md`、`reports/stocks/`、`reports/data/`、`.env`）。

## 步驟 2：GitHub 設定

repo → Settings → Secrets and variables → Actions

**Secrets**：

| 名稱 | 內容 | 必填 |
|---|---|---|
| `GEMINI_API_KEY` | Google AI Studio 金鑰 | ✅ |
| `FINMIND_API_TOKEN` | FinMind 金鑰 | ✅ |

**Variables**（選填）：

| 名稱 | 內容 |
|---|---|
| `STREAMLIT_APP_URL` | Streamlit 網址；每次排程送一次輕量 GET |
| `AUTOPILOT_MODE` | 預設 `cloud_master`；改成 `local_master` 時雲端停止交易 |

**寫入權限**：Settings → Actions → General → Workflow permissions → **Read and write permissions** → Save。

## 步驟 3：Streamlit Cloud

1. https://share.streamlit.io → **Create app** → 選這個 repo、Branch `main`、Main file `dashboard.py`。
2. **Advanced settings → Secrets** 貼上：

```toml
AUTOPILOT_MODE = "cloud_master"
GEMINI_API_KEY = "貼上金鑰"
FINMIND_API_TOKEN = "貼上金鑰"
PRIVACY_PIN = "你的金庫密碼"
GUEST_PIN = "給家人朋友的來賓密碼"   # 選填；沒設定時使用程式內建的預設來賓密碼
GITHUB_REPO = "<你的帳號>/tw-stock-war-room"
GITHUB_TOKEN = "github_pat_..."
```

`GITHUB_TOKEN` 讓 Streamlit 寫回成交紀錄。建立方式：
- 路徑：GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token。
- Repository access：只選這個 repo。
- Permissions：**Contents: Read and write**。

3. **分享給家人朋友（不需 GitHub 帳號）**：App 右上 **Share → Who can view this app** 改成 **Public**。
   - 網頁本身有雙層通行門禁：輸入 `PRIVACY_PIN` 是老闆模式（完整權限、持倉金庫自動解鎖）；輸入 `GUEST_PIN` 是 VIP 來賓模式。
   - 來賓模式：真實持股區塊完全不顯示；一鍵更新、全員同步、深度診斷、戰術提問、模擬室投決會與刷新、背景引擎開關等按鈕都停用。
   - 連續輸錯 5 次鎖 60 秒（以瀏覽器分頁為單位）。來賓密碼請設成不易猜的數字，並定期更換。
   - `?keepalive=1` 輕量喚醒不需要密碼，只回傳狀態字串、不顯示任何資料。

## 步驟 4：測試

1. GitHub repo → **Actions** → `tw-market-autopilot` → **Run workflow**，等它變綠勾。
2. 點進「雲端單次排程（--once）」看紀錄：
   - 盤中應出現 `⚡ Tick 完成`，以及 `🤫 沒有成交：盤中靜默，不 commit` 或 `☁️ 成交後已寫回 GitHub`。
   - 休市日或非交易時段只會出現一行狀態就結束。

## 步驟 5：本機改成觀看端（重要）

雲端確認正常後，把本機 `.env` 的 `AUTOPILOT_MODE=local_master` 改成 `AUTOPILOT_MODE=cloud_master`，再重新啟動 `start_war_room.bat`。之後本機只每 30 秒用 `git fetch` 拉回帳本，所有下單按鈕停用。

**想改回本機交易**：`.env`、GitHub Variables、Streamlit Secrets 三邊都改成 `local_master`，三邊一致才不會有兩邊同時交易。

## 已知限制

- **Streamlit 休眠**：一般 HTTP GET 不會讓休眠中的 Streamlit 開機，也不會執行 Python。
  - Streamlit 休眠時，盯盤只靠 GitHub Actions 每 30 分鐘一次。兩次之間觸及停損價不會立刻處理，要等下一次排程。
  - GitHub 排程常延遲 5–20 分鐘，尖峰時偶爾整次跳過。
- **兩邊同時醒著時**：GitHub Actions 和 Streamlit 都可能召開投決會。
  - **帳本**：受 `last_trade_timestamp` 保護，不會出現兩本帳。
  - **學習資料庫**：兩邊幾乎同時成交時，以最後寫回的一方為準，可能少收錄另一方那次投決會的預測紀錄。
- **盤中靜默的代價**：
  - 觀看端看不到即時心跳，畫面顯示「☁️ 雲端靜默盯盤（成交或收盤才同步）」。
  - 盤中的停損價、停利目標價調整要等下次成交或收盤才會寫回。
- **海外主機連不到證交所 MIS**：
  - 整張價改用 Yahoo Finance v8。
  - Yahoo v8 不提供委買委賣價：零股買進以現價 +1 檔、賣出以現價 −1 檔估算，標註【海外零股估算價】。
  - 同樣因為沒有五檔資料，「五檔委買比 ≥ 0.75」的買進觸發不會成立；停損價、停利目標價與巡檢照常運作。
- **休市日表只有 2026 年**：2027 年起請把證交所公告加進 `tools/tw_market_calendar.py` 的 `CLOSED_DAYS`。
- **暫停全部雲端排程**：Actions → `tw-market-autopilot` → 右上 `⋯` → **Disable workflow**。

> 僅供研究，非投資建議。模擬帳本與真實持股完全分開。
