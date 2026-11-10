# -*- coding: utf-8 -*-
"""五位 3D 公仔分析員頭像裁切：從 3 張原始角色設定圖裁出正面／45 度側面立牌與迷你頭像。

用法：
    C:/Users/singk/anaconda3/python.exe tools/crop_character_avatars.py                # 原圖重裁＋戰術頭像
    C:/Users/singk/anaconda3/python.exe tools/crop_character_avatars.py --badges-only  # 只由 assets/avatars 立牌重做戰術頭像

搜尋順序：專案根目錄 → ./assets → C:/Users/singk/Downloads → C:/Users/singk/Desktop
    圖片 A（檔名含 4wunnx）：上排 新聞達人、下排 籌碼王
    圖片 B（檔名含 e9zofq）：上排 K線技術人、下排 川大帝MAGA
    圖片 C（檔名含 2dwrxc）：巴董事長
輸出 assets/avatars/：
- 10 張 360×450 立牌 PNG（等比縮放、四周以原圖背景色補邊，不變形）與 avatar_base64.json。
- 戰術通訊頭像：每位成員正面／45 度側面各一張 176×176 半身頭像（自動偵測頭部位置，含肩膀），
  與 96×96 迷你頭像，彙整為 avatar_badges.json（tools/ui_theme.py 只內嵌這份，約 0.3 MB）。
  UI 顯示尺寸：一般成員 68px、巴董事長 84px（176px 原圖確保高解析螢幕清晰）。
找不到原始設定圖時自動略過重裁，直接用 assets/avatars/ 既有立牌產生戰術頭像。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import base64
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "assets" / "avatars"
SEARCH_DIRS = [ROOT, ROOT / "assets", Path("C:/Users/singk/Downloads"), Path("C:/Users/singk/Desktop")]
SOURCES = {"A": "4wunnx", "B": "e9zofq", "C": "2dwrxc"}
SIZE = (360, 450)
HEAD_SIZE = 96
BADGE_SIZE = 176

# (來源圖, 輸出檔名, (left, top, right, bottom) 相對比例)
# 註：A 圖新聞達人下緣 0.49→0.465、籌碼王上緣 0.50→0.475；B 圖 K線技術人下緣 0.48→0.44、川大帝MAGA 上緣 0.48→0.445，
#     避免裁到上下排相鄰角色或切掉自己的頭髮／耳機。
CROPS = [
    ("A", "role1_mark_front", (0.02, 0.05, 0.24, 0.465)),
    ("A", "role1_mark_angle", (0.26, 0.05, 0.48, 0.465)),
    ("A", "role2_elon_front", (0.02, 0.475, 0.24, 0.94)),
    ("A", "role2_elon_angle", (0.26, 0.475, 0.48, 0.94)),
    ("B", "role3_jensen_front", (0.02, 0.05, 0.24, 0.44)),
    ("B", "role3_jensen_angle", (0.26, 0.05, 0.48, 0.44)),
    ("B", "role5_trump_front", (0.01, 0.445, 0.25, 0.94)),
    ("B", "role5_trump_angle", (0.26, 0.445, 0.49, 0.94)),
    ("C", "role4_buffett_front", (0.02, 0.06, 0.26, 0.88)),
    ("C", "role4_buffett_angle", (0.27, 0.06, 0.50, 0.88)),
]


def find_source(token):
    for d in SEARCH_DIRS:
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and token in p.name and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                return p
    return None


def border_color(img):
    """取裁切框四邊像素的中位數色，作為補邊背景色（與原圖棚拍背景一致）。"""
    w, h = img.size
    px = img.load()
    samples = [px[x, 0] for x in range(w)] + [px[x, h - 1] for x in range(w)] + \
              [px[0, y] for y in range(h)] + [px[w - 1, y] for y in range(h)]
    return tuple(sorted(c[i] for c in samples)[len(samples) // 2] for i in range(3))


def fit_contain(img, size):
    """等比縮放置中，四周以邊緣背景色補滿，不變形。"""
    canvas = Image.new("RGB", size, border_color(img))
    fitted = ImageOps.contain(img, size, Image.LANCZOS)
    canvas.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
    return canvas


def to_b64(img, fmt="PNG"):
    buf = io.BytesIO()
    if fmt == "WEBP":
        img.save(buf, format="WEBP", quality=86, method=6)
    else:
        img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def bust_box(img, side_ratio=0.43, threshold=48):
    """自動偵測公仔頭頂與頭部中心，回傳含頭與肩膀的正方形裁切框 (left, top, right, bottom)。

    做法：與邊緣背景色差異 > threshold 的像素視為前景；第一個前景占比 ≥ 3% 的列為頭頂；
    頭頂往下 18% 高度內前景像素的 x 中位數為頭部中心。"""
    arr = np.asarray(img).astype(int)
    h, w, _ = arr.shape
    bg = np.array(border_color(img))
    fg = np.abs(arr - bg).max(axis=2) > threshold
    rows = np.where(fg.mean(axis=1) >= 0.03)[0]
    top = int(rows[0]) if len(rows) else int(h * 0.02)
    head = fg[top: top + int(h * 0.18)]
    xs = np.where(head)[1]
    cx = int(np.median(xs)) if len(xs) else w // 2
    side = int(h * side_ratio)
    t = max(0, top - int(h * 0.025))
    return cx - side // 2, t, cx + side // 2, t + side


def crop_padded(img, box):
    """裁切框超出圖片範圍時以背景色補邊。"""
    l, t, r, b = box
    canvas = Image.new("RGB", (r - l, b - t), border_color(img))
    src = img.crop((max(0, l), max(0, t), min(img.width, r), min(img.height, b)))
    canvas.paste(src, (max(0, l) - l, max(0, t) - t))
    return canvas


def build_badges():
    """由 assets/avatars/*_front.png、*_angle.png 產生戰術通訊頭像與 avatar_badges.json。"""
    b64 = {}
    for _, name, _ in CROPS:
        path = OUT_DIR / f"{name}.png"
        if not path.exists():
            print(f"  ⚠️ 缺少 {path.name}，略過")
            continue
        img = Image.open(path).convert("RGB")
        box = bust_box(img)
        badge = crop_padded(img, box).resize((BADGE_SIZE, BADGE_SIZE), Image.LANCZOS)
        key, kind = name.rsplit("_", 1)
        badge.save(OUT_DIR / f"{key}_badge_{kind}.png", optimize=True)
        b64[f"{key}_badge_{kind}"] = to_b64(badge, "WEBP")  # 內嵌用 WebP（約 PNG 的 1/8 大小）
        if kind == "front":
            head = badge.resize((HEAD_SIZE, HEAD_SIZE), Image.LANCZOS)
            head.save(OUT_DIR / f"{key}_head.png", optimize=True)
            b64[f"{key}_head"] = to_b64(head, "WEBP")
        print(f"  ✅ {key}_badge_{kind}.png ← 偵測框 {box}")
    with open(OUT_DIR / "avatar_badges.json", "w", encoding="utf-8") as f:
        json.dump(b64, f)
    size_kb = (OUT_DIR / "avatar_badges.json").stat().st_size / 1024
    print(f"已輸出 {len(b64)} 張戰術頭像與 avatar_badges.json（{size_kb:,.0f} KB）")


def main():
    parser = argparse.ArgumentParser(description="五位成員 3D 公仔頭像裁切")
    parser.add_argument("--badges-only", action="store_true", help="只由既有立牌重做戰術頭像")
    args = parser.parse_args()
    sources = {}
    for key, token in SOURCES.items():
        path = None if args.badges_only else find_source(token)
        if path is None:
            print(("" if args.badges_only else f"找不到檔名含 {token} 的原始設定圖，") + "直接使用 assets/avatars/ 既有立牌。")
            build_badges()
            return
        sources[key] = Image.open(path).convert("RGB")
        print(f"圖片 {key}：{path}（{sources[key].width}×{sources[key].height}）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    images, b64 = {}, {}
    for src, name, (l, t, r, b) in CROPS:
        img = sources[src]
        w, h = img.size
        box = (round(l * w), round(t * h), round(r * w), round(b * h))
        out = fit_contain(img.crop(box), SIZE)
        out.save(OUT_DIR / f"{name}.png", optimize=True)
        images[name] = out
        b64[name] = to_b64(out)
        print(f"  ✅ {name}.png ← {src} {box}")
    with open(OUT_DIR / "avatar_base64.json", "w", encoding="utf-8") as f:
        json.dump(b64, f)
    size_kb = (OUT_DIR / "avatar_base64.json").stat().st_size / 1024
    print(f"已輸出 {len(CROPS)} 張立牌與 avatar_base64.json（{size_kb:,.0f} KB）")
    build_badges()


if __name__ == "__main__":
    main()
