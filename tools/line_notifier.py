# -*- coding: utf-8 -*-
"""LINE Messaging API Push 推播模組。

用法：
    python tools/line_notifier.py --test
    python tools/line_notifier.py --message "自訂訊息"
    python tools/line_notifier.py --file reports/stocks/2330_role4_chief.md
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    # 改用 Windows 憑證庫驗證 HTTPS（防毒軟體 / 代理攔截 TLS 時 certifi 會驗證失敗）
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", encoding="utf-8")

PUSH_URL = "https://api.line.me/v2/bot/message/push"
CHUNK_SIZE = 4900
MAX_MESSAGES = 5


def _is_placeholder(value):
    return not value or value.upper().startswith("YOUR_")


def is_line_configured():
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    to = os.getenv("LINE_TARGET_USER_ID", "").strip()
    return not _is_placeholder(token) and not _is_placeholder(to)


def _chunks(text, size=CHUNK_SIZE):
    parts = []
    while text:
        parts.append(text[:size])
        text = text[size:]
    return parts[:MAX_MESSAGES] or [""]


def push_text(text, to=None, token=None):
    """推播文字訊息，超過 5000 字自動切段（最多 5 則）。成功回傳 True。"""
    token = token or os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    to = to or os.getenv("LINE_TARGET_USER_ID", "").strip()
    if _is_placeholder(token) or _is_placeholder(to):
        print("[LINE] 未設定 LINE_CHANNEL_ACCESS_TOKEN / LINE_TARGET_USER_ID，略過推播。訊息內容如下：")
        print(text)
        return False
    body = {"to": to, "messages": [{"type": "text", "text": c} for c in _chunks(text)]}
    try:
        resp = requests.post(
            PUSH_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"[LINE] 推播連線失敗：{exc}")
        return False
    if resp.status_code == 200:
        return True
    print(f"[LINE] 推播失敗 HTTP {resp.status_code}：{resp.text}")
    return False


def main():
    parser = argparse.ArgumentParser(description="LINE Messaging API Push 推播")
    parser.add_argument("--test", action="store_true", help="發送測試訊息")
    parser.add_argument("--message", help="要推播的文字")
    parser.add_argument("--file", help="推播指定 UTF-8 文字檔內容")
    args = parser.parse_args()

    if args.test:
        text = f"✅ 台股 SEAL OP BASE 戰情室 LINE 推播測試成功\n時間：{datetime.now():%Y-%m-%d %H:%M:%S}"
    elif args.message:
        text = args.message
    elif args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            text = f.read()
    else:
        parser.print_help()
        sys.exit(1)

    ok = push_text(text)
    print("[LINE] 推播成功" if ok else "[LINE] 推播未送出")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
