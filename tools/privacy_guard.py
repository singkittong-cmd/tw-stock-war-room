# -*- coding: utf-8 -*-
"""🔒 推送前的真實持股防線（本機才有 reports/portfolio.json，雲端主機上這裡全部是 no-op）。

1. `split_premarket_alert()`：盤前總結的 `holdings_alert`（持股開盤叮嚀，可能寫股數）
   搬到不進 git 的 `reports/us_market/tw_premarket_holdings_alert.json`，同步用的總結檔只留空陣列。
2. `find_leaks(rel_paths)`：真實持股代號附近 250 字內出現真實股數（全部／一半／三分之一）、
   整數張數或成本價時，回報該檔，推送時自動排除。
   `reports/simulation/` 不掃描：模擬引擎不讀真實持股，掃描只會誤擋成交帳本。

用法：
    C:/Users/singk/anaconda3/python.exe tools/privacy_guard.py            # 掃描 git 追蹤中與已修改的檔案，只印檔名
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO_PATH = ROOT / "reports" / "portfolio.json"
PREMARKET_PATH = ROOT / "reports" / "us_market" / "tw_premarket_summary.json"
PRIVATE_ALERT_PATH = ROOT / "reports" / "us_market" / "tw_premarket_holdings_alert.json"
SKIP_PREFIXES = ("reports/simulation/",)
TEXT_SUFFIXES = (".json", ".jsonl", ".md", ".txt", ".csv")
WINDOW = 250


def _read(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def split_premarket_alert():
    """把總結檔裡的持股叮嚀搬到私人檔；回傳搬移筆數。"""
    pre = _read(PREMARKET_PATH)
    if not isinstance(pre, dict) or not pre.get("holdings_alert"):
        return 0
    alerts = pre["holdings_alert"]
    _write(PRIVATE_ALERT_PATH, {"date": pre.get("date"), "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "holdings_alert": alerts})
    pre["holdings_alert"] = []
    _write(PREMARKET_PATH, pre)
    return len(alerts)


def load_premarket_alert(pre):
    """dashboard 用：優先讀總結檔本身，否則讀同一交易日的私人檔。"""
    if (pre or {}).get("holdings_alert"):
        return pre["holdings_alert"]
    priv = _read(PRIVATE_ALERT_PATH) or {}
    return (priv.get("holdings_alert") or []) if priv.get("date") == (pre or {}).get("date") else []


def materialize_portfolio_from_secret(raw=None):
    """Streamlit Cloud：沒有 reports/portfolio.json 時，用 Secrets 的 PORTFOLIO_JSON 建立（.gitignore 已排除，不會推送）。
    已存在時不覆蓋（金庫解鎖後在雲端編輯的內容保留到伺服器重開）。回傳持股檔數，沒有建立回傳 0。"""
    raw = str(raw or os.environ.get("PORTFOLIO_JSON") or "").strip()
    if not raw or PORTFOLIO_PATH.exists():
        return 0
    try:
        rows = json.loads(raw)
    except ValueError:
        return 0
    rows = rows if isinstance(rows, list) else (rows or {}).get("holdings") or []
    if not rows:
        return 0
    PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write(PORTFOLIO_PATH, rows)
    return len(rows)


def _patterns():
    rows = _read(PORTFOLIO_PATH)
    rows = rows if isinstance(rows, list) else (rows or {}).get("holdings") or []
    out = []
    for r in rows:
        sid = str(r.get("stock_id") or "").strip()
        if not sid:
            continue
        try:
            sh, cp = int(float(r.get("shares") or 0)), float(r.get("cost_price") or 0)
        except (TypeError, ValueError):
            continue
        nums = {sh, sh // 2, sh - sh // 2, sh // 3} - {0}
        pats = [rf"(?<![\d.,])(?:{n:,}|{n})\s*股" for n in nums]
        pats += [rf'"shares"\s*:\s*{sh}\b'] if sh else []
        if sh >= 1000 and sh % 1000 == 0:
            pats.append(rf"(?<![\d.,]){sh // 1000}\s*張(?!\d)")
        if cp:
            pats += [rf"成本\D{{0,6}}(?:{cp:,g}|{cp:g})(?![\d])", rf'"(?:cost|cost_price|avg_cost)"\s*:\s*{cp:g}\b']
        out.append((re.compile(rf"(?<!\d){re.escape(sid)}(?!\d)"), [re.compile(p) for p in pats]))
    return out


def redact_holdings_text(text):
    """來賓畫面用：真實持股代號附近 250 字內出現的真實股數（全部／一半／三分之一＋「股」）、整數張數與成本價換成 ●。
    只看代號附近，避免模擬基金剛好相同的股數被誤遮；也不遮代號或名稱本身，避免「被遮的就是持股」反而洩露持倉。
    沒有 portfolio.json 時原樣回傳。"""
    text = str(text or "")
    spans = []
    for sid_re, pats in _patterns():
        sids = [m.start() for m in sid_re.finditer(text)]
        if not sids:
            continue
        for pat in pats:
            spans += [(m.start(), m.end()) for m in pat.finditer(text)
                      if any(abs(m.start() - i) <= WINDOW for i in sids)]  # 只遮真實持股代號附近的數字，不誤遮巧合
    for a, b in sorted(set(spans), reverse=True):
        text = text[:a] + "●" + text[b:]
    return text


def find_leaks(rel_paths):
    """回傳含真實持股股數／成本的檔案（相對路徑）。沒有 portfolio.json（雲端）時一律回傳空清單。"""
    pats = _patterns()
    if not pats:
        return []
    leaks = []
    for rel in rel_paths:
        if rel.startswith(SKIP_PREFIXES) or not rel.endswith(TEXT_SUFFIXES):
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except Exception:
            continue
        if any(any(p.search(text[max(0, m.start() - WINDOW):m.end() + WINDOW]) for p in ps)
               for sid_re, ps in pats for m in sid_re.finditer(text)):
            leaks.append(rel)
    return leaks


def main():
    moved = split_premarket_alert()
    out = subprocess.run(["git", "ls-files", "-m", "-o", "-c", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8").stdout.split()
    leaks = find_leaks(sorted(set(out)))
    print(f"盤前持股叮嚀搬到私人檔：{moved} 筆")
    print("含真實持股的 git 檔案：", leaks or "無")
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
