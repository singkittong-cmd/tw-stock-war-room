# -*- coding: utf-8 -*-
"""☁️ GitHub 雲端帳本同步 + 單一帳本防衝突機制。

一、誰負責交易（AUTOPILOT_MODE，全世界永遠只有一個交易執行者）
- `cloud_master`（雲端上線後建議）：雲端唯一執行交易。
  · GitHub Actions `--once`：每次排程都檢查一次報價與進出場（Streamlit 休眠也照跑）。
  · Streamlit Cloud 伺服器內的 SimRoomDaemon：有人打開網頁、伺服器醒著時，額外每 15 秒盯盤。
  · 兩者交易前都先取回 GitHub 帳本、成交後比對再寫回；盤中沒成交不 commit。
  · 本機電腦只當觀看端：背景每 30 秒從 GitHub 拉回 reports/simulation，禁止任何模擬下單。
- `local_master`：本機 dashboard 的 SimRoomDaemon 負責交易；Streamlit Cloud 與 GitHub Actions 只看不下單。
- 未設定時：Streamlit Cloud／GitHub Actions 預設 cloud_master，本機預設 local_master（尚未上雲端前的行為）。

二、交易前強制同步、交易後立刻寫回
- 交易前：`pull_ledger_before_trade()` 從 GitHub 取回最新 sim_account／sim_trades／sim_nav_history，記下 `last_trade_timestamp`。
- 交易後：`publish_ledger_after_trade()` 先確認 GitHub 上的 `last_trade_timestamp` 沒被別人改過，才推回；
  若有人搶先交易，放棄本次結果、改用 GitHub 版本，確保只有一本 100 萬模擬帳本。

三、推送與拉取方式
- 本機與 GitHub Actions：git（`git fetch` + `git checkout FETCH_HEAD -- <資料檔>`；commit 只含資料路徑）。
- Streamlit Cloud：GitHub REST API（需在 Secrets 設定 GITHUB_REPO 與可寫入的 GITHUB_TOKEN），
  以 Git Data API 一次 commit 多個檔案，只推送本機有改動的檔案。

用法：
    C:/Users/singk/anaconda3/python.exe tools/github_memory_sync.py --status
    C:/Users/singk/anaconda3/python.exe tools/github_memory_sync.py --pull [--sim-only]
    C:/Users/singk/anaconda3/python.exe tools/github_memory_sync.py --push -m "訊息"
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

try:
    from tools import privacy_guard
except ImportError:  # 直接以腳本執行時
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import privacy_guard

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "reports" / ".github_sync_state.json"
TIME_FMT = "%Y-%m-%d %H:%M:%S"
# 雲端會寫入、需要同步的資料（相對 repo 根目錄）；個人持股相關檔案全部在 .gitignore 排除、不在此清單
SYNC_PATHS = [
    "reports/simulation", "reports/learning_db", "reports/news", "reports/us_market", "reports/memory",
    "reports/market_close_summary.json", "reports/watchlist_standby.json", "reports/watchlist_radar_live.json",
    "reports/sector_watchlist.json", "reports/universe_pool_scores.json",
    "reports/cloud_autopilot_state.json",
]
SIM_PATHS = ["reports/simulation"]
MIRROR_PATHS = SIM_PATHS + ["reports/cloud_autopilot_state.json"]  # 觀看端每 30 秒拉回：帳本、雲端心跳、Actions 執行摘要
LEDGER_FILES = ["reports/simulation/sim_account.json", "reports/simulation/sim_trades.json",
                "reports/simulation/sim_nav_history.json"]
ACCOUNT_REL = LEDGER_FILES[0]
TRADE_PUSH_PATHS = SIM_PATHS + ["reports/learning_db"]  # 實際成交後才 commit：帳本、成交紀錄、學習資料庫
# Streamlit 交易主機自己產生的資料；其餘（新聞、美股早報、記憶庫、明日建議買進價）由 GitHub Actions 產生
TRADER_OWNED = ["reports/simulation", "reports/learning_db", "reports/watchlist_radar_live.json"]
ACTIONS_OWNED = [p for p in SYNC_PATHS if p not in TRADER_OWNED]
# 管理者每日深度覆盤後的操盤規則與診斷書（tools/strategy_optimizer.py）：交易主機每 10 分鐘拉回，投決會即時套用
STRATEGY_PATHS = ["reports/simulation/strategy_rules.json", "reports/simulation/strategy_review_latest.json"]
SKIP_RE = re.compile(r"(\.lock|\.tmp|daemon_command\.json)$")
PULL_EVERY_SEC = 120
MIRROR_EVERY_SEC = 30
BOT_NAME, BOT_EMAIL = "github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com"
UA = "tw-stock-war-room-sync"

_state_lock = threading.RLock()


def _now():
    return datetime.now().strftime(TIME_FMT)


def _read_state():
    with _state_lock:
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}


def _write_state(state):
    with _state_lock:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(f"{STATE_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)


def _update_state(**changes):
    with _state_lock:
        state = _read_state()
        baseline = changes.pop("baseline", None)
        state.update(changes)
        if baseline:
            state.setdefault("baseline", {}).update(baseline)
        _write_state(state)
        return state


def _git(*args, timeout=90):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    for _ in range(10):  # Windows：dashboard 正在讀檔時 replace 可能短暫失敗
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    tmp.unlink(missing_ok=True)
    raise PermissionError(f"無法寫入 {path}")


# ---------------------------------------------------------------- 角色判斷

def where():
    """目前在哪裡執行：github_actions／streamlit_cloud／local。"""
    if os.getenv("GITHUB_ACTIONS") == "true":
        return "github_actions"
    if str(ROOT).replace("\\", "/").startswith("/mount/src"):  # Streamlit Community Cloud 的程式碼路徑
        return "streamlit_cloud"
    return "local"


def autopilot_mode():
    """cloud_master（雲端唯一交易）或 local_master（本機唯一交易）。舊值 github/cloud 視同 cloud_master。"""
    m = (os.getenv("AUTOPILOT_MODE") or "").strip().lower()
    if m in ("cloud_master", "github", "cloud"):
        return "cloud_master"
    if m in ("local_master", "local"):
        return "local_master"
    return "local_master" if where() == "local" else "cloud_master"


def is_trader_here():
    """這台主機是否有權寫入模擬帳本（下單、結算）。
    Streamlit Cloud 沒有有效 GITHUB_TOKEN 時成交寫不回 GitHub（會被下一次拉帳本蓋掉），自動降為觀看端，交易交給 GitHub Actions。"""
    if where() == "streamlit_cloud" and not _token():
        return False
    return (where() == "local") == (autopilot_mode() == "local_master")


def cloud_mode():
    """雲端主控（cloud_master）：畫面用雲端用語；本機在此模式下只看不交易。"""
    return autopilot_mode() == "cloud_master"


def sync_enabled():
    return cloud_mode() or os.getenv("GITHUB_SYNC", "").strip() == "1"


def has_git_repo():
    return (ROOT / ".git").exists()


REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# GitHub token 格式：classic ghp_、fine-grained github_pat_、OAuth gho_、App ghs_／ghu_、舊式 40 碼十六進位
TOKEN_RE = re.compile(r"^(?:(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|[0-9a-f]{40})$")


def repo_slug():
    """GITHUB_REPO（或 Actions 的 GITHUB_REPOSITORY）必須是「帳號/專案」的純 ASCII 格式；
    不合格（空白、中文預留字、網址）時改用 git remote origin 的網址解析。"""
    for raw in (os.getenv("GITHUB_REPO"), os.getenv("GITHUB_REPOSITORY")):
        slug = (raw or "").strip().removesuffix(".git")
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)$", slug)
        slug = m.group(1) if m else slug
        if slug.isascii() and REPO_RE.match(slug):
            return slug
    if has_git_repo():
        url = _git("remote", "get-url", "origin").stdout.strip()
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
        if m and REPO_RE.match(m.group(1)):
            return m.group(1)
    return ""


def branch_name():
    b = os.getenv("GITHUB_BRANCH") or os.getenv("GITHUB_REF_NAME") or ""
    if not b and has_git_repo():
        b = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return b if b and b != "HEAD" else "main"


def token_status():
    """回傳 ("ok"|"missing"|"invalid", 說明)。說明不含 token 內容。"""
    raw = (os.getenv("GITHUB_TOKEN") or "").strip()
    if not raw:
        return "missing", "未設定 GITHUB_TOKEN"
    if not raw.isascii():
        return "invalid", "GITHUB_TOKEN 含中文或非 ASCII 字元（可能還是範本預留字），已忽略、不放進 HTTP 標頭"
    if not TOKEN_RE.match(raw):
        return "invalid", "GITHUB_TOKEN 格式不對（應為 github_pat_… 或 ghp_… 開頭），已忽略"
    return "ok", "GITHUB_TOKEN 格式正確"


def _token():
    """只回傳通過驗證的 token；空白、中文預留字或格式錯誤一律視為沒有 token（避免 requests 以 latin-1 編碼標頭時崩潰）。"""
    return (os.getenv("GITHUB_TOKEN") or "").strip() if token_status()[0] == "ok" else ""


def git_fetch_usable():
    """git 能不能拿來拉取：要有 .git；Streamlit Cloud 容器的 remote 是 SSH（git@github.com:…）且沒有金鑰，
    git fetch 必定「Host key verification failed」，視為不可用（不再嘗試，也不顯示這個假警報）。"""
    if not has_git_repo():
        return False
    if where() == "streamlit_cloud":
        url = _git("remote", "get-url", "origin").stdout.strip()
        return url.startswith("https://")
    return True


def use_api():
    """Streamlit Cloud 一律用 REST API（雲端容器的 git 沒有推送權限）；沒有 git repo 時也用 API。"""
    return where() == "streamlit_cloud" or not has_git_repo()


def can_sync():
    return (has_git_repo() and not use_api()) or bool(repo_slug())


def ledger_sync_active():
    """雲端主控且本機是交易者時，每次交易前後都要與 GitHub 同步帳本。"""
    return cloud_mode() and is_trader_here() and can_sync()


def _tracked(rel, paths=None):
    paths = paths or SYNC_PATHS
    return any(rel == p or rel.startswith(p + "/") for p in paths) and not SKIP_RE.search(rel)


def _blob_sha(path):
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _local_files(paths):
    out = []
    for p in paths:
        full = ROOT / p
        files = [full] if full.is_file() else (sorted(f for f in full.rglob("*") if f.is_file()) if full.exists() else [])
        out += [f.relative_to(ROOT).as_posix() for f in files]
    return [r for r in out if _tracked(r, paths)]


# ---------------------------------------------------------------- GitHub REST API

def _headers(raw=False):
    h = {"Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json", "User-Agent": UA}
    if _token():
        h["Authorization"] = f"Bearer {_token()}"
    return h


def _api(method, path, **kwargs):
    repo = repo_slug()
    if not repo:
        raise RuntimeError("找不到 GitHub repo（請設定 GITHUB_REPO=帳號/專案名稱）")
    r = requests.request(method, f"https://api.github.com/repos/{repo}{path}", headers=kwargs.pop("headers", _headers()),
                         timeout=kwargs.pop("timeout", 30), **kwargs)
    if r.status_code == 404:
        raise RuntimeError("GitHub API 404：私人 repo 請設定 GITHUB_TOKEN")
    if r.status_code in (401, 403):
        raise RuntimeError(f"GitHub API {r.status_code}：GITHUB_TOKEN 無效或權限不足（{r.text[:120]}）")
    return r


def _api_head():
    r = _api("GET", f"/git/ref/heads/{branch_name()}")
    r.raise_for_status()
    return r.json()["object"]["sha"]


def _api_tree(commit_sha):
    c = _api("GET", f"/git/commits/{commit_sha}")
    c.raise_for_status()
    t = _api("GET", f"/git/trees/{c.json()['tree']['sha']}", params={"recursive": "1"})
    t.raise_for_status()
    return c.json()["tree"]["sha"], {e["path"]: e["sha"] for e in t.json().get("tree") or [] if e.get("type") == "blob"}


# ---------------------------------------------------------------- 拉取

def _pull_git(branch, paths):
    r = _git("fetch", "-q", "origin", branch)
    if r.returncode:
        raise RuntimeError(f"git fetch 失敗：{r.stderr.strip()[:200]}")
    sha = _git("rev-parse", "FETCH_HEAD").stdout.strip()
    remote = {}
    for line in _git("ls-tree", "-r", "FETCH_HEAD", "--", *paths).stdout.splitlines():
        meta, _, rel = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob" and _tracked(rel, paths):
            remote[rel] = parts[2]
    changed = [rel for rel, s in remote.items() if not (ROOT / rel).exists() or _blob_sha(ROOT / rel) != s]
    if changed:
        r = _git("checkout", "FETCH_HEAD", "--", *changed)
        if r.returncode:
            raise RuntimeError(f"git checkout 失敗：{r.stderr.strip()[:200]}")
    return sha, len(changed), remote


def _pull_api(paths):
    sha = _api_head()
    _, tree = _api_tree(sha)
    remote = {rel: s for rel, s in tree.items() if _tracked(rel, paths)}
    changed = 0
    for rel, s in remote.items():
        local = ROOT / rel
        if local.exists() and _blob_sha(local) == s:
            continue  # 內容相同不下載
        raw = _api("GET", f"/contents/{rel}", params={"ref": sha}, headers=_headers(raw=True))
        raw.raise_for_status()
        _atomic_write(local, raw.content)
        changed += 1
    return sha, changed, remote


def pull_latest(force=False, paths=None, scope="all", every=PULL_EVERY_SEC):
    """從 GitHub 拉回資料檔（只覆蓋資料，不動程式碼）。force=False 時依 scope 節流。回傳同步狀態 dict。"""
    paths = paths or SYNC_PATHS
    state = _read_state()
    if not force:
        if not sync_enabled():
            return {**state, "skipped": "本機主控模式不自動同步"}
        if time.time() - (state.get("last_check_epoch_" + scope) or 0) < every:
            return state
    branch = branch_name()
    info = {"last_check_epoch_" + scope: time.time(), "checked_at": _now(), "repo": repo_slug(), "branch": branch,
            "scope": scope}
    errors = []
    # 讀取順序：Streamlit Cloud 有有效 token 時先用 API；沒有（或 token 是預留字）時先用容器內建的 git clone 拉取，
    # API 失敗也會改用 git，git 失敗再試 API（公開 repo 不需 token）
    if use_api() and _token():
        methods = ("api",)  # 有有效 token：只用 API，不做 git 備援（避免顯示 git fetch 假警報）
    else:
        methods = ("git", "api") if git_fetch_usable() else ("api",)
    if token_status()[0] == "invalid":
        errors.append(f"token：{token_status()[1]}")
    if use_api() and not _token() and not git_fetch_usable():
        return _update_state(**info, ok=False, error=f"{token_status()[1]}；Streamlit Cloud 的 git 是 SSH 且沒有金鑰、無法拉取，"
                                                     "請在 Streamlit Secrets 填入有效的 GITHUB_TOKEN（github_pat_…）")
    for method in methods:
        try:
            sha, n, remote = _pull_git(branch, paths) if method == "git" else _pull_api(paths)
            return _update_state(**info, ok=True, method=method, commit=sha[:7], files=n, synced_at=_now(),
                                 error=None, warning="；".join(errors)[:300] or None, baseline=remote)
        except Exception as exc:  # git 失敗改用 API；都失敗時保留本機檔案
            errors.append(f"{method}：{exc}")
    return _update_state(**info, ok=False, error="；".join(errors)[:400])


def sync_status():
    return _read_state()


def remote_text(rel):
    """GitHub 分支最新版的單一檔案內容（不寫入本機）；找不到時回傳 None。"""
    if use_api() and _token():
        try:
            r = _api("GET", f"/contents/{rel}", params={"ref": branch_name()}, headers=_headers(raw=True))
            if r.status_code == 200:
                return r.content.decode("utf-8")
        except (RuntimeError, requests.RequestException):
            return None  # 有 token 時只信任 API，不做 git 備援
        return None
    if git_fetch_usable():
        branch = branch_name()
        if _git("fetch", "-q", "origin", branch).returncode == 0:
            r = _git("show", f"FETCH_HEAD:{rel}")
            return r.stdout if r.returncode == 0 else None
    if not _token():
        r = _api("GET", f"/contents/{rel}", params={"ref": branch_name()}, headers=_headers(raw=True))  # 公開 repo
        return r.content.decode("utf-8") if r.status_code == 200 else None
    return None


# ---------------------------------------------------------------- 推送

def commit_and_push(message, paths=None, retries=3):
    """git：add 資料路徑 → 只 commit 這些路徑 → pull --rebase（衝突以本次為準）→ push。沒有變動時不 commit。"""
    if not has_git_repo():
        return {"pushed": False, "reason": "不是 git repo"}
    paths = [p for p in (paths or SYNC_PATHS) if (ROOT / p).exists()]
    if not paths:
        return {"pushed": False, "reason": "沒有資料檔"}
    privacy_guard.split_premarket_alert()  # 持股開盤叮嚀搬到不進 git 的私人檔
    _git("add", "-A", "--", *paths)  # 鎖檔、暫存檔與指令檔由 .gitignore 排除
    staged = [f for f in _git("diff", "--cached", "--name-only", "--", *paths).stdout.splitlines() if f.strip()]
    leaks = privacy_guard.find_leaks(staged)
    if leaks:  # 含真實持股股數／成本的檔案一律不推送
        _git("reset", "-q", "--", *leaks)
        _update_state(privacy_blocked=leaks, privacy_blocked_at=_now())
        print(f"🔒 含真實持股資料，略過不推送：{', '.join(leaks)}")
    files = [f for f in staged if f not in leaks]
    if not files:
        return {"pushed": False, "reason": "沒有變動" if not leaks else f"只剩含真實持股的檔案，已略過：{leaks}"}
    ident = []
    if not _git("config", "user.email").stdout.strip():
        ident = ["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]
    r = _git(*ident, "commit", "-q", "-m", message, "--", *files)  # 只 commit 這些資料檔，不會帶到其他已暫存的程式碼
    if r.returncode:
        return {"pushed": False, "reason": f"commit 失敗：{r.stderr.strip()[:200]}"}
    branch = branch_name()
    last = ""
    for _ in range(retries):
        pr = _git(*ident, "pull", "-q", "--rebase", "--autostash", "-X", "theirs", "origin", branch)
        if pr.returncode:
            _git("rebase", "--abort")
            last = pr.stderr.strip()[:200]
        ps = _git("push", "-q", "origin", f"HEAD:{branch}")
        if ps.returncode == 0:
            return {"pushed": True, "method": "git", "commit": _git("rev-parse", "--short", "HEAD").stdout.strip()}
        last = ps.stderr.strip()[:200]
        time.sleep(3)
    return {"pushed": False, "reason": f"push 失敗：{last}"}


def push_api(message, paths=None, retries=3):
    """REST API：只推送「本機內容 ≠ 上次同步基準」的檔案，以 Git Data API 一次 commit。"""
    if not _token():
        state, why = token_status()
        return {"pushed": False, "reason": f"{why}：Streamlit Cloud 寫回 GitHub 需要在 Secrets 填入可寫入 Contents 的 "
                                           f"fine-grained token（github_pat_…）" if state != "ok" else why}
    paths = paths or SYNC_PATHS
    baseline = _read_state().get("baseline") or {}
    shas = {rel: _blob_sha(ROOT / rel) for rel in _local_files(paths)}
    changed = [rel for rel, s in shas.items() if baseline.get(rel) != s]
    leaks = privacy_guard.find_leaks(changed)
    if leaks:
        _update_state(privacy_blocked=leaks, privacy_blocked_at=_now())
        changed = [rel for rel in changed if rel not in leaks]
    if not changed:
        return {"pushed": False, "reason": "沒有變動"}
    entries = []
    for rel in changed:
        b = _api("POST", "/git/blobs", json={"content": base64.b64encode((ROOT / rel).read_bytes()).decode(),
                                             "encoding": "base64"}, timeout=60)
        b.raise_for_status()
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": b.json()["sha"]})
    last = ""
    for _ in range(retries):
        head = _api_head()
        base_tree, _ = _api_tree(head)
        t = _api("POST", "/git/trees", json={"base_tree": base_tree, "tree": entries})
        t.raise_for_status()
        c = _api("POST", "/git/commits", json={"message": message, "tree": t.json()["sha"], "parents": [head]})
        c.raise_for_status()
        u = _api("PATCH", f"/git/refs/heads/{branch_name()}", json={"sha": c.json()["sha"], "force": False})
        if u.status_code == 200:
            _update_state(baseline={rel: shas[rel] for rel in changed}, pushed_at=_now(), push_commit=c.json()["sha"][:7])
            return {"pushed": True, "method": "api", "commit": c.json()["sha"][:7], "files": len(changed)}
        last = f"HTTP {u.status_code} {u.text[:120]}"  # 期間有人 push：以最新 HEAD 重建 commit
        time.sleep(2)
    return {"pushed": False, "reason": f"API push 失敗：{last}"}


def publish(message, paths=None):
    """依環境自動選 git 或 REST API 推送；任何錯誤都回傳 dict，不拋例外。"""
    try:
        return push_api(message, paths) if use_api() else commit_and_push(message, paths)
    except Exception as exc:
        return {"pushed": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}


# ---------------------------------------------------------------- 單一帳本防衝突

def _local_trade_ts():
    try:
        return json.loads((ROOT / ACCOUNT_REL).read_text(encoding="utf-8")).get("last_trade_timestamp")
    except (OSError, json.JSONDecodeError):
        return None


def pull_ledger_before_trade():
    """交易前：從 GitHub 取回最新模擬帳本，回傳 {"ok", "base_ts", ...}。未啟用雲端同步時直接放行。"""
    if not ledger_sync_active():
        return {"ok": True, "skipped": True, "base_ts": _local_trade_ts()}
    local_ts = _local_trade_ts()
    try:
        txt = remote_text(ACCOUNT_REL)
    except Exception as exc:
        return {"ok": False, "error": f"讀取 GitHub 帳本失敗：{exc}"[:300], "base_ts": local_ts}
    if txt is None:  # GitHub 上還沒有帳本（第一次上線）：以本機為準
        return {"ok": True, "pulled": False, "base_ts": local_ts, "remote_missing": True}
    try:
        remote_ts = json.loads(txt).get("last_trade_timestamp")
    except json.JSONDecodeError:
        remote_ts = "unreadable"
    if remote_ts == local_ts:  # 本機已是最新一筆交易：不覆寫（保留本機較新的盤中報價）
        return {"ok": True, "pulled": False, "base_ts": local_ts}
    r = pull_latest(force=True, paths=LEDGER_FILES, scope="ledger")
    return {"ok": bool(r.get("ok")), "pulled": True, "error": r.get("error"), "commit": r.get("commit"),
            "base_ts": _local_trade_ts(), "remote_ts": remote_ts}


def publish_ledger_after_trade(base_ts, message, paths=None):
    """交易後：GitHub 上的 last_trade_timestamp 仍是交易前那一版才推送；被別人搶先時放棄本次結果、改用 GitHub 版本。"""
    if not ledger_sync_active():
        return {"pushed": False, "reason": "未啟用雲端帳本同步"}
    try:
        txt = remote_text(ACCOUNT_REL)
        remote_ts = json.loads(txt).get("last_trade_timestamp") if txt else None
    except Exception as exc:
        return {"pushed": False, "reason": f"讀取 GitHub 帳本失敗，稍後由定期同步補推：{exc}"[:300]}
    if txt is not None and remote_ts != base_ts:
        pull_latest(force=True, paths=LEDGER_FILES, scope="ledger")
        return {"pushed": False, "conflict": True,
                "reason": f"GitHub 帳本已被其他執行者更新（{remote_ts}），本次交易結果作廢、改用 GitHub 版本"}
    return publish(message, paths or SIM_PATHS)


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", encoding="utf-8")
    parser = argparse.ArgumentParser(description="☁️ GitHub 雲端帳本同步")
    parser.add_argument("--pull", action="store_true", help="立即從 GitHub 拉取最新資料")
    parser.add_argument("--sim-only", action="store_true", help="搭配 --pull/--push：只處理 reports/simulation")
    parser.add_argument("--push", action="store_true", help="commit + push 資料檔")
    parser.add_argument("-m", "--message", default=None)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    paths = SIM_PATHS if args.sim_only else None
    if args.pull:
        print(json.dumps({k: v for k, v in pull_latest(force=True, paths=paths).items() if k != "baseline"},
                         ensure_ascii=False, indent=2))
    if args.push:
        print(json.dumps(publish(args.message or f"🤖 autopilot：雲端帳本更新 {_now()}", paths), ensure_ascii=False))
    if args.status or not (args.pull or args.push):
        print(json.dumps({"where": where(), "autopilot_mode": autopilot_mode(), "trader_here": is_trader_here(),
                          "ledger_sync_active": ledger_sync_active(), "repo": repo_slug(), "branch": branch_name(),
                          "git_repo": has_git_repo(), "use_api": use_api(), "token": token_status()[1],
                          **{k: v for k, v in sync_status().items() if k != "baseline"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
