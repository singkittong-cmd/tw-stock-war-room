# -*- coding: utf-8 -*-
"""部位隱私遮罩：把報告與卡片文字中的股數、成本、損益兩平、帳面損益等部位數字替換為遮罩。

dashboard.py 與 tools/ui_theme.py 共用；解鎖狀態由呼叫端傳入（PRIVACY_PIN 驗證在 dashboard.py）。
"""
import re

MASK = "🔒 ****"
MASK_TEXT = "[🔒部位資訊已隱藏]"

# 報告內文打碼：關鍵字後接數字（可帶正負號、%、元、股、張與括號補充金額）整段遮蔽
_NUM = r"[+\-−]?\s*\d[\d,]*(?:\.\d+)?\s*(?:%|％|元|股|張)?"
_SENSITIVE_KEY = (
    r"(?:持有|持股|庫存|(?<!營業)(?<!銷貨)(?<!營運)(?<!資金)(?<!交易)(?<!製造)(?<!生產)成本價?|均價"
    r"|損益兩平價?|兩平價|移動停利價?|帳面淨?(?:損益|獲利|虧損|報酬率?)|未實現淨?(?:損益|報酬率?)|淨(?:損益|獲利|虧損|報酬率?)"
    r"|總成本|目前市值|持倉市值|即時市值|部位市值"
    r"|net_pnl|net_return_pct|breakeven_price|cost_price|total_cost|market_value|shares)"
)
_SENSITIVE_RE = re.compile(
    _SENSITIVE_KEY + r"[^\d\n。；;｜|]{0,8}" + _NUM
    + r"(?:\s*[（(][^）)\n]{0,24}[）)])?"
    + r"(?:\s*[/／、~～–-]\s*" + _NUM + r")*"
)
# 任何「數字 + 股」都是部位股數（排除股價、股東、股本等詞）；「N 張整股／零股」同樣是部位
_SHARES_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:股(?![價東本利息票市數])|張(?=整股|零股))")
# 「持股（3264、5483、6257）」這類點名持股代號的括號清單
_HOLDING_LIST_RE = re.compile(r"持股[（(][^）)\n]{1,80}[）)]")
# Markdown 表格中表頭（或直式表格列首）含這些字的欄位整欄遮蔽
_SENSITIVE_HEADER_RE = re.compile(r"股數|持股|成本|兩平|市值|淨損益|帳面|未實現|部位損益|淨獲利|淨虧損")


def _mask_table_cells(lines):
    """Markdown 表格：表頭欄名含敏感字的欄位，以及列首為敏感欄名的整列數值，替換為遮罩。"""
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        is_header = (line.strip().startswith("|") and i + 1 < len(lines)
                     and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]))
        if not is_header:
            out.append(line)
            i += 1
            continue
        headers = [c.strip() for c in line.strip().strip("|").split("|")]
        hidden = {j for j, h in enumerate(headers) if _SENSITIVE_HEADER_RE.search(h)}
        out += [line, lines[i + 1]]
        i += 2
        while i < len(lines) and lines[i].strip().startswith("|"):
            cells = lines[i].strip().strip("|").split("|")
            row_hidden = bool(cells) and bool(_SENSITIVE_HEADER_RE.search(cells[0]))
            cells = [f" {MASK} " if j in hidden or (row_hidden and j > 0) else c for j, c in enumerate(cells)]
            out.append("|" + "|".join(cells) + "|")
            i += 1
    return out


_private_re = None


def set_private_terms(terms):
    """設定「只存在於個人持股、不在公開觀察池」的代號與名稱；鎖定時這些字一律以 🔒 取代。"""
    global _private_re
    parts = []
    for t in sorted({str(t).strip() for t in terms if t and str(t).strip()}, key=len, reverse=True):
        parts.append(rf"(?<!\d){re.escape(t)}(?!\d)" if t.isdigit() else re.escape(t))
    _private_re = re.compile("|".join(parts)) if parts else None


def mask_sensitive_text(text, unlocked):
    """未解鎖時遮蔽部位數字與持股代號；解鎖時原文返回。"""
    if not text or unlocked:
        return text
    masked = "\n".join(_mask_table_cells(str(text).split("\n")))
    masked = _SENSITIVE_RE.sub(MASK_TEXT, masked)
    masked = _HOLDING_LIST_RE.sub(f"持股（{MASK}）", masked)
    if _private_re is not None:
        masked = _private_re.sub("🔒", masked)
    return _SHARES_RE.sub(MASK_TEXT, masked)
