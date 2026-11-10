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
