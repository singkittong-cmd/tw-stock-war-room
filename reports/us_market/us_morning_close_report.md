# 🌅 美股收盤匯報（美股 2026-10-30（週五）收盤；對應台股 2026-11-02（週一））

> 資料抓取：`reports/us_market/us_semi_quotes.json`，fetched_at 2026-11-01 21:20:24（台北時間）。漲跌幅以正規盤收盤為準，盤後價僅供參考。

## 一句話結論
Intel 財測崩跌（-28.6%）與 Amazon 自由現金流因 AI 投資轉為大幅流出，重燃「AI 資本支出能否回本」疑慮，費半單日重挫 -7.20%、TSM ADR -5.95%，台股週一半導體與 AI 伺服器鏈開盤承壓幾成定局；ADR 對台股溢價仍有 +11.75%，2330 跳空下跌風險高，盤前不追、先看防守位。

## 指數與 ETF 收盤（^SOX、SMH、^IXIC、^GSPC）

| 指標 | 收盤 | 漲跌幅 | 日高 / 日低 | 量比 | 盤後 |
|---|---|---|---|---|---|
| ^SOX 費城半導體 | 10,688.80 | **-7.20%** | 10,957.32 / 10,439.01 | n/a | n/a |
| SMH 半導體 ETF | 522.49 | -6.98% | 535.32 / 511.63 | 1.96 | 526.71（盤後 +0.81%） |
| ^IXIC 那斯達克 | 25,437.00 | -2.86% | 25,673.00 / 25,177.33 | 0.97 | n/a |
| ^GSPC 標普 500 | 7,580.07 | -1.82% | 7,637.48 / 7,527.56 | 0.80 | n/a |

- SMH 量比 1.96，屬放量殺盤；那指與標普量比 < 1，賣壓集中在半導體與 AI 硬體，而非全面恐慌。
- 據 Reuters/Yahoo，那指與標普均為 6 月以來最差單日表現，晶片指數 10 月累計跌逾 15%（[Yahoo/Reuters](https://finance.yahoo.com/markets/stocks/articles/nasdaq-futures-sink-amazon-intel-114737736.html)、[Washington Times/AP](https://www.washingtontimes.com/news/2026/oct/30/wall-street-falls-worst-day-since-june-sell-tech-stocks-deepens/)）。

## 台積電 ADR 與折溢價（TSM 收盤、換算台股價、溢價率、盤後價）

| 項目 | 數值 |
|---|---|
| TSM ADR 收盤 | **406.13 美元（-5.95%）**，日高 418.09／日低 400.12，量比 1.91 |
| 盤後價 | 408.785 美元（盤後 +0.65%，僅供參考） |
| 美元兌台幣 | 32.605（+0.12%） |
| ADR 換算台股價 | 2,648.37 元 |
| 2330 台股收盤（2026-10-30） | 2,370 元 |
| 溢價率（正規盤） | **+11.75%** |
| 溢價率（盤後價） | +12.48% |

- 解讀：台股 10-30 收盤早於美股大跌，ADR -5.95% 尚未反映到 2330。溢價 +11.75% 代表 ADR 雖大跌但仍高於台股，溢價本身無法提供跳空下跌的緩衝，週一 2330 傳導方向以 ADR 跌幅為主要參考。
- 台幣微貶（32.605，+0.12%），外資匯出壓力需由角色 2 盤中驗證。
- 其他台股 ADR：**ASX（日月光）-11.34%**（量比 1.90）、**UMC（聯電）-7.54%**（量比 2.08），跌幅均大於 TSM。

## 次產業強弱（AI/ASIC、記憶體 MU→台股記憶體、設備、AI 伺服器與散熱 VRT→台股散熱）

| 次產業 | 美股指標（漲跌幅／量比） | 強弱 | 台股連動 |
|---|---|---|---|
| AI GPU/ASIC | NVDA -4.22%（1.67）、AMD **-10.88%**（2.03）、AVGO -5.48%（1.68）、MRVL **-10.05%**（1.97） | 最弱：AMD、MRVL 雙位數重挫 | 3661 世芯-KY、3443 創意、2454 聯發科 |
| 記憶體 | MU -5.30%（1.18）、WDC -7.41%（0.87） | 偏弱，但 MU 跌幅小於費半 | 2408 南亞科、2344 華邦電、8299 群聯 |
| 設備 | ASML -6.55%（1.42）、AMAT -7.49%（1.47）、LRCX -6.01%（1.83） | 偏弱，與費半同步 | 3131 弘塑、6187 萬潤、6488 環球晶、5483 中美晶、1560 中砂 |
| AI 伺服器與散熱 | DELL **-10.77%**（1.66）、VRT -5.46%（1.10）、SMCI -4.30%（1.08） | DELL 最弱 | 2317 鴻海、2382 廣達、3017 奇鋐、3324 雙鴻 |
| 台股 ADR | TSM -5.95%、UMC -7.54%、ASX -11.34% | 封測 ADR 最弱 | 2330、2303、3711、2449、3264、6257 |

- 相對強弱：NVDA（-4.22%）與 SMCI（-4.30%）跌幅最輕；AMD、MRVL、DELL、ASX 跌逾 10%，是本次殺盤重災區。
- 盤後各檔普遍小幅反彈 0.2%–0.8%（盤後，僅供參考），UMC（-0.24%）與 DELL（-0.03%）盤後仍弱。

## 重大新聞與財報（附來源連結）

1. **Intel 財測大幅低於預期，單日 -28.6%（1974 年以來最差）**：Q4 營收財測 150–160 億美元，低於 Q3 的 165 億；毛利率財測 38.0%（前季 44.1%）；調整後 EPS 財測 0.25 美元，低於市場共識 0.43 美元。
   來源：[StockTitan](https://www.stocktitan.net/articles/stock-market-today-live-october-30-2026)、[Yahoo/Reuters](https://finance.yahoo.com/markets/stocks/articles/nasdaq-futures-sink-amazon-intel-114737736.html)、[TheStreet](https://www.thestreet.com/stock-market-today/stock-market-today-dow-jones-sp-500-nasdaq-updates-oct-30-2026)
2. **Amazon 財報 EPS 1.89 美元，低於 LSEG 共識 1.96；近 12 個月自由現金流為流出 424 億美元，公司稱主因 AI 投資**；盤前一度 -7.14%，收盤 -1.41%。市場解讀為「AI 重資本支出能否回本」的疑慮再起。
   來源：[StockTitan](https://www.stocktitan.net/articles/stock-market-today-live-october-30-2026)、[Yahoo/Reuters](https://finance.yahoo.com/markets/stocks/articles/nasdaq-futures-sink-amazon-intel-114737736.html)
3. **AI 硬體鏈連帶殺盤**：Reuters 報導晶片指數 -7.2%、NVDA -4.2%（標普最大拖累）、AMD -10.9%、Dell -10.8%、HP -9.6%；Wealthspire 的 Oliver Pursche 指出本週焦點在「是否會有正的投資報酬」，未達高預期的公司遭重罰。
   來源：[Yahoo/Reuters](https://finance.yahoo.com/markets/stocks/articles/nasdaq-futures-sink-amazon-intel-114737736.html)、[Washington Times/AP](https://www.washingtontimes.com/news/2026/oct/30/wall-street-falls-worst-day-since-june-sell-tech-stocks-deepens/)
4. **總經**：美國就業成本指數（ECI）截至 9 月的三個月上升 0.8%，但報導指科技股賣壓在數據公布前後「幾乎未變」，非主因。
   來源：[StockTitan](https://www.stocktitan.net/articles/stock-market-today-live-october-30-2026)
5. **殖利率背景**：10 月上旬美 10 年期公債殖利率一度升至 5.365%，為 2002 年 4 月以來最高（10-07 報導），10-14 約在 5.22%；高利率環境是 10 月半導體股走弱的背景因素。10-30 當日殖利率收盤水準未取得可靠報導（見資料缺口）。
   來源：[Stocktwits](https://stocktwits.com/news-articles/markets/equity/s-and-p-500-and-nasdaq-100-slip-from-record-highs-amid-pressure-from-soaring-treasury-yields-spcx-amd-nvda-mrvl-in-focus/cZDvp0iRBZ9)、[The Star](https://www.thestar.com.my/news/world/2026/10/14/us-stocks-end-lower-amid-chip-selloff)
6. **10 月半導體弱勢脈絡**：SOX 於 10-07 至 10-16 連跌，為 2008 年 10 月以來最長連跌；10-22 Lam Research 提及近期供應鏈限制，AMD 當日 -8.10%。
   來源：[Stocktwits](https://stocktwits.com/news-articles/Markets/Equity/semiconductor-stocks-sox-losing-streak-2-trillion-ai-selloff/cZxKaOoRBsy)、[TIKR](https://www.tikr.com/blog/amd-stock-fell-8-in-a-day-as-lam-researchs-supply-warning-sank-chip-stocks-heres-where-the-stock-could-go-by-2030)

## 交給台股角色 1、2、3 的 3 個連動議題

1. **【晶圓代工與封測：2330／2303／3711／2449，持股 3264 欣銓、6257 矽格】** TSM ADR -5.95%（換算 2,648.37 元，仍溢價 +11.75%）、ASX ADR -11.34%、UMC ADR -7.54%，而台股 10-30 收盤尚未反映。
   - 角色 3：2330 週一跳空後是否守住防守支撐／MA20？3264、6257 是否同步跌破短均線並爆量？
   - 角色 2：外資週一對 2330、3711 是否轉為大額賣超、期貨淨空單是否擴大？盤中五檔委買/委賣比是否 < 0.8？
   - 角色 1：Intel 財測下修是否波及 2330 先進製程／封測（測試）訂單能見度？有無最新重訊或法說修正？

2. **【記憶體與矽晶圓：持股 2408 南亞科、5483 中美晶，龍頭 2344 華邦電、8299 群聯、6488 環球晶】** MU -5.30%（量比 1.18）、WDC -7.41%，跌幅接近或小於費半；設備 AMAT -7.49%、LRCX -6.01%、ASML -6.55%。
   - 角色 1：DRAM／NAND 合約價趨勢是否仍向上，足以抵消 AI 資本支出疑慮？Amazon 自由現金流 -424 億美元是否意味雲端資本支出放緩、拖累 HBM／伺服器 DRAM 需求？
   - 角色 2：2408、2344 融資是否在高檔、週一投信／外資是否同步調節？
   - 角色 3：2408 與 5483 是否已跌破絕對停損或防守支撐，是否需要啟動持倉第 5 節判定 A／B？

3. **【AI 伺服器、ASIC 與被動元件／銅箔：龍頭 2317 鴻海、2382 廣達、3661 世芯-KY、3443 創意、3017 奇鋐、3324 雙鴻、3037 欣興、2368 金像電；持股 2327 國巨、6173 信昌電、8358 金居】** AMD -10.88%、MRVL -10.05%、DELL -10.77%、AVGO -5.48%、VRT -5.46%，是本次殺盤最重的次產業。
   - 角色 1：Intel、Amazon 財報後，AI 伺服器出貨與 ASIC 案量是否有客戶砍單或遞延的實際證據？若沒有，可能只是情緒性殺盤。
   - 角色 2：3661、3443、2317、2382 外資是否出現「前日大買、次日大賣」的隔日沖現象？2327、8358 融資是否偏高、有無被動 ETF 調整？
   - 角色 3：3017、3324、2327、6173、8358 的 MA20 乖離與 RSI 是否已從過熱轉為超賣（RSI < 20 或乖離 < -10%），可否等到回測、止跌後再布局？

## 資料缺口

- **^SOX 量比與成交量**：檔案中 volume = 0、volume_ratio = null，無法判斷費半是否爆量。
- **10-30 當日美債殖利率收盤**：搜尋未取得可靠的收盤數字（Yahoo 頁面報價列顯示 10 年期 4.9290%、-0.06，但未標示時間，且與 10 月中旬 5.2% 以上報導有落差，不採用）。
- **Fed 政策／PCE**：未找到 10-30 當日相關報導，無法評估總經對殺盤的貢獻。
- **MRVL、MU、AVGO、ASML、VRT、TSM 的個股新聞**：檢索到的報導未說明個別原因，目前僅能歸因於整體 AI 資本支出疑慮，屬推論，未獲個股新聞證實。
- **AMD 跌幅**：Reuters 寫 -10.9%，與檔案的 -10.88% 一致；Dell 的 -10.8% 與檔案的 -10.77% 一致。
- 台股 2026-11-02 期貨夜盤／台指期盤前資料不在本角色資料範圍內，需由總分析員另行補足。
