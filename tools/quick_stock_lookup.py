# -*- coding: utf-8 -*-
"""個股名稱／代號即時快查：輸入「台積電」「奇鋐」「2408」等，1–2 秒內回傳結構化快篩結果（0 Token）。

用法：
    C:/Users/singk/anaconda3/python.exe tools/quick_stock_lookup.py 華邦電
    C:/Users/singk/anaconda3/python.exe tools/quick_stock_lookup.py 2408 --json

名稱解析順序：內建常用字典 → reports/portfolio.json → reports/sector_watchlist.json → 本機快取
（reports/data/stock_name_cache.json）→ Yahoo 台股搜尋 → 證交所代號查詢（查到後寫回快取）。
快篩內容（三路平行抓取）：
- 即時報價：證交所 MIS + Yahoo 雙引擎（現價、漲跌、今日高低、VWAP、五檔委買/委賣比、當盤方向）。
- K線技術人（朱家泓）：Yahoo 日 K 6 個月 → 趨勢定多空、四線排列、葛蘭碧、轉折 K、紀律停損。
- 籌碼王（權證小哥）：reports/data/<代號>_chips.json；沒有預抓檔時即時抓 FinMind 法人與融資（4 秒上限）。
- 巴董事長：依趨勢、籌碼、VWAP 與不對稱風報比給一句話操作建議。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import finmind_cli as fm  # noqa: E402
from tools import kline_patterns  # noqa: E402
from tools import realtime_quote_engine as rq  # noqa: E402

REPORTS = ROOT / "reports"
DATA_DIR = REPORTS / "data"
NAME_CACHE = DATA_DIR / "stock_name_cache.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
YAHOO_SEARCH = "https://tw.stock.yahoo.com/_td-stock/api/resource/AutocompleteService;query="
TWSE_SEARCH = "https://www.twse.com.tw/rwd/zh/api/codeQuery"
CHIPS_TIMEOUT_SEC = 4.0
CODE_RE = re.compile(r"^\d{4,6}[A-Z]?$")

# 台股常用個股與 ETF（代號 → 名稱）；其餘由 portfolio、20 強觀察池與線上搜尋補足
COMMON = {
    "2330": "台積電", "2317": "鴻海", "2454": "聯發科", "2308": "台達電", "2382": "廣達", "2303": "聯電",
    "2412": "中華電", "2881": "富邦金", "2882": "國泰金", "2891": "中信金", "2886": "兆豐金", "2884": "玉山金",
    "2885": "元大金", "2892": "第一金", "2880": "華南金", "5880": "合庫金", "2883": "凱基金", "2887": "台新金",
    "2357": "華碩", "2353": "宏碁", "2356": "英業達", "2324": "仁寶", "3231": "緯創", "6669": "緯穎",
    "2376": "技嘉", "2377": "微星", "2395": "研華", "3017": "奇鋐", "3324": "雙鴻", "2345": "智邦",
    "3711": "日月光投控", "2408": "南亞科", "2344": "華邦電", "2337": "旺宏", "8299": "群聯", "3260": "威剛",
    "3034": "聯詠", "2379": "瑞昱", "3661": "世芯-KY", "3443": "創意", "3035": "智原", "6643": "M31",
    "3529": "力旺", "5274": "信驊", "3008": "大立光", "2327": "國巨", "2492": "華新科", "3037": "欣興",
    "8046": "南電", "3189": "景碩", "2368": "金像電", "6274": "台燿", "2383": "台光電", "3131": "弘塑",
    "6187": "萬潤", "3583": "辛耘", "6488": "環球晶", "5483": "中美晶", "6147": "頎邦", "2449": "京元電子",
    "6239": "力成", "3264": "欣銓", "6257": "矽格", "6173": "信昌電", "8358": "金居", "1301": "台塑",
    "1303": "南亞", "1326": "台化", "6505": "台塑化", "2002": "中鋼", "1216": "統一", "2603": "長榮",
    "2609": "陽明", "2615": "萬海", "2618": "長榮航", "2610": "華航", "2207": "和泰車", "1101": "台泥",
    "2301": "光寶科", "2360": "致茂", "3653": "健策", "3665": "貿聯-KY", "4938": "和碩", "2474": "可成",
    "0050": "元大台灣50", "0056": "元大高股息", "00878": "國泰永續高股息", "00919": "群益台灣精選高息",
    "00929": "復華台灣科技優息", "006208": "富邦台50",
}


# ---------------------------------------------------------------- 名稱 ⇄ 代號

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save_cache(cache):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = NAME_CACHE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    tmp.replace(NAME_CACHE)


def load_name_map():
    """回傳 {代號: 名稱}；後者覆蓋前者：內建 → 快取 → 20 強 → 持股（持股名稱去掉結尾 *）。"""
    names = dict(COMMON)
    names.update(_read_json(NAME_CACHE, {}))
    for r in _read_json(REPORTS / "sector_watchlist.json", []):
        if r.get("stock_id") and r.get("name"):
            names[str(r["stock_id"])] = r["name"]
    for p in _read_json(REPORTS / "portfolio.json", []):
        if p.get("stock_id") and p.get("name"):
            names[str(p["stock_id"])] = str(p["name"]).rstrip("*")
    return names


def _norm(name):
    return re.sub(r"[\s\-－]|KY$", "", str(name or "")).upper()


def _remote_search(query):
    """線上搜尋（Yahoo 台股優先，證交所備援），只收 4 碼個股與 00 開頭 ETF，回傳 [(代號, 名稱, 市場)]。"""
    found = []
    try:
        resp = requests.get(YAHOO_SEARCH + urllib.parse.quote(query), headers=HEADERS, timeout=3)
        for r in (resp.json().get("ResultSet") or {}).get("Result") or []:
            sym = str(r.get("symbol", ""))
            code, _, suffix = sym.partition(".")
            if suffix in ("TW", "TWO") and (len(code) == 4 or code.startswith("00")) and code.isdigit():
                found.append((code, r.get("name") or code, "otc" if suffix == "TWO" else "tse"))
    except (requests.RequestException, ValueError):
        pass
    if not found:
        try:
            resp = requests.get(TWSE_SEARCH, params={"query": query}, headers=HEADERS, timeout=3)
            for s in resp.json().get("suggestions") or []:
                code, _, name = str(s).partition("\t")
                if name and (len(code) == 4 or code.startswith("00")) and code.isdigit():
                    found.append((code, name, "tse"))
        except (requests.RequestException, ValueError):
            pass
    return found


def resolve(query):
    """把名稱或代號解析成 (代號, 名稱)；找不到時丟出 LookupError。"""
    q = str(query or "").strip().upper()
    if not q:
        raise LookupError("請輸入股票名稱或代號")
    names = load_name_map()
    if CODE_RE.match(q):
        if q in names:
            return q, names[q]
        hits = [h for h in _remote_search(q) if h[0] == q]
        name = hits[0][1] if hits else q
        if hits:
            _remember(q, name)
        return q, name
    nq = _norm(q)
    exact = [c for c, n in names.items() if _norm(n) == nq]
    if exact:
        return exact[0], names[exact[0]]
    partial = sorted((c for c, n in names.items() if nq in _norm(n)), key=lambda c: len(names[c]))
    if partial:
        return partial[0], names[partial[0]]
    hits = _remote_search(query.strip())
    if not hits:
        raise LookupError(f"查無「{query}」對應的台股代號")
    best = next((h for h in hits if _norm(h[1]) == nq), hits[0])
    _remember(best[0], best[1])
    return best[0], best[1]


def _remember(code, name):
    cache = _read_json(NAME_CACHE, {})
    if cache.get(code) != name:
        cache[code] = name
        _save_cache(cache)


# ---------------------------------------------------------------- 三路資料

def fetch_quote(stock_id):
    quotes, yahoo, _ = rq.fetch_realtime([stock_id], max_yahoo_age=20)
    q, y = quotes.get(stock_id), yahoo.get(stock_id)
    if not q and not (y and y.get("price")):
        raise rq.MISError("MIS 與 Yahoo 皆無報價")
    s = rq.summarize(stock_id, q, y)
    s.update({
        "high": (q or {}).get("high") or (y or {}).get("bars_high"),
        "low": (q or {}).get("low") or (y or {}).get("bars_low"),
        "open": (q or {}).get("open") or (y or {}).get("day_open"),
        "prev_close": (q or {}).get("prev_close") or (y or {}).get("prev_close"),
        "bid_ask_ratio": s.get("orderbook_ratio"),
    })
    return s


def fetch_kline(stock_id):
    """Yahoo 日 K（不耗 FinMind 額度）→ 指標 → 朱家泓判讀；失敗改讀預抓檔 daily_30d。"""
    try:
        raw = fm.fetch_price_yahoo(stock_id)
        if len(raw) < 25:
            raise fm.YahooError("日 K 不足 25 根")
        df = fm.compute_indicators(raw)
        source = "Yahoo 日 K（即時）"
    except Exception as exc:  # Yahoo 失敗時退回預抓檔
        rows = ((_read_json(DATA_DIR / f"{stock_id}_kline.json", {}).get("data")) or {}).get("daily_30d") or []
        if not rows:
            raise RuntimeError(f"日 K 取得失敗：{exc}") from exc
        import pandas as pd
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        source = "reports/data 預抓檔（30 日）"
    last = df.iloc[-1]
    prev5 = df["close"].iloc[-6] if len(df) > 5 else df["close"].iloc[0]
    zhu = kline_patterns.analyze(df)

    def num(col):
        v = last.get(col) if hasattr(last, "get") else None
        return round(float(v), 2) if v is not None and v == v else None

    vol_ma20 = num("vol_ma20")
    return {
        "source": source,
        "date": str(last["date"])[:10],
        "close": float(last["close"]),
        "volume_lots": num("volume_lots"),
        "vol_ratio_20": round(float(last["volume_lots"]) / vol_ma20, 2) if vol_ma20 else None,
        "ma5": num("ma5"), "ma10": num("ma10"), "ma20": num("ma20"), "ma60": num("ma60"),
        "bias20_pct": round((float(last["close"]) / num("ma20") - 1) * 100, 2) if num("ma20") else None,
        "change_5d_pct": round((float(last["close"]) / float(prev5) - 1) * 100, 2) if prev5 else None,
        "high20": float(df["high"].tail(20).max()),
        "low20": float(df["low"].tail(20).min()),
        "zhu": zhu,
    }


def fetch_chips(stock_id):
    """優先讀預抓檔；沒有時即時抓 FinMind 三大法人與融資（各一次請求）。"""
    payload = _read_json(DATA_DIR / f"{stock_id}_chips.json", {})
    data = payload.get("data") or {}
    if data.get("institutional"):
        return {"as_of": data["institutional"].get("latest_date"), "fetched_at": payload.get("fetched_at"),
                "source": "reports/data 預抓檔", "institutional": data["institutional"], "margin": data.get("margin")}
    from tools import batch_prefetch as bp  # 延遲匯入：只有缺預抓檔時才需要
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_inst, f_margin = pool.submit(bp.chips_institutional, stock_id), pool.submit(bp.chips_margin, stock_id)
        inst = f_inst.result(timeout=CHIPS_TIMEOUT_SEC)
        try:
            margin = f_margin.result(timeout=CHIPS_TIMEOUT_SEC)
        except Exception:
            margin = None
    return {"as_of": inst.get("latest_date"), "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "FinMind 即時抓取", "institutional": inst, "margin": margin}


# ---------------------------------------------------------------- 籌碼王（權證小哥邏輯）

def chip_verdict(chips, kline=None, quote=None):
    """權證小哥實戰籌碼判讀：隔日沖／假外資、權證自營避險、大戶 vs 散戶、天花板線／地板線、盤中五檔。"""
    chips, kline, quote = chips or {}, kline or {}, quote or {}
    inst, margin = chips.get("institutional") or {}, chips.get("margin") or {}
    if not inst:
        return {"label": "籌碼資料缺口", "score": 0, "points": [], "alerts": ["無法人資料"], "one_line": "籌碼資料缺口"}
    f, t = inst.get("foreign") or {}, inst.get("trust") or {}
    f5, t5 = f.get("sum_5d") or 0, t.get("sum_5d") or 0
    fs, ts = inst.get("foreign_streak_days") or 0, inst.get("trust_streak_days") or 0
    flips = inst.get("foreign_flip_count_10d") or 0
    m5 = margin.get("margin_change_5d")
    chg5 = kline.get("change_5d_pct")
    vol = kline.get("volume_lots")
    bias = kline.get("bias20_pct")
    vr = kline.get("vol_ratio_20")
    score, points, alerts, label = 0, [], [], None

    score += (1 if f5 > 0 else -1 if f5 < 0 else 0) + (1 if t5 > 0 else -1 if t5 < 0 else 0)
    points.append(f"外資 5 日 {f5:+,.0f} 張（連{'買' if fs > 0 else '賣'} {abs(fs)} 日）、投信 5 日 {t5:+,.0f} 張"
                  f"（連{'買' if ts > 0 else '賣'} {abs(ts)} 日）")
    # 隔日沖主力與假外資
    daily = inst.get("daily_10d") or []
    if flips >= 3:
        alerts.append(f"外資近 10 日方向急翻 {flips} 次，隔日沖嫌疑，外資買超不計入多方")
        score -= 1
    if len(daily) >= 2 and (daily[-2].get("foreign") or 0) > 0 > (daily[-1].get("foreign") or 0):
        alerts.append("外資前日買超、今日反手賣超（隔日沖倒貨型態）")
    if vol and (f.get("last_1d") or 0) > 0 and f["last_1d"] / vol >= 0.15:
        alerts.append(f"外資單日買超占成交量 {f['last_1d'] / vol * 100:.0f}%（高週轉），防隔日沖開高倒貨")
    # 投信首買／連買占量
    if ts == 1 and (t.get("last_1d") or 0) > 0:
        points.append("投信首買" + (f"，占成交量 {t['last_1d'] / vol * 100:.1f}%" if vol else ""))
        score += 1
    elif ts >= 3:
        points.append(f"投信連買 {ts} 日" + (f"，最新一日占成交量 {t['last_1d'] / vol * 100:.1f}%" if vol and t.get("last_1d") else ""))
    # 權證與自營商避險
    hedge, self_ = inst.get("dealer_hedging"), inst.get("dealer_self")
    if hedge:
        h5 = hedge.get("sum_5d") or 0
        points.append(f"自營避險 5 日 {h5:+,.0f} 張（自行買賣 {(self_ or {}).get('sum_5d', 0):+,.0f} 張）："
                      + ("權證買盤迫使造市商追買現股" if h5 > 0 else "權證避險部位賣出、殺低現股" if h5 < 0 else "避險動能中性"))
    # 大戶 vs 散戶（融資）
    if m5 is not None:
        points.append(f"融資 5 日 {m5:+,.0f} 張、券資比 {margin.get('short_margin_ratio_pct', 'N/A')}%")
        if chg5 is not None and chg5 < 0 and m5 > 0 and (fs < 0 or ts < 0 or f5 + t5 < 0):
            label, score = "散戶融資承接（極度偏空）", min(score, -3)
            alerts.append(f"股價 5 日 {chg5:+.2f}%、融資反增 {m5:+,.0f} 張、法人賣超：散戶融資承接")
        elif m5 < 0 and t5 > 0 and ts >= 1 and (0 if flips >= 3 else f5) + t5 > 0:  # 隔日沖外資不算鎖碼
            label, score = "籌碼安定鎖碼", max(score, 3)
            points.append("融資退場、投信承接：籌碼由散戶流向法人")
        elif chg5 is not None and chg5 > 0 and m5 > 0 and f5 + t5 < 0:
            alerts.append("股價漲但融資增、法人賣：籌碼凌亂")
            score -= 1
    # 天花板線／地板線
    if bias is not None:
        if bias >= 10:
            alerts.append(f"月線正乖離 {bias:+.1f}%：觸及天花板線（短線極限賣點）")
            score -= 1
        elif bias <= -10 or (bias <= -7 and vr and vr >= 1.5):
            points.append(f"月線負乖離 {bias:+.1f}%" + (f"、量比 {vr:.1f}" if vr else "") + "：接近地板線（超跌爆量支撐）")
    # 盤中五檔與大單方向
    ratio, side = quote.get("bid_ask_ratio"), quote.get("tick_side") or ""
    if ratio is not None:
        if ratio < 0.8 and "內盤" in side:
            alerts.append(f"盤中五檔委買/委賣 {ratio:.2f} 且內盤主動賣出：賣壓湧現")
            score -= 1
        elif ratio > 1.25 and "外盤" in side:
            points.append(f"盤中五檔委買/委賣 {ratio:.2f} 且外盤主動買進：大單敲進")
            score += 1
    if not label:
        label = ("鎖碼偏多" if score >= 3 else "偏多" if score >= 1 else "偏空" if score <= -1 else "中性")
        if score <= -3:
            label = "極度偏空"
    one_line = f"{label}｜外資 5 日 {f5:+,.0f}、投信 5 日 {t5:+,.0f}" + (f"、融資 5 日 {m5:+,.0f}" if m5 is not None else "")
    if alerts:
        one_line += f"｜⚠️ {alerts[0]}"
    return {"label": label, "score": score, "points": points, "alerts": alerts, "one_line": one_line}


# ---------------------------------------------------------------- 巴董事長（美股大叔：不對稱風報比）

def chairman_call(quote, kline, chip):
    quote, kline, chip = quote or {}, kline or {}, chip or {}
    zhu = kline.get("zhu") or {}
    price = quote.get("price") or kline.get("close")
    if not price or zhu.get("error"):
        return {"action": "資料不足", "line": "報價或日 K 不足，暫不給建議", "rr": None}
    trend = zhu.get("trend", "盤整趨勢")
    stop = zhu.get("discipline_stop")
    supports = [v for v in (kline.get("ma10"), kline.get("ma20"), kline.get("low20")) if v and v < price]
    support = max(supports) if supports else kline.get("low20")
    highs = [h[1] for h in zhu.get("swing_highs") or [] if h[1] > price * 1.005]
    resistance = min(highs) if highs else (kline.get("high20") if (kline.get("high20") or 0) > price * 1.005 else round(price * 1.05, 2))
    stop = stop if stop and stop < price else (round(support * 0.97, 2) if support else None)
    rr = round((resistance - price) / (price - stop), 2) if stop and price > stop and resistance else None
    vwap = quote.get("vwap")
    above_vwap = vwap is not None and price >= vwap
    score = chip.get("score", 0)
    bias = kline.get("bias20_pct") or 0
    hot = bias >= 10 or (zhu.get("bearish_count") or 0) > 0
    stop_txt = f"停損 {stop:g}" if stop else "停損待定"
    if trend == "空頭趨勢" and zhu.get("has_reversal_signal") and score >= 2:
        action, line = "小量試單", f"空頭中見轉折紅 K 且籌碼回流：恐慌錯殺型機會，僅試單 1/3，{stop_txt}"
    elif trend == "空頭趨勢":
        why = "籌碼雖回流但未見轉折紅 K" if score >= 2 else "籌碼未回流"
        action, line = "暫不買進觀望", f"空頭趨勢＋{why}：暫不買進，持有者反彈減碼，{stop_txt}"
    elif trend == "多頭趨勢" and hot:
        action, line = "不追高", f"多頭但{'正乖離過大' if bias >= 10 else '出現高檔轉折警訊'}：不追價，等回測 {support:g} 附近再評估" if support else "多頭過熱：不追價"
    elif trend == "多頭趨勢" and score >= 1 and (above_vwap or vwap is None):
        action, line = "分批布局", f"多頭＋籌碼偏多共振：回測 {support:g} 附近分批，{stop_txt}，目標 {resistance:g}"
    elif chip.get("label", "").startswith(("散戶融資承接", "極度偏空")):
        action, line = "減碼避險", f"籌碼王判定{chip['label']}：持有者逢反彈減碼，{stop_txt}"
    else:
        lo = support if support else kline.get("low20")
        action, line = "持有觀望", f"{trend}：區間 {lo:g}–{resistance:g} 低買高出，跌破 {stop_txt.replace('停損 ', '')} 出場"
    if rr is not None:
        line += f"｜風報比 {rr:.2f}"
        if rr < 1.5 and action in ("分批布局", "小量試單"):
            action, line = "持有觀望", line + "（< 1.5，不對稱優勢不足，先觀望）"
    if not above_vwap and vwap is not None and action == "分批布局":
        line += "；盤中跌破 VWAP，等站回再進"
    return {"action": action, "line": line, "rr": rr, "support": support, "resistance": resistance, "stop": stop}


# ---------------------------------------------------------------- 主流程

def lookup(query):
    started = time.perf_counter()
    stock_id, name = resolve(query)
    errors = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {"quote": pool.submit(fetch_quote, stock_id), "kline": pool.submit(fetch_kline, stock_id),
                   "chips": pool.submit(fetch_chips, stock_id)}
        results = {}
        for key, fut in futures.items():
            try:
                results[key] = fut.result(timeout=CHIPS_TIMEOUT_SEC + 2)
            except FutureTimeout:
                results[key] = None
                errors.append(f"{key} 逾時")
            except Exception as exc:  # 任一路失敗不影響其他兩路
                results[key] = None
                errors.append(f"{key}：{type(exc).__name__}: {exc}"[:160])
    quote, kline, chips = results["quote"], results["kline"], results["chips"]
    if quote and quote.get("name") and name == stock_id:
        name = quote["name"]
        _remember(stock_id, name)
    chip = chip_verdict(chips, kline, quote)
    return {
        "query": query,
        "stock_id": stock_id,
        "name": name,
        "looked_up_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.perf_counter() - started, 2),
        "quote": quote,
        "kline": kline,
        "chips": {k: v for k, v in (chips or {}).items() if k in ("as_of", "fetched_at", "source")} or None,
        "chip_verdict": chip,
        "chairman": chairman_call(quote, kline, chip),
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description="台股名稱／代號即時快篩（0 Token）")
    parser.add_argument("query", help="股票名稱或代號，例如 台積電、奇鋐、2408")
    parser.add_argument("--json", action="store_true", help="輸出完整 JSON")
    args = parser.parse_args()
    try:
        r = lookup(args.query)
    except LookupError as exc:
        print(f"❌ {exc}")
        sys.exit(1)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return
    q, k, c, b = r["quote"] or {}, r["kline"] or {}, r["chip_verdict"], r["chairman"]
    zhu = k.get("zhu") or {}
    print(f"🔍 {r['stock_id']} {r['name']}（{r['elapsed_sec']} 秒）")
    print(f"  現價 {q.get('price')}（{q.get('price_source')}）漲跌 {q.get('change_pct')}%｜高 {q.get('high')} 低 {q.get('low')}"
          f"｜VWAP {q.get('vwap')}｜五檔比 {q.get('bid_ask_ratio')}")
    print(f"  ⚔️ K線技術人：{zhu.get('one_line', zhu.get('error'))}")
    print(f"     {zhu.get('action', '')}｜{zhu.get('stop_basis', '')}")
    print(f"  🎧 籌碼王：{c['one_line']}")
    print(f"  👑 巴董事長：{b['action']}｜{b['line']}")
    for e in r["errors"]:
        print(f"  ⚠️ {e}")


if __name__ == "__main__":
    main()
