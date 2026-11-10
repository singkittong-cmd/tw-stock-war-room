# -*- coding: utf-8 -*-
"""台灣時間工具：雲端主機（GitHub Actions、Streamlit Cloud）預設是 UTC，專案裡大量使用 datetime.now()，
所以在程式最開頭呼叫 force_taipei_tz()，讓整個行程的本地時間變成 Asia/Taipei。"""
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

TW_TZ = ZoneInfo("Asia/Taipei")


def force_taipei_tz():
    """Linux/macOS：設定 TZ 並 tzset()；Windows 本機本來就是台灣時間，不需要處理。"""
    if os.environ.get("TZ") != "Asia/Taipei" and hasattr(time, "tzset"):
        os.environ["TZ"] = "Asia/Taipei"
        time.tzset()


def now_tw():
    """不含時區資訊的台灣時間（與專案其他模組的 datetime.now() 格式一致）。"""
    return datetime.now(TW_TZ).replace(tzinfo=None)
