# -*- coding: utf-8 -*-
"""台股戰情室深色主題：全域 CSS、台股配色 Token、五位成員戰術頭像與可重複使用的 HTML 視覺元件。

兩種視圖共用：
- 🏠 日常主畫面：個股快篩卡、新聞 TOP 10、美股 TOP 10、收盤大盤總結卡。
- 🎖️ WAR ROOM：主席位（巴董事長 84px 金框）＋四席成員（68px 八角戰術框）、覆盤狀態、四格戰術問答。

所有元件回傳 HTML 字串，由 dashboard.py 以 st.markdown(..., unsafe_allow_html=True) 渲染。
所有外部文字一律先經 html.escape；涉及部位的文字先經 privacy_mask.mask_sensitive_text。
台股慣例：紅＝上漲／多頭／進場，綠＝下跌／空頭／停損，琥珀＝攤平／觀望。
"""
import html
import json
import re
from pathlib import Path

from tools.privacy_mask import mask_sensitive_text

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&family=Noto+Sans+TC:wght@400;500;700&display=swap');

:root {
  --bg-card: #111827; --bg-card-2: #0F172A; --border: rgba(148, 163, 184, 0.16);
  --text: #E5E7EB; --muted: #94A3B8; --faint: #64748B; --gold: #F5C542;
  --bull-bg: rgba(239, 68, 68, 0.15); --bull-bd: #EF4444; --bull-tx: #F87171;
  --bear-bg: rgba(16, 185, 129, 0.15); --bear-bd: #10B981; --bear-tx: #34D399;
  --amber-bg: rgba(245, 158, 11, 0.15); --amber-bd: #F59E0B; --amber-tx: #FBBF24;
  --info-bg: rgba(59, 130, 246, 0.15); --info-bd: #3B82F6; --info-tx: #93C5FD;
  --neutral-bg: rgba(148, 163, 184, 0.12); --neutral-bd: #64748B; --neutral-tx: #CBD5E1;
}
html, body, [class*="css"], .stMarkdown, .stText, button, input, textarea {
  font-family: 'Inter', 'Noto Sans TC', -apple-system, 'Segoe UI', sans-serif;
}
#MainMenu, footer, [data-testid="stDecoration"], [data-testid="stStatusWidget"] { visibility: hidden; height: 0; }
header[data-testid="stHeader"] { background: transparent; height: 0; }
.block-container { padding-top: 1.1rem !important; padding-bottom: 2rem; max-width: 1560px; }
[data-testid="stMetricValue"], .num, .tabular, [data-testid="stDataFrame"] {
  font-variant-numeric: tabular-nums; font-family: 'JetBrains Mono', monospace;
}
[data-testid="stMetricValue"] { font-size: 1.4rem; }

/* ---------- 共用膠囊與徽章 ---------- */
.pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 0.76rem; font-weight: 600;
        border: 1px solid; white-space: nowrap; font-variant-numeric: tabular-nums; }
.bull { background: var(--bull-bg); border-color: var(--bull-bd); color: var(--bull-tx); }
.bear { background: var(--bear-bg); border-color: var(--bear-bd); color: var(--bear-tx); }
.amber { background: var(--amber-bg); border-color: var(--amber-bd); color: var(--amber-tx); }
.info { background: var(--info-bg); border-color: var(--info-bd); color: var(--info-tx); }
.neutral { background: var(--neutral-bg); border-color: var(--neutral-bd); color: var(--neutral-tx); }
.up { color: var(--bull-tx); } .down { color: var(--bear-tx); } .flat { color: var(--muted); }
.big-badge { font-size: 0.92rem; padding: 5px 13px; border-radius: 10px; }
.code-badge { display: inline-block; background: #1E293B; color: #93C5FD; border: 1px solid #334155;
              border-radius: 6px; padding: 1px 8px; font-family: 'JetBrains Mono', monospace; font-size: 0.85rem; }
.tl-time { color: var(--faint); font-size: 0.72rem; font-family: 'JetBrains Mono', monospace; }
.tl-src { color: var(--muted); font-size: 0.72rem; }
.upd { display: inline-block; color: #94A3B8; font-size: .74rem; font-family: 'JetBrains Mono', 'Noto Sans TC', monospace;
       background: rgba(148, 163, 184, .08); border: 1px solid rgba(148, 163, 184, .18); border-radius: 6px;
       padding: 1px 8px; margin: 0 0 6px; }
.upd.stale { color: #FBBF24; border-color: rgba(245, 158, 11, .35); }
.wr-title { font-size: 1.3rem; font-weight: 700; color: var(--text); letter-spacing: .01em; }
.vault-bar { color: #94A3B8; font-size: 0.88rem; padding: 4px 0; }

/* ---------- 戰術通訊頭像（一般 68px 八角框、主席 84px 金框；hover 轉 45 度側面） ---------- */
.tac-av { position: relative; flex-shrink: 0; display: inline-block; }
.tac-av .tac-in { position: absolute; inset: 3px; overflow: hidden; background: #E5E7EB; }
.tac-av.member { width: 68px; height: 68px; background: var(--c);
  clip-path: polygon(29% 0, 71% 0, 100% 29%, 100% 71%, 71% 100%, 29% 100%, 0 71%, 0 29%); }
.tac-av.member .tac-in { clip-path: polygon(29% 0, 71% 0, 100% 29%, 100% 71%, 71% 100%, 29% 100%, 0 71%, 0 29%); }
.tac-wrap { filter: drop-shadow(0 0 6px color-mix(in srgb, var(--c) 55%, transparent)); display: inline-block; }
.tac-av.chair { width: 84px; height: 84px; border-radius: 50%;
  background: conic-gradient(from 0deg, #FDE68A, #B45309, #F5C542, #92400E, #FDE68A);
  box-shadow: 0 0 0 2px #0B1120, 0 0 0 4px rgba(245, 197, 66, .55), 0 0 22px rgba(245, 197, 66, .45); }
.tac-av.chair .tac-in { inset: 4px; border-radius: 50%; }
.tac-face { position: absolute; inset: 0; background-size: cover; background-position: center top;
            transition: opacity .35s ease, transform .35s ease; }
.tac-face.angle { opacity: 0; transform: scale(1.04); }
.seat:hover .tac-face.front, .tac-av:hover .tac-face.front, .station:hover .tac-face.front { opacity: 0; transform: scale(1.04); }
.seat:hover .tac-face.angle, .tac-av:hover .tac-face.angle, .station:hover .tac-face.angle { opacity: 1; transform: none; }
.tac-emoji { position: absolute; inset: 0; display: grid; place-items: center; font-size: 1.8rem; background: #1E293B; }
.mini-av { display: inline-block; border-radius: 50%; border: 2px solid; background-size: cover; background-position: center top;
           vertical-align: middle; margin-right: 6px; flex-shrink: 0; background-color: #E5E7EB; }

/* ---------- 🏠 日常主畫面 ---------- */
.station { display: flex; align-items: center; gap: 12px; margin: 2px 0 8px; }
.station .nm { color: var(--text); font-weight: 700; font-size: 1.05rem; }
.station .tg { color: var(--c); font-size: 0.74rem; font-weight: 600; letter-spacing: .03em; }
.station .sub { color: var(--muted); font-size: 0.76rem; }
.top10 { list-style: none; margin: 0; padding: 0; counter-reset: rk; }
.top10 li { display: grid; grid-template-columns: 26px 1fr auto; gap: 8px; align-items: start; padding: 8px 4px;
            border-bottom: 1px dashed rgba(148, 163, 184, .14); }
.top10 li:before { counter-increment: rk; content: counter(rk); color: var(--faint); font-family: 'JetBrains Mono', monospace;
                   font-size: .82rem; padding-top: 1px; text-align: right; }
.top10 .t { color: #E2E8F0; font-size: .86rem; font-weight: 600; line-height: 1.35; }
.top10 .t a { color: inherit; text-decoration: none; } .top10 .t a:hover { color: #93C5FD; }
.top10 .p { color: var(--muted); font-size: .77rem; line-height: 1.4; margin-top: 2px; }
.top10 .pub-date { display: inline-block; color: #93C5FD; background: rgba(59, 130, 246, .12); border: 1px solid rgba(59, 130, 246, .35);
                   border-radius: 5px; padding: 0 6px; margin-right: 4px; font: 600 .7rem 'JetBrains Mono', monospace; }
.top10 .pub-date.none { color: var(--faint); border-color: var(--border); background: none; }
.top10 .src { margin-top: 3px; }
.top10 .meta { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 5px; }
.top10 .meta .pub-date { margin-right: 0; }
.top10 .src-pill { display: inline-flex; align-items: center; max-width: 100%; padding: 1px 9px; border-radius: 999px;
                   background: rgba(96, 165, 250, .10); border: 1px solid rgba(96, 165, 250, .35);
                   overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.top10 .src-pill.none { background: none; border-color: var(--border); }
.top10 .src-link { color: #60A5FA; font-size: .72rem; text-decoration: none; }
.top10 .src-link:hover { text-decoration: underline; color: #93C5FD; }
.top10 .src-link.none { color: var(--faint); }
.close-bullets { margin: 4px 0 0; padding-left: 0; list-style: none; }
.close-bullets li { padding: 9px 12px; margin-bottom: 8px; border-radius: 10px; background: rgba(2, 6, 23, .45);
                    border-left: 3px solid var(--c, #3B82F6); color: #E2E8F0; font-size: .88rem; line-height: 1.55; }
.close-bullets li b { color: var(--c, #93C5FD); margin-right: 6px; }
.close-stats { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
.lookup-card { background: linear-gradient(135deg, #111827 0%, #0B1222 100%); border: 1px solid rgba(59, 130, 246, .35);
               border-radius: 14px; padding: 14px 18px; margin: 6px 0 4px; }
.lookup-head { display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 10px; }
.lookup-price { font-family: 'JetBrains Mono', monospace; font-size: 1.9rem; font-weight: 700; color: var(--text); }
.kv-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; margin: 12px 0; }
.kv { background: var(--bg-card-2); border: 1px solid var(--border); border-radius: 10px; padding: 7px 10px; }
.kv .l { color: var(--muted); font-size: .7rem; } .kv .v { color: var(--text); font-size: .98rem; font-weight: 600;
  font-family: 'JetBrains Mono', monospace; } .kv .n { color: var(--faint); font-size: .68rem; }
.verdict-row { display: grid; grid-template-columns: 1fr 1fr 1.15fr; gap: 10px; }
.say { background: #0B1222; border: 1px solid var(--border); border-radius: 12px; padding: 10px 12px; font-size: .82rem;
       color: #CBD5E1; line-height: 1.5; }
.say .who { color: var(--text); font-size: .8rem; font-weight: 700; margin-bottom: 6px; display: flex; align-items: center; gap: 4px; }
.say ul { margin: 4px 0 0; padding-left: 1.05rem; } .say li { margin-bottom: 2px; }
.say.boss { background: linear-gradient(135deg, #2A1F06 0%, #0F172A 100%); border-color: rgba(245, 197, 66, .55); }
.close-card { background: linear-gradient(120deg, #0F172A 0%, #111827 60%, #10192E 100%); border: 1px solid var(--border);
              border-radius: 14px; padding: 16px 20px; }
.close-grid { display: grid; grid-template-columns: 1.2fr 1fr 1fr 1.3fr; gap: 14px; }
.close-grid h5 { color: var(--muted); font-size: .76rem; font-weight: 600; margin: 0 0 6px; letter-spacing: .04em; }
.close-big { font-family: 'JetBrains Mono', monospace; font-size: 1.6rem; font-weight: 700; color: var(--text); }
.close-grid .ln { color: #CBD5E1; font-size: .8rem; line-height: 1.55; }

/* ---------- 🎖️ WAR ROOM ---------- */
.st-key-war_table { position: relative; overflow: hidden; border-radius: 18px; padding: 18px 18px 10px;
  border: 1px solid rgba(34, 197, 94, .35);
  background:
    radial-gradient(ellipse at 50% 0%, rgba(245, 197, 66, .10) 0%, transparent 55%),
    repeating-linear-gradient(0deg, rgba(34, 197, 94, .07) 0 1px, transparent 1px 34px),
    repeating-linear-gradient(90deg, rgba(34, 197, 94, .07) 0 1px, transparent 1px 34px),
    linear-gradient(180deg, #06140F 0%, #0A1512 50%, #070D0B 100%);
  box-shadow: inset 0 0 60px rgba(0, 0, 0, .65), 0 0 0 1px rgba(0, 0, 0, .4); }
.st-key-war_table:before { content: ''; position: absolute; left: 50%; top: 50%; width: 1400px; height: 1400px;
  margin: -700px 0 0 -700px; pointer-events: none; border-radius: 50%;
  background: conic-gradient(from 0deg, rgba(34, 197, 94, .16), rgba(34, 197, 94, 0) 14%, transparent 100%);
  animation: war-sweep 7s linear infinite; }
.st-key-war_table:after { content: 'WAR ROOM ▸ TACTICAL CONFERENCE'; position: absolute; top: 8px; left: 16px;
  color: rgba(134, 239, 172, .55); font: 600 .62rem 'JetBrains Mono', monospace; letter-spacing: .2em; pointer-events: none; }
@keyframes war-sweep { to { transform: rotate(360deg); } }
.st-key-war_table > div { position: relative; z-index: 1; }
.chair-seat { text-align: center; padding: 12px 10px 8px; border-radius: 16px; margin-top: 10px;
  background: linear-gradient(180deg, rgba(42, 31, 6, .85) 0%, rgba(15, 23, 42, .85) 100%);
  border: 1px solid rgba(245, 197, 66, .55); box-shadow: 0 0 26px rgba(245, 197, 66, .12); }
.chair-seat.selected { box-shadow: 0 0 0 2px var(--gold), 0 0 30px rgba(245, 197, 66, .35); }
.nameplate { display: inline-block; margin-top: 6px; padding: 2px 14px; border-radius: 6px; font-weight: 800; font-size: .95rem;
  color: #1C1206; background: linear-gradient(180deg, #FDE68A, #D4A017); letter-spacing: .08em; }
.chair-seat .tg, .seat .tg { color: var(--muted); font-size: .7rem; margin-top: 3px; }
.directive { margin-top: 8px; text-align: left; background: rgba(2, 6, 23, .6); border: 1px dashed rgba(245, 197, 66, .4);
  border-radius: 10px; padding: 7px 10px; color: #FDE68A; font-size: .8rem; line-height: 1.45; }
.seat { text-align: center; padding: 10px 8px 8px; border-radius: 14px; min-height: 212px;
  background: rgba(2, 6, 23, .72); border: 1px solid color-mix(in srgb, var(--c) 45%, transparent);
  transition: transform .18s ease, box-shadow .18s ease; }
.seat:hover { transform: translateY(-3px); box-shadow: 0 0 18px color-mix(in srgb, var(--c) 35%, transparent); }
.seat.selected { box-shadow: 0 0 0 2px var(--c), 0 0 22px color-mix(in srgb, var(--c) 45%, transparent); }
.seat .nm { color: var(--text); font-weight: 800; font-size: .95rem; margin-top: 4px; }
.seat .logic { display: inline-block; margin-top: 3px; padding: 1px 8px; border-radius: 999px; font-size: .66rem; font-weight: 700;
  color: var(--c); border: 1px solid color-mix(in srgb, var(--c) 60%, transparent); }
.seat .st { margin-top: 7px; color: #CBD5E1; font-size: .74rem; line-height: 1.35; min-height: 2.7em; text-align: left; }
.seat .rf { margin-top: 5px; color: #A7F3D0; font-size: .7rem; line-height: 1.35; text-align: left;
  border-top: 1px dashed rgba(148, 163, 184, .2); padding-top: 5px; }
.acc-badge { display: inline-block; background: rgba(16, 185, 129, .12); color: #6EE7B7; border: 1px solid rgba(16, 185, 129, .45);
  font-size: .66rem; font-weight: 700; padding: 1px 7px; border-radius: 10px; margin: 3px 0 0; white-space: nowrap; }
.acc-badge.none { background: rgba(148, 163, 184, .1); color: #94A3B8; border-color: rgba(148, 163, 184, .35); }
.sleep-badge { display: inline-block; background: #1E1B4B; color: #C7D2FE; border: 1px solid #6366F1; font-size: .66rem;
  font-weight: 700; padding: 1px 7px; border-radius: 10px; margin-left: 4px; animation: wr-float 2.4s ease-in-out infinite; }
@keyframes wr-float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-3px); } }
.radar-banner { border: 1px solid rgba(34, 197, 94, .45); background: linear-gradient(90deg, rgba(6, 78, 59, .45), rgba(2, 6, 23, .7));
  color: #A7F3D0; border-radius: 12px; padding: 10px 14px; font: 600 .84rem 'JetBrains Mono', 'Noto Sans TC', monospace; }
.panel-head { display: flex; align-items: center; gap: 12px; margin: 8px 0 10px; }
.panel-head .t { font-size: 1.08rem; font-weight: 700; color: var(--text); }
.panel-head .s { font-size: .78rem; color: var(--muted); }
.qa-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; margin-top: 8px; }
.qa-grid .say { min-height: 120px; }
.qa-grid .say.boss { grid-column: 1 / -1; }

/* ---------- 深度研究：MD 裁決主卡、成員卡、時間軸、觸價通知 ---------- */
.md-card { background: linear-gradient(135deg, #1E293B 0%, #0F172A 100%); border: 1px solid rgba(245, 197, 66, 0.45);
           border-radius: 14px; padding: 18px 20px; margin-bottom: 14px; }
.md-grid { display: grid; grid-template-columns: 150px 1fr; gap: 18px; align-items: center; }
.ring { width: 128px; height: 128px; border-radius: 50%; display: grid; place-items: center; }
.ring-inner { width: 100px; height: 100px; border-radius: 50%; background: #0F172A; display: grid; place-items: center; text-align: center; }
.ring-inner .n { font-family: 'JetBrains Mono', monospace; font-size: 1.6rem; font-weight: 700; color: var(--text); }
.ring-inner .t { color: var(--muted); font-size: 0.7rem; }
.levels { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; margin-top: 10px; }
.redteam { background: rgba(239, 68, 68, 0.08); border: 1px solid rgba(239, 68, 68, 0.45); border-radius: 10px;
           padding: 10px 12px; margin-top: 12px; color: #FECACA; font-size: 0.86rem; }
.pod { background: var(--bg-card); border: 1px solid var(--border); border-radius: 14px; padding: 12px 14px; min-height: 200px; }
.pod-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 6px; }
.pod-title { color: var(--text); font-weight: 600; font-size: 0.9rem; }
.pod ul { margin: 0; padding-left: 1.1rem; color: #CBD5E1; font-size: 0.8rem; line-height: 1.5; }
.alert-card { background: linear-gradient(90deg, rgba(239, 68, 68, 0.18), rgba(17, 24, 39, 0.9)); border: 1px solid #EF4444;
              border-radius: 12px; padding: 10px 14px; margin-bottom: 10px; color: #FEE2E2; font-size: 0.86rem; }
.timeline { border-left: 2px solid #1F2937; margin-left: 6px; padding-left: 14px; }
.tl-item { position: relative; padding: 0 0 12px 0; }
.tl-item:before { content: ''; position: absolute; left: -20px; top: 5px; width: 10px; height: 10px; border-radius: 50%;
                  background: #3B82F6; border: 2px solid #0B1120; }
.tl-title a { color: #E2E8F0; text-decoration: none; font-size: 0.86rem; }
.tl-title a:hover { color: #93C5FD; }
.micro { background: var(--bg-card-2); border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px; }
.micro .lbl { color: var(--muted); font-size: 0.7rem; }
.micro .val { color: var(--text); font-size: 0.98rem; font-weight: 600; font-family: 'JetBrains Mono', monospace; }

/* ---------- 指定按鈕外觀（Streamlit 以 st-key-<key> class 標記容器） ---------- */
.st-key-btn_enter_war_room button { background: linear-gradient(135deg, #1F2D1A 0%, #3F4F25 45%, #8A6D1D 100%) !important;
  color: #FEF3C7 !important; border: 1px solid #D4A017 !important; font-weight: 800 !important; letter-spacing: .02em;
  box-shadow: 0 0 0 1px rgba(0, 0, 0, .4), 0 0 18px rgba(212, 160, 23, .35); min-height: 3rem; }
.st-key-btn_enter_war_room button:hover { box-shadow: 0 0 26px rgba(245, 197, 66, .6); }
.st-key-btn_enter_sim button, .st-key-btn_war_to_sim button {
  background: linear-gradient(135deg, #064E3B 0%, #047857 45%, #B7791F 100%) !important; color: #FFFBEB !important;
  border: 1px solid #F5C542 !important; font-weight: 800 !important; min-height: 3rem;
  box-shadow: 0 0 18px rgba(16, 185, 129, .35); }
.st-key-btn_enter_sim button:hover, .st-key-btn_war_to_sim button:hover { box-shadow: 0 0 26px rgba(245, 197, 66, .55); }
.st-key-btn_sim_meeting button { background: linear-gradient(135deg, #B45309, #F5C542) !important; color: #1C1206 !important;
  border: 0 !important; font-weight: 800 !important; min-height: 2.8rem; }

/* ---------- 🧪 SIMULATION ROOM ---------- */
.kpi-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; margin: 6px 0 10px; }
@media (max-width: 1280px) { .kpi-grid { grid-template-columns: repeat(3, 1fr); } }
.kpi { background: linear-gradient(180deg, #0F1F1A 0%, #0B1222 100%); border: 1px solid rgba(16, 185, 129, .35);
       border-radius: 14px; padding: 12px 14px; }
.kpi .l { color: #A7F3D0; font-size: .74rem; font-weight: 600; }
.kpi .v { color: #F8FAFC; font: 700 1.45rem 'JetBrains Mono', monospace; margin-top: 4px; }
.kpi .s { color: var(--muted); font-size: .74rem; margin-top: 2px; }
.goal-bar { position: relative; height: 10px; background: #1F2937; border-radius: 6px; margin: 8px 0 4px; overflow: hidden; }
.goal-bar .fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 6px;
                  background: linear-gradient(90deg, #10B981, #F5C542); }
.goal-bar .mark { position: absolute; top: -2px; bottom: -2px; width: 2px; background: #F8FAFC; opacity: .7; }
.committee { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.committee .say { min-height: 96px; }
.committee .say.boss { grid-column: 1 / -1; }
.decision-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 6px 0;
                border-bottom: 1px dashed rgba(148, 163, 184, .18); font-size: .84rem; color: #E2E8F0; }
.st-key-btn_war_sync button { background: linear-gradient(135deg, #7F1D1D, #DC2626) !important; color: #FFF !important;
  border: 1px solid #FCA5A5 !important; font-weight: 800 !important; }
.st-key-btn_chair button { background: linear-gradient(180deg, #FDE68A, #D4A017) !important; color: #1C1206 !important;
  border: 0 !important; font-weight: 800 !important; }

/* ---------- 📱 精簡卡片視圖（持股／20 大龍頭；電腦與手機皆可切換） ---------- */
.scard-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; margin: 6px 0 10px; }
.scard { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.25fr); gap: 10px; padding: 10px 12px;
         background: var(--bg-card); border: 1px solid var(--border); border-left: 3px solid var(--c, #64748B); border-radius: 12px; }
.scard .id { color: var(--text); font-weight: 700; font-size: .92rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.scard .px { font: 700 1.15rem 'JetBrains Mono', monospace; color: var(--text); margin: 3px 0; white-space: nowrap; }
.scard .sub { color: var(--muted); font-size: .72rem; }
.scard .kv2 { display: grid; grid-template-columns: auto 1fr; gap: 3px 8px; font-size: .78rem; align-content: start; }
.scard .kv2 .k { color: var(--muted); white-space: nowrap; }
.scard .kv2 .v { color: #E2E8F0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
                 font-variant-numeric: tabular-nums; }
.scard .note { grid-column: 1 / -1; color: #FDE68A; font-size: .76rem; line-height: 1.45;
               border-top: 1px dashed rgba(148, 163, 184, .2); padding-top: 6px; }

/* ---------- 📱 手機導航列（只在手機顯示） ---------- */
.st-key-mnav { display: none; }

/* ================================================================ 📱 手機直式（≤ 768px） */
@media (max-width: 768px) {
  /* 優化 1：左右留白縮到 0.6rem；頂部 1.5rem＋固定導航列高度 */
  .block-container, [data-testid="stMainBlockContainer"] {
    padding-left: .6rem !important; padding-right: .6rem !important; padding-top: calc(1.5rem + 52px) !important; }
  .wr-title { font-size: 1.1rem; }

  /* 優化 2：模擬室 5 張指標卡 → 每排 2 張（2×2＋1） */
  .kpi-grid { display: flex !important; flex-wrap: wrap; gap: 8px; }
  .kpi { flex: 1 1 46%; min-width: 0; padding: 9px 10px; }
  .kpi .l { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: .7rem; }
  .kpi .v { font-size: 1.15rem; white-space: nowrap; }
  .kpi .s { font-size: .68rem; }
  .kv-grid, .levels { display: flex !important; flex-wrap: wrap; gap: 6px; }
  .kv-grid .kv, .levels > * { flex: 1 1 46%; min-width: 0; }
  .kv .v, .micro .val, .close-big, .lookup-price { white-space: nowrap; }
  .verdict-row, .close-grid, .md-grid, .committee, .qa-grid { grid-template-columns: 1fr !important; }
  .md-grid .ring { margin: 0 auto; }

  /* 優化 3：四位成員公仔卡 → 手指左右滑動的卡片列（每張 145px，一屏約 2.3 張） */
  .st-key-member_row [data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap !important; overflow-x: auto; -webkit-overflow-scrolling: touch; scroll-snap-type: x mandatory;
    gap: 10px !important; padding-bottom: 6px; scrollbar-width: thin; }
  .st-key-member_row [data-testid="stColumn"] {
    flex: 0 0 145px !important; width: 145px !important; min-width: 145px !important; scroll-snap-align: start; }
  .st-key-member_row .seat { min-height: 0; padding: 8px 6px; }
  .st-key-member_row .seat .st { min-height: 0; font-size: .7rem; }
  .st-key-member_row .seat .rf { display: none; }
  .st-key-member_row .acc-badge { white-space: normal; }
  .st-key-member_row button p { font-size: .74rem; }
  .st-key-war_table { padding: 14px 8px 8px; }
  .chair-seat { padding: 8px 8px 6px; }

  /* 優化 4：資料表與卡片 */
  [data-testid="stDataFrame"] { -webkit-overflow-scrolling: touch; }
  .scard-list { grid-template-columns: 1fr; }

  /* 優化 5：TOP 10 新聞手機閱讀 */
  .top10 li { grid-template-columns: 20px minmax(0, 1fr) auto; gap: 6px; padding: 10px 2px; }
  .top10 .t { font-size: .95rem; line-height: 1.55; }
  .top10 .p { font-size: .95rem; line-height: 1.55; }
  .top10 .src-link { font-size: .78rem; }
  .top10 .meta .pub-date, .top10 .src-pill { padding: 3px 10px; }
  .close-bullets li { font-size: .92rem; }

  /* 優化 6：頂部固定雙按鈕導航（各 50%、高 44px） */
  .st-key-mnav { display: block; position: fixed; top: 0; left: 0; right: 0; z-index: 999990;
    padding: 6px .6rem; background: rgba(5, 8, 10, .94); backdrop-filter: blur(8px);
    border-bottom: 1px solid rgba(148, 163, 184, .2); }
  .st-key-mnav [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 8px !important; }
  .st-key-mnav [data-testid="stColumn"] { flex: 1 1 50% !important; width: 50% !important; min-width: 0 !important; }
  .st-key-mnav button { min-height: 44px; width: 100%; font-weight: 700; }
  /* 各視圖標題列裡的切換按鈕由手機導航列取代 */
  [data-testid="stColumn"]:has(.st-key-btn_enter_war_room), [data-testid="stColumn"]:has(.st-key-btn_enter_sim),
  [data-testid="stColumn"]:has(.st-key-btn_leave_war_room), [data-testid="stColumn"]:has(.st-key-btn_war_to_sim),
  [data-testid="stColumn"]:has(.st-key-btn_sim_home), [data-testid="stColumn"]:has(.st-key-btn_sim_to_war) { display: none !important; }
}
</style>
"""

WAR_ROOM_CSS = """
<style>
.stApp { background: radial-gradient(ellipse at 50% -10%, #0E2A1F 0%, #07110D 45%, #05080A 100%) !important; }
.wr-title { color: #BBF7D0; font-family: 'JetBrains Mono', 'Noto Sans TC', monospace; letter-spacing: .06em; }
</style>
"""

# ---------------------------------------------------------------- 五位成員

AGENTS = {
    1: {"nick": "新聞達人", "title": "全市場要聞與基本面情報", "logic": "要聞 × 基本面", "color": "#3B82F6",
        "key": "role1_mark", "member": "news", "icon": "🕵️‍♂️"},
    2: {"nick": "籌碼王", "title": "法人、權證避險與五檔大單", "logic": "法人與五檔籌碼", "color": "#10B981",
        "key": "role2_elon", "member": "chips", "icon": "🎧"},
    3: {"nick": "K線技術人", "title": "趨勢、四線排列與轉折 K", "logic": "趨勢與轉折K", "color": "#84CC16",
        "key": "role3_jensen", "member": "kline", "icon": "⚔️"},
    4: {"nick": "巴董事長", "title": "主席｜反向挑戰與最終拍板", "logic": "主席拍板", "color": "#F5C542",
        "key": "role4_buffett", "member": "chairman", "icon": "👑"},
    5: {"nick": "川大帝MAGA", "title": "美股費半與台股盤前預判", "logic": "美股與盤前", "color": "#EF4444",
        "key": "role5_trump", "member": "maga", "icon": "🦅"},
}
MEMBER_ROLE = {a["member"]: n for n, a in AGENTS.items()}

AVATAR_JSON = Path(__file__).resolve().parent.parent / "assets" / "avatars" / "avatar_badges.json"


def _load_avatars():
    """讀取 tools/crop_character_avatars.py 產生的戰術頭像（WebP Base64）；缺檔時回傳空 dict（改用 emoji）。"""
    if not AVATAR_JSON.exists():
        return {}
    try:
        with open(AVATAR_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


AVATARS = _load_avatars()


def has_figurines():
    return all(f"{a['key']}_badge_front" in AVATARS and f"{a['key']}_badge_angle" in AVATARS for a in AGENTS.values())


def avatar_css() -> str:
    """把戰術頭像定義成 CSS class（整頁只傳一次，約 76 KB）；頭像元件以 class 引用，10 秒局部刷新不重傳圖片。

    頭像外框、hover 側面切換的版面樣式在 inject_css()；本函式只負責圖片 class，
    缺少 assets/avatars/avatar_badges.json 時回傳空字串。"""
    rules = []
    for num, a in AGENTS.items():
        for kind, suffix in (("bf", "badge_front"), ("ba", "badge_angle"), ("head", "head")):
            data = AVATARS.get(f"{a['key']}_{suffix}")
            if data:
                rules.append(f".av{num}-{kind} {{ background-image: url(data:image/webp;base64,{data}); }}")
    return "<style>" + "\n".join(rules) + "</style>" if rules else ""


def war_room_css() -> str:
    """WAR ROOM 視圖專用的深綠戰術背景。"""
    return WAR_ROOM_CSS


def tac_avatar(role_num):
    """戰術通訊頭像：巴董事長 84px 金色主席框，其餘 68px 八角戰術框；hover 切換 45 度側面。"""
    a = AGENTS[role_num]
    kind = "chair" if role_num == 4 else "member"
    if has_figurines():
        inner = (f'<div class="tac-face front av{role_num}-bf"></div>'
                 f'<div class="tac-face angle av{role_num}-ba"></div>')
    else:
        inner = f'<div class="tac-emoji">{a["icon"]}</div>'
    av = f'<div class="tac-av {kind}" style="--c:{a["color"]}"><div class="tac-in">{inner}</div></div>'
    return av if kind == "chair" else f'<span class="tac-wrap" style="--c:{a["color"]}">{av}</span>'


def mini_avatar(role_num, size=26):
    """迷你圓形頭像（無頭像圖時退回成員 emoji）。"""
    a = AGENTS[role_num]
    if f"{a['key']}_head" in AVATARS:
        return (f'<span class="mini-av av{role_num}-head" style="width:{size}px;height:{size}px;'
                f'border-color:{a["color"]}"></span>')
    return a["icon"]


ACTION_TYPES = {"🟢": "bull", "🔴": "bear", "🟡": "amber", "💰": "info", "⚪": "neutral"}
RATING_TYPES = {"強多": "bull", "偏多": "bull", "中性": "neutral", "偏空": "bear", "強空": "bear"}
PROPOSAL_TYPES = {"加碼": "bull", "攤平": "amber", "獲利了結": "info", "續抱": "neutral", "停損減碼": "bear"}
TAG_TYPES = {"利多": "bull", "利空": "bear", "中性": "neutral", "觀察": "amber"}


_ROLE_RENAMES = [(re.compile(rf"角色\s?{n}"), a["nick"]) for n, a in AGENTS.items()]


_GURU_PAREN = re.compile(r"\s*[（(][^（）()]{0,12}(?:權證小哥|朱家泓|美股大叔)[^（）()]{0,16}[）)]")
_GURU_WORD = re.compile(r"(?:依|以)?(?:權證小哥|朱家泓|美股大叔(?:\s*[（(]Uncle Stock Notes[）)])?)(?:的)?(?:實戰)?"
                        r"(?:籌碼|K線與趨勢|宏觀與機構敘事|機構)?(?:邏輯|戰法|視角|四步驟|四大檢查|準則|觀點)?")


# 比喻詞 → 直白交易文字（長詞優先）；舊報告與模型回覆在顯示前一律轉換
PLAIN_TERMS = [
    ("已進入擊球進場區", "達建議買進價"), ("進入預設擊球區", "達建議買進價"), ("已進入擊球區", "已達建議買進價"),
    ("進入擊球區", "達建議買進價"), ("擊球進場區", "達建議買進價"), ("尚未回測擊球區", "高於買進價・等待回檔"),
    ("接近擊球區", "接近買進價 (差距1.5%內)"), ("擊球區", "建議買進價區間"), ("擊球", "達建議買進價"),
    ("進場觸價雷達", "建議買進價監控表"), ("獵物掃描器", "建議買進價監控表"), ("觸價雷達", "建議買進價監控表"),
    ("進場雷達", "建議買進價監控表"), ("破線不接刀", "跌破停損價・暫不買進"), ("散戶接刀", "散戶融資承接"),
    ("不接刀", "暫不買進"), ("接刀", "逢跌買進"), ("獵物分數", "買進評分"), ("獵物", "候選股"), ("獵殺", "交易"),
    ("巡弋", "監控"), ("子彈", "可用現金"),
]


def plain_terms(text):
    for old, new in PLAIN_TERMS:
        text = text.replace(old, new)
    return text


def name_roles(text):
    """顯示用清理：「角色 N」換成成員正式名稱、移除「（權證小哥邏輯）」等名師字眼，並把比喻詞換成直白交易文字。"""
    if not isinstance(text, str):
        return text
    for pat, name in _ROLE_RENAMES:
        text = pat.sub(name, text)
    return plain_terms(_GURU_WORD.sub("", _GURU_PAREN.sub("", text)))


def _mask(text, privacy_unlocked):
    return name_roles(mask_sensitive_text(text, privacy_unlocked))


def esc(v):
    return html.escape("" if v is None else str(v))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


UP_TX, DOWN_TX, FLAT_TX = "#F87171", "#34D399", "#CBD5E1"  # 台股紅漲（+）、綠跌（-）、零灰白


def fmt_num(v, decimals=2):
    """純數字（指數點數等非金額）：千分位。"""
    f = _f(v)
    return f"{f:,.{decimals}f}" if f is not None else "N/A"


def fmt_money(v, decimals=None, signed=False):
    """金額：$1,000,000、$477.50；signed=True 時 +$12,500／-$3,200／$0。decimals=None 時整數金額不帶小數。"""
    f = _f(v)
    if f is None:
        return "N/A"
    d = decimals if decimals is not None else (0 if abs(f - round(f)) < 1e-9 else 2)
    body = f"${abs(f):,.{d}f}"
    if signed and f > 0:
        return "+" + body
    return ("-" + body) if f < 0 else body


def fmt_price(v):
    """股價：一律 $ 與兩位小數，例如 $2,240.00。"""
    return fmt_money(v, 2)


def fmt_pct(v):
    """漲跌幅／報酬率：+1.52%／-0.48%／0.00%。"""
    f = _f(v)
    if f is None:
        return "N/A"
    return "0.00%" if round(f, 2) == 0 else f"{f:+.2f}%"


def sign_color(v):
    f = _f(v)
    return FLAT_TX if f is None or round(f, 6) == 0 else UP_TX if f > 0 else DOWN_TX


def signed_html(amount=None, pct=None, decimals=None, unit=""):
    """紅正綠負的金額／百分比 span，例如 +$15,200 (+1.52%)；零值為 $0 (0.00%) 灰白色。"""
    ref = amount if amount is not None else pct
    parts = []
    if amount is not None:
        parts.append(fmt_money(amount, decimals, signed=True) + unit)
    if pct is not None:
        parts.append(f"({fmt_pct(pct)})" if amount is not None else fmt_pct(pct))
    return f'<span style="color:{sign_color(ref)};font-weight:700">{esc(" ".join(parts))}</span>'


def sign_class(v):
    f = _f(v)
    if f is None or f == 0:
        return "flat"
    return "up" if f > 0 else "down"


def pill(text, kind="neutral", extra=""):
    return f'<span class="pill {kind} {extra}">{esc(text)}</span>'


def change_pill(pct):
    f = _f(pct)
    kind = "neutral" if f is None or f == 0 else "bull" if f > 0 else "bear"
    return pill(fmt_pct(pct), kind)


def action_kind(label):
    return ACTION_TYPES.get((label or "")[:1], "neutral")


def tone_kind(text):
    """依文字判斷膠囊顏色（偏多紅、偏空綠、觀望琥珀）。"""
    t = str(text or "")
    if any(w in t for w in ("偏空", "空頭", "暫不買進", "減碼", "倒貨", "賣壓", "極度")):
        return "bear"
    if any(w in t for w in ("鎖碼", "偏多", "多頭", "布局", "加碼", "試單")):
        return "bull"
    if any(w in t for w in ("觀望", "盤整", "不追高")):
        return "amber"
    return "neutral"


def updated_tag(ts, note="", stale=False):
    """灰色小字更新時間標籤：🕒 資料最後更新：YYYY-MM-DD HH:MM:SS（超過一天未更新改為琥珀色）。"""
    extra = f"｜{esc(note)}" if note else ""
    return f'<span class="upd{" stale" if stale else ""}">🕒 資料最後更新：{esc(ts or "尚無資料")}{extra}</span>'


def inject_css() -> str:
    """回傳全域 CSS（dashboard 每次渲染開頭呼叫一次）。"""
    return CSS


def _safe_link(url):
    url = str(url or "")
    return url if url.startswith(("https://", "http://")) else "#"


# ---------------------------------------------------------------- 🏠 日常主畫面元件

def render_station_head(role_num, subtitle, badge_html=""):
    """日常主畫面欄位標頭：縮小版戰術頭像＋成員名＋邏輯標籤。badge_html 須為程式組成的安全 HTML。"""
    a = AGENTS[role_num]
    return (f'<div class="station" style="--c:{a["color"]}">{tac_avatar(role_num)}<div>'
            f'<div class="nm">{a["icon"]} {esc(a["nick"])} {badge_html}</div>'
            f'<div class="tg">{esc(a["logic"])}｜{esc(a["title"])}</div><div class="sub">{esc(subtitle)}</div></div></div>')


def render_top10_list(items):
    """items: [{"title", "point", "tag"（利多/利空/中性/觀察）, "pub_date", "source", "url"}]，文字須已遮罩。
    每則顯示 [發布日期時間] 徽章、重點摘要，以及在新分頁開啟的「🔗 查看原文出處 (媒體)」連結（url 只接受 http/https）。"""
    if not items:
        return '<p class="tl-src">尚無資料</p>'
    rows = []
    for it in items[:10]:
        url = _safe_link(it.get("url") or it.get("link"))
        date = it.get("pub_date") or it.get("meta")
        badge = f'<span class="pub-date">{esc(date)}</span>' if date else '<span class="pub-date none">時間不明</span>'
        source = esc(it.get("source") or "出處")
        link = (f'<span class="src-pill"><a class="src-link" href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
                f'🔗 原文出處・{source}</a></span>' if url != "#"
                else '<span class="src-pill none"><span class="src-link none">⚠️ 無可驗證出處</span></span>')
        tag = it.get("tag") or "中性"
        rows.append(f'<li><div><div class="t">{esc(it.get("title"))}</div><div class="p">{esc(it.get("point"))}</div>'
                    f'<div class="meta">{badge}{link}</div></div>{pill(tag, TAG_TYPES.get(tag, "neutral"))}</li>')
    return f'<ol class="top10">{"".join(rows)}</ol>'


def _kv(label, value, note=""):
    return (f'<div class="kv"><div class="l">{esc(label)}</div><div class="v">{value}</div>'
            f'<div class="n">{esc(note)}</div></div>')


def render_lookup_card(r):
    """即時快篩診斷卡：現價、今日高低、VWAP、五檔比＋K線技術人／籌碼王／巴董事長三格判讀（不含任何持倉資訊）。"""
    q, k = r.get("quote") or {}, r.get("kline") or {}
    zhu, chip, boss = k.get("zhu") or {}, r.get("chip_verdict") or {}, r.get("chairman") or {}
    price = q.get("price") or k.get("close")
    vwap = q.get("vwap")
    vwap_note = ("站上 VWAP" if price and vwap and price >= vwap else "跌破 VWAP") if vwap else ""
    ratio = q.get("bid_ask_ratio")
    head = (f'<div class="lookup-head"><div><span class="code-badge">{esc(r.get("stock_id"))}</span>'
            f' <b style="color:#E5E7EB;font-size:1.1rem">{esc(r.get("name"))}</b>'
            f'<div class="lookup-price">{esc(fmt_price(price))} {change_pill(q.get("change_pct"))}</div>'
            f'<div class="tl-src">{esc(q.get("price_source") or k.get("source") or "")}'
            f'{"・" + esc(int(q["price_age_sec"])) + " 秒前" if q.get("price_age_sec") is not None else ""}'
            f'｜快篩 {esc(r.get("elapsed_sec"))} 秒｜{esc(r.get("looked_up_at", "")[11:])}</div></div>'
            f'<div>{pill(boss.get("action", "N/A"), tone_kind(boss.get("action")), "big-badge")}</div></div>')
    grid = ('<div class="kv-grid">'
            + _kv("今日高／低", f'{esc(fmt_price(q.get("high")))} / {esc(fmt_price(q.get("low")))}', f'開 {fmt_price(q.get("open"))}')
            + _kv("盤中 VWAP", esc(fmt_price(vwap)), vwap_note)
            + _kv("五檔委買/委賣", esc(f"{ratio:.2f}" if ratio is not None else "N/A"), q.get("tick_side") or "")
            + _kv("MA5／MA20", f'{esc(fmt_price(k.get("ma5")))} / {esc(fmt_price(k.get("ma20")))}',
                  f'月線乖離 {fmt_pct(k.get("bias20_pct"))}')
            + _kv("量比（20 日）", esc(k.get("vol_ratio_20") if k.get("vol_ratio_20") is not None else "N/A"),
                  f'5 日 {fmt_pct(k.get("change_5d_pct"))}')
            + '</div>')
    patterns = "、".join(f'{p["pattern"]}（{p["date"][5:]}）' for p in zhu.get("patterns") or []) or "近 3 日無關鍵轉折 K"
    kline_box = (f'<div class="say"><div class="who">{mini_avatar(3)}K線技術人｜趨勢判讀 '
                 f'{pill(zhu.get("trend", "N/A"), tone_kind(zhu.get("trend")))}</div><ul>'
                 f'<li>{esc(zhu.get("trend_rule", zhu.get("error", "")))}</li>'
                 f'<li>{esc(zhu.get("ma_summary", ""))}</li><li>{esc(zhu.get("granville", ""))}</li>'
                 f'<li>轉折 K：{esc(patterns)}</li><li>{esc(zhu.get("stop_basis", ""))}</li></ul></div>')
    chip_items = (chip.get("alerts") or [])[:2] + (chip.get("points") or [])[:3]
    chip_box = (f'<div class="say"><div class="who">{mini_avatar(2)}籌碼王｜籌碼判讀 '
                f'{pill(chip.get("label", "N/A"), tone_kind(chip.get("label")))}</div><ul>'
                + "".join(f"<li>{'⚠️ ' if i < len(chip.get('alerts') or []) and i < 2 else ''}{esc(t)}</li>"
                          for i, t in enumerate(chip_items))
                + f'</ul><div class="tl-src" style="margin-top:4px">法人資料日 {esc((r.get("chips") or {}).get("as_of", "N/A"))}</div></div>')
    boss_box = (f'<div class="say boss"><div class="who">{mini_avatar(4)}巴董事長｜一句話綜合操作建議</div>'
                f'<div style="font-size:.9rem;color:#FDE68A;font-weight:600">{esc(boss.get("line", ""))}</div>'
                f'<ul><li>支撐 {esc(fmt_price(boss.get("support")))}｜壓力 {esc(fmt_price(boss.get("resistance")))}'
                f'｜停損 {esc(fmt_price(boss.get("stop")))}</li>'
                f'<li>{esc(zhu.get("action", ""))}</li></ul></div>')
    return f'<div class="lookup-card">{head}{grid}<div class="verdict-row">{kline_box}{chip_box}{boss_box}</div></div>'


def render_market_close_card(s, reflection_text=""):
    """台股收盤後大盤與資金總結卡：頂部 4 個關鍵數字＋3–5 點條列重點（s["bullets"]，文字須已遮罩）。"""
    t, inst = s.get("taiex") or {}, s.get("institutional") or {}

    def amt(v):
        return signed_html(_f(v), decimals=1, unit=" 億") if _f(v) is not None else "N/A"

    stats = ('<div class="close-stats">'
             + pill(f'加權 {fmt_num(t.get("close"))} 點', "neutral") + change_pill(t.get("change_pct"))
             + f' <span class="tl-src">外資 {amt(inst.get("foreign"))}｜投信 {amt(inst.get("trust"))}｜'
               f'自營商 {amt(inst.get("dealer"))}</span></div>')
    colors = ("#3B82F6", "#10B981", "#F59E0B", "#F5C542", "#A78BFA")
    bullets = s.get("bullets") or []
    items = "".join(f'<li style="--c:{colors[i % len(colors)]}"><b>{i + 1}. {esc(b.get("label"))}</b>{esc(b.get("text"))}</li>'
                    for i, b in enumerate(bullets[:5]))
    body = f'<ul class="close-bullets">{items}</ul>' if items else '<p class="tl-src">尚無條列重點，請按「🔄 一鍵更新」。</p>'
    note = (f'<div class="tl-src" style="margin-top:4px">重點由 {esc(s.get("bullets_engine", "python"))} 依收盤數據整理'
            f'（加權資料日 {esc(t.get("date", "N/A"))}、法人資料日 {esc(inst.get("date", "N/A"))}）</div>')
    return f'<div class="close-card">{stats}{body}{note}</div>'


# ---------------------------------------------------------------- 🎖️ WAR ROOM 元件

def accuracy_badge(stat):
    """🎯 歷史準確率 XX% (已累積 N 次驗證)；stat 來自 learning_engine.member_accuracy()。"""
    n = (stat or {}).get("total") or 0
    if not n:
        return '<span class="acc-badge none">🎯 歷史準確率 尚無 (已累積 0 次驗證)</span>'
    return f'<span class="acc-badge">🎯 歷史準確率 {stat["accuracy_pct"]:.0f}% (已累積 {n} 次驗證)</span>'


def render_chairman_seat(directive, reflection, selected=False, accuracy=None):
    a = AGENTS[4]
    return (f'<div class="chair-seat {"selected" if selected else ""}">{tac_avatar(4)}'
            f'<div><span class="nameplate">👑 {esc(a["nick"])}</span></div><div>{accuracy_badge(accuracy)}</div>'
            f'<div class="tg">CHAIRMAN SEAT｜{esc(a["logic"])}｜反向挑戰 × 不對稱風報比</div>'
            f'<div class="directive">📣 最新全局指令：{esc(directive)}</div>'
            f'<div class="seat" style="min-height:0;border:0;background:none;padding:4px 0 0">'
            f'<div class="rf" style="border:0">{esc(reflection)}</div></div></div>')


def render_member_seat(role_num, status, reflection, selected=False, sleeping=False, accuracy=None):
    a = AGENTS[role_num]
    sleep = '<span class="sleep-badge">💤 休眠鎖定</span>' if sleeping else ""
    return (f'<div class="seat {"selected" if selected else ""}" style="--c:{a["color"]}">{tac_avatar(role_num)}'
            f'<div class="nm">{a["icon"]} {esc(a["nick"])}{sleep}</div><div>{accuracy_badge(accuracy)}</div>'
            f'<div class="logic">{esc(a["logic"])}</div>'
            f'<div class="st">📡 {esc(status)}</div><div class="rf">{esc(reflection)}</div></div>')


def render_panel_head(role_num, subtitle):
    a = AGENTS[role_num]
    return (f'<div class="panel-head">{tac_avatar(role_num)}<div><div class="t" style="color:{a["color"]}">'
            f'{a["icon"]} {esc(a["nick"])} 個人詳細分析報告</div><div class="s">{esc(subtitle)}</div></div></div>')


def render_radar_banner(text):
    return f'<div class="radar-banner">📡 {esc(text)}</div>'


def render_qa_board(answers):
    """四格戰術對話框：answers = {"question", "news", "chips", "kline", "chairman"}，文字須已遮罩。"""
    def box(role, label, text, cls=""):
        body = esc(text or "（未回覆）").replace("\n", "<br>")
        return (f'<div class="say {cls}"><div class="who">{mini_avatar(role, 30)}{label}</div>'
                f'<div>{body}</div></div>')
    cells = [box(1, "🕵️‍♂️ 新聞達人 回答｜消息面與基本面查核", answers.get("news")),
             box(2, "🎧 籌碼王 回答", answers.get("chips")),
             box(3, "⚔️ K線技術人 回答", answers.get("kline"))]
    if answers.get("maga"):
        cells.append(box(5, "🦅 川大帝MAGA 美股連動補充", answers["maga"]))
    cells.append(box(4, "👑 巴董事長 反向 Challenge 與最終總結", answers.get("chairman"), "boss"))
    return f'<div class="qa-grid">{"".join(cells)}</div>'


def render_stock_cards(cards):
    """📱 精簡卡片視圖：每檔一張雙欄小卡。
    cards: [{"stock_id", "name", "price", "change_pct", "sub"（左下小字）, "kind"（左邊框色：bull/bear/amber/info/neutral）,
             "rows": [(標籤, 已轉義的 HTML 值)], "note"（底部一句話，須已遮罩）}]"""
    if not cards:
        return '<p class="tl-src">尚無資料</p>'
    colors = {"bull": "#EF4444", "bear": "#10B981", "amber": "#F59E0B", "info": "#3B82F6", "neutral": "#64748B"}
    out = []
    for c in cards:
        rows = "".join(f'<span class="k">{esc(k)}</span><span class="v">{v}</span>' for k, v in c.get("rows") or [])
        note = f'<div class="note">{esc(c["note"])}</div>' if c.get("note") else ""
        out.append(f'<div class="scard" style="--c:{colors.get(c.get("kind"), colors["neutral"])}">'
                   f'<div><div class="id"><span class="code-badge">{esc(c.get("stock_id"))}</span> {esc(c.get("name") or "")}</div>'
                   f'<div class="px">{esc(fmt_price(c.get("price")))}</div><div>{change_pill(c.get("change_pct"))}</div>'
                   f'<div class="sub" style="margin-top:4px">{esc(c.get("sub") or "")}</div></div>'
                   f'<div class="kv2">{rows}</div>{note}</div>')
    return f'<div class="scard-list">{"".join(out)}</div>'


# ---------------------------------------------------------------- 🧪 SIMULATION ROOM 元件

def render_sim_kpis(acct, goal, stats, bench=None):
    """🏆 年底目標儀表板：6 張大字卡（含 0050 對標）＋目標進度條（110 萬低標、120 萬高標）；金額 $、損益紅正綠負。"""
    eq, cap = acct.get("total_equity", 0), acct.get("initial_capital", 1_000_000)
    pnl = eq - cap
    ret = acct.get("total_return_pct", 0.0)
    high = acct.get("target_return_high_pct", 20.0)
    fill = max(0.0, min(100.0, ret / high * 100)) if high else 0
    low_mark = acct.get("target_return_low_pct", 10.0) / high * 100 if high else 50
    win = f'{stats["win_rate"]:.1f}%' if stats.get("win_rate") is not None else "—"

    def card(label, value, sub, extra=""):
        return f'<div class="kpi"><div class="l">{esc(label)}</div><div class="v">{value}</div>{extra}<div class="s">{sub}</div></div>'

    return ('<div class="kpi-grid">'
            + card("💰 初始本金", fmt_money(cap), f"{esc(acct.get('start_date'))} 起算")
            + card("📈 目前總資產淨值", fmt_money(eq),
                   f'累計損益 {signed_html(pnl, pct=ret)}｜已實現 {signed_html(acct.get("realized_pnl", 0))}'
                   f'｜未實現 {signed_html(acct.get("unrealized_pnl", 0))}')
            + card("🎯 累計報酬率 vs 年底目標", signed_html(pct=ret),
                   f'110 萬達成 {goal["low_progress_pct"]:.0f}%｜120 萬達成 {goal["high_progress_pct"]:.0f}%｜剩 {goal["days_left"]} 天',
                   f'<div class="goal-bar"><div class="fill" style="width:{fill:.0f}%"></div>'
                   f'<div class="mark" style="left:{low_mark:.0f}%"></div></div>')
            + card("💵 可用現金 & 資金水位", fmt_money(acct.get("cash_balance", 0)),
                   f'現金 {goal["cash_pct"]:.1f}%｜持股 {100 - goal["cash_pct"]:.1f}%｜{len(acct.get("holdings") or [])} 檔')
            + card("📊 累計出手 & 勝率", f'{stats.get("trades", 0)} 筆', f'買 {stats.get("buys", 0)}｜賣 {stats.get("sells", 0)}｜勝率 {win}')
            + bench_card(card, ret, bench)
            + '</div>')


def bench_card(card, ret, bench):
    """📊 0050 台灣五十同期表現 vs 模擬室超額報酬（Alpha）。bench 來自 universe_pool.benchmark_0050()。"""
    b = bench or {}
    if b.get("return_pct") is None:
        return card("📊 0050 同期表現 vs Alpha", "N/A", esc(f"0050 報價暫時無法取得{('：' + b['error'][:40]) if b.get('error') else ''}"))
    alpha = ret - b["return_pct"]
    verdict = "跑贏 0050" if alpha > 0 else "落後 0050" if alpha < 0 else "與 0050 持平"
    return card("📊 0050 同期表現 vs 超額報酬 (Alpha)", signed_html(pct=alpha),
                f'0050 同期 {signed_html(pct=b["return_pct"])}（{esc(b["base_close"])}→{esc(b["last_close"])}）｜{verdict}'
                f'<br><span class="tl-time">{esc(b["base_date"])} 收盤起算・{esc(b.get("source", ""))}</span>')


def render_committee(meeting):
    """🏛️ 最新五人投決會：四位成員發言＋巴董事長 challenge 與總指令（文字須已遮罩）。"""
    if not meeting:
        return '<p class="tl-src">尚未召開投決會。</p>'

    def box(role, label, text, cls=""):
        return (f'<div class="say {cls}"><div class="who">{mini_avatar(role, 30)}{label}</div>'
                f'<div>{esc(text or "（未發言）").replace(chr(10), "<br>")}</div></div>')

    m, boss = meeting.get("members") or {}, meeting.get("chairman") or {}
    reflection = f'<div class="directive" style="margin:0 0 10px">🔄 昨日落差覆盤：{esc(meeting.get("reflection") or "—")}</div>'
    return (reflection + '<div class="committee">'
            + box(5, "🦅 川大帝MAGA｜美股宏觀", m.get("maga"))
            + box(1, "🕵️‍♂️ 新聞達人｜營收法說題材", m.get("news"))
            + box(2, "🎧 籌碼王｜籌碼面", m.get("chips"))
            + box(3, "⚔️ K線技術人｜技術面", m.get("kline"))
            + box(4, "👑 巴董事長｜Challenge 與最終拍板",
                  f'【盲點挑戰】{boss.get("challenge", "")}\n【總體指令】{boss.get("summary", "")}', "boss")
            + '</div>')


# ---------------------------------------------------------------- 深度研究元件

def render_agent_pod_card(role_num, role_name, badge_text, badge_type, summary_html):
    """成員觀點卡；summary_html 須為已 escape 的 <li> 清單或段落。"""
    a = AGENTS[role_num]
    return (f'<div class="pod"><div class="pod-head"><span class="pod-title">{mini_avatar(role_num)}'
            f'{esc(a["nick"])}｜{esc(role_name)}</span>{pill(badge_text, badge_type)}</div>{summary_html}</div>')


def render_md_verdict_card(summary, privacy_unlocked):
    """巴董事長金邊裁決主卡：裁決徽章、信心圓環、價位格與 Red-Team 壓力測試盒。"""
    s = summary or {}
    conf = _f(s.get("confidence")) or 0
    verdict = s.get("verdict", "N/A")
    kind = {"強力買進": "bull", "分批布局": "bull", "持有觀望": "amber", "減碼": "bear", "出場避險": "bear"}.get(verdict, "neutral")
    color = "#EF4444" if conf >= 70 else "#F59E0B" if conf >= 50 else "#10B981"
    zone = s.get("entry_zone") or []
    zone_txt = f"{fmt_price(zone[0])} – {fmt_price(zone[1])}" if isinstance(zone, list) and len(zone) >= 2 else "N/A"

    def lv(label, value):
        return f'<div class="micro"><div class="lbl">{esc(label)}</div><div class="val">{esc(value)}</div></div>'

    advice = s.get("position_advice")
    advice_html = (f'<div class="say boss" style="margin-top:10px"><div class="who">💼 持倉部位建議</div>'
                   f'{esc(_mask(advice, privacy_unlocked))}</div>') if advice else ""
    return (
        '<div class="md-card"><div class="md-grid">'
        f'<div class="ring" style="background: conic-gradient({color} {conf * 3.6:.0f}deg, #1F2937 0deg)">'
        f'<div class="ring-inner"><div><div class="n">{conf:.0f}</div><div class="t">信心度</div></div></div></div>'
        f'<div><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
        f'<span class="pod-title" style="font-size:1.05rem">{mini_avatar(4, 34)}巴董事長最終裁決</span>'
        f'{pill(verdict, kind, "big-badge")}'
        f'<span class="tl-time">更新 {esc(s.get("updated_at", "N/A"))}｜{esc(s.get("mode", ""))}</span></div>'
        f'<div class="levels">{lv("📥 建議買進價", zone_txt)}{lv("🛑 停損價", fmt_price(s.get("stop_loss")))}'
        f'{lv("🎯 目標 1", fmt_price(s.get("target_1")))}{lv("🎯 目標 2", fmt_price(s.get("target_2")))}'
        f'{lv("⚖️ 風報比", s.get("risk_reward_ratio", "N/A"))}</div>'
        f'<div class="say" style="margin-top:10px"><div class="who">⚖️ 成員衝突仲裁</div>'
        f'{esc(_mask(s.get("conflict_point", "N/A"), privacy_unlocked))}</div>'
        f'</div></div>'
        f'<div class="redteam">🔥 <b>Red-Team 反向壓力測試</b>：'
        f'{esc(_mask(s.get("red_team_warning", "N/A"), privacy_unlocked))}</div>'
        f'{advice_html}</div>'
    )


def render_entry_alert_card(hits):
    lines = "".join(
        f'<div>🎯 <b>{esc(r["stock_id"])} {esc(r.get("name"))}</b>　現價 <span class="num">{esc(fmt_price(r.get("price")))}</span>'
        f'（{signed_html(pct=r.get("change_pct"))}）｜建議買進價 {esc(fmt_price(r.get("entry_low")))}–{esc(fmt_price(r.get("entry_high")))}'
        f'｜五檔比 {esc(r.get("bid_ask_ratio"))}｜停損 {esc(fmt_price(r.get("stop_loss")))}｜{esc(r.get("signal_hint"))}</div>'
        for r in hits)
    return (f'<div class="alert-card">🔔 <b>盤中買進通知：{len(hits)} 檔半導體龍頭現價已達建議買進價</b>'
            f'{lines}</div>')


def render_news_timeline(items):
    rows = "".join(
        f'<div class="tl-item"><div class="tl-time">{esc(n.get("published_at", "")[5:])}　'
        f'<span class="tl-src">{esc(n.get("source"))}</span></div>'
        f'<div class="tl-title"><a href="{esc(_safe_link(n.get("link")))}" target="_blank" rel="noopener">'
        f'{esc(n.get("title"))}</a></div></div>'
        for n in items)
    return f'<div class="timeline">{rows}</div>'
