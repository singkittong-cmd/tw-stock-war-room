# -*- coding: utf-8 -*-
"""K線技術人（朱家泓戰法）量化判讀：趨勢定多空、四線排列與扣抵、葛蘭碧八大法則、關鍵轉折 K 線、紀律停損。

輸入為日 K DataFrame（欄位 date/open/high/low/close/volume_lots，可缺均線，會自動補算），
由 finmind_cli.role_kline（batch_prefetch 預抓）與 quick_stock_lookup（即時快篩）共用。
台股慣例：收盤 > 開盤為紅 K（上漲），收盤 < 開盤為黑 K（下跌）。
"""
import math

import pandas as pd

MA_LIST = (5, 10, 20, 60)


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def prepare(df):
    """補齊均線、量比欄位，回傳依日期排序的新 DataFrame。"""
    df = df.copy().sort_values("date").reset_index(drop=True)
    if "volume_lots" not in df.columns and "volume" in df.columns:
        df["volume_lots"] = df["volume"] / 1000.0
    for n in MA_LIST:
        if f"ma{n}" not in df.columns or df[f"ma{n}"].isna().all():
            df[f"ma{n}"] = df["close"].rolling(n).mean()
    if "vol_ma20" not in df.columns:
        df["vol_ma20"] = df["volume_lots"].rolling(20, min_periods=5).mean()
    return df


# ---------------------------------------------------------------- 第一步：趨勢定多空（頭頭高、底底高）

def swing_points(df, lookback=60, window=3):
    """近 lookback 根 K 棒中，左右各 window 根內的最高點／最低點視為轉折頭與底。"""
    d = df.tail(lookback).reset_index(drop=True)
    highs, lows = [], []
    for i in range(window, len(d) - window):
        seg = d.iloc[i - window:i + window + 1]
        if d.loc[i, "high"] >= seg["high"].max():
            highs.append((str(pd.Timestamp(d.loc[i, "date"]).date()), float(d.loc[i, "high"])))
        if d.loc[i, "low"] <= seg["low"].min():
            lows.append((str(pd.Timestamp(d.loc[i, "date"]).date()), float(d.loc[i, "low"])))
    return highs, lows


def trend_structure(df, lookback=60):
    highs, lows = swing_points(df, lookback)
    close = float(df["close"].iloc[-1])
    out = {"lookback_days": min(lookback, len(df)), "swing_highs": highs[-2:], "swing_lows": lows[-2:]}
    if len(highs) < 2 or len(lows) < 2:
        out.update(trend="盤整趨勢", rule="轉折點不足兩組，視為盤整")
        return out
    (_, h1), (_, h2) = highs[-2:]
    (_, l1), (_, l2) = lows[-2:]
    if h2 > h1 and l2 > l1:
        trend, rule = "多頭趨勢", f"頭頭高（{h1:g}→{h2:g}）、底底高（{l1:g}→{l2:g}）"
    elif h2 < h1 and l2 < l1:
        trend, rule = "空頭趨勢", f"頭頭低（{h1:g}→{h2:g}）、底底低（{l1:g}→{l2:g}）"
    else:
        trend, rule = "盤整趨勢", f"頭（{h1:g}→{h2:g}）與底（{l1:g}→{l2:g}）方向不一致"
    # 收盤跌破最近一個底：多頭趨勢遭破壞；突破最近一個頭：空頭趨勢遭破壞
    if trend == "多頭趨勢" and close < l2:
        trend, rule = "盤整趨勢", rule + f"；但收盤 {close:g} 已跌破前底 {l2:g}，多頭結構被破壞"
    elif trend == "空頭趨勢" and close > h2:
        trend, rule = "盤整趨勢", rule + f"；但收盤 {close:g} 已突破前頭 {h2:g}，空頭結構被破壞"
    out.update(trend=trend, rule=rule, last_high=h2, last_low=l2)
    return out


# ---------------------------------------------------------------- 第二步：四線排列、斜率與扣抵

def _slope(series, n=3):
    s = series.dropna()
    if len(s) <= n:
        return None
    return float(s.iloc[-1] - s.iloc[-1 - n])


def ma_arrangement(df):
    last = df.iloc[-1]
    close = float(last["close"])
    vals = {n: _f(last[f"ma{n}"]) for n in MA_LIST}
    slopes = {n: _slope(df[f"ma{n}"]) for n in MA_LIST}
    arrow = {n: ("↑" if s and s > 0 else "↓" if s and s < 0 else "→") for n, s in slopes.items()}
    if all(vals.values()):
        if vals[5] > vals[10] > vals[20] > vals[60]:
            order = "四線多頭排列（5>10>20>60）"
        elif vals[5] < vals[10] < vals[20] < vals[60]:
            order = "四線空頭排列（5<10<20<60）"
        else:
            order = "四線糾結（排列未定）"
    else:
        order = "均線資料不足"
    # 扣抵：下一日 MA 要扣掉的價格；現價高於扣抵值 → 均線易上彎
    deduct = {}
    for n in (20, 60):
        if len(df) >= n:
            ref = float(df["close"].iloc[-n])
            deduct[f"ma{n}"] = {"deduct_price": ref, "hint": "現價高於扣抵值，均線易上彎" if close > ref
                                else "現價低於扣抵值，均線易下彎"}
    up_count = sum(1 for n in MA_LIST if vals[n] and close > vals[n])
    return {"values": vals, "slopes": {f"ma{n}": arrow[n] for n in MA_LIST}, "order": order,
            "above_count": up_count, "deduct": deduct,
            "summary": f"{order}｜斜率 5{arrow[5]} 10{arrow[10]} 20{arrow[20]} 60{arrow[60]}｜站上 {up_count}/4 條"}


def granville(df):
    """以 MA20（月線）判斷葛蘭碧八大法則位置。"""
    if len(df) < 25 or df["ma20"].isna().iloc[-1]:
        return {"rule": None, "label": "資料不足"}
    c0, c1 = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
    m0, m1 = float(df["ma20"].iloc[-1]), float(df["ma20"].iloc[-2])
    slope = _slope(df["ma20"], 3) or 0
    up, down = slope > 0, slope < 0
    bias = (c0 / m0 - 1) * 100
    low0 = float(df["low"].iloc[-1])
    if c1 < m1 and c0 > m0 and not down:
        return {"rule": 1, "label": "葛蘭碧①突破買點：站上走平/上揚月線"}
    if up and c0 > m0 and low0 <= m0 * 1.015:
        return {"rule": 2, "label": "葛蘭碧②回測買點：拉回月線不破"}
    if up and c1 < m1 and c0 >= m0 * 0.99:
        return {"rule": 3, "label": "葛蘭碧③假跌破買點：跌破上揚月線後快速收回"}
    if down and bias < -10:
        return {"rule": 4, "label": f"葛蘭碧④負乖離過大（{bias:.1f}%）：僅限短線搶反彈"}
    if c1 > m1 and c0 < m0 and not up:
        return {"rule": 5, "label": "葛蘭碧⑤跌破賣點：跌破走平/下彎月線"}
    if down and c0 < m0 and float(df["high"].iloc[-1]) >= m0 * 0.985:
        return {"rule": 6, "label": "葛蘭碧⑥反彈不過賣點：反彈碰下彎月線"}
    if down and c0 > m0 and bias < 2:
        return {"rule": 7, "label": "葛蘭碧⑦假突破賣點：站上下彎月線但力道弱"}
    if up and bias > 10:
        return {"rule": 8, "label": f"葛蘭碧⑧正乖離過大（{bias:.1f}%）：留意回檔"}
    side = "月線之上" if c0 > m0 else "月線之下"
    return {"rule": None, "label": f"位於{side}（乖離 {bias:+.1f}%），未觸發八大法則訊號"}


# ---------------------------------------------------------------- 第三步：關鍵轉折 K 線

def _body(r):
    return abs(r["close"] - r["open"])


def _range(r):
    return max(r["high"] - r["low"], 1e-9)


def _is_red(r):
    return r["close"] > r["open"]


def _is_black(r):
    return r["close"] < r["open"]


def candle_patterns(df, days=3):
    """掃描最近 days 根 K 棒的朱家泓核心轉折型態，回傳由新到舊的清單。"""
    d = df.reset_index(drop=True)
    n = len(d)
    if n < 25:
        return []
    high60 = float(d["high"].tail(60).max())
    low20 = float(d["low"].tail(20).min())
    out = []

    def add(i, name, kind, note):
        out.append({"date": str(pd.Timestamp(d.loc[i, "date"]).date()), "pattern": name, "type": kind,
                    "note": note, "low": float(d.loc[i, "low"]), "high": float(d.loc[i, "high"])})

    for i in range(n - 1, max(n - 1 - days, 2), -1):
        r, p, pp = d.loc[i], d.loc[i - 1], d.loc[i - 2]
        body, rng = _body(r), _range(r)
        upper = r["high"] - max(r["open"], r["close"])
        lower = min(r["open"], r["close"]) - r["low"]
        vol_ratio = (r["volume_lots"] / r["vol_ma20"]) if _f(r.get("vol_ma20")) else None
        long_body = body >= rng * 0.6 and body / max(p["close"], 1e-9) >= 0.02
        near_high = r["high"] >= high60 * 0.95
        near_low = r["low"] <= low20 * 1.03
        # ---- 底部／回檔轉折（多方）
        if _is_black(p) and _is_red(r) and r["close"] >= p["open"] and r["open"] <= p["close"] and long_body:
            add(i, "長紅吞噬", "bullish", "紅 K 實體完整吞噬前一根黑 K，空方力竭")
        if (_is_black(pp) and _body(pp) >= _range(pp) * 0.5 and _body(p) <= _range(p) * 0.3
                and _is_red(r) and r["close"] >= (pp["open"] + pp["close"]) / 2):
            add(i, "晨星", "bullish", "長黑後十字/小 K 止跌，再以紅 K 收復長黑一半以上")
        if (all(_is_red(x) for x in (pp, p, r)) and pp["close"] < p["close"] < r["close"]
                and min(_body(x) / _range(x) for x in (pp, p, r)) >= 0.5):
            add(i, "紅三兵", "bullish", "連三根紅 K 收高，買盤接力")
        if lower >= body * 2 and upper <= max(body, rng * 0.1) and near_low:
            add(i, "錘子線（下影線守支撐）", "bullish", f"長下影線測試 {r['low']:g} 後收回，低檔有承接")
        if r["low"] > p["high"]:
            add(i, "跳空向上缺口", "bullish", f"缺口 {p['high']:g}–{r['low']:g}，不補即為強勢支撐")
        # ---- 高檔轉折警訊（空方）
        if upper >= body * 2 and lower <= max(body, rng * 0.1) and near_high:
            add(i, "墓碑線／長上影線", "bearish", f"高檔上攻 {r['high']:g} 被賣壓壓回")
        if (_is_red(p) and _body(p) >= _range(p) * 0.6 and _is_black(r) and r["open"] > p["high"]
                and r["close"] < (p["open"] + p["close"]) / 2):
            add(i, "烏雲罩頂", "bearish", "開高後收進前一根長紅實體一半以下")
        if _is_red(p) and _is_black(r) and r["open"] >= p["close"] and r["close"] <= p["open"] and long_body:
            add(i, "長黑吞噬", "bearish", "黑 K 實體完整吞噬前一根紅 K，多方失守")
        if vol_ratio and vol_ratio >= 2 and near_high and abs(r["close"] / p["close"] - 1) <= 0.005:
            add(i, "高檔爆量不漲", "bearish", f"量比 {vol_ratio:.1f} 倍但收盤持平，疑似出貨")
        if r["high"] < p["low"]:
            add(i, "跳空向下缺口", "bearish", f"缺口 {r['high']:g}–{p['low']:g}，未回補前為壓力")
    return out


# ---------------------------------------------------------------- 第四步：紀律停損

def discipline_stop(df, patterns, trend):
    """有多方轉折 K → 停損設在轉折紅 K 低點；否則以月線或近 20 日低點為絕對停損。"""
    close = float(df["close"].iloc[-1])
    ma20 = _f(df["ma20"].iloc[-1])
    low20 = float(df["low"].tail(20).min())
    bull = [p for p in patterns if p["type"] == "bullish"]
    if bull:
        ref = bull[0]
        return {"stop_ref": ref["low"], "basis": f"跌破 {ref['date']} {ref['pattern']} 低點 {ref['low']:g} 即停損",
                "has_reversal": True}
    if ma20 and close > ma20:
        return {"stop_ref": round(min(ma20, low20 if low20 > ma20 * 0.95 else ma20), 2),
                "basis": f"收盤跌破月線 {ma20:.2f} 即停損", "has_reversal": False}
    note = "空頭趨勢未見轉折紅 K，暫不買進" if trend == "空頭趨勢" else "未見轉折紅 K，等待訊號再進場"
    return {"stop_ref": low20, "basis": f"{note}；若已持有，跌破近 20 日低點 {low20:g} 出場", "has_reversal": False}


# ---------------------------------------------------------------- 綜合判讀

def analyze(df):
    """朱家泓四步驟完整判讀，回傳可直接寫入 JSON 的 dict。資料不足 25 根時回傳 {"error": ...}。"""
    if df is None or len(df) < 25:
        return {"error": "日 K 不足 25 根，無法判讀"}
    df = prepare(df)
    trend = trend_structure(df)
    ma = ma_arrangement(df)
    gv = granville(df)
    patterns = candle_patterns(df)
    stop = discipline_stop(df, patterns, trend["trend"])
    bull = [p for p in patterns if p["type"] == "bullish"]
    bear = [p for p in patterns if p["type"] == "bearish"]
    t = trend["trend"]
    if t == "空頭趨勢":
        action = "空頭趨勢：反彈只看減碼，不隨便摸底" + ("；已見轉折紅 K，僅可小量試單並嚴守停損" if bull else "")
    elif t == "多頭趨勢":
        action = ("多頭趨勢回檔出現轉折紅 K：可依紀律找買點" if bull
                  else "多頭趨勢：等回檔到支撐並出現轉折紅 K 再買") + ("；但出現高檔轉折警訊，先減碼" if bear else "")
    else:
        action = "盤整趨勢：區間上緣減碼、下緣見轉折紅 K 再承接" + ("；出現空方轉折警訊" if bear else "")
    signal_txt = "、".join(p["pattern"] for p in patterns[:3]) or "近 3 日無關鍵轉折 K"
    return {
        "trend": t,
        "trend_rule": trend["rule"],
        "swing_highs": trend["swing_highs"],
        "swing_lows": trend["swing_lows"],
        "ma_order": ma["order"],
        "ma_slopes": ma["slopes"],
        "ma_summary": ma["summary"],
        "ma_deduct": ma["deduct"],
        "granville": gv["label"],
        "granville_rule": gv["rule"],
        "patterns": patterns,
        "bullish_count": len(bull),
        "bearish_count": len(bear),
        "discipline_stop": stop["stop_ref"],
        "stop_basis": stop["basis"],
        "has_reversal_signal": stop["has_reversal"],
        "action": action,
        "one_line": f"{t}｜{ma['order']}｜{gv['label'].split('：')[0]}｜{signal_txt}",
    }


def analyze_daily_rows(rows):
    """由 kline.json 的 daily_30d（list of dict）判讀；舊預抓檔沒有 zhu_analysis 欄位時使用。"""
    if not rows:
        return {"error": "無日 K 資料"}
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return analyze(df)
