# -*- coding: utf-8 -*-
"""零 Claude Token 全員更新引擎：Python 預抓＋濃縮 → 單次多人設請求（預設免費 Gemini）→ 寫回 dashboard 讀取的檔案。

用法：
    C:/Users/singk/anaconda3/python.exe tools/fast_team_runner.py --task daily_home_update [--provider gemini]
    C:/Users/singk/anaconda3/python.exe tools/fast_team_runner.py --task war_room_sync --provider groq
    C:/Users/singk/anaconda3/python.exe tools/fast_team_runner.py --task ask_war_room --question "2408 該攤平嗎？" --stock 2408
    C:/Users/singk/anaconda3/python.exe tools/fast_team_runner.py --task deep_dive --stock 2408 --provider claude_lite
    C:/Users/singk/anaconda3/python.exe tools/fast_team_runner.py --task sim_trade_session --mode intraday_hunt
        🧪 100 萬模擬房五人投決會：Python 篩出模擬持倉＋20 強前 5 檔 → 單次多人設討論 → Python 風控後模擬交割
    C:/Users/singk/anaconda3/python.exe tools/fast_team_runner.py --task update_single_agent --agent chip_king --stock 2408
        --agent：news_master（新聞達人）／chip_king（籌碼王）／kline_tech（K線技術人）／chairman_buffett（巴董事長）／
                 trump_maga（川大帝MAGA，休眠中也強制刷新美股）

provider：gemini（預設）／groq／claude_lite（單次 claude -p、--tools ""、不派子代理）／auto。
所有任務都只送 compact_data_builder 的高密度摘要；持股只帶「持有中＋帳面報酬率%」，不送股數、成本與損益金額。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import compact_data_builder as cdb  # noqa: E402
from tools import llm_router  # noqa: E402

REPORTS = ROOT / "reports"
NEWS_DIR = REPORTS / "news"
US_DIR = REPORTS / "us_market"
STOCKS_DIR = REPORTS / "stocks"
DECISIONS_PATH = REPORTS / "portfolio_intraday_decisions.json"
QA_PATH = REPORTS / "memory" / "war_room_qa_latest.json"
TIME_FMT = "%Y-%m-%d %H:%M:%S"

FINAL_ACTIONS = ("🟢 順勢加碼", "🟡 支撐攤平", "💰 分批獲利", "⚪ 續抱觀察", "🔴 減碼停損")
PROPOSALS = ("加碼", "攤平", "獲利了結", "續抱", "停損減碼")
VERDICTS = ("強力買進", "分批布局", "持有觀望", "減碼", "出場避險")
RATINGS = ("強多", "偏多", "中性", "偏空", "強空")
TAGS = ("利多", "利空", "中性")

PERSONAS = (
    "你是台股戰情室的多重人設引擎，必須用繁體中文、只根據使用者提供的數據回答，嚴禁編造數字。\n"
    "成員：新聞達人（消息面與基本面查核）；籌碼王（權證小哥邏輯：隔日沖/假外資=外資急翻≥3次或高週轉大買、"
    "自營商自行vs權證避險、股價跌+融資增+法人賣=散戶融資承接、融資退+投信連買=籌碼鎖碼、月線乖離≥10%天花板線/≤-10%地板線、"
    "五檔比<0.8且內盤=賣壓）；K線技術人（朱家泓邏輯：頭頭高底底高定多空、空頭只減碼不摸底、四線排列與斜率、"
    "轉折K線、跌破轉折紅K低點或月線即停損、未見轉折暫不買進）；巴董事長（美股大叔邏輯：Priced-in與預期差、機構部位、"
    "CapEx與Guidance、不對稱風報比<1.5不進場、恐慌錯殺+底部共振才分批、擁擠樂觀時減碼，並引用【覆盤】修正昨日落差）。"
    "回覆內容只用成員名字稱呼，不要寫出權證小哥、朱家泓、美股大叔等名師名稱或「XX邏輯」字樣。"
    "\n【歷史對帳學習鐵律】數據中的【成員準確率】【學習對帳】【大盤覆盤】是系統以實盤開盤/盤中/收盤價自動驗算的結果，必須據此調整判斷："
    "各成員先依自己的「修正原則」與「重複模式」修正（例如外資常一日行情的個股，籌碼王把外資單日買超評分減半、需連買≥2日才算多方；"
    "收盤後看多常遇跳空低開的個股，首筆買進量減半）；巴董事長總結時，準確率（尤其近10次）較高的成員意見權重較高，"
    "對「剛看錯」的成員必須點名質疑並說明這次如何避免重犯。"
    "用語一律直白：建議買進價、停損價、停利目標價、加碼、減碼、可用現金，不使用任何比喻、戰爭或狩獵類詞彙。"
)


def _now():
    return datetime.now().strftime(TIME_FMT)


def _read(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _as_dict(obj):
    """模型 JSON 一律轉成 dict：模型偶爾把答案包成 [{...}]（只有一個元素的陣列）。
    陣列取其中的 dict（多個時合併，先出現者優先）；其他型別（字串、數字、空陣列）視為解析失敗。"""
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if len(dicts) == 1:
            return dicts[0]
        if dicts:
            merged = {}
            for d in dicts:
                for k, v in d.items():
                    merged.setdefault(k, v)
            return merged
    raise ValueError(f"模型回覆的 JSON 是 {type(obj).__name__}，不是物件")


def _ask_json(system, user, provider, max_tokens=4096):
    """單次請求並解析成 dict；解析失敗或結構不對時同一引擎重試一次。"""
    last = None
    for _ in range(2):
        r = llm_router.call_llm_ex(system, user, provider=provider, temperature=0.3, json_mode=True, max_tokens=max_tokens)
        if not isinstance(r, dict) or not isinstance(r.get("text"), str):
            last = ValueError(f"llm_router 回傳 {type(r).__name__}，缺少文字欄位")
            continue
        try:
            return _as_dict(llm_router.parse_json(r["text"])), r
        except (json.JSONDecodeError, ValueError) as exc:
            last = exc
    raise llm_router.LLMError(f"模型回覆不是合法 JSON 物件：{last}")


def _dict(x):
    return x if isinstance(x, dict) else {}


def _list(x):
    return x if isinstance(x, list) else []


def _pick(value, allowed, default):
    v = str(value or "").strip()
    return next((a for a in allowed if a in v or v in a and v), default) if v else default


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _portfolio_ids():
    return [str(p["stock_id"]) for p in _read(REPORTS / "portfolio.json", []) if p.get("stock_id")]


# ================================================================ 1) 日常主畫面

NEWS_SYSTEM = PERSONAS + (
    "\n你現在只扮演「新聞達人」。從新聞清單挑出今天對台股資金流向與基本面最重要的 10 則（剔除業配、單純漲跌播報），"
    "每則必須用清單中的新聞編號 idx 引用，嚴禁自行編造網址或日期（網址與日期由系統依編號自動帶入）。"
    "輸出 JSON：{\"market_sentiment\":\"偏多|中立偏多|分歧震盪|偏空警戒\",\"one_line_takeaway\":\"\","
    "\"top_themes\":[\"題材 (代號名稱)\"],\"top_news_10\":[{\"idx\":新聞編號,\"title\":\"精簡標題\",\"point\":\"一句話重點與對台股影響\","
    "\"tag\":\"利多|利空|中性\"}],\"non_holding_opportunities\":[{\"stock_id\":\"\",\"name\":\"\",\"catalyst\":\"\"}],"
    "\"risk_warnings\":[\"\"],\"reflection\":\"昨日判斷覆盤與修正一句話\"}。top_news_10 恰好 10 則且 idx 不重複。"
)
US_SYSTEM = PERSONAS + (
    "\n你現在只扮演「川大帝MAGA」（美股大叔宏觀與機構敘事）。資料清單中 Q 開頭為美股報價、N 開頭為美股新聞，"
    "每一條 TOP 10 都必須以 ref 引用清單編號，嚴禁自行編造網址或日期（由系統依編號自動帶入）。輸出 JSON："
    "{\"us_top10\":[{\"ref\":\"Q1 或 N3\",\"title\":\"\",\"point\":\"一句話重點＋對台股哪個次產業/代號的傳導\",\"tag\":\"利多|利空|中性\"}],"
    "\"us_key_drivers\":[\"3 條驅動因素（含預期差或機構部位觀點）\"],\"tw_index_forecast\":\"台股下一交易日走勢預判與關鍵點位\","
    "\"opening_strategy\":\"開盤應對策略\",\"top_sector_picks\":{\"favor\":[\"次產業：理由\"],\"avoid\":[\"次產業：理由\"]},"
    "\"reflection\":\"昨日預判覆盤與修正一句話\"}。us_top10 恰好 10 條、ref 不重複，至少涵蓋 SOX、TSM ADR 折溢價、NVDA、MU、VRT，"
    "並至少 3 條引用 N 新聞。"
)
CLOSE_SYSTEM = PERSONAS + (
    "\n你現在扮演巴董事長，把台股收盤數據精煉成 4 點條列重點（每點 40–70 字，數字只能來自數據），依序為："
    "1.大盤指數與量能結構（收盤、漲跌、量比、朱家泓均線位階）；2.三大法人與資金流向（外資、投信、自營商、融資）；"
    "3.今日強弱勢族群輪動（領漲主流、領跌重災區）；4.昨日預判覆盤與明日開盤關鍵觀察點（支撐壓力與操作定調）。"
    "輸出 JSON：{\"bullets\":[{\"label\":\"大盤指數與量能結構\",\"text\":\"\"},…]}，3–5 點。"
)
CLOSE_LABELS = ("大盤指數與量能結構", "三大法人與資金流向", "今日強弱勢族群輪動", "昨日預判覆盤與明日開盤關鍵觀察點")


def _ref_fields(src):
    """由爬蟲原始資料帶入出處（url／source／pub_date），模型不經手。"""
    url = src.get("url") or src.get("link")
    return {"url": url if str(url or "").startswith(("https://", "http://")) else None,
            "source": src.get("source"), "pub_date": src.get("pub_date") or src.get("published_at")}


def _md_item(i, n):
    link = f" [🔗 來源：{n['source']}]({n['url']})" if n.get("url") else ""
    return f"{i}. `[{n.get('pub_date') or '時間不明'}]` **{n['title']}**（{n['tag']}）— {n['point']}{link}"


def _news_brief(provider, digest):
    held = set(_portfolio_ids())
    data, meta = _ask_json(NEWS_SYSTEM, f"{digest['reflection']}\n{digest['market']}\n新聞清單：\n{digest['news']}", provider)
    items, top, used = digest["news_items"], [], set()
    for n in data.get("top_news_10") or []:
        idx = int(_num(n.get("idx")) or 0)
        if not 0 < idx <= len(items) or idx in used:
            continue  # 引用不到真實新聞的條目直接捨棄（不留沒有出處的內容）
        used.add(idx)
        top.append({"title": n.get("title") or items[idx - 1].get("title"), "point": n.get("point", ""),
                    "tag": _pick(n.get("tag"), TAGS, "中性"), **_ref_fields(items[idx - 1])})
    for i, src in enumerate(items, 1):  # 模型給不足 10 則時，依時間補上原始新聞（同樣帶真實出處）
        if len(top) >= 10:
            break
        if i not in used:
            top.append({"title": src.get("title"), "point": (src.get("summary") or "")[:60] or src.get("category", ""),
                        "tag": "中性", **_ref_fields(src)})
    top = top[:10]
    summary = {
        "updated_at": _now(), "generated_by": f"fast_team_runner（{meta['provider']}）",
        "market_sentiment": _pick(data.get("market_sentiment"), ("中立偏多", "偏多", "分歧震盪", "偏空警戒"), "分歧震盪"),
        "one_line_takeaway": data.get("one_line_takeaway", ""), "top_themes": (data.get("top_themes") or [])[:4],
        "top_news_10": top,
        # 持股代號一律剔除（新聞達人只點名非持股機會）
        "non_holding_opportunities": [o for o in data.get("non_holding_opportunities") or []
                                      if isinstance(o, dict) and str(o.get("stock_id")) not in held][:3],
        "risk_warnings": (data.get("risk_warnings") or [])[:4],
    }
    _write_json(NEWS_DIR / "daily_news_summary.json", summary)
    md = [f"# 📰 今日全市場重大財經新聞快報（{summary['updated_at'][:16]}）", "",
          "## 【🔄 昨日結論覆盤與思考修正】", data.get("reflection") or "記憶庫尚無可驗證的昨日結論", "",
          f"## 一句話結論（新聞情緒：{summary['market_sentiment']}）", summary["one_line_takeaway"], "",
          "## 今日財經新聞 TOP 10", *[_md_item(i, n) for i, n in enumerate(top, 1)],
          "", "## 熱門題材", *[f"- {t}" for t in summary["top_themes"]], "", "## 非持股潛力觀察名單",
          *[f"- {o.get('stock_id')} {o.get('name')}：{o.get('catalyst')}" for o in summary["non_holding_opportunities"]],
          "", "## 風險警示", *[f"- {r}" for r in summary["risk_warnings"]], "",
          f"> 由新聞達人（{meta['provider']}・{meta['model']}）依 Python 濃縮新聞產生，出處連結與時間取自爬蟲原始資料，0 Claude Token。"]
    _write_text(NEWS_DIR / "daily_news_brief.md", "\n".join(md))
    return {"provider": meta["provider"], "elapsed_sec": meta["elapsed_sec"], "top_news": len(top),
            "with_url": sum(1 for n in top if n.get("url"))}


def _us_brief(provider, us_status, force=False):
    refs = cdb.us_refs()
    by_ref = {r["ref"]: r for r in refs}
    listing = "\n".join(f"{r['ref']} {r['text']}" for r in refs)
    data, meta = _ask_json(US_SYSTEM, f"{cdb.market_reflection_line()}\n{cdb.market_digest()}\n資料清單：\n{listing}", provider)
    top, used = [], set()
    for x in data.get("us_top10") or []:
        ref = str(x.get("ref") or "").strip().upper() if isinstance(x, dict) else ""
        if ref not in by_ref or ref in used:
            continue  # 無法對應真實報價或新聞的條目捨棄
        used.add(ref)
        r = by_ref[ref]
        top.append({"ref": ref, "title": x.get("title") or r["text"][:30], "point": x.get("point", ""),
                    "tag": _pick(x.get("tag"), TAGS, "中性"), "url": r["url"], "source": r["source"], "pub_date": r["pub_date"]})
    for r in refs:  # 不足 10 條時以報價補齊
        if len(top) >= 10:
            break
        if r["ref"] not in used and r["kind"] == "quote":
            chg = r.get("change_pct") or 0
            top.append({"ref": r["ref"], "title": r["text"].split(" 收")[0], "point": r["text"],
                        "tag": "利多" if chg > 0.5 else "利空" if chg < -0.5 else "中性",
                        "url": r["url"], "source": r["source"], "pub_date": r["pub_date"]})
    pre = _read(US_DIR / "tw_premarket_summary.json")
    locked_today = (us_status["status"] == "LOCKED_COMPLETED" and pre.get("date") == us_status["tw_trading_date"]
                    and not force)
    pre["us_top10"] = top[:10]
    pre["us_top10_updated_at"] = _now()
    if not locked_today:  # 已由早報定錨的當日預判保留不覆蓋，只補 TOP 10（使用者手動強制更新時例外）
        snap = _read(US_DIR / "us_semi_quotes.json")
        q = snap.get("quotes") or {}
        pre.update({"date": us_status["tw_trading_date"], "generated_at": _now(),
                    "generated_by": f"fast_team_runner（{meta['provider']}）",
                    "sox_chg_pct": (q.get("^SOX") or {}).get("change_pct"), "tsm_adr_chg_pct": (q.get("TSM") or {}).get("change_pct"),
                    "tsm_premium_pct": (snap.get("tsm_adr") or {}).get("premium_pct"),
                    "us_key_drivers": (data.get("us_key_drivers") or [])[:4],
                    "tw_index_forecast": data.get("tw_index_forecast", ""), "opening_strategy": data.get("opening_strategy", ""),
                    "top_sector_picks": data.get("top_sector_picks") or {"favor": [], "avoid": []}})
        pre.setdefault("holdings_alert", [])
    _write_json(US_DIR / "tw_premarket_summary.json", pre)
    md = [f"# 🌅 今日台股盤前總結與當日走勢預判（{pre.get('date')}）", "",
          "## 【🔄 昨日結論覆盤與思考修正】", data.get("reflection") or "記憶庫尚無可驗證的昨日結論", "",
          "## 🦅 美股收盤後 TOP 10", *[_md_item(i, x) for i, x in enumerate(pre["us_top10"], 1)],
          "", "## 美股驅動因素", *[f"- {d}" for d in pre.get("us_key_drivers") or []], "",
          "## 台股大盤預判", pre.get("tw_index_forecast", ""), "", "## 開盤策略", pre.get("opening_strategy", ""), "",
          f"> 由川大帝MAGA（{meta['provider']}・{meta['model']}）依 Python 濃縮美股數據產生，出處連結與時間取自 Yahoo Finance／新聞原始資料，0 Claude Token"
          + ("；今日早報已定錨，預判沿用早報版本。" if locked_today else "。")]
    _write_text(US_DIR / "tw_premarket_summary.md", "\n".join(md))
    return {"provider": meta["provider"], "elapsed_sec": meta["elapsed_sec"], "us_top10": len(pre["us_top10"]),
            "with_url": sum(1 for x in pre["us_top10"] if x.get("url")), "kept_locked_forecast": locked_today}


def fallback_close_bullets(s):
    """模型失敗時的 Python 版條列重點（數字直接取自收盤總結）。"""
    t, inst, mg, rot = s.get("taiex") or {}, s.get("institutional") or {}, s.get("margin") or {}, s.get("rotation") or {}
    out = []
    if t:
        out.append({"label": CLOSE_LABELS[0], "text": f"加權收 {t.get('close'):,.0f}（{t.get('change_pct'):+.2f}%），"
                    + (f"量比 {t['vol_ratio_20']}，" if t.get("vol_ratio_20") else "")
                    + f"{'站上' if t['close'] >= t['ma20'] else '跌破'}月線 {t['ma20']:,.0f}，{t.get('trend')}"})
    if inst:
        out.append({"label": CLOSE_LABELS[1], "text": f"外資 {inst['foreign']:+.1f} 億、投信 {inst['trust']:+.1f} 億、自營商 {inst['dealer']:+.1f} 億"
                    + (f"；融資 {mg['change_100m']:+.1f} 億" if mg else "")})
    if rot.get("strong"):
        out.append({"label": CLOSE_LABELS[2], "text": "領漲：" + "、".join(f"{n}({v:+.2f}%)" for n, v in rot["strong"])
                    + "；領跌：" + "、".join(f"{n}({v:+.2f}%)" for n, v in rot.get("weak") or [])})
    if t:
        out.append({"label": CLOSE_LABELS[3], "text": f"{(s.get('reflection') or {}).get('headline') or '尚無覆盤'}；"
                    f"明日支撐 {t.get('low20'):,.0f}、壓力 {t.get('ma20'):,.0f}"})
    return out


def _close_bullets(provider):
    path = REPORTS / "market_close_summary.json"
    s = _read(path)
    if not s:
        return {"bullets": 0, "errors": ["尚無 market_close_summary.json"]}
    engine, errors = "python", []
    try:
        data, meta = _ask_json(CLOSE_SYSTEM, f"{cdb.market_digest()}\n{cdb.market_reflection_line()}\n"
                               f"收盤數據：{json.dumps({k: s.get(k) for k in ('taiex', 'institutional', 'margin', 'rotation')}, ensure_ascii=False)}",
                               provider, max_tokens=1500)
        bullets = [{"label": str(b.get("label") or CLOSE_LABELS[min(i, 3)]), "text": str(b.get("text") or "")}
                   for i, b in enumerate(data.get("bullets") or []) if isinstance(b, dict) and b.get("text")][:5]
        engine = meta["provider"]
    except (llm_router.LLMError, ValueError) as exc:
        bullets, errors = [], [f"收盤重點：{exc}"]
    if len(bullets) < 3:
        bullets = fallback_close_bullets(s)
    s.update(bullets=bullets, bullets_updated_at=_now(), bullets_engine=engine)
    _write_json(path, s)
    return {"bullets": len(bullets), "engine": engine, "errors": errors}


def daily_home_update(provider="gemini", fetch=True):
    """新聞達人 TOP 10 + 川大帝MAGA 美股 TOP 10 + 巴董事長收盤 3–5 點重點（三個請求平行）。"""
    from tools import market_close_summary as close_mod
    from tools import market_news_fetcher as news_mod
    from tools import memory_reflection as mem
    from tools import us_semi_tracker as us_mod
    started, out = time.perf_counter(), {"task": "daily_home_update", "errors": []}
    us_status = us_mod.check_status()
    if fetch:
        news_mod.run()
        if us_status["status"] != "LOCKED_COMPLETED":
            now = datetime.now()
            try:
                us_mod.run("night_open" if now.hour >= 20 or now.hour < 5 else "morning_close")
            except Exception as exc:  # 美股失敗不影響新聞與收盤總結
                out["errors"].append(f"美股抓取：{exc}")
        else:
            out["us_fetch"] = "今日早報已完成，略過重抓美股報價（美股新聞照常更新）"
            try:
                us_mod.run("news")
            except Exception as exc:
                out["errors"].append(f"美股新聞：{exc}")
        try:
            close_mod.build(force=True)
        except Exception as exc:
            out["errors"].append(f"收盤總結：{exc}")
        mem.run()
    digest = cdb.build_news_and_us_digest()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {"news": pool.submit(_news_brief, provider, digest), "us": pool.submit(_us_brief, provider, us_status),
                   "close": pool.submit(_close_bullets, provider)}
        for key, fut in futures.items():
            try:
                out[key] = fut.result()
                out["errors"] += out[key].pop("errors", []) if isinstance(out[key], dict) else []
            except Exception as exc:  # 單一人設失敗不影響其他
                out["errors"].append(f"{key}：{exc}")
    if fetch:
        mem.snapshot()
    out["elapsed_sec"] = round(time.perf_counter() - started, 2)
    return out


# ================================================================ 2) WAR ROOM 全員同步（全持股單次裁決）

SYNC_SYSTEM = PERSONAS + (
    "\n請在一次回覆中，對每一檔持股依序完成：籌碼王提案 → K線技術人提案 → 巴董事長反向 Challenge 與最終拍板。"
    "提案動作只能是：加碼、攤平、獲利了結、續抱、停損減碼。最終動作只能是：🟢 順勢加碼、🟡 支撐攤平、💰 分批獲利、⚪ 續抱觀察、🔴 減碼停損。"
    "仲裁鐵律：現價≤停損→🔴；有人提加碼/攤平但出現內盤賣壓/法人連賣/散戶融資承接/跌破VWAP/空頭趨勢→否決；"
    "攤平僅限現價在支撐上方2%內、五檔比≥1、非內盤主動賣出、法人未連賣、非空頭趨勢，且加碼≤原部位1/3；"
    "帳面獲利且逼近壓力→💰；兩方偏多且站上VWAP與MA20→🟢。理由各 40–60 字並引用數字，"
    "不得寫股數、成本或損益金額，部位只寫比例（如 1/2、1/3）。輸出 JSON：{\"代號\":{\"intraday_trend_summary\":\"\","
    "\"role2_action\":\"\",\"role2_reason\":\"\",\"role3_action\":\"\",\"role3_reason\":\"\",\"role4_final_action\":\"\","
    "\"role4_execution_plan\":\"仲裁理由＋執行價位＋比例\",\"key_support_intraday\":數字,\"key_resistance_intraday\":數字,"
    "\"reflection_fix\":\"昨日覆盤修正一句話\"}}"
)


def _stop_of(stock_id):
    k = cdb._data(stock_id, "kline")
    return _num((k.get("zhu_analysis") or {}).get("discipline_stop")) or _num((k.get("levels") or {}).get("stop_loss"))


def war_room_sync(provider="gemini", refresh=True):
    from tools import memory_reflection as mem
    from tools import portfolio_intraday as pi
    from tools import watchlist_intraday_radar as radar_mod
    started, out = time.perf_counter(), {"task": "war_room_sync", "errors": []}
    if refresh:
        for name, fn in (("portfolio_intraday", pi.run), ("radar", radar_mod.run), ("memory_reflection", mem.run)):
            try:
                fn()
            except Exception as exc:
                out["errors"].append(f"{name}：{exc}")
    ids = _portfolio_ids()
    if not ids:
        out["errors"].append("portfolio.json 沒有持股")
        return out
    cards = "\n\n".join(cdb.build_stock_compact_digest(s) for s in ids)
    data, meta = _ask_json(SYNC_SYSTEM, f"{cdb.market_digest()}\n{cdb.market_reflection_line()}\n\n{cards}", provider,
                           max_tokens=2000 + 450 * len(ids))
    live = {p["stock_id"]: p for p in _read(REPORTS / "portfolio_live_status.json").get("positions") or []}
    decisions, p2, p3 = {}, {}, {}
    for sid in ids:
        d = data.get(sid) or {}
        if not d:
            out["errors"].append(f"{sid}：模型未回覆")
            continue
        final = _pick(d.get("role4_final_action"), FINAL_ACTIONS, "⚪ 續抱觀察")
        price, stop = _num((live.get(sid) or {}).get("current_price")), _stop_of(sid)
        plan = d.get("role4_execution_plan", "")
        if price and stop and price <= stop and not final.startswith("🔴"):  # Python 端硬性風控：跌破停損一律減碼停損
            final, plan = "🔴 減碼停損", f"【系統風控】現價 {price:g} ≤ 停損 {stop:g}，覆寫為減碼停損。原計畫：{plan}"
        if d.get("reflection_fix"):
            plan += f"｜🔄 覆盤修正：{d['reflection_fix']}"
        decisions[sid] = {
            "stock_id": sid, "stock_name": (live.get(sid) or {}).get("name"), "updated_at": datetime.now().strftime("%H:%M:%S"),
            "intraday_trend_summary": d.get("intraday_trend_summary", ""),
            "role2_action": _pick(d.get("role2_action"), PROPOSALS, "續抱"), "role2_reason": d.get("role2_reason", ""),
            "role3_action": _pick(d.get("role3_action"), PROPOSALS, "續抱"), "role3_reason": d.get("role3_reason", ""),
            "role4_final_action": final, "role4_execution_plan": plan,
            "key_support_intraday": _num(d.get("key_support_intraday")),
            "key_resistance_intraday": _num(d.get("key_resistance_intraday")),
            "engine": meta["provider"],
        }
        p2[sid] = {"action": decisions[sid]["role2_action"], "reason": decisions[sid]["role2_reason"], "flow_summary": ""}
        p3[sid] = {"action": decisions[sid]["role3_action"], "reason": decisions[sid]["role3_reason"],
                   "key_support": decisions[sid]["key_support_intraday"], "key_resistance": decisions[sid]["key_resistance_intraday"]}
    if decisions:
        _write_json(DECISIONS_PATH, decisions)
        _write_json(REPORTS / "intraday_proposals_role2.json", p2)
        _write_json(REPORTS / "intraday_proposals_role3.json", p3)
        mem.snapshot()
    out.update(provider=meta["provider"], model=meta["model"], decided=len(decisions), prompt_chars=len(cards),
               elapsed_sec=round(time.perf_counter() - started, 2))
    return out


# ================================================================ 3) WAR ROOM 戰術提問

QA_SYSTEM = PERSONAS + (
    "\n使用者在盤中提出戰術問題。請一次輸出四位成員的回答 JSON：{\"news\":\"新聞達人回答（消息面與基本面查核）\","
    "\"chips\":\"籌碼王回答（五檔、大戶單、隔日沖、自營避險、融資券）\",\"kline\":\"K線技術人回答（趨勢、四線位階、轉折K與停損位）\","
    "\"chairman\":\"巴董事長：點出前三位盲點或衝突，結合昨日覆盤，給出一錘定音的操作指令（價位＋比例）\"}。"
    "每位 3–6 行，數字只能來自數據，不得寫股數、成本或損益金額。"
)


def _ensure_prefetch(stock_id):
    if not (REPORTS / "data" / f"{stock_id}_kline.json").exists():
        from tools import batch_prefetch as bp
        bp.run([stock_id], workers=3, roles=["kline", "chips"])


def ask_war_room(question, stock=None, scope=None, provider="gemini"):
    started = time.perf_counter()
    scope = scope or ("stock" if stock else "market")
    sid = None
    if scope == "stock":
        from tools import quick_stock_lookup as qsl
        sid = qsl.resolve(stock)[0]
        _ensure_prefetch(sid)
        context = cdb.build_stock_compact_digest(sid)
    elif scope == "holdings":
        context = "\n\n".join(cdb.build_stock_compact_digest(s) for s in _portfolio_ids())
    else:
        d = cdb.build_news_and_us_digest()
        context = f"{d['us']}\n新聞：\n{d['news'][:500]}"
    context = f"{cdb.market_digest()}\n{cdb.market_reflection_line()}\n{context}"
    data, meta = _ask_json(QA_SYSTEM, f"問題：{question}\n\n數據：\n{context}", provider, max_tokens=2500)
    qa = {"question": question, "asked_at": _now(), "scope": scope, "stock_id": sid, "engine": meta["provider"],
          **{k: str(data.get(k) or "") for k in ("news", "chips", "kline", "chairman")}}
    _write_json(QA_PATH, qa)
    return {"task": "ask_war_room", "provider": meta["provider"], "elapsed_sec": round(time.perf_counter() - started, 2),
            "chairman": qa["chairman"][:120], "errors": meta["errors"]}



# ---------------------------------------------------------------- 3b) VIP 來賓提問（隔離真實持股）

GUEST_QA_SYSTEM = PERSONAS + (
    "\n這是開放給來賓的提問台。數據只有公開市場資料、半導體 20 大龍頭建議買進價監控表與 100 萬模擬基金的公開持倉，"
    "不含任何人的私人持股；嚴禁猜測或提到任何人的私人持股、股數、成本或損益，也不要說「你的持股」。"
    "請一次輸出五位成員的回答 JSON：{\"news\":\"新聞達人回答（消息面與基本面查核）\","
    "\"chips\":\"籌碼王回答（法人、融資券、五檔與隔日沖判讀）\",\"kline\":\"K線技術人回答（趨勢、均線位階、轉折K與停損位）\","
    "\"maga\":\"川大帝MAGA回答（美股、費半與 ADR 對這個問題的影響）\","
    "\"chairman\":\"巴董事長：點出前四位的盲點或衝突，給出具體的建議買進價區間、停損價、停利目標價與風報比；"
    "風報比<1.5 或數據不足時寫明「暫不建議進場」與資料缺口，不硬給價位\"}。"
    "每位 3–6 行，所有數字只能來自數據。巴董事長最後一行固定寫「僅供研究參考，非投資建議」。"
)
GUEST_MAX_STOCKS = 3


def guest_stocks_in(question, limit=GUEST_MAX_STOCKS):
    """從來賓問題找出提到的台股（代號或名稱），依出現順序回傳 [(代號, 名稱)]。只認名稱表內的代號，避免把價位當代號。"""
    import re
    from tools import quick_stock_lookup as qsl
    names = dict(qsl.COMMON)  # 不呼叫 load_name_map()：它會讀 portfolio.json，來賓流程完全不碰真實持股檔
    names.update(qsl._read_json(qsl.NAME_CACHE, {}))
    names.update({str(r["stock_id"]): r["name"] for r in qsl._read_json(REPORTS / "sector_watchlist.json", [])
                  if r.get("stock_id") and r.get("name")})
    q = str(question or "")
    nq = qsl._norm(q)
    found = {}
    for m in re.finditer(r"(?<![\d.$＄])(\d{4}|00\d{2,4}[A-Z]?)(?![\d.])(?!\s*(?:元|塊|點|%|％|張|股|萬|億))", q.upper()):
        found.setdefault(m.group(1), m.start())
    hits = sorted(((c, n) for c, n in names.items() if len(qsl._norm(n)) >= 2 and qsl._norm(n) in nq),
                  key=lambda x: -len(qsl._norm(x[1])))
    taken = []
    for code, name in hits:  # 長名稱優先（南亞科 先於 南亞），被長名稱包住的短名稱略過
        n = qsl._norm(name)
        if any(n in t for t in taken):
            continue
        taken.append(n)
        found.setdefault(code, nq.find(n))
    return [(c, names.get(c, c)) for c, _ in sorted(found.items(), key=lambda x: x[1])][:limit]


def _fmt(v, f="{:,.2f}"):
    try:
        return f.format(float(v))
    except (TypeError, ValueError):
        return "N/A"


def _guest_stock_block(sid, name):
    """即時快查（報價＋日K均線＋法人籌碼），加上預抓檔與 20 強監控表的公開價位。不讀 portfolio 相關檔案。"""
    from tools import quick_stock_lookup as qsl
    r = qsl.lookup(sid, resolved=(sid, name))
    r = _dict(r)
    q, k = _dict(r.get("quote")), _dict(r.get("kline"))
    zhu, cv, boss = _dict(k.get("zhu")), _dict(r.get("chip_verdict")), _dict(r.get("chairman"))
    lines = [f"■{sid}{r.get('name') or name} 現價{_fmt(q.get('price') or k.get('close'))}({_fmt(q.get('change_pct'), '{:+.2f}')}%)"
             f" 開{_fmt(q.get('open'))} 高{_fmt(q.get('high'))} 低{_fmt(q.get('low'))} VWAP{_fmt(q.get('vwap'))}"
             f" 五檔比{_fmt(q.get('bid_ask_ratio'))}{q.get('tick_side') or ''}（{q.get('price_source') or k.get('source') or ''}）",
             f"【技術】{zhu.get('trend', 'N/A')}；MA5/10/20/60={_fmt(k.get('ma5'))}/{_fmt(k.get('ma10'))}/{_fmt(k.get('ma20'))}/"
             f"{_fmt(k.get('ma60'))}；月線乖離{_fmt(k.get('bias20_pct'), '{:+.1f}')}%；量比{_fmt(k.get('vol_ratio_20'))}；"
             f"5日{_fmt(k.get('change_5d_pct'), '{:+.1f}')}%；20日高/低{_fmt(k.get('high20'))}/{_fmt(k.get('low20'))}；"
             f"{zhu.get('ma_summary', '')}；轉折K:{'、'.join(str(_dict(p).get('pattern', '')) for p in _list(zhu.get('patterns'))) or '無'}；"
             f"停損依據:{zhu.get('stop_basis', '')}",
             f"【籌碼】{cv.get('label', 'N/A')}：" + "；".join(str(x) for x in _list(cv.get("alerts"))[:2] + _list(cv.get("points"))[:3])[:260],
             f"【系統快篩】支撐{_fmt(boss.get('support'))} 壓力{_fmt(boss.get('resistance'))} 停損{_fmt(boss.get('stop'))}"
             f" 風報比{_fmt(boss.get('rr'))}；{boss.get('line', '')}"]
    if (REPORTS / "data" / f"{sid}_fundamental.json").exists():
        lines.append(cdb.fundamental_line(sid))
    radar = next((x for x in _list(_dict(cdb._read(REPORTS / "watchlist_radar_live.json")).get("results"))
                  if isinstance(x, dict) and x.get("stock_id") == sid), None)
    if radar:
        lines.append(f"【20 強監控表】{radar.get('signal_label') or radar.get('signal')} 建議買進{_fmt(radar.get('entry_low'))}–"
                     f"{_fmt(radar.get('entry_high'))} 停損{_fmt(radar.get('stop_loss'))} 停利目標{_fmt(radar.get('target_1'))}"
                     f" 風報比{_fmt(radar.get('risk_reward'))}；{radar.get('role4_note') or ''}")
    if _list(r.get("errors")):
        lines.append("【資料缺口】" + "；".join(str(e) for e in r["errors"])[:160])
    return "\n".join(lines)


def _guest_background():
    """公開背景：大盤、美股、新聞、20 大龍頭監控表、100 萬模擬基金公開持倉。"""
    from tools import sim_portfolio_engine as sim
    radar = _dict(cdb._read(REPORTS / "watchlist_radar_live.json"))
    rows = [f"{x.get('stock_id', '')}{x.get('name', '')} {x.get('signal_label') or x.get('signal')} 現價{_fmt(x.get('price'))}"
            f" 建議買進{_fmt(x.get('entry_low'))}–{_fmt(x.get('entry_high'))} 停損{_fmt(x.get('stop_loss'))}"
            for x in _list(radar.get("results"))[:20] if isinstance(x, dict)]
    try:
        acct = _dict(sim.load_account())
        hold = "；".join(f"{h.get('stock_id', '')}{h.get('name', '')} {h.get('shares', 0)}股 均價{_fmt(h.get('avg_cost'))}"
                        f" 未實現{_fmt(h.get('unrealized_pct'), '{:+.2f}')}% 停損{_fmt(h.get('stop_loss'))} 目標{_fmt(h.get('target_price'))}"
                        for h in _list(acct.get("holdings")) if isinstance(h, dict)) or "空手"
        sim_line = (f"【100萬模擬基金公開持倉】總淨值{_fmt(acct.get('total_equity'), '{:,.0f}')} 可用現金"
                    f"{_fmt(acct.get('cash_balance'), '{:,.0f}')}；{hold}")
    except Exception as exc:
        sim_line = f"【100萬模擬基金】讀取失敗：{type(exc).__name__}"
    news, _ = cdb.news_digest(limit=10, max_chars=500)
    return "\n".join([cdb.market_digest(), cdb.us_digest(), f"新聞：\n{news}",
                      f"【半導體 20 大龍頭建議買進價監控表】（{radar.get('updated_at', 'N/A')}）", *rows, sim_line])


def ask_war_room_guest(question, provider="gemini"):
    """VIP 來賓提問：Prompt 只含公開資料，絕不讀 portfolio.json／持股報價檔／持股叮嚀／持股覆盤；結果不寫檔，由呼叫端存在 session。"""
    from tools import privacy_guard
    started = time.perf_counter()
    stocks = guest_stocks_in(question)
    blocks, errors = [], []
    for sid, name in stocks:
        try:
            blocks.append(_guest_stock_block(sid, name))
        except Exception as exc:  # 單檔失敗不影響其他
            errors.append(f"{sid} 即時快查失敗：{type(exc).__name__}")
    focus = "\n\n".join(blocks) if blocks else "（問題未提到特定個股，請以大盤、美股與 20 大龍頭回答）"
    data, meta = _ask_json(GUEST_QA_SYSTEM, f"問題：{question}\n\n【提問個股即時資料】\n{focus}\n\n【公開背景】\n"
                           f"{_guest_background()}", provider, max_tokens=2800)
    clean = privacy_guard.redact_holdings_text  # 最後一道：真實股數／成本一律遮掉
    if not any(str(data.get(k) or "").strip() for k in ("news", "chips", "kline", "maga", "chairman")):
        raise llm_router.LLMError("模型回覆缺少五位成員的回答欄位")
    return {"question": question, "asked_at": _now(), "stock_ids": [s for s, _ in stocks], "engine": meta["provider"],
            "elapsed_sec": round(time.perf_counter() - started, 2), "errors": errors + [str(e) for e in _list(_dict(meta).get("errors"))],
            **{k: clean(str(data.get(k) or "")) for k in ("news", "chips", "kline", "maga", "chairman")}}


# ================================================================ 4) 巴董事長深入個股研究（單次深潛）

DEEP_SYSTEM = PERSONAS + (
    "\n請對單一個股做完整會診：新聞達人、籌碼王、K線技術人各給評級與 3 點論點，巴董事長做 Red-Team（Priced-in、隔日沖、"
    "投信被動ETF、乖離過大）並拍板。輸出 JSON：{\"reflection\":\"昨日覆盤與修正\",\"one_line\":\"\",\"verdict\":\"強力買進|分批布局|持有觀望|減碼|出場避險\","
    "\"confidence\":0-100,\"entry_zone\":[下緣,上緣],\"stop_loss\":數字,\"target_1\":數字,\"target_2\":數字,"
    "\"role1_rating\":\"強多|偏多|中性|偏空|強空\",\"role1_points\":[\"\"],\"role2_rating\":\"\",\"role2_points\":[\"\"],"
    "\"role3_rating\":\"\",\"role3_points\":[\"\"],\"red_team\":[\"四項各一句：成立/不成立+證據\"],\"conflict_point\":\"\","
    "\"red_team_warning\":\"\",\"position_advice\":\"持有中才填，只寫比例與價位，否則 null\",\"invalidation\":\"失效條件\"}。"
    "entry_zone 上緣不得高於現價+1%，stop_loss 不得比 K 線停損寬鬆。"
)


def deep_dive(stock, provider="gemini", mode=None):
    from tools import memory_reflection as mem
    from tools import quick_stock_lookup as qsl
    started = time.perf_counter()
    sid, name = qsl.resolve(stock)
    _ensure_prefetch(sid)
    mem.run([sid])
    digest = cdb.build_stock_compact_digest(sid)
    data, meta = _ask_json(DEEP_SYSTEM, f"{cdb.market_digest()}\n{cdb.market_reflection_line()}\n{digest}", provider,
                           max_tokens=3000)
    mode = mode or ("intraday" if datetime.now().weekday() < 5 and "09:00" <= datetime.now().strftime("%H:%M") <= "13:35"
                    else "after_hours")
    k = cdb._data(sid, "kline")
    price = _num((cdb.live_position(sid) or {}).get("current_price")) or _num(k.get("close"))
    zone = [_num(x) for x in (data.get("entry_zone") or [])[:2]]
    if len(zone) == 2 and all(zone) and price:
        zone = sorted(zone)
        zone[1] = min(zone[1], round(price * 1.01, 2))
        zone[0] = min(zone[0], zone[1])
    else:
        zone = None
    stop, t1 = _num(data.get("stop_loss")), _num(data.get("target_1"))
    k_stop = _stop_of(sid)
    if stop and k_stop and stop < k_stop:  # 停損不可比 K線技術人寬鬆
        stop = k_stop
    rr = None
    if zone and stop and t1:
        mid = sum(zone) / 2
        rr = round((t1 - mid) / (mid - stop), 2) if mid > stop else None
    verdict = _pick(data.get("verdict"), VERDICTS, "持有觀望")
    if rr is not None and rr < 1.5 and verdict in ("強力買進", "分批布局"):
        verdict = "持有觀望"
    zhu = k.get("zhu_analysis") or {}
    if zhu.get("trend") == "空頭趨勢" and not zhu.get("has_reversal_signal") and verdict in ("強力買進", "分批布局"):
        # 安全優先矩陣第 3 條：空頭趨勢且未見轉折紅 K → 上限持有觀望（暫不買進）
        data["conflict_point"] = f"【系統風控】空頭趨勢未見轉折紅 K，{verdict}降為持有觀望。" + (data.get("conflict_point") or "")
        verdict = "持有觀望"
    held = (cdb.live_position(sid) or {}).get("in_portfolio")
    summary = {
        "stock_id": sid, "stock_name": name, "updated_at": _now(), "mode": mode, "verdict": verdict,
        "confidence": int(_num(data.get("confidence")) or 50), "risk_reward_ratio": rr, "entry_zone": zone,
        "stop_loss": stop, "target_1": t1, "target_2": _num(data.get("target_2")),
        "role1_rating": _pick(data.get("role1_rating"), RATINGS, "中性"), "role2_rating": _pick(data.get("role2_rating"), RATINGS, "中性"),
        "role3_rating": _pick(data.get("role3_rating"), RATINGS, "中性"),
        "conflict_point": data.get("conflict_point", ""), "red_team_warning": data.get("red_team_warning", ""),
        "position_advice": data.get("position_advice") if held else None, "engine": meta["provider"],
    }
    _write_json(STOCKS_DIR / f"{sid}_summary.json", summary)
    stamp = summary["updated_at"][:16]
    tail = f"\n\n> 由 {meta['provider']}・{meta['model']} 單次濃縮會診產生（未派子代理）。僅供研究參考，非投資建議。"
    for suffix, label, rkey, pkey in (("role1_fundamental", "新聞達人基本面", "role1_rating", "role1_points"),
                                      ("role2_chips", "籌碼王籌碼", "role2_rating", "role2_points"),
                                      ("role3_kline", "K線技術人 K 線", "role3_rating", "role3_points")):
        _write_text(STOCKS_DIR / f"{sid}_{suffix}.md", "\n".join(
            [f"# {sid} {name}｜{label}報告（{stamp}，快速會診）", f"## 評級：{summary[rkey]}", "## 核心論點",
             *[f"{i}. {p}" for i, p in enumerate(data.get(pkey) or [], 1)]]) + tail)
    chief = [f"# {sid} {name}｜巴董事長總裁決（{stamp}，{'盤中' if mode == 'intraday' else '盤後'}）",
             "## 【🔄 昨日結論覆盤與思考修正】", data.get("reflection") or "記憶庫尚無可驗證的昨日結論",
             "## 一句話結論", data.get("one_line", ""), "## 濃縮數據", "```", digest, "```",
             "## Red-Team 反向挑戰", *[f"- {x}" for x in data.get("red_team") or []],
             "## 衝突仲裁", summary["conflict_point"],
             "## 交易計畫", f"- 裁決：{verdict}（信心 {summary['confidence']}）",
             f"- 進場區：{zone}｜停損：{stop}｜目標 1/2：{t1}/{summary['target_2']}｜風報比：{rr}",
             "## 持倉部位建議", summary["position_advice"] or "非持股或未提供", "## 失效條件", data.get("invalidation", "")]
    _write_text(STOCKS_DIR / f"{sid}_role4_chief.md", "\n".join(chief) + tail)
    mem.snapshot()
    return {"task": "deep_dive", "stock_id": sid, "provider": meta["provider"], "verdict": verdict, "rr": rr,
            "prompt_chars": len(digest), "elapsed_sec": round(time.perf_counter() - started, 2), "errors": meta["errors"]}


# ================================================================ 6) 🧪 SIMULATION ROOM：五人投決會 ➔ 巴董事長拍板模擬交易

SIM_ACTIONS = ("BUY", "ADD", "TRIM", "EXIT_PROFIT", "EXIT_STOP", "HOLD")
SIM_SYSTEM = PERSONAS + (
    "\n你們正在召開「2026 年底衝刺 +10%～+20%」100 萬模擬基金投決會（與使用者真實持股無關）。依序發言："
    "川大帝MAGA（美股費半與對應次產業風向）→ 新聞達人（營收、法說會、題材催化劑）→ 籌碼王（投信/外資鎖碼度、自營商權證避險、"
    "融資增減、盤中五檔內外盤，嚴防隔日沖倒貨）→ K線技術人（頭頭高底底高趨勢、四線排列、關鍵轉折K、精確停損位）→ "
    "巴董事長 challenge 前四位盲點並拍板。鐵律：單一持股部位 15～30 萬元；股數必須是 100 的倍數；至少保留總淨值 10% 現金；"
    "風報比<1.5 不買；空頭趨勢且未見轉折紅K不買；停損必須低於現價且不得比 K 線停損寬鬆；寧可空手也不亂買，"
    "但也要考慮距離年底目標的時間壓力與資金效率。已持有者可 ADD/TRIM/EXIT_PROFIT/EXIT_STOP/HOLD（HOLD 可上移停損做移動停利）。"
    "輸出 JSON：{\"reflection\":\"昨日預判落差覆盤一句話\",\"maga\":\"\",\"news\":\"\",\"chips\":\"\",\"kline\":\"\","
    "\"chairman_challenge\":\"點出前四位盲點\",\"chairman_summary\":\"本次總體配置指令\","
    "\"decisions\":[{\"stock_id\":\"\",\"action\":\"BUY|ADD|TRIM|EXIT_PROFIT|EXIT_STOP|HOLD\",\"shares\":股數,"
    "\"stop_loss\":數字,\"target_price\":數字,\"reason\":\"巴董事長拍板理由（40–80字）\",\"team_view\":\"四位成員對此股的共識與分歧一句話\"}]}。"
    "每位成員發言 3–5 句並引用數字；decisions 只能包含候選名單中的代號。"
)


def _sim_candidates(sim, scan, top_n=5):
    """步驟 A：目前模擬持倉 ＋ 20 強中買進評分最高的前 top_n 檔（排除已持有）。"""
    acct = sim.load_account()
    held = [h["stock_id"] for h in acct["holdings"]]
    prey = {p["stock_id"]: p for p in scan.get("prey") or []}
    # 最小下單單位 100 股就超過單檔 30 萬上限的高價股（如聯發科、創意）無法依規則建倉，不列入新機會
    fresh = [p["stock_id"] for p in scan.get("prey") or []
             if not p["held"] and p.get("price") and p["price"] * sim.LOT_UNIT <= sim.POSITION_MAX][:top_n]
    return [(sid, prey.get(sid, {}), sid in held) for sid in held + fresh]


def _sim_card(sid, p, held_row):
    radar = (f"建議買進價監控表:{p.get('signal_label', '')}｜進場{p.get('entry_low')}–{p.get('entry_high')}｜監控表停損價{p.get('stop_loss')}"
             f"｜目標{p.get('target_1')}｜RR{p.get('risk_reward')}｜買進評分{p.get('score')}"
             + (f"｜100股需{p['price'] * 100 / 10000:.1f}萬，單檔15–30萬可買{int(300000 // (p['price'] * 100)) * 100}股以內"
                if p.get("price") else "")) if p else ""
    pos = (f"【模擬持有 {held_row['shares']} 股，均價 {held_row['avg_cost']:.2f}，未實現 {held_row.get('unrealized_pct', 0):+.2f}%，"
           f"停損 {held_row.get('stop_loss')}，目標 {held_row.get('target_price')}】") if held_row else ""
    return f"{cdb.build_stock_compact_digest(sid, include_position=False)}\n{radar}{pos}"


def _sim_guard(sim, d, quotes, acct, candidates):
    """步驟 C 前的 Python 硬性風控：回傳 (可執行的參數 dict, None) 或 (None, 拒絕原因)。"""
    sid = str(d.get("stock_id") or "").strip()
    action = str(d.get("action") or "HOLD").upper()
    if sid not in candidates:
        return None, "不在候選名單"
    if action not in SIM_ACTIONS:
        return None, f"不支援的動作 {action}"
    q = quotes.get(sid)
    if not q and action != "HOLD":
        return None, "取不到即時報價"
    price = q["price"] if q else None
    h = sim.holding_of(acct, sid)
    stop, target = _num(d.get("stop_loss")), _num(d.get("target_price"))
    if action == "HOLD":
        return {"sid": sid, "action": "HOLD", "price": price, "stop": stop, "target": target}, None
    if action in ("TRIM", "EXIT_PROFIT", "EXIT_STOP"):
        if not h:
            return None, "沒有此模擬持倉"
        shares = h["shares"] if action != "TRIM" else int((_num(d.get("shares")) or 0) // sim.LOT_UNIT * sim.LOT_UNIT)
        if shares <= 0:
            return None, "減碼股數不足 100 股"
        return {"sid": sid, "action": action, "price": price, "shares": min(shares, h["shares"])}, None
    # ---- BUY / ADD
    zhu = sim._zhu(sid)
    if zhu.get("trend") == "空頭趨勢" and not zhu.get("has_reversal_signal"):
        return None, "空頭趨勢且未見轉折紅 K（安全矩陣：暫不買進）"
    k_stop = _stop_of(sid)
    if k_stop and k_stop < price and (not stop or stop < k_stop):
        stop = k_stop  # 停損不得比 K線技術人寬鬆
    if not stop or stop >= price:
        return None, f"停損 {stop} 必須低於現價 {price}"
    prey = candidates[sid]
    if not target or target <= price:
        target = _num(prey.get("target_1"))
    if not target or target <= price:
        return None, "沒有高於現價的目標價"
    rr = (target - price) / (price - stop)
    if rr < 1.5:
        return None, f"風報比 {rr:.2f} < 1.5"
    held_value = (h["shares"] * price) if h else 0
    room = sim.POSITION_MAX - held_value
    reserve = acct["total_equity"] * sim.CASH_RESERVE_PCT / 100
    budget = min(room, acct["cash_balance"] - reserve)
    want = (_num(d.get("shares")) or 0) * price
    if action == "BUY" or not h:
        want = max(want, sim.POSITION_MIN)  # 新建倉至少 15 萬
    value = min(want, budget)
    shares = int(value // price // sim.LOT_UNIT * sim.LOT_UNIT)
    if not h and shares * price < sim.POSITION_MIN:  # 新建倉：無條件進位到滿足 15 萬下限的最小 100 股單位
        shares = int(-(-sim.POSITION_MIN // (price * sim.LOT_UNIT)) * sim.LOT_UNIT)
    while shares > 0 and shares * price + sim.fee_of(shares * price) > budget:
        shares -= sim.LOT_UNIT  # 含手續費超出預算時退一單位
    if shares <= 0:
        if price * sim.LOT_UNIT > sim.POSITION_MAX:
            return None, f"單價 {price:g} 元，最小單位 100 股需 {price * sim.LOT_UNIT:,.0f} 元，超過單檔上限 30 萬"
        return None, f"可用預算 {budget:,.0f} 元（保留 10% 現金、單檔上限 30 萬）買不到 100 股"
    if not h and shares * price < sim.POSITION_MIN * 0.95:
        return None, f"預算只夠 {shares * price:,.0f} 元，低於新建倉下限 15 萬"
    return {"sid": sid, "action": "ADD" if h else "BUY", "price": price, "shares": shares, "stop": round(stop, 2),
            "target": round(target, 2), "rr": round(rr, 2)}, None


def sim_trade_session(mode="intraday_hunt", provider="gemini"):
    """🧪 五人投決會：A 量化篩選 → B 單次多人設討論（預設免費 Gemini）→ C Python 風控後以最新真實價模擬交割。"""
    from tools import batch_prefetch as bp
    from tools import sim_portfolio_engine as sim
    started = time.perf_counter()
    out = {"task": "sim_trade_session", "mode": mode, "errors": []}
    sim.init_account()
    sector_ids = [r["stock_id"] for r in _read(REPORTS / "sector_watchlist.json", [])]
    try:
        bp.run(sector_ids, workers=8, roles=["kline", "chips"])  # 新鮮檔自動沿用，不浪費額度
    except Exception as exc:
        out["errors"].append(f"20 強預抓：{exc}")
    scan = sim.refresh_sim_intraday()
    out["errors"] += scan.get("errors") or []
    executed = []
    trading = sim.is_tw_market_trading_hours()
    if not trading:
        mode = "watchlist_plan"  # 非盤中：只產出盤後觀察與明日開盤預備劇本，嚴禁下單交割
    out["mode"], out["trading_allowed"] = mode, trading
    # ---- 強制風控：模擬持倉跌破停損先出清（不等模型；僅盤中）
    for a in scan.get("alerts") or [] if trading else []:
        if a["type"] == "STOP":
            try:
                t = sim.execute_trade(a["stock_id"], "EXIT_STOP", sim.holding_of(sim.load_account(), a["stock_id"])["shares"],
                                      buffett_verdict=f"【系統風控】{a['text']}，依紀律停損出清")
                executed.append({"stock_id": a["stock_id"], "action": "EXIT_STOP", "status": "executed", "trade_id": t["trade_id"],
                                 "shares": t["shares"], "price": t["price"], "reason": f"系統風控：{a['text']}"})
            except sim.SimError as exc:
                out["errors"].append(f"停損出清 {a['stock_id']}：{exc}")
    acct = sim.load_account()
    goal = sim.goal_status(acct)
    cands = _sim_candidates(sim, scan)
    cand_map = {sid: p for sid, p, _ in cands}
    if not cands:
        out["errors"].append("沒有候選標的")
        return out
    mode_label = {"intraday_hunt": "盤中交易", "daily_rebalance": "每日再平衡", "watchlist_plan": "盤後觀察（WATCHLIST_PLAN）"}[mode]
    plan_rule = ("" if trading else
                 "\n⚠️ 目前非台股盤中時段（週一至週五 09:00–13:30 以外），本場嚴禁下單：decisions 的 action 一律填 WATCHLIST_PLAN，"
                 "shares 填明日預計股數，stop_loss／target_price 填預設價位，reason 寫明日開盤的進場觸發條件（價位、五檔、VWAP）。")
    header = (f"模式：{mode_label}｜日期 {_now()[:16]}｜距 {acct['target_date']} 剩 {goal['days_left']} 天{plan_rule}\n"
              f"總淨值 {acct['total_equity']:,}（{acct['total_return_pct']:+.2f}%）｜現金 {acct['cash_balance']:,}（{goal['cash_pct']}%）"
              f"｜距 110 萬還差 {goal['gap_to_low']:,}、距 120 萬還差 {goal['gap_to_high']:,}｜持倉 {len(acct['holdings'])} 檔")
    cards = "\n\n".join(_sim_card(sid, p, sim.holding_of(acct, sid)) for sid, p, _ in cands)
    user = f"{header}\n{cdb.us_digest()}\n{cdb.market_digest()}\n{cdb.market_reflection_line()}\n\n候選名單：\n{cards}"
    data, meta = _ask_json(SIM_SYSTEM, user, provider, max_tokens=4000)
    quotes = sim.latest_quotes(list(cand_map))
    members = {k: str(data.get(k) or "") for k in ("maga", "news", "chips", "kline")}
    for d in data.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        acct = sim.load_account()
        plan, why = _sim_guard(sim, d, quotes, acct, cand_map)
        row = {"stock_id": d.get("stock_id"), "proposed_action": str(d.get("action") or "").upper(),
               "proposed_shares": d.get("shares"), "reason": d.get("reason", ""), "team_view": d.get("team_view", "")}
        if not trading:  # 只記錄明日預備劇本＋系統風控預審結果，不交割
            row["proposed_action"] = "WATCHLIST_PLAN"
            preview, why_not = _sim_guard(sim, {**d, "action": "BUY" if not sim.holding_of(acct, d.get("stock_id")) else "HOLD"},
                                          quotes, acct, cand_map)
            executed.append({**row, "status": "plan", "shares": (preview or {}).get("shares"), "price": (preview or {}).get("price"),
                             "stop_loss": (preview or {}).get("stop") or _num(d.get("stop_loss")),
                             "target_price": (preview or {}).get("target") or _num(d.get("target_price")),
                             "rr": (preview or {}).get("rr"),
                             "note": "風控預審通過，明日盤中可執行" if preview else f"風控預審未通過：{why_not}"})
            continue
        if plan is None:
            executed.append({**row, "status": "rejected", "reject_reason": why})
            continue
        if plan["action"] == "HOLD":
            h = sim.holding_of(acct, plan["sid"])
            if h and plan.get("stop") and plan["stop"] > (h.get("stop_loss") or 0) and plan["stop"] < (plan["price"] or 0):
                sim.update_levels(plan["sid"], plan["stop"], plan.get("target"))  # 只允許上移停損（移動停利）
                row["note"] = f"停損上移至 {plan['stop']}"
            executed.append({**row, "action": "HOLD", "status": "hold"})
            continue
        name = (quotes.get(plan["sid"]) or {}).get("name") or cand_map[plan["sid"]].get("name")
        try:
            t = sim.execute_trade(plan["sid"], plan["action"], plan["shares"], price=plan["price"], name=name,
                                  stop_loss=plan.get("stop"), target_price=plan.get("target"), thesis=d.get("reason"),
                                  team_discussion=d.get("team_view") or "｜".join(f"{MEMBER_NAMES[k]}：{v[:40]}" for k, v in members.items()),
                                  buffett_verdict=f"{d.get('reason', '')}｜停損 {plan.get('stop')}｜目標 {plan.get('target')}"
                                                  + (f"｜風報比 {plan['rr']}" if plan.get("rr") else ""))
            executed.append({**row, "action": t["action"], "status": "executed", "trade_id": t["trade_id"], "shares": t["shares"],
                             "price": t["price"], "stop_loss": plan.get("stop"), "target_price": plan.get("target"), "rr": plan.get("rr")})
        except sim.SimError as exc:
            executed.append({**row, "status": "rejected", "reject_reason": str(exc)})
    after = sim.load_account()
    meeting = {
        "held_at": _now(), "mode": mode, "trading_allowed": trading, "engine": meta["provider"], "model": meta["model"],
        "account_before": {"total_equity": acct["total_equity"], "cash": acct["cash_balance"]},
        "account_after": {"total_equity": after["total_equity"], "cash": after["cash_balance"],
                          "return_pct": after["total_return_pct"], "holdings": len(after["holdings"])},
        "goal": goal, "candidates": [{"stock_id": sid, "name": p.get("name"), "held": held, "score": p.get("score"),
                                      "signal": p.get("signal_label"), "zhu_trend": p.get("zhu_trend"), "price": p.get("price")}
                                     for sid, p, held in cands],
        "reflection": data.get("reflection", ""), "members": members,
        "chairman": {"challenge": data.get("chairman_challenge", ""), "summary": data.get("chairman_summary", "")},
        "decisions": executed,
    }
    _write_json(sim.MEETING_PATH, meeting)
    out.update(provider=meta["provider"], executed=sum(1 for e in executed if e["status"] == "executed"),
               rejected=sum(1 for e in executed if e["status"] == "rejected"),
               planned=sum(1 for e in executed if e["status"] == "plan"), total_equity=after["total_equity"],
               cash=after["cash_balance"], elapsed_sec=round(time.perf_counter() - started, 2))
    return out


# ================================================================ 5) 單一成員獨立更新

AGENT_ALIASES = {"news_master": "news", "chip_king": "chips", "kline_tech": "kline", "chairman_buffett": "chairman",
                 "trump_maga": "maga"}
MEMBER_NAMES = {"news": "新聞達人", "chips": "籌碼王", "kline": "K線技術人", "chairman": "巴董事長", "maga": "川大帝MAGA"}
MEMBER_REPORTS = {
    "news": ("role1_fundamental", "role1_rating", "新聞達人基本面",
             "新聞達人（消息面與基本面查核：營收 YoY/MoM、毛利率與 EPS、近期新聞真利多或短線炒作、Priced-in 線索）"),
    "chips": ("role2_chips", "role2_rating", "籌碼王籌碼",
              "籌碼王（權證小哥四大檢查：隔日沖與假外資、自營商自行 vs 權證避險、大戶 vs 散戶融資、天花板線/地板線與盤中五檔）"),
    "kline": ("role3_kline", "role3_rating", "K線技術人 K 線",
              "K線技術人（朱家泓四步驟：趨勢定多空、四線排列與扣抵、關鍵轉折 K 與量價、紀律停損與目標價）"),
}


def _member_report(member, sid, provider):
    """單一成員個人報告（單次請求），寫入 reports/stocks/<代號>_<roleN>.md 並同步 summary 的評級。"""
    suffix, rating_key, label, persona = MEMBER_REPORTS[member]
    system = PERSONAS + (
        f"\n你現在只扮演{persona}，對單一個股寫個人報告。輸出 JSON：{{\"reflection\":\"昨日結論覆盤與思考修正\","
        "\"rating\":\"強多|偏多|中性|偏空|強空\",\"points\":[\"3 點核心論點（引用數字）\"],"
        "\"sections\":[{\"title\":\"分析段落標題\",\"text\":\"內容\"}],\"next_view\":\"對明日的新判斷與關鍵價位\"}。"
        "sections 3–4 段，不得寫股數、成本或損益金額。"
    )
    from tools import quick_stock_lookup as qsl
    _, name = qsl.resolve(sid)
    data, meta = _ask_json(system, f"{cdb.market_digest()}\n{cdb.market_reflection_line()}\n{cdb.build_stock_compact_digest(sid)}",
                           provider, max_tokens=2500)
    rating = _pick(data.get("rating"), RATINGS, "中性")
    try:  # 學習資料庫：單人報告也存成一筆預測，之後以實盤自動驗算
        from tools import learning_engine
        k = cdb._data(sid, "kline")
        lv = k.get("levels") or {}
        learning_engine.record_prediction(
            member, sid, rating, price=(cdb.live_position(sid) or {}).get("current_price") or k.get("close"),
            buy_zone=lv.get("defense_support"), stop_loss=_stop_of(sid), target_price=lv.get("resistance_1"),
            rationale=f"{rating}：{(data.get('points') or [''])[0]}", source="single_report", name=name)
    except Exception as exc:  # 寫入失敗不影響報告
        print(f"⚠️ 學習資料庫寫入失敗：{exc!r}", file=sys.stderr)
    lines = [f"# {sid} {name}｜{label}報告（{_now()[:16]}，單獨更新）", "",
             "## 【🔄 昨日結論覆盤與思考修正】", data.get("reflection") or "記憶庫尚無可驗證的昨日結論", "",
             f"## 評級：{rating}", "## 核心論點", *[f"{i}. {p}" for i, p in enumerate(data.get("points") or [], 1)], ""]
    for sec in data.get("sections") or []:
        if isinstance(sec, dict):
            lines += [f"## {sec.get('title', '')}", str(sec.get("text", "")), ""]
    lines += ["## 對明日的新判斷", data.get("next_view", ""), "",
              f"> 由{MEMBER_NAMES[member]}（{meta['provider']}・{meta['model']}）單次濃縮請求產生（未派子代理）。僅供研究參考，非投資建議。"]
    _write_text(STOCKS_DIR / f"{sid}_{suffix}.md", "\n".join(lines))
    summary_path = STOCKS_DIR / f"{sid}_summary.json"
    summary = _read(summary_path)
    if summary:  # 巴董事長裁決卡上的成員評級同步更新
        summary[rating_key] = rating
        _write_json(summary_path, summary)
    return {"provider": meta["provider"], "rating": rating}


def update_single_agent(agent, stock=None, provider="gemini"):
    """只更新一位成員：news_master／chip_king／kline_tech／chairman_buffett／trump_maga（或 news/chips/kline/chairman/maga）。"""
    from tools import batch_prefetch as bp
    from tools import market_news_fetcher as news_mod
    from tools import memory_reflection as mem
    from tools import us_semi_tracker as us_mod
    started = time.perf_counter()
    member = AGENT_ALIASES.get(agent, agent)
    if member not in MEMBER_NAMES:
        raise LookupError(f"未知的成員：{agent}")
    sid = None
    if stock and member != "maga":
        from tools import quick_stock_lookup as qsl
        sid = qsl.resolve(stock)[0]
    out = {"task": "update_single_agent", "agent": agent, "member": member, "member_name": MEMBER_NAMES[member],
           "stock_id": sid, "errors": []}

    def step(name, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # 資料步驟失敗時記錄並繼續，讓報告仍可用舊資料產出
            out["errors"].append(f"{name}：{exc}")
            return None

    if member == "news":
        step("新聞抓取", news_mod.run)
        r = step("新聞 TOP 10", _news_brief, provider, cdb.build_news_and_us_digest())
        out.update(provider=(r or {}).get("provider"))
        if sid:
            step("基本面預抓", bp.run, [sid], workers=3, force=True, roles=["fundamental"])
            r = step("基本面報告", _member_report, "news", sid, provider)
    elif member in ("chips", "kline"):
        if not sid:
            raise LookupError(f"{MEMBER_NAMES[member]}單獨更新需要指定個股")
        step("預抓", bp.run, [sid], workers=3, force=True, roles=["chips" if member == "chips" else "kline"])
        if sid in _portfolio_ids():
            from tools import portfolio_intraday as pi
            step("持股盤中報價", pi.run)
        from tools import watchlist_intraday_radar as radar_mod
        step("20 強建議買進價監控表", radar_mod.run)
        step("覆盤", mem.run, [sid])
        r = step("個人報告", _member_report, member, sid, provider)
    elif member == "chairman":
        if not sid:
            raise LookupError("巴董事長單獨更新需要指定個股")
        step("預抓", bp.run, [sid], workers=3)
        r = step("深入研究", deep_dive, sid, provider)
    else:  # 川大帝MAGA：即使今日已休眠，使用者手動更新時也強制刷新（不改變鎖定狀態）
        now = datetime.now()
        step("美股報價與新聞", us_mod.run, "night_open" if now.hour >= 20 or now.hour < 5 else "morning_close", force=True)
        r = step("美股 TOP 10", _us_brief, provider, us_mod.check_status(), force=True)
    out["provider"] = out.get("provider") or (r or {}).get("provider")
    if r is None and not out["errors"]:
        out["errors"].append("報告未產生")
    if r is not None and member != "chairman":
        step("記憶庫收錄", mem.snapshot)
    out["elapsed_sec"] = round(time.perf_counter() - started, 2)
    return out


TASKS = {"daily_home_update": daily_home_update, "war_room_sync": war_room_sync, "ask_war_room": ask_war_room,
         "deep_dive": deep_dive, "update_single_agent": update_single_agent, "sim_trade_session": sim_trade_session}


def main():
    parser = argparse.ArgumentParser(description="零 Claude Token 全員更新引擎")
    parser.add_argument("--task", required=True, choices=list(TASKS))
    parser.add_argument("--provider", default="gemini", choices=["auto", *llm_router.PROVIDERS])
    parser.add_argument("--question", help="ask_war_room 的問題")
    parser.add_argument("--stock", help="ask_war_room／deep_dive／update_single_agent 的個股名稱或代號")
    parser.add_argument("--agent", choices=[*AGENT_ALIASES, *MEMBER_NAMES],
                        help="update_single_agent 的成員：news_master／chip_king／kline_tech／chairman_buffett／trump_maga")
    parser.add_argument("--scope", choices=["market", "holdings", "stock"], help="ask_war_room 範圍（預設依 --stock 判斷）")
    parser.add_argument("--no-fetch", action="store_true", help="daily_home_update／war_room_sync 不重抓資料")
    parser.add_argument("--mode", default="intraday_hunt", choices=["intraday_hunt", "daily_rebalance"],
                        help="sim_trade_session：intraday_hunt 盤中交易／daily_rebalance 每日再平衡")
    args = parser.parse_args()
    try:
        if args.task == "daily_home_update":
            out = daily_home_update(args.provider, fetch=not args.no_fetch)
        elif args.task == "war_room_sync":
            out = war_room_sync(args.provider, refresh=not args.no_fetch)
        elif args.task == "ask_war_room":
            if not args.question:
                parser.error("ask_war_room 需要 --question")
            out = ask_war_room(args.question, args.stock, args.scope, args.provider)
        elif args.task == "sim_trade_session":
            out = sim_trade_session(args.mode, args.provider)
        elif args.task == "update_single_agent":
            if not args.agent:
                parser.error("update_single_agent 需要 --agent")
            out = update_single_agent(args.agent, args.stock, args.provider)
        else:
            if not args.stock:
                parser.error("deep_dive 需要 --stock")
            out = deep_dive(args.stock, args.provider)
    except (llm_router.LLMError, LookupError) as exc:
        print(f"❌ {exc}")
        sys.exit(1)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
