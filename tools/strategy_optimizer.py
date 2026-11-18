# -*- coding: utf-8 -*-
"""🧠 管理者專屬：高階推理模型・每日實盤檢討 × 操盤規則自動修正引擎（只檢討 100 萬模擬基金，與真實持股無關）。

流程（run_review）：
1. 打包證據 build_evidence()：
   - reports/simulation/sim_account.json（淨值、現金、持倉）、sim_trades.json（每筆買賣、理由、已實現損益）、sim_nav_history.json（每日淨值）
   - reports/learning_db/agent_predictions_ledger.json（五位成員預測 vs 實際開高低收的驗算結果；排除真實持股會診來源）
   - reports/learning_db/market_snapshots_log.jsonl（模擬交易過的個股與大盤近 5 個交易日收盤快照；排除持股快照）
   - 0050 同期報酬、目前生效的 strategy_rules.json
   - 全文最後經 privacy_guard.redact_holdings_text 過濾（真實股數／成本一律遮掉）
2. llm_router.call_high_tier_reasoning()：五位成員坦承失準、巴董事長逐一 Challenge 並診斷績效不佳的根因。
3. updated_rules 經 sim_portfolio_engine.clamp_rules() 夾到安全範圍後寫入 reports/simulation/strategy_rules.json；
   診斷書寫入 reports/simulation/strategy_review_latest.json；兩檔透過 github_memory_sync 推回 GitHub。
   之後的投決會（fast_team_runner.sim_trade_session）、背景引擎（can_buy）與交割（execute_trade）全部讀這份規則。

用法：
    C:/Users/singk/anaconda3/python.exe tools/strategy_optimizer.py --evidence   # 只印證據摘要與字數，不呼叫模型
    C:/Users/singk/anaconda3/python.exe tools/strategy_optimizer.py              # 執行覆盤並套用規則、推回 GitHub
    C:/Users/singk/anaconda3/python.exe tools/strategy_optimizer.py --no-push    # 執行覆盤並套用規則，不推送
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import llm_router  # noqa: E402
from tools import privacy_guard  # noqa: E402
from tools import sim_portfolio_engine as sim  # noqa: E402
from tools import ui_theme  # noqa: E402

REPORTS = ROOT / "reports"
LEDGER_PATH = REPORTS / "learning_db" / "agent_predictions_ledger.json"
SNAP_PATH = REPORTS / "learning_db" / "market_snapshots_log.jsonl"
REVIEW_PATH = sim.SIM_DIR / "strategy_review_latest.json"
PRIVATE_SOURCES = {"intraday_decision", "summary"}   # 真實持股會診與個股裁決（可能含持股部位）不送模型
MEMBER_NAMES = {"news": "新聞達人", "chips": "籌碼王", "kline": "K線技術人", "maga": "川大帝MAGA", "chairman": "巴董事長"}
RULE_LABELS = {
    "max_single_position_pct": ("單一持股上限", "% 總淨值"), "stop_loss_pct": ("硬性停損", "%"),
    "take_profit_pct": ("分批停利目標", "%"), "cash_reserve_pct": ("平常最低現金水位", "%"),
    "min_cash_reserve_pct": ("大盤偏弱最低現金水位", "%"), "avoid_chase_pct": ("禁止追高門檻（當日漲幅）", "%"),
}
MAX_EVIDENCE_CHARS = 32000
HISTORY_KEEP = 20

REVIEW_SYSTEM = (
    "你是台股戰情室的「高階推理・每日戰略深度檢討會」，用繁體中文。成員：新聞達人（消息面與基本面）、籌碼王（法人、融資、五檔、"
    "隔日沖）、K線技術人（趨勢、四線、轉折K、停損）、川大帝MAGA（美股費半與台股連動）、巴董事長（最高決策者，機構操盤手視角，"
    "Priced-in、預期差、不對稱風報比、部位控管）。只能根據提供的實盤證據，嚴禁編造數字；每個論點都要引用證據中的交易編號、日期、"
    "股價或準確率。這是 100 萬模擬基金的檢討，與任何人的真實持股無關。\n"
    "任務：\n"
    "1. 新聞達人、籌碼王、K線技術人、川大帝MAGA 各自坦承近期哪幾筆判斷失準、為什麼看錯（例：誤判隔日沖、追高開高走低、"
    "忽略費半轉弱、停損放太寬），以及明天要怎麼修正。\n"
    "2. 巴董事長用最嚴格的機構操盤手視角逐一 Challenge 每位成員的盲點，並直接診斷績效不好的根因：選股邏輯、進場價位、"
    "停損太鬆或太緊、部位配置、現金水位，哪一項是主因。\n"
    "3. 依診斷輸出可執行的規則修正 updated_rules（數值範圍：max_single_position_pct 5–40、stop_loss_pct 2–15、"
    "take_profit_pct 3–40、cash_reserve_pct 0–50、min_cash_reserve_pct 0–80 且不得低於 cash_reserve_pct、avoid_chase_pct 1–10、"
    "agent_weights 每位 0.2–2.0（依近期準確率調整，準確率高者調升）、custom_disciplines 最多 5 條、每條 ≤ 60 字、"
    "必須是盤中投決會可以直接檢查的具體規則）。沒有充分證據的參數維持原值，不要為改而改。\n"
    "用語直白：建議買進價、停損價、停利目標價、加碼、減碼、可用現金；不使用比喻詞，嚴禁出現「擊球區、獵殺、巡弋、接刀、子彈」。\n"
    "只輸出一個 JSON 物件：{\"period\":\"檢討期間\",\"performance_summary\":\"一句話績效（含與 0050 比較）\","
    "\"members\":{\"news\":\"\",\"chips\":\"\",\"kline\":\"\",\"maga\":\"\"},"
    "\"chairman_challenge\":[{\"member\":\"news|chips|kline|maga\",\"challenge\":\"點名盲點與要求\"}],"
    "\"diagnosis\":{\"stock_selection\":\"\",\"entry_timing\":\"\",\"stop_loss\":\"\",\"position_sizing\":\"\",\"root_cause\":\"最主要原因\"},"
    "\"chairman_summary\":\"總結診斷與明日作戰原則\","
    "\"updated_rules\":{\"max_single_position_pct\":數字,\"stop_loss_pct\":數字,\"take_profit_pct\":數字,\"cash_reserve_pct\":數字,"
    "\"min_cash_reserve_pct\":數字,\"avoid_chase_pct\":數字,\"agent_weights\":{\"news\":數字,\"chips\":數字,\"kline\":數字,\"maga\":數字},"
    "\"custom_disciplines\":[\"\"]},\"rule_rationale\":{\"參數名稱\":\"為什麼這樣改（引用證據）\"}}"
)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dict(x):
    return x if isinstance(x, dict) else {}


def _list(x):
    return x if isinstance(x, list) else []


def clean(text):
    """存檔與顯示前：遮罩真實持股股數／成本，並把比喻詞（擊球區、獵殺、巡弋、接刀、子彈…）換成直白交易用語。"""
    return ui_theme.plain_terms(privacy_guard.redact_holdings_text(str(text or "")))


def _fmt(v, f="{:,.2f}"):
    n = _num(v)
    return f.format(n) if n is not None else "N/A"


# ---------------------------------------------------------------- 1) 證據打包

def build_evidence(max_chars=MAX_EVIDENCE_CHARS):
    """回傳 (證據全文, 統計)。全文已遮罩真實持股股數與成本。"""
    acct = _dict(sim.load_account())
    trades = [t for t in _list(_read(sim.TRADES_PATH, [])) if isinstance(t, dict)]
    nav = [n for n in _list(_read(sim.NAV_PATH, [])) if isinstance(n, dict)]
    rules = sim.load_strategy_rules()
    parts = [f"【檢討時間】{_now()}｜模擬基金起始 {acct.get('start_date')}｜目標 {acct.get('target_date')} 達 +10%～+20%"]

    holdings = "；".join(f"{h.get('stock_id')}{h.get('name', '')} {h.get('shares')}股 均價{_fmt(h.get('avg_cost'))} 現價{_fmt(h.get('current_price'))}"
                        f" 未實現{_fmt(h.get('unrealized_pct'), '{:+.2f}')}% 停損{_fmt(h.get('stop_loss'))} 目標{_fmt(h.get('target_price'))}"
                        for h in _list(acct.get("holdings")) if isinstance(h, dict)) or "空手"
    parts.append(f"【帳戶】總淨值 {_fmt(acct.get('total_equity'), '{:,.0f}')}（{_fmt(acct.get('total_return_pct'), '{:+.2f}')}%）"
                 f"｜現金 {_fmt(acct.get('cash_balance'), '{:,.0f}')}｜已實現 {_fmt(acct.get('realized_pnl'), '{:+,.0f}')}"
                 f"｜未實現 {_fmt(acct.get('unrealized_pnl'), '{:+,.0f}')}｜持倉：{holdings}")
    try:
        from tools import universe_pool
        b = universe_pool.benchmark_0050(acct.get("start_date") or _now()[:10])
        if b.get("return_pct") is not None:
            alpha = (_num(acct.get("total_return_pct")) or 0) - b["return_pct"]
            parts.append(f"【對標 0050】同期 {b['return_pct']:+.2f}%（{b['base_date']} {b['base_close']} → {b['last_date']} {b['last_close']}），"
                         f"模擬基金超額報酬 Alpha {alpha:+.2f}%")
    except Exception:
        pass
    parts.append("【目前生效的操盤規則】\n" + sim.rules_prompt(acct, rules))

    parts.append(f"【每日淨值】（{len(nav)} 天）" + "；".join(
        f"{n.get('date')} {_fmt(n.get('total_equity'), '{:,.0f}')}({_fmt(n.get('return_pct'), '{:+.2f}')}%) 現金{_fmt(n.get('cash'), '{:,.0f}')}"
        for n in nav[-20:]))

    trade_lines = []
    for t in trades[-40:]:
        trade_lines.append(f"#{t.get('trade_id')} {str(t.get('timestamp'))[:16]} {t.get('stock_id')}{t.get('name', '')} {t.get('action')} "
                           f"{t.get('shares')}股@{_fmt(t.get('price'))} 已實現{_fmt(t.get('realized_pnl'), '{:+,.0f}')} "
                           f"停損{_fmt(t.get('stop_loss'))} 目標{_fmt(t.get('target_price'))}｜拍板：{str(t.get('buffett_verdict') or '')[:90]}"
                           f"｜共識：{str(t.get('team_discussion') or '')[:70]}")
    parts.append(f"【全部模擬交易】（{len(trades)} 筆）\n" + ("\n".join(trade_lines) or "尚無交易"))

    ledger = _dict(_read(LEDGER_PATH, {}))
    stats = _dict(ledger.get("member_stats"))
    parts.append("【成員實盤準確率】" + "；".join(
        f"{MEMBER_NAMES.get(m, m)} {_fmt(_dict(s).get('accuracy_pct'), '{:.0f}')}%（{_dict(s).get('total', 0)} 次，近 10 次 "
        f"{_fmt(_dict(s).get('recent10_pct'), '{:.0f}')}%，CORRECT {_dict(s).get('correct', 0)}／DEVIATED {_dict(s).get('deviated', 0)}"
        f"／WRONG {_dict(s).get('wrong', 0)}）" for m, s in stats.items()))
    preds = [p for p in _list(ledger.get("predictions")) if isinstance(p, dict) and p.get("source") not in PRIVATE_SOURCES
             and _dict(p.get("grade")).get("result")]
    misses = [p for p in preds if p["grade"]["result"] in ("WRONG", "DEVIATED")][-45:]
    sim_preds = [p for p in preds if p.get("source") == "sim_committee"][-25:]
    seen, pred_lines = set(), []
    for p in sim_preds + misses:
        if p.get("id") in seen:
            continue
        seen.add(p.get("id"))
        g = p["grade"]
        pred_lines.append(f"{str(p.get('timestamp'))[:16]} {MEMBER_NAMES.get(p.get('member'), p.get('member'))}［{p.get('source')}］"
                          f"{p.get('stock_id')}{p.get('name') or ''} @{_fmt(p.get('price_at_prediction'))} {p.get('stance')}／{p.get('action')}"
                          f" 停損{_fmt(p.get('stop_loss'))} 目標{_fmt(p.get('target_price'))} → {g.get('result')} "
                          f"{_fmt(g.get('return_pct'), '{:+.2f}')}%｜{'；'.join(str(x) for x in _list(g.get('reasons')))[:80]}"
                          f"｜當時理由：{str(p.get('rationale') or '')[:60]}")
    parts.append(f"【成員預測 vs 實盤驗算】（模擬投決會近 25 筆＋落差與看錯近 45 筆）\n" + ("\n".join(pred_lines) or "尚無已驗算預測"))

    traded = {str(t.get("stock_id")) for t in trades} | {str(h.get("stock_id")) for h in _list(acct.get("holdings")) if isinstance(h, dict)}
    rows = []
    if SNAP_PATH.exists():
        with open(SNAP_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                groups = s.get("groups") or []
                if s.get("session") != "CLOSE" or "holding" in groups:  # 持股快照不送模型
                    continue
                if s.get("stock_id") in traded or "market" in groups or s.get("stock_id") == "TAIEX":
                    rows.append(s)
    dates = sorted({r.get("trade_date") for r in rows if r.get("trade_date")})[-5:]
    snap_lines = [f"{r.get('trade_date')} {r.get('stock_id')}{r.get('name') or ''} 開{_fmt(r.get('open'))} 高{_fmt(r.get('high'))} "
                  f"低{_fmt(r.get('low'))} 收{_fmt(r.get('last'))}({_fmt(r.get('change_pct'), '{:+.2f}')}%) 量比{_fmt(r.get('volume_ratio'))}"
                  for r in rows if r.get("trade_date") in dates]
    parts.append(f"【近 5 個交易日收盤快照（模擬交易過的個股＋大盤）】\n" + ("\n".join(snap_lines[-80:]) or "尚無快照"))

    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（證據過長已截斷）"
    text = privacy_guard.redact_holdings_text(text)
    stats_out = {"trades": len(trades), "nav_days": len(nav), "predictions": len(pred_lines), "snapshots": len(snap_lines),
                 "chars": len(text)}
    return text, stats_out


# ---------------------------------------------------------------- 2) 規則套用

def _rules_doc():
    data = _read(sim.RULES_PATH, {})
    return data if isinstance(data, dict) else {}


def diff_rules(before, after, rationale=None):
    """修正前 vs 修正後對照表（只列出全部參數，changed 標記是否變動）。"""
    rationale = _dict(rationale)
    rows = []
    for key, (label, unit) in RULE_LABELS.items():
        rows.append({"key": key, "label": label, "unit": unit, "before": before.get(key), "after": after.get(key),
                     "changed": before.get(key) != after.get(key), "reason": clean(rationale.get(key))[:160]})
    for k in sim.AGENT_KEYS:
        b, a = _dict(before.get("agent_weights")).get(k), _dict(after.get("agent_weights")).get(k)
        rows.append({"key": f"agent_weights.{k}", "label": f"{MEMBER_NAMES[k]} 發言權重", "unit": "", "before": b, "after": a,
                     "changed": b != a, "reason": clean(_dict(rationale.get("agent_weights")).get(k) or rationale.get("agent_weights"))[:160]
                     if isinstance(rationale.get("agent_weights"), (dict, str)) else ""})
    rows.append({"key": "custom_disciplines", "label": "巴董事長自訂鐵律", "unit": "",
                 "before": "；".join(_list(before.get("custom_disciplines"))) or "（無）",
                 "after": "；".join(_list(after.get("custom_disciplines"))) or "（無）",
                 "changed": _list(before.get("custom_disciplines")) != _list(after.get("custom_disciplines")),
                 "reason": clean(rationale.get("custom_disciplines"))[:160]})
    return rows


def apply_rules(updated, engine_label, rationale=None):
    """夾到安全範圍後寫入 strategy_rules.json（保留最近 20 次變更紀錄），回傳 (修正前, 修正後, 對照表)。"""
    doc = _rules_doc()
    before = sim.clamp_rules(doc.get("rules") if isinstance(doc.get("rules"), dict) else doc)
    merged = {**before, **{k: v for k, v in _dict(updated).items() if k in sim.DEFAULT_RULES}}
    after = sim.clamp_rules(merged)
    after["custom_disciplines"] = [clean(d) for d in after["custom_disciplines"]]
    rows = diff_rules(before, after, rationale)
    version = int(doc.get("version") or 0) + 1
    history = _list(doc.get("history"))[-(HISTORY_KEEP - 1):]
    history.append({"version": version, "at": _now(), "by": engine_label,
                    "changes": [{k: r[k] for k in ("key", "before", "after")} for r in rows if r["changed"]]})
    sim._write(sim.RULES_PATH, {"version": version, "updated_at": _now(), "updated_by": engine_label,
                                "rules": after, "history": history})
    return before, after, rows


# ---------------------------------------------------------------- 3) 檢討會

def _as_dict(obj):
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list):
        ds = [x for x in obj if isinstance(x, dict)]
        if ds:
            out = {}
            for d in ds:
                for k, v in d.items():
                    out.setdefault(k, v)
            return out
    raise llm_router.LLMError(f"高階模型回覆的 JSON 是 {type(obj).__name__}，不是物件")


def run_review(publish=True):
    """執行一次高階模型深度覆盤並套用規則；回傳診斷書 dict（同時寫入 strategy_review_latest.json）。"""
    started = datetime.now()
    evidence, stats = build_evidence()
    r = llm_router.call_high_tier_reasoning(f"以下是 100 萬模擬基金的完整實盤證據：\n\n{evidence}", REVIEW_SYSTEM,
                                            json_mode=True, max_tokens=24000)
    data = _as_dict(llm_router.parse_json(r["text"]))
    engine_label = f"{r['provider']}/{r['model']}"
    before, after, rows = apply_rules(data.get("updated_rules"), engine_label, data.get("rule_rationale"))
    members = _dict(data.get("members"))
    review = {
        "generated_at": _now(), "engine": r["provider"], "model": r["model"], "elapsed_sec": r["elapsed_sec"],
        "engine_errors": [str(e)[:160] for e in _list(r.get("errors"))], "evidence": stats,
        "period": clean(str(data.get("period") or "")), "performance_summary": clean(str(data.get("performance_summary") or "")),
        "members": {k: clean(str(members.get(k) or "")) for k in sim.AGENT_KEYS},
        "chairman_challenge": [{"member": str(_dict(c).get("member") or ""), "challenge": clean(str(_dict(c).get("challenge") or ""))}
                               for c in _list(data.get("chairman_challenge"))][:8],
        "diagnosis": {k: clean(str(_dict(data.get("diagnosis")).get(k) or ""))
                      for k in ("stock_selection", "entry_timing", "stop_loss", "position_sizing", "root_cause")},
        "chairman_summary": clean(str(data.get("chairman_summary") or "")),
        "rules_before": before, "rules_after": after, "rule_changes": rows,
        "rules_version": _dict(_rules_doc()).get("version"),
    }
    sim._write(REVIEW_PATH, review)
    review["publish"] = None
    if publish:
        try:
            from tools import github_memory_sync as ghsync
            if ghsync.can_sync():
                changed = sum(1 for x in rows if x["changed"])
                review["publish"] = ghsync.publish(f"🧠 strategy: 高階覆盤 v{review['rules_version']}（{engine_label}，修正 {changed} 項）",
                                                   ghsync.STRATEGY_PATHS)
            else:
                review["publish"] = {"pushed": False, "reason": "這台主機沒有 GitHub 同步設定"}
        except Exception as exc:
            review["publish"] = {"pushed": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
    review["total_sec"] = round((datetime.now() - started).total_seconds(), 1)
    return review


def load_review():
    return _dict(_read(REVIEW_PATH, {}))


def main():
    ap = argparse.ArgumentParser(description="管理者高階推理：每日實盤檢討 × 操盤規則自動修正")
    ap.add_argument("--evidence", action="store_true", help="只印證據摘要，不呼叫模型")
    ap.add_argument("--no-push", action="store_true", help="套用規則但不推回 GitHub")
    args = ap.parse_args()
    if args.evidence:
        text, stats = build_evidence()
        print(stats)
        print(text[:3000])
        return
    print(f"🧠 高階推理引擎：{llm_router.high_tier_provider()}／{llm_router.high_tier_model() or '預設'}")
    rv = run_review(publish=not args.no_push)
    print(f"✅ 完成（{rv['engine']}／{rv['model']}，{rv['total_sec']} 秒）｜{rv['performance_summary']}")
    print(f"根因：{rv['diagnosis']['root_cause']}")
    for row in rv["rule_changes"]:
        if row["changed"]:
            print(f"  ⚙️ {row['label']}：{row['before']} → {row['after']}｜{row['reason'][:60]}")
    print(f"推送：{rv['publish']}")


if __name__ == "__main__":
    main()
