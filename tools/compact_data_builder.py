# -*- coding: utf-8 -*-
"""JSON 數據極致濃縮器：在 Python 端先消化 reports/ 下冗長的明細陣列，只交給模型高密度摘要（省 85% Token）。

用法：
    C:/Users/singk/anaconda3/python.exe tools/compact_data_builder.py --stock 2408     # 個股 350 字摘要
    C:/Users/singk/anaconda3/python.exe tools/compact_data_builder.py --market         # 新聞＋美股＋大盤摘要

隱私：持股摘要只帶「持有中」與帳面報酬率%，絕不帶股數、成本與損益金額（資料會送往第三方模型）。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import kline_patterns  # noqa: E402

REPORTS = ROOT / "reports"
DATA_DIR = REPORTS / "data"
MEMORY_DIR = REPORTS / "memory"
NEWS_NOISE = re.compile(r"盤中速報|漲停|跌停|今日熱門|直播|懶人包|一分鐘|抽獎|開箱|投資網誌")
US_DIGEST = ["^SOX", "TSM", "NVDA", "AMD", "AVGO", "MRVL", "MU", "WDC", "ASML", "AMAT", "VRT", "SMCI", "^IXIC", "^GSPC"]


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _data(stock_id, role):
    return _read(DATA_DIR / f"{stock_id}_{role}.json").get("data") or {}


def _n(v, fmt="{:,.0f}"):
    try:
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return "N/A"


def _short_title(title, n=26):
    t = re.sub(r"\s*-\s*[^-]{2,20}$", "", str(title or "")).strip()  # 去掉「- 來源名稱」尾巴
    t = re.sub(r"^(【[^】]{1,8}】|\d{4}\s+\S+\s+-\s+)+", "", t).strip()  # 去掉「【即時新聞】」等前綴
    return t[:n] + ("…" if len(t) > n else "")


def live_position(stock_id):
    for p in _read(REPORTS / "portfolio_live_status.json").get("positions") or []:
        if p.get("stock_id") == stock_id:
            return p
    return {}


# ---------------------------------------------------------------- 個股

def fundamental_line(stock_id):
    f = _data(stock_id, "fundamental")
    parts = []
    rev = (f.get("monthly_revenue") or [None])[0]
    if rev:
        parts.append(f"{rev['month']}月營收YoY{_n(rev.get('yoy_pct'), '{:+.0f}')}%MoM{_n(rev.get('mom_pct'), '{:+.1f}')}%")
    q = (f.get("financials") or [None])[0]
    if q:
        parts.append(f"{q['date'][:7]}季毛利{_n(q.get('gross_margin_pct'), '{:.1f}')}%營益{_n(q.get('operating_margin_pct'), '{:.1f}')}%EPS{q.get('eps')}")
    news = [_short_title(n.get("title"), 15) for n in (f.get("news") or [])[:3]]
    if news:
        parts.append("新聞:" + "/".join(news))
    return "【基本面】" + ("；".join(parts) if parts else "無預抓資料")


def chips_line(stock_id, live=None):
    c = _data(stock_id, "chips")
    inst, m = c.get("institutional") or {}, c.get("margin") or {}
    if not inst:
        return "【籌碼】無預抓資料"
    f, t = inst.get("foreign") or {}, inst.get("trust") or {}
    flips = inst.get("foreign_flip_count_10d") or 0
    s = (f"【籌碼】外資1/5/20日{_n(f.get('last_1d'))}/{_n(f.get('sum_5d'))}/{_n(f.get('sum_20d'))}張"
         f"急翻{flips}次{'(疑隔日沖)' if flips >= 3 else ''}；投信連{'買' if (t.get('streak_days') or 0) > 0 else '賣'}"
         f"{abs(t.get('streak_days') or 0)}日5日{_n(t.get('sum_5d'))}")
    if inst.get("dealer_self") is not None:
        s += f"；自營自行{_n((inst['dealer_self'] or {}).get('sum_5d'))}/避險{_n((inst.get('dealer_hedging') or {}).get('sum_5d'))}(5日)"
    if m:
        s += f"；融資5日{_n(m.get('margin_change_5d'), '{:+,.0f}')}券資比{m.get('short_margin_ratio_pct')}%"
    live = live or {}
    if live.get("bid_ask_ratio") is not None:
        s += f"；五檔比{_n(live['bid_ask_ratio'], '{:.2f}')}{live.get('tick_side', '')}"
    return s


def kline_line(stock_id):
    k = _data(stock_id, "kline")
    if not k:
        return "【技術】無預抓資料"
    zhu = k.get("zhu_analysis") or kline_patterns.analyze_daily_rows(k.get("daily_30d") or [])
    lv = k.get("levels") or {}
    pats = "、".join(p["pattern"] for p in (zhu.get("patterns") or [])[:3]) or "無轉折K"
    order = zhu.get("ma_order", "")
    order = "多排" if "多頭排列" in order else "空排" if "空頭排列" in order else "糾結"
    mas = "/".join(_n(k.get(f"ma{n}"), "{:.1f}") for n in (5, 10, 20, 60))
    return (f"【技術】{zhu.get('trend', 'N/A')}；MA5/10/20/60={mas}({order}{''.join(zhu.get('ma_slopes', {}).values())})；"
            f"MACD柱{_n(k.get('macd_hist'), '{:+.1f}')}RSI{_n(k.get('rsi14'), '{:.0f}')}乖離{_n(k.get('bias20_pct'), '{:+.1f}')}%；"
            f"近3日K:{pats}；壓{lv.get('resistance_1')}/{lv.get('resistance_2')}"
            f"撐{lv.get('defense_support')}停損{zhu.get('discipline_stop') or lv.get('stop_loss')}")


def reflection_line(stock_id):
    r = _read(MEMORY_DIR / f"{stock_id}_reflection.json")
    if not r:
        return "【覆盤】無可驗證的昨日結論"
    gaps = [x for x in r.get("reviews") or [] if x.get("verdict") == "落差"]
    if not gaps:
        return f"【覆盤】{r.get('hits', 0)}/{r.get('total', 0)}項準確，無落差"
    g = gaps[0]
    return f"【覆盤】{g['member_name']}昨判{g['prediction']['direction']}→{'、'.join(g['tags'])[:16]}；修正:{g['corrections'][0][:20]}"


def learning_line(stock_id):
    """學習資料庫：該股歷史開盤/盤中/收盤對帳結果＋成員修正原則（learning_engine 失敗時略過）。"""
    try:
        from tools import learning_engine
        return learning_engine.stock_learning_line(stock_id)
    except Exception:
        return "【學習對帳】資料庫讀取失敗"


def build_stock_compact_digest(stock_id: str, include_position: bool = True) -> str:
    """單檔高密度摘要（約 450–900 字）：即時、基本面、權證小哥籌碼、朱家泓技術、昨日覆盤、歷史對帳學習。"""
    live = live_position(stock_id)
    radar = next((r for r in _read(REPORTS / "watchlist_radar_live.json").get("results") or [] if r["stock_id"] == stock_id), {})
    k = _data(stock_id, "kline")
    name = live.get("name") or radar.get("name") or _data(stock_id, "fundamental").get("stock_name") or ""
    price = live.get("current_price") or radar.get("price") or k.get("close")
    chg = live.get("change_pct") if live else radar.get("change_pct", k.get("change_pct"))
    vwap = live.get("vwap") or radar.get("vwap")
    head = f"■{stock_id}{name} 現價{price}({_n(chg, '{:+.2f}')}%) VWAP{vwap}"
    if include_position and live.get("in_portfolio"):
        head += f"【持有中，帳面{_n(live.get('net_return_pct'), '{:+.2f}')}%】"
    live_for_chips = live or {"bid_ask_ratio": radar.get("bid_ask_ratio"), "tick_side": radar.get("tick_side")}
    return "\n".join([head, fundamental_line(stock_id), chips_line(stock_id, live_for_chips), kline_line(stock_id),
                      reflection_line(stock_id), learning_line(stock_id)])


# ---------------------------------------------------------------- 全市場

def news_digest(limit=18, max_chars=800):
    raw = _read(REPORTS / "news" / "daily_market_news_raw.json")
    lines, items, seen = [], [], set()
    for n in raw.get("news") or []:
        title = _short_title(n.get("title"), 34)
        if NEWS_NOISE.search(str(n.get("title"))) or title[:12] in seen:
            continue
        seen.add(title[:12])
        point = re.sub(r"\s+", " ", str(n.get("summary") or ""))[:24]
        items.append(n)
        lines.append(f"{len(items)}.{title}" + (f"｜{point}" if point else ""))
        if len(items) >= limit or sum(len(x) for x in lines) > max_chars:
            break
    return "\n".join(lines), items


def us_digest():
    snap = _read(REPORTS / "us_market" / "us_semi_quotes.json")
    q, adr = snap.get("quotes") or {}, snap.get("tsm_adr") or {}
    cells = [f"{s.lstrip('^')}{_n((q.get(s) or {}).get('change_pct'), '{:+.1f}')}%" for s in US_DIGEST if q.get(s)]
    text = "｜".join(cells)
    if adr.get("premium_pct") is not None:
        text += f"｜TSM ADR換算{adr.get('tsm_converted_twd')}元，對2330溢價{adr['premium_pct']:+.2f}%"
    return f"美股收盤({snap.get('fetched_at', 'N/A')}):" + text if cells else "美股資料缺口"


def us_refs(news_limit=12):
    """美股 TOP 10 的可引用來源清單：Q1… 為 14 檔報價（Yahoo Finance 報價頁）、N1… 為美股新聞（原文網址）。
    模型只能回傳 ref 編號，url／pub_date／source 一律由 Python 依編號對回，杜絕捏造網址。"""
    snap = _read(REPORTS / "us_market" / "us_semi_quotes.json")
    q, adr = snap.get("quotes") or {}, snap.get("tsm_adr") or {}
    refs = []
    for s in US_DIGEST:
        x = q.get(s) or {}
        if x.get("change_pct") is None:
            continue
        text = f"{x.get('name', s)}({s}) 收{_n(x.get('price'), '{:,.2f}')} {_n(x.get('change_pct'), '{:+.2f}')}%"
        if s == "TSM" and adr.get("premium_pct") is not None:
            text += f"，換算台股{adr.get('tsm_converted_twd')}元、對2330溢價{adr['premium_pct']:+.2f}%"
        refs.append({"ref": f"Q{len(refs) + 1}", "kind": "quote", "symbol": s, "text": text,
                     "url": x.get("url") or f"https://finance.yahoo.com/quote/{s}",
                     "pub_date": x.get("pub_date") or x.get("market_time_tpe"), "source": x.get("source") or "Yahoo Finance",
                     "change_pct": x.get("change_pct")})
    news = (_read(REPORTS / "us_market" / "us_news_raw.json").get("news") or [])[:news_limit]
    for i, n in enumerate(news, 1):
        refs.append({"ref": f"N{i}", "kind": "news", "text": f"{n['title'][:90]}（{n['source']}）", "url": n["url"],
                     "pub_date": n["pub_date"], "source": n["source"]})
    return refs


def market_digest():
    s = _read(REPORTS / "market_close_summary.json")
    if not s:
        return "【大盤】無收盤總結"
    t, inst, rot = s.get("taiex") or {}, s.get("institutional") or {}, s.get("rotation") or {}
    return (f"【大盤】加權{t.get('close')}({_n(t.get('change_pct'), '{:+.2f}')}%){t.get('trend', '')}，MA20 {t.get('ma20')}"
            + (f"，量比{t['vol_ratio_20']}" if t.get("vol_ratio_20") else "") + "；"
            f"外資{_n(inst.get('foreign'), '{:+.0f}')}億投信{_n(inst.get('trust'), '{:+.0f}')}億；"
            f"強:{'、'.join(n for n, _ in rot.get('strong') or [])}；弱:{'、'.join(n for n, _ in rot.get('weak') or [])}")


def market_reflection_line():
    """大盤覆盤＋五位成員近期準確率＋大盤歷史對帳修正原則（每次 Prompt 都會帶入）。"""
    r = _read(MEMORY_DIR / "market_reflection.json")
    line = f"【大盤覆盤】{r.get('headline', '無')}"[:120]
    try:
        from tools import learning_engine
        line += "\n" + learning_engine.market_learning_line()
    except Exception:
        pass
    return line


def build_news_and_us_digest() -> dict:
    """{"news": 18 則精簡標題（≤800 字）, "news_items": 對應原始新聞（取連結用）, "us": 美股 14 檔表, "market": 大盤, "reflection": 大盤覆盤}"""
    news, items = news_digest()
    return {"news": news, "news_items": items, "us": us_digest(), "market": market_digest(),
            "reflection": market_reflection_line()}


def main():
    parser = argparse.ArgumentParser(description="JSON 數據極致濃縮器")
    parser.add_argument("--stock", action="append", help="個股代號（可重複）")
    parser.add_argument("--market", action="store_true", help="輸出新聞＋美股＋大盤摘要")
    args = parser.parse_args()
    for sid in args.stock or []:
        d = build_stock_compact_digest(sid)
        print(d, f"\n（{len(d)} 字）\n")
    if args.market or not args.stock:
        m = build_news_and_us_digest()
        for k in ("news", "us", "market", "reflection"):
            print(m[k], f"\n（{len(m[k])} 字）\n")


if __name__ == "__main__":
    main()
