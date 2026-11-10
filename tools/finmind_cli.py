# -*- coding: utf-8 -*-
"""FinMind 資料抓取與技術指標計算 CLI。

用法：
    C:/Users/singk/anaconda3/python.exe tools/finmind_cli.py --role fundamental --stock 2330
    C:/Users/singk/anaconda3/python.exe tools/finmind_cli.py --role chips --stock 2330 --mode intraday
    C:/Users/singk/anaconda3/python.exe tools/finmind_cli.py --role chips --stock 2330 --mode after_hours
    C:/Users/singk/anaconda3/python.exe tools/finmind_cli.py --role kline --stock 2330

輸出：標準輸出印出 Markdown 摘要，同時將結構化結果寫入 reports/data/<代號>_<role>.json。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import argparse
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

try:
    # 改用 Windows 憑證庫驗證 HTTPS（防毒軟體 / 代理攔截 TLS 時 certifi 會驗證失敗）
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.kline_patterns import analyze as zhu_analyze  # noqa: E402
from tools.yahoo_quote import YahooError, fetch_daily  # noqa: E402

REPORTS_DIR = ROOT / "reports"
DATA_DIR = REPORTS_DIR / "data"
SNAPSHOT_PATH = REPORTS_DIR / "intraday_snapshot.json"

load_dotenv(ROOT / ".env", encoding="utf-8")

API_URL = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.getenv("FINMIND_API_TOKEN", "").strip()
BIG_TICK_LOTS = 20

INST_GROUP = {
    "Foreign_Investor": "foreign",
    "Foreign_Dealer_Self": "foreign",
    "Investment_Trust": "trust",
    "Dealer_self": "dealer",
    "Dealer_Hedging": "dealer",
    "Dealer": "dealer",
}
INST_LABEL = {"foreign": "外資", "trust": "投信", "dealer": "自營商"}


class FinMindError(Exception):
    pass


# ---------------------------------------------------------------- 共用工具

def _date_str(d):
    return d.strftime("%Y-%m-%d")


def _days_ago(n):
    return _date_str(datetime.now() - timedelta(days=n))


def fetch_dataset(dataset, data_id=None, start_date=None, end_date=None, retries=2):
    """呼叫 FinMind v4 API，回傳 DataFrame；失敗丟出 FinMindError。"""
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    last_err = "unknown"
    for attempt in range(retries + 1):
        try:
            resp = requests.get(API_URL, params=params, headers=headers, timeout=30)
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_err = str(exc)
            time.sleep(1.5 * (attempt + 1))
            continue
        status = payload.get("status", resp.status_code)
        if status == 200:
            return pd.DataFrame(payload.get("data", []))
        last_err = f"HTTP {resp.status_code} status={status} msg={payload.get('msg')}"
        if resp.status_code in (400, 402, 403) or status in (400, 402, 403):
            break
        time.sleep(1.5 * (attempt + 1))
    raise FinMindError(f"{dataset}({data_id}) 抓取失敗：{last_err}")


def safe_fetch(dataset, data_id=None, start_date=None, end_date=None):
    try:
        return fetch_dataset(dataset, data_id, start_date, end_date), None
    except FinMindError as exc:
        return pd.DataFrame(), str(exc)


def tick_size(price):
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def round_tick(price, mode="nearest"):
    """依台股升降單位取價。mode: nearest / down / up。"""
    if price is None or not np.isfinite(price) or price <= 0:
        return None
    t = tick_size(price)
    q = price / t
    if mode == "down":
        q = math.floor(q + 1e-9)
    elif mode == "up":
        q = math.ceil(q - 1e-9)
    else:
        q = round(q)
    return round(q * t, 2)


def to_native(obj):
    if isinstance(obj, dict):
        return {str(k): to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_native(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return None if not np.isfinite(v) else round(v, 4)
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    return obj


def fmt(v, digits=2, suffix=""):
    if v is None:
        return "N/A"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not np.isfinite(f):
        return "N/A"
    return f"{f:,.{digits}f}{suffix}"


def md_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines)


def get_stock_name(stock_id):
    df, _ = safe_fetch("TaiwanStockInfo", stock_id)
    if not df.empty and "stock_name" in df.columns:
        return str(df["stock_name"].iloc[0])
    return stock_id


def save_json(stock_id, role, data, mode=None, source="tools/finmind_cli.py"):
    """寫入 reports/data/<代號>_<role>[_<mode>].json；外層附 fetched_at 供分析員判斷資料新鮮度。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{stock_id}_{role}" + (f"_{mode}" if mode else "") + ".json"
    path = DATA_DIR / name
    payload = {
        "stock_id": stock_id,
        "role": role,
        "mode": mode,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "data": data,
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(to_native(payload), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- 價格與技術指標（K線技術人與 dashboard 共用）

def fetch_price(stock_id, days=400):
    df = fetch_dataset("TaiwanStockPrice", stock_id, _days_ago(days))
    if df.empty:
        raise FinMindError(f"TaiwanStockPrice({stock_id}) 無資料")
    df = df.rename(columns={"max": "high", "min": "low", "Trading_Volume": "volume"})
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["close"] > 0].copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def compute_indicators(df):
    df = df.copy()
    close = df["close"]
    df["volume_lots"] = df["volume"] / 1000.0
    for n in (5, 10, 20, 60):
        df[f"ma{n}"] = close.rolling(n).mean()
    df["vol_ma5"] = df["volume_lots"].rolling(5).mean()
    df["vol_ma20"] = df["volume_lots"].rolling(20).mean()
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd_dif"] = ema12 - ema26
    df["macd_dea"] = df["macd_dif"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd_dif"] - df["macd_dea"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi14"] = 100 - 100 / (1 + rs)
    df.loc[avg_loss == 0, "rsi14"] = 100.0
    df["bias20"] = (close - df["ma20"]) / df["ma20"] * 100
    return df


def price_with_indicators(stock_id, days=400):
    return compute_indicators(fetch_price(stock_id, days))


def fetch_price_yahoo(stock_id, rng="6mo"):
    """Yahoo 日 K 轉成與 fetch_price 相同欄位（volume 為股數），不耗 FinMind 額度、收盤後即有今日 K 棒。"""
    rows = fetch_daily(stock_id, rng)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["close"] > 0].drop_duplicates("date", keep="last")
    return df.sort_values("date").reset_index(drop=True)


def kline_price(stock_id):
    """K 線資料來源：Yahoo 6 個月日 K 優先，失敗才用 FinMind。回傳 (DataFrame, 來源說明)。"""
    try:
        df = fetch_price_yahoo(stock_id)
        if len(df) >= 60:
            return df, "Yahoo Finance 日 K（6mo，不耗 FinMind 額度）"
    except YahooError:
        pass
    return fetch_price(stock_id, 400), "FinMind TaiwanStockPrice"


_TAIEX_CACHE = {}
_TAIEX_LOCK = threading.Lock()


def fetch_taiex(days=400):
    """加權指數每次執行只抓一次（batch_prefetch 多執行緒共用）：Yahoo ^TWII 優先，失敗才用 FinMind。"""
    with _TAIEX_LOCK:
        if days not in _TAIEX_CACHE:
            try:
                _TAIEX_CACHE[days] = fetch_price_yahoo("^TWII")
            except YahooError:
                _TAIEX_CACHE[days] = fetch_price("TAIEX", days)
        return _TAIEX_CACHE[days]


def _period_return(series, n):
    if len(series) <= n:
        return None
    base = series.iloc[-n - 1]
    return (series.iloc[-1] / base - 1) * 100 if base else None


def role_kline(stock_id):
    raw, price_source = kline_price(stock_id)
    df = compute_indicators(raw)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    close = float(last["close"])

    taiex_err = None
    rel = {}
    try:
        tx = fetch_taiex(400)
        for n in (5, 20, 60):
            s_ret = _period_return(df["close"], n)
            t_ret = _period_return(tx["close"], n)
            rel[n] = {
                "stock_ret": s_ret,
                "taiex_ret": t_ret,
                "excess": (s_ret - t_ret) if s_ret is not None and t_ret is not None else None,
            }
        taiex_close = float(tx["close"].iloc[-1])
    except FinMindError as exc:
        taiex_err = str(exc)
        taiex_close = None

    low20 = float(df["low"].tail(20).min())
    high20 = float(df["high"].tail(20).max())
    low60 = float(df["low"].tail(60).min())
    high60 = float(df["high"].tail(60).max())
    ma5, ma10, ma20, ma60 = (float(last[c]) if pd.notna(last[c]) else None for c in ("ma5", "ma10", "ma20", "ma60"))

    support_cands = [v for v in (ma20, low20) if v is not None and v < close]
    support = max(support_cands) if support_cands else low20
    support = round_tick(support, "down")
    stop_loss = round_tick(min(support, low20) * 0.98, "down")
    r1_raw = high20 if high20 > close * 1.005 else close * 1.05
    r1 = round_tick(r1_raw, "up")
    r2_raw = high60 if high60 > r1 * 1.01 else r1 * 1.05
    r2 = round_tick(r2_raw, "up")

    macd_cross = "無"
    recent = df.tail(6).reset_index(drop=True)
    for i in range(1, len(recent)):
        a, b = recent.loc[i - 1], recent.loc[i]
        if a["macd_dif"] <= a["macd_dea"] and b["macd_dif"] > b["macd_dea"]:
            macd_cross = f"{b['date'].strftime('%Y-%m-%d')} 黃金交叉"
        elif a["macd_dif"] >= a["macd_dea"] and b["macd_dif"] < b["macd_dea"]:
            macd_cross = f"{b['date'].strftime('%Y-%m-%d')} 死亡交叉"

    if ma5 and ma20 and ma60 and ma5 > ma20 > ma60:
        ma_align = "多頭排列（MA5 > MA20 > MA60）"
    elif ma5 and ma20 and ma60 and ma5 < ma20 < ma60:
        ma_align = "空頭排列（MA5 < MA20 < MA60）"
    else:
        ma_align = "均線糾結 / 排列未定"

    rsi = float(last["rsi14"]) if pd.notna(last["rsi14"]) else None
    bias = float(last["bias20"]) if pd.notna(last["bias20"]) else None
    vol_ratio = float(last["volume_lots"] / last["vol_ma20"]) if last["vol_ma20"] else None

    score = 0
    score += 1 if ma20 and close > ma20 else -1
    score += 1 if ma20 and ma60 and ma20 > ma60 else -1
    score += 1 if last["macd_dif"] > last["macd_dea"] else -1
    if rsi is not None:
        if 50 <= rsi <= 70:
            score += 1
        elif rsi > 80 or rsi < 30:
            score -= 1
    if bias is not None and abs(bias) > 10:
        score -= 1
    hint = "偏多" if score >= 2 else "偏空" if score <= -2 else "中性"

    data = {
        "stock_id": stock_id,
        "price_source": price_source,
        "date": last["date"].strftime("%Y-%m-%d"),
        "close": close,
        "change": close - float(prev["close"]),
        "change_pct": (close / float(prev["close"]) - 1) * 100,
        "volume_lots": float(last["volume_lots"]),
        "vol_ma5": float(last["vol_ma5"]),
        "vol_ma20": float(last["vol_ma20"]),
        "vol_ratio_20": vol_ratio,
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "ma_alignment": ma_align,
        "macd_dif": float(last["macd_dif"]),
        "macd_dea": float(last["macd_dea"]),
        "macd_hist": float(last["macd_hist"]),
        "macd_recent_cross": macd_cross,
        "rsi14": rsi,
        "bias20_pct": bias,
        "low20": low20, "high20": high20, "low60": low60, "high60": high60,
        "taiex_close": taiex_close,
        "relative_strength": rel,
        "taiex_error": taiex_err,
        "levels": {
            "defense_support": support,
            "stop_loss": stop_loss,
            "resistance_1": r1,
            "resistance_2": r2,
        },
        "quant_hint": hint,
        "quant_score": score,
        # K線技術人（朱家泓戰法）：趨勢定多空、四線排列與扣抵、葛蘭碧、關鍵轉折 K、紀律停損
        "zhu_analysis": zhu_analyze(df),
        "daily_30d": [
            {"date": r["date"].strftime("%Y-%m-%d"), "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]), "volume_lots": float(r["volume_lots"]),
             "ma5": r["ma5"], "ma10": r["ma10"], "ma20": r["ma20"], "ma60": r["ma60"], "rsi14": r["rsi14"],
             "macd_hist": r["macd_hist"], "bias20_pct": r["bias20"]}
            for _, r in df.tail(30).iterrows()
        ],
    }

    rows = []
    for _, r in df.tail(10).iterrows():
        rows.append([
            r["date"].strftime("%m-%d"), fmt(r["open"]), fmt(r["high"]), fmt(r["low"]), fmt(r["close"]),
            fmt(r["volume_lots"], 0), fmt(r["ma5"]), fmt(r["ma20"]), fmt(r["rsi14"], 1),
        ])
    rel_rows = [[f"{n} 日", fmt(v["stock_ret"], 2, "%"), fmt(v["taiex_ret"], 2, "%"), fmt(v["excess"], 2, "%")]
                for n, v in rel.items()]
    md = [
        f"# K線技術人 K 線量價原始數據：{stock_id}（資料日 {data['date']}）",
        "",
        f"- 收盤 {fmt(close)}，漲跌 {fmt(data['change'])}（{fmt(data['change_pct'], 2, '%')}）",
        f"- 成交量 {fmt(data['volume_lots'], 0)} 張；5 日均量 {fmt(data['vol_ma5'], 0)}；20 日均量 {fmt(data['vol_ma20'], 0)}；量比(對20均) {fmt(vol_ratio)}",
        f"- MA5 {fmt(ma5)} / MA20 {fmt(ma20)} / MA60 {fmt(ma60)} → {ma_align}",
        f"- MACD(12,26,9)：DIF {fmt(data['macd_dif'])} / DEA {fmt(data['macd_dea'])} / 柱 {fmt(data['macd_hist'])}；近 5 日交叉：{macd_cross}",
        f"- RSI(14) {fmt(rsi, 1)}；MA20 乖離 {fmt(bias, 2, '%')}",
        f"- 20 日區間 {fmt(low20)} ~ {fmt(high20)}；60 日區間 {fmt(low60)} ~ {fmt(high60)}",
        "",
        "## 演算法定錨價位（供分析員複核，非最終結論）",
        f"- 防守支撐價：{fmt(support)}（MA20 與 20 日低點中、位於現價下方者取高）",
        f"- 絕對停損價：{fmt(stop_loss)}（min(防守支撐, 20 日低) × 0.98，依升降單位下取）",
        f"- 第一壓力價：{fmt(r1)}（20 日高；若已突破則取現價 +5%）",
        f"- 第二壓力價：{fmt(r2)}（60 日高；若不足則取第一壓力 +5%）",
        f"- 量化傾向：{hint}（分數 {score}）",
        "",
        "## 相對加權指數（TAIEX）強弱",
        md_table(["期間", "個股報酬", "TAIEX 報酬", "超額報酬"], rel_rows) if rel_rows else f"TAIEX 抓取失敗：{taiex_err}",
        "",
        "## 近 10 日 K 線",
        md_table(["日期", "開", "高", "低", "收", "量(張)", "MA5", "MA20", "RSI"], rows),
    ]
    return "\n".join(md), data


# ---------------------------------------------------------------- 新聞達人：基本面

def role_fundamental(stock_id):
    errors = []
    name = get_stock_name(stock_id)

    rev, err = safe_fetch("TaiwanStockMonthRevenue", stock_id, _days_ago(800))
    if err:
        errors.append(err)
    rev_rows, rev_data, ytd = [], [], {}
    if not rev.empty:
        rev["revenue"] = pd.to_numeric(rev["revenue"], errors="coerce")
        rev["ym"] = rev["revenue_year"].astype(int) * 100 + rev["revenue_month"].astype(int)
        rev = rev.drop_duplicates("ym").sort_values("ym").reset_index(drop=True)
        by_ym = dict(zip(rev["ym"], rev["revenue"]))
        for _, r in rev.tail(6).iloc[::-1].iterrows():
            y, m = int(r["revenue_year"]), int(r["revenue_month"])
            prev_ym = (y - 1) * 100 + 12 if m == 1 else y * 100 + m - 1
            mom = (r["revenue"] / by_ym[prev_ym] - 1) * 100 if by_ym.get(prev_ym) else None
            yoy_base = by_ym.get((y - 1) * 100 + m)
            yoy = (r["revenue"] / yoy_base - 1) * 100 if yoy_base else None
            rev_data.append({"year": y, "month": m, "revenue": float(r["revenue"]), "mom_pct": mom, "yoy_pct": yoy})
            rev_rows.append([f"{y}-{m:02d}", fmt(r["revenue"] / 1e8, 2), fmt(mom, 2, "%"), fmt(yoy, 2, "%")])
        ly, lm = int(rev["revenue_year"].iloc[-1]), int(rev["revenue_month"].iloc[-1])
        cur = sum(by_ym.get(ly * 100 + m, 0) for m in range(1, lm + 1))
        last_year = [by_ym.get((ly - 1) * 100 + m) for m in range(1, lm + 1)]
        if all(v for v in last_year):
            ytd = {"year": ly, "through_month": lm, "revenue": cur, "yoy_pct": (cur / sum(last_year) - 1) * 100}

    fs, err = safe_fetch("TaiwanStockFinancialStatements", stock_id, _days_ago(800))
    if err:
        errors.append(err)
    fs_rows, fs_data = [], []
    if not fs.empty:
        fs["value"] = pd.to_numeric(fs["value"], errors="coerce")
        pv = fs.pivot_table(index="date", columns="type", values="value", aggfunc="last").sort_index()

        def pick(row, keys):
            for k in keys:
                if k in row.index and pd.notna(row[k]):
                    return float(row[k])
            return None

        for d, row in pv.tail(5).iloc[::-1].iterrows():
            revenue = pick(row, ["Revenue", "OperatingRevenue"])
            gp = pick(row, ["GrossProfit"])
            op = pick(row, ["OperatingIncome"])
            ni = pick(row, ["IncomeAfterTaxes", "NetIncome", "EquityAttributableToOwnersOfParent"])
            eps = pick(row, ["EPS"])
            gm = gp / revenue * 100 if revenue and gp is not None else None
            om = op / revenue * 100 if revenue and op is not None else None
            nm = ni / revenue * 100 if revenue and ni is not None else None
            fs_data.append({"date": str(d), "revenue": revenue, "gross_margin_pct": gm,
                            "operating_margin_pct": om, "net_margin_pct": nm, "eps": eps})
            fs_rows.append([str(d), fmt(revenue / 1e8 if revenue else None, 1), fmt(gm, 2, "%"),
                            fmt(om, 2, "%"), fmt(nm, 2, "%"), fmt(eps)])

    news, err = safe_fetch("TaiwanStockNews", stock_id, _days_ago(14), _date_str(datetime.now()))
    if err or news.empty:
        frames = []
        for i in range(14, -1, -1):
            d = _days_ago(i)
            day_df, day_err = safe_fetch("TaiwanStockNews", stock_id, d, d)
            if not day_df.empty:
                frames.append(day_df)
            elif day_err and "402" in day_err:
                errors.append(day_err)
                break
        news = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if news.empty and err:
            errors.append(err)
    news_data = []
    if not news.empty:
        news = news.drop_duplicates("title").sort_values("date", ascending=False).head(30)
        for _, r in news.iterrows():
            news_data.append({"date": str(r.get("date")), "source": r.get("source", ""),
                              "title": r.get("title", ""), "link": r.get("link", "")})

    data = {"stock_id": stock_id, "stock_name": name, "monthly_revenue": rev_data, "ytd_revenue": ytd,
            "financials": fs_data, "news": news_data, "errors": errors}
    md = [
        f"# 新聞達人 基本面原始數據：{stock_id} {name}",
        "",
        "## 近 6 個月營收（單位：億元）",
        md_table(["年月", "營收", "MoM", "YoY"], rev_rows) if rev_rows else "（無資料）",
    ]
    if ytd:
        md.append(f"\n- {ytd['year']} 年 1-{ytd['through_month']} 月累計營收 {fmt(ytd['revenue'] / 1e8, 1)} 億，YoY {fmt(ytd['yoy_pct'], 2, '%')}")
    md += [
        "",
        "## 近 5 季損益（營收單位：億元）",
        md_table(["季別", "營收", "毛利率", "營益率", "淨利率", "EPS"], fs_rows) if fs_rows else "（無資料）",
        "",
        "## 近 14 日新聞",
    ]
    md += [f"- {n['date'][:16]}｜{n['source']}｜{n['title']}" for n in news_data] or ["（無資料）"]
    if errors:
        md += ["", "## 資料缺口"] + [f"- {e}" for e in errors]
    return "\n".join(md), data


# ---------------------------------------------------------------- 籌碼王：籌碼

def load_snapshot(stock_id):
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    stock = snap.get("stocks", {}).get(stock_id)
    if stock is not None:
        stock = dict(stock)
        stock["snapshot_updated_at"] = snap.get("updated_at")
    return stock


def role_chips_intraday(stock_id):
    errors = []
    today = _date_str(datetime.now())
    tick, err = safe_fetch("TaiwanStockPriceTick", stock_id, today)
    if err:
        errors.append(err)
    data = {"stock_id": stock_id, "mode": "intraday", "date": today, "tick": None, "snapshot": None, "errors": errors}
    md = [f"# 籌碼王 盤中籌碼原始數據：{stock_id}（{today}）", ""]

    if not tick.empty:
        tick["volume"] = pd.to_numeric(tick["volume"], errors="coerce")
        tick["deal_price"] = pd.to_numeric(tick["deal_price"], errors="coerce")
        tick["TickType"] = pd.to_numeric(tick["TickType"], errors="coerce")
        outer = float(tick.loc[tick["TickType"] == 2, "volume"].sum())
        inner = float(tick.loc[tick["TickType"] == 1, "volume"].sum())
        total = float(tick["volume"].sum())
        vwap = float((tick["deal_price"] * tick["volume"]).sum() / total) if total else None
        big = tick[tick["volume"] >= BIG_TICK_LOTS]
        big_buy = float(big.loc[big["TickType"] == 2, "volume"].sum())
        big_sell = float(big.loc[big["TickType"] == 1, "volume"].sum())
        recent_big = [{"time": str(r["Time"]), "price": float(r["deal_price"]), "lots": float(r["volume"]),
                       "side": "外盤" if r["TickType"] == 2 else "內盤" if r["TickType"] == 1 else "未明"}
                      for _, r in big.tail(15).iterrows()]
        data["tick"] = {
            "outer_lots": outer, "inner_lots": inner, "total_lots": total,
            "outer_inner_ratio": outer / inner if inner else None, "vwap": vwap,
            "last_price": float(tick["deal_price"].iloc[-1]),
            "big_tick_threshold": BIG_TICK_LOTS, "big_tick_count": int(len(big)),
            "big_buy_lots": big_buy, "big_sell_lots": big_sell, "recent_big_ticks": recent_big,
        }
        md += [
            "## FinMind 逐筆成交（TaiwanStockPriceTick）",
            f"- 最新成交 {fmt(data['tick']['last_price'])}；VWAP {fmt(vwap)}",
            f"- 外盤 {fmt(outer, 0)} 張 / 內盤 {fmt(inner, 0)} 張 / 總量 {fmt(total, 0)} 張；內外盤比(外/內) {fmt(data['tick']['outer_inner_ratio'])}",
            f"- {BIG_TICK_LOTS} 張以上大單 {len(big)} 筆：大單外盤 {fmt(big_buy, 0)} 張、大單內盤 {fmt(big_sell, 0)} 張",
            "",
            md_table(["時間", "價格", "張數", "方向"], [[b["time"], fmt(b["price"]), fmt(b["lots"], 0), b["side"]] for b in recent_big])
            if recent_big else "（今日尚無大單）",
            "",
        ]
    else:
        md += ["## FinMind 逐筆成交", "- 無資料（非交易時段、尚未開盤或 Token 權限不足），改讀證交所五檔快照。", ""]

    snap = load_snapshot(stock_id)
    data["snapshot"] = snap
    md.append("## 證交所 MIS 五檔快照（reports/intraday_snapshot.json）")
    if snap:
        bids = snap.get("bids", [])
        asks = snap.get("asks", [])
        bid_vols = snap.get("bid_vols", [])
        ask_vols = snap.get("ask_vols", [])
        md += [
            f"- 快照時間 {snap.get('snapshot_updated_at')}；成交 {fmt(snap.get('price'))}（{fmt(snap.get('pct_change'), 2, '%')}）；累計量 {fmt(snap.get('acc_vol'), 0)} 張",
            f"- 五檔委買比 {fmt(snap.get('bid_ratio'), 3)}（<0.45 代表賣壓主導）；最近一筆 {fmt(snap.get('tick_vol'), 0)} 張，判定 {snap.get('last_side', '未明')}",
            "",
            md_table(["檔位", "委買價", "委買量", "委賣價", "委賣量"],
                     [[i + 1,
                       fmt(bids[i]) if i < len(bids) else "-", fmt(bid_vols[i], 0) if i < len(bid_vols) else "-",
                       fmt(asks[i]) if i < len(asks) else "-", fmt(ask_vols[i], 0) if i < len(ask_vols) else "-"]
                      for i in range(5)]),
        ]
        large = snap.get("recent_large_ticks", [])
        if large:
            md += ["", "### 哨兵記錄之大單", md_table(["時間", "價格", "張數", "方向"],
                                                   [[t.get("time"), fmt(t.get("price")), fmt(t.get("lots"), 0), t.get("side")] for t in large[-15:]])]
    else:
        md.append("- 無快照（哨兵 intraday_sentinel.py 未運行）")
    if errors:
        md += ["", "## 資料缺口"] + [f"- {e}" for e in errors]
    return "\n".join(md), data


def institutional_net(stock_id, days=120):
    """回傳以日期為索引、欄位 foreign/trust/dealer 的買賣超張數 DataFrame。"""
    inst = fetch_dataset("TaiwanStockInstitutionalInvestorsBuySell", stock_id, _days_ago(days))
    if inst.empty:
        return pd.DataFrame(columns=["foreign", "trust", "dealer"])
    inst["buy"] = pd.to_numeric(inst["buy"], errors="coerce")
    inst["sell"] = pd.to_numeric(inst["sell"], errors="coerce")
    inst["group"] = inst["name"].map(INST_GROUP)
    inst = inst.dropna(subset=["group"])
    inst["net_lots"] = (inst["buy"] - inst["sell"]) / 1000.0
    pv = inst.pivot_table(index="date", columns="group", values="net_lots", aggfunc="sum").fillna(0.0)
    for c in ("foreign", "trust", "dealer"):
        if c not in pv.columns:
            pv[c] = 0.0
    pv.index = pd.to_datetime(pv.index)
    return pv[["foreign", "trust", "dealer"]].sort_index()


def _streak(series):
    vals = list(series)
    if not vals or vals[-1] == 0:
        return 0
    sign = 1 if vals[-1] > 0 else -1
    n = 0
    for v in reversed(vals):
        if v * sign > 0:
            n += 1
        else:
            break
    return n * sign


def role_chips_after_hours(stock_id):
    errors = []
    data = {"stock_id": stock_id, "mode": "after_hours", "institutional": None, "margin": None,
            "shareholding": None, "errors": errors}
    md = [f"# 籌碼王 盤後籌碼原始數據：{stock_id}", ""]

    try:
        pv = institutional_net(stock_id, 60)
    except FinMindError as exc:
        errors.append(str(exc))
        pv = pd.DataFrame()
    if not pv.empty:
        summary = {}
        for c in ("foreign", "trust", "dealer"):
            s = pv[c]
            summary[c] = {"last": float(s.iloc[-1]), "sum5": float(s.tail(5).sum()),
                          "sum20": float(s.tail(20).sum()), "streak_days": _streak(s)}
        f10 = pv["foreign"].tail(10).tolist()
        flips = sum(1 for a, b in zip(f10, f10[1:]) if a * b < 0 and abs(b) >= 0.5 * abs(a))
        summary["foreign_flip_count_10d"] = flips
        summary["latest_date"] = pv.index[-1].strftime("%Y-%m-%d")
        summary["daily"] = [{"date": d.strftime("%Y-%m-%d"), **{c: float(r[c]) for c in ("foreign", "trust", "dealer")}}
                            for d, r in pv.tail(30).iterrows()]
        data["institutional"] = summary
        md += [
            f"## 三大法人買賣超（張，最新 {summary['latest_date']}）",
            md_table(["法人", "最新一日", "近 5 日", "近 20 日", "連續天數(+買/-賣)"],
                     [[INST_LABEL[c], fmt(summary[c]["last"], 0), fmt(summary[c]["sum5"], 0),
                       fmt(summary[c]["sum20"], 0), summary[c]["streak_days"]] for c in ("foreign", "trust", "dealer")]),
            f"- 外資近 10 日買賣方向急翻次數：{flips}（≥3 次需提防隔日沖 / 短線當沖型外資）",
            "",
            md_table(["日期", "外資", "投信", "自營商"],
                     [[d["date"], fmt(d["foreign"], 0), fmt(d["trust"], 0), fmt(d["dealer"], 0)] for d in summary["daily"][-10:]]),
            "",
        ]

    mg, err = safe_fetch("TaiwanStockMarginPurchaseShortSale", stock_id, _days_ago(45))
    if err:
        errors.append(err)
    if not mg.empty:
        for c in ("MarginPurchaseTodayBalance", "MarginPurchaseYesterdayBalance",
                  "ShortSaleTodayBalance", "ShortSaleYesterdayBalance"):
            mg[c] = pd.to_numeric(mg[c], errors="coerce")
        mg = mg.sort_values("date")
        rows, daily = [], []
        for _, r in mg.tail(10).iterrows():
            m_chg = r["MarginPurchaseTodayBalance"] - r["MarginPurchaseYesterdayBalance"]
            s_chg = r["ShortSaleTodayBalance"] - r["ShortSaleYesterdayBalance"]
            ratio = r["ShortSaleTodayBalance"] / r["MarginPurchaseTodayBalance"] * 100 if r["MarginPurchaseTodayBalance"] else None
            daily.append({"date": r["date"], "margin_balance": r["MarginPurchaseTodayBalance"], "margin_change": m_chg,
                          "short_balance": r["ShortSaleTodayBalance"], "short_change": s_chg, "short_margin_ratio_pct": ratio})
            rows.append([r["date"], fmt(r["MarginPurchaseTodayBalance"], 0), fmt(m_chg, 0),
                         fmt(r["ShortSaleTodayBalance"], 0), fmt(s_chg, 0), fmt(ratio, 2, "%")])
        m20 = mg.tail(20)
        data["margin"] = {
            "daily": daily,
            "margin_change_20d": float(m20["MarginPurchaseTodayBalance"].iloc[-1] - m20["MarginPurchaseYesterdayBalance"].iloc[0]),
            "short_change_20d": float(m20["ShortSaleTodayBalance"].iloc[-1] - m20["ShortSaleYesterdayBalance"].iloc[0]),
        }
        md += [
            "## 融資融券（張）",
            md_table(["日期", "融資餘額", "融資增減", "融券餘額", "融券增減", "券資比"], rows),
            f"- 近 20 日融資增減 {fmt(data['margin']['margin_change_20d'], 0)} 張；融券增減 {fmt(data['margin']['short_change_20d'], 0)} 張",
            "",
        ]

    sh, err = safe_fetch("TaiwanStockShareholding", stock_id, _days_ago(45))
    if err:
        errors.append(err)
    if not sh.empty and "ForeignInvestmentSharesRatio" in sh.columns:
        sh["ForeignInvestmentSharesRatio"] = pd.to_numeric(sh["ForeignInvestmentSharesRatio"], errors="coerce")
        sh = sh.sort_values("date")
        latest = float(sh["ForeignInvestmentSharesRatio"].iloc[-1])
        base = sh["ForeignInvestmentSharesRatio"].tail(20).iloc[0]
        data["shareholding"] = {"date": sh["date"].iloc[-1], "foreign_ratio_pct": latest,
                                "change_20d_pct_points": float(latest - base)}
        md += [
            "## 外資持股比率",
            f"- {sh['date'].iloc[-1]}：{fmt(latest, 2, '%')}；近 20 交易日變化 {fmt(latest - base, 2)} 個百分點",
            "",
        ]
    if errors:
        md += ["## 資料缺口"] + [f"- {e}" for e in errors]
    return "\n".join(md), data


# ---------------------------------------------------------------- 主程式

def main():
    parser = argparse.ArgumentParser(description="FinMind 台股資料 CLI（四角色分析員共用）")
    parser.add_argument("--role", required=True, choices=["fundamental", "chips", "kline"])
    parser.add_argument("--stock", required=True, help="股票代號，例如 2330")
    parser.add_argument("--mode", default="after_hours", choices=["intraday", "after_hours"],
                        help="僅 chips 角色使用")
    parser.add_argument("--no-save", action="store_true", help="不寫入 reports/data JSON")
    args = parser.parse_args()
    stock = args.stock.strip()

    try:
        if args.role == "fundamental":
            md, data = role_fundamental(stock)
            mode = None
        elif args.role == "kline":
            md, data = role_kline(stock)
            mode = None
        elif args.mode == "intraday":
            md, data = role_chips_intraday(stock)
            mode = "intraday"
        else:
            md, data = role_chips_after_hours(stock)
            mode = "after_hours"
    except FinMindError as exc:
        print(f"[錯誤] {exc}")
        sys.exit(2)

    print(md)
    if not args.no_save:
        path = save_json(stock, args.role, data, mode)
        print(f"\n> 結構化資料已寫入 {path.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
