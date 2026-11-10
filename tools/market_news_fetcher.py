# -*- coding: utf-8 -*-
"""全市場財經新聞爬蟲（免金鑰）：鉅亨網 Anue API + Google News 台灣財經 RSS。

用法：
    C:/Users/singk/anaconda3/python.exe tools/market_news_fetcher.py
    C:/Users/singk/anaconda3/python.exe tools/market_news_fetcher.py --limit 45 --max-age-hours 48

輸出 reports/news/daily_market_news_raw.json：去重後依發布時間由新到舊的前 N 則新聞
（預設 45 則、只收 48 小時內；週末與連假新聞較少時會自然少於 45 則）。
每則必帶 `url`（真實原文網址；鉅亨網 https://news.cnyes.com/news/id/{newsId}，Google News 為其原文轉址連結）、
`source`（媒體名稱）與 `pub_date`（YYYY-MM-DD HH:MM，台北時間）；`link`／`published_at` 為舊欄位名稱，內容相同。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import argparse
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

try:
    # 改用 Windows 憑證庫驗證 HTTPS（防毒軟體 / 代理攔截 TLS 時 certifi 會驗證失敗）
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

NEWS_DIR = ROOT / "reports" / "news"
RAW_PATH = NEWS_DIR / "daily_market_news_raw.json"
TPE = timezone(timedelta(hours=8))
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 8

CNYES_SOURCES = [
    ("headline", 15, "重大頭條"),
    ("tw_stock", 20, "台股盤面與產業"),
    ("wd_stock", 10, "國際與美股"),
]
GOOGLE_SOURCES = [
    ("https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=zh-TW&gl=TW&ceid=TW:zh-Hant", "Google 財經頭條"),
    ("https://news.google.com/rss/search?q=台股+OR+法說會+OR+半導體+OR+外資+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
     "Google 台股／法說／半導體／外資"),
]


def _strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").replace("&nbsp;", " ").strip()


def fetch_cnyes(category, limit, label):
    url = f"https://api.cnyes.com/media/api/v1/newslist/category/{category}"
    resp = requests.get(url, params={"limit": limit}, headers=HEADERS, timeout=TIMEOUT)
    items = ((resp.json().get("items") or {}).get("data")) or []
    out = []
    for it in items:
        ts = it.get("publishAt")
        if not ts or not it.get("title"):
            continue
        if not it.get("newsId"):
            continue
        pub = datetime.fromtimestamp(int(ts), TPE).strftime("%Y-%m-%d %H:%M")
        url = f"https://news.cnyes.com/news/id/{it['newsId']}"
        out.append({
            "title": it["title"].strip(),
            "summary": _strip_html(it.get("summary"))[:200],
            "published_at": pub,
            "pub_date": pub,
            "epoch": int(ts),
            "source": "鉅亨網",
            "category": label,
            "stocks": [s.get("code") for s in (it.get("stock") or []) if isinstance(s, dict) and s.get("code")]
                      if isinstance(it.get("stock"), list) else [],
            "keywords": it.get("keyword") or [],
            "url": url,
            "link": url,
        })
    return out


def fetch_google(url, label):
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    root = ET.fromstring(resp.content)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        # Google News 標題格式為「標題 - 媒體」，拆掉尾端媒體名
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        try:
            dt = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(TPE)
        except (TypeError, ValueError):
            continue
        link = (item.findtext("link") or "").strip()
        if not link.startswith(("https://", "http://")):
            continue  # 沒有可驗證的原文網址就不收
        out.append({
            "title": title,
            "summary": "",
            "published_at": dt.strftime("%Y-%m-%d %H:%M"),
            "pub_date": dt.strftime("%Y-%m-%d %H:%M"),
            "epoch": int(dt.timestamp()),
            "source": source or "Google News",
            "category": label,
            "stocks": [],
            "keywords": [],
            "url": link,
            "link": link,
        })
    return out


def _norm(title):
    """去重鍵：去掉空白與標點，只留前 30 字（不同媒體同一事件的標題常只差標點與後綴）。"""
    return re.sub(r"[\s\W_]+", "", title)[:30]


def run(limit=45, max_age_hours=48):
    started = time.perf_counter()
    jobs = [("cnyes", c) for c in CNYES_SOURCES] + [("google", g) for g in GOOGLE_SOURCES]

    def one(job):
        kind, args = job
        label = args[-1]
        try:
            return label, (fetch_cnyes(*args) if kind == "cnyes" else fetch_google(*args)), None
        except (requests.RequestException, ValueError, ET.ParseError) as exc:
            return label, [], f"{label}：{exc}"

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(one, jobs))

    cutoff = time.time() - max_age_hours * 3600
    seen, merged = set(), []
    # 鉅亨網排前面：同一事件優先保留有摘要的版本
    for _, items, _ in results:
        for n in items:
            key = _norm(n["title"])
            if not key or key in seen or n["epoch"] < cutoff:
                continue
            seen.add(key)
            merged.append(n)
    merged.sort(key=lambda n: n["epoch"], reverse=True)
    news = merged[:limit]
    out = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.perf_counter() - started, 2),
        "source_counts": {label: len(items) for label, items, _ in results},
        "errors": [e for _, _, e in results if e],
        "count": len(news),
        "news": news,
    }
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RAW_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RAW_PATH)
    return out


def main():
    parser = argparse.ArgumentParser(description="全市場財經新聞爬蟲（鉅亨網 + Google News RSS）")
    parser.add_argument("--limit", type=int, default=45, help="保留則數（預設 45）")
    parser.add_argument("--max-age-hours", type=int, default=48, help="只收幾小時內的新聞（預設 48）")
    args = parser.parse_args()
    out = run(args.limit, args.max_age_hours)
    print(f"財經新聞 {out['updated_at']}：{out['count']} 則（耗時 {out['elapsed_sec']} 秒）；來源 {out['source_counts']}")
    if out["errors"]:
        print("來源錯誤：" + "；".join(out["errors"]))
    for n in out["news"][:10]:
        print(f"  {n['published_at']}｜{n['source']}｜{n['title']}")


if __name__ == "__main__":
    main()
