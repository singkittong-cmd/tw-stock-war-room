# -*- coding: utf-8 -*-
"""多模型免費路由器：日常分析走免費 Google Gemini／Groq，只有使用者指定時才動用 Claude（單次、無工具）。

用法：
    C:/Users/singk/anaconda3/python.exe tools/llm_router.py --test     # 顯示各引擎金鑰狀態並各送一句測試

.env 設定：
    GEMINI_API_KEY=...             Google AI Studio 免費金鑰（https://aistudio.google.com/apikey）
    GROQ_API_KEY=...               Groq 免費金鑰（選填，https://console.groq.com/keys）
    DEFAULT_LLM_PROVIDER=gemini    gemini / groq / claude_lite
    GEMINI_MODEL=gemini-3.5-flash  主模型（gemini-2.5-flash 已不開放給新申請的金鑰）
    GEMINI_FALLBACK_MODELS=gemini-3.5-flash-lite,gemini-flash-lite-latest  主模型塞車／逾時／下架時依序備援
    GROQ_MODEL=llama-3.3-70b-versatile

provider：
- gemini：POST generativelanguage.googleapis.com（金鑰放 x-goog-api-key 標頭，不出現在網址與錯誤訊息）。
- groq：POST api.groq.com OpenAI 相容端點。
- claude_lite：`claude -p --tools ""`，停用所有工具與子代理，只回答 Python 預先濃縮好的數據。
- auto：DEFAULT_LLM_PROVIDER 優先，失敗時依序改用其他「有金鑰的免費引擎」；auto 不會自動動用 Claude。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
load_dotenv(ENV_PATH, encoding="utf-8")

try:
    # 改用 Windows 憑證庫驗證 HTTPS（防毒軟體 / 代理攔截 TLS 時 certifi 會驗證失敗）
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
PROVIDERS = ("gemini", "groq", "claude_lite")
FREE_PROVIDERS = ("gemini", "groq")
TIMEOUT_SEC = 90
RETRY_STATUS = (429, 500, 503)
RETRY_WAITS = (2, 5)
GEMINI_TIMEOUT_SEC = 60
MAX_OUTPUT_CAP = 16384
GEMINI_FALLBACKS = "gemini-3.5-flash-lite,gemini-flash-lite-latest"
CLAUDE_TIMEOUT_SEC = 300
ENV_DEFAULTS = {
    "GEMINI_API_KEY": "你的_Google_AI_Studio_免費_Key",
    "GROQ_API_KEY": "你的_Groq_免費_Key_選填",
    "DEFAULT_LLM_PROVIDER": "gemini",
    "GEMINI_MODEL": "gemini-3.5-flash",
    "GEMINI_FALLBACK_MODELS": GEMINI_FALLBACKS,
    "GROQ_MODEL": "llama-3.3-70b-versatile",
}


class LLMError(RuntimeError):
    pass


def _key(name):
    """回傳有效金鑰；空白或範本佔位字（你的_…／YOUR_…）視為未設定。"""
    v = (os.getenv(name) or "").strip()
    return "" if not v or v.startswith(("你的", "YOUR", "your")) else v


def has_key(provider):
    return {"gemini": bool(_key("GEMINI_API_KEY")), "groq": bool(_key("GROQ_API_KEY")), "claude_lite": True}.get(provider, False)


def _scrub(text):
    """錯誤訊息中移除任何金鑰字串，避免洩漏到畫面或日誌。"""
    text = str(text)
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY"):
        k = _key(name)
        if k:
            text = text.replace(k, "***")
    return re.sub(r"key=[\w\-]+", "key=***", text)[:400]


# ---------------------------------------------------------------- 各引擎

def gemini_models():
    """主模型＋備援模型鏈（.env GEMINI_MODEL、GEMINI_FALLBACK_MODELS，逗號分隔）。"""
    fallback = os.getenv("GEMINI_FALLBACK_MODELS", GEMINI_FALLBACKS)
    return list(dict.fromkeys([os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip()]
                              + [m.strip() for m in fallback.split(",") if m.strip()]))


def _thinking_config(model, level_ok=True):
    """思考 token 會吃掉 maxOutputTokens（實測 3.5-flash 預設思考 5,377 token 導致 JSON 被截斷）。
    2.5 系列用 thinkingBudget=0；3.x 用 thinkingLevel=low；模型不接受時（400）改為不帶。"""
    if model.startswith("gemini-2.5"):
        return {"thinkingBudget": 0}
    return {"thinkingLevel": "low"} if level_ok else None


def _gemini_post(model, body):
    # AIza… 與 AQ.… 金鑰都放在 x-goog-api-key 標頭（不拼在網址、不用 Bearer）
    headers = {"Content-Type": "application/json", "x-goog-api-key": _key("GEMINI_API_KEY")}
    for wait in RETRY_WAITS + (None,):
        resp = requests.post(GEMINI_URL.format(model=model), json=body, timeout=GEMINI_TIMEOUT_SEC, headers=headers)
        if resp.status_code not in RETRY_STATUS or wait is None:
            return resp
        time.sleep(wait)  # 503 高負載／429 限流多為暫時性，退避後重試
    return resp


def _gemini_once(model, system_prompt, user_prompt, temperature, json_mode, max_tokens):
    config = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if json_mode:
        config["responseMimeType"] = "application/json"  # 官方 JSON 模式（REST 欄位亦可寫 response_mime_type）
    thinking = _thinking_config(model)
    if thinking:
        config["thinkingConfig"] = thinking
    body = {"systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": config}
    resp = _gemini_post(model, body)
    if resp.status_code == 400 and "thinkingConfig" in config:  # 此模型不支援該思考參數 → 不帶再試
        config.pop("thinkingConfig")
        resp = _gemini_post(model, body)
    if resp.status_code != 200:
        raise LLMError(f"{model} HTTP {resp.status_code}：{_scrub(resp.text)}")
    data = resp.json()
    try:
        cand = data["candidates"][0]
        parts = cand["content"]["parts"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"{model} 無回覆內容：{_scrub(json.dumps(data, ensure_ascii=False))}") from exc
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if cand.get("finishReason") == "MAX_TOKENS" and max_tokens < MAX_OUTPUT_CAP:
        # 輸出被截斷（JSON 不完整）：放大額度重試一次
        return _gemini_once(model, system_prompt, user_prompt, temperature, json_mode, min(max_tokens * 2, MAX_OUTPUT_CAP))
    return text, model


def _gemini(system_prompt, user_prompt, temperature, json_mode, max_tokens):
    """依模型鏈嘗試：主模型塞車、逾時、下架（404）時自動改用下一個備援模型。"""
    errors = []
    for model in gemini_models():
        try:
            return _gemini_once(model, system_prompt, user_prompt, temperature, json_mode, max_tokens)
        except (LLMError, requests.RequestException) as exc:
            errors.append(_scrub(exc)[:160])
            if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
                break  # 金鑰問題換模型也沒用
    raise LLMError("Gemini 全部模型失敗｜" + "｜".join(errors))


def _groq(system_prompt, user_prompt, temperature, json_mode, max_tokens):
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    resp = requests.post(GROQ_URL, json=body, timeout=TIMEOUT_SEC,
                         headers={"Authorization": f"Bearer {_key('GROQ_API_KEY')}", "Content-Type": "application/json"})
    if resp.status_code != 200:
        raise LLMError(f"Groq HTTP {resp.status_code}：{_scrub(resp.text)}")
    return resp.json()["choices"][0]["message"]["content"], model


def _claude_lite(system_prompt, user_prompt, temperature, json_mode, max_tokens, timeout=CLAUDE_TIMEOUT_SEC, model=None):
    """單次 claude -p：`--tools ""` 停用全部工具（不能 Read 檔、不能派子代理），提示詞走 stdin。"""
    prompt = f"{system_prompt}\n\n{user_prompt}"
    if json_mode:
        prompt += "\n\n只輸出一個 JSON 物件，不要任何說明文字或 Markdown 圍欄。"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = 'claude -p --tools "" --output-format text'
    if model and re.fullmatch(r"[A-Za-z0-9.\-]+", model):  # 只接受模型代號字元，避免組進 shell 指令時被注入
        cmd += f" --model {model}"
    try:
        result = subprocess.run(cmd, input=prompt, shell=True,
                                encoding="utf-8", errors="replace", capture_output=True, cwd=str(ROOT), env=env,
                                timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"claude -p 逾時（>{timeout} 秒）") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise LLMError(f"claude -p 失敗（rc={result.returncode}）：{(result.stderr or result.stdout)[-300:]}")
    # 偶爾會在開頭多出一行 <tag> 雜訊，去掉後才回傳
    text = re.sub(r"\A(\s*</?[A-Za-z_]+>[^\n]*\n)+", "", result.stdout).strip()
    return text, "claude（單次、無工具）"


CALLERS = {"gemini": _gemini, "groq": _groq, "claude_lite": _claude_lite}


# ---------------------------------------------------------------- 👑 高階推理（管理者每日深度覆盤專用）
# HIGH_TIER_PROVIDER：gemini（預設）／anthropic（有 ANTHROPIC_API_KEY 時為預設）／claude_cli（本機已登入的 claude -p，0 額外 API 費用）
# HIGH_TIER_MODEL：覆寫該引擎的模型代號（例：gemini-2.5-pro、claude-sonnet-4-6、claude CLI 的 opus）
HIGH_TIER_PROVIDERS = ("gemini", "anthropic", "claude_cli")
# gemini-2.5-pro 已不開放新使用者（HTTP 404），預設改用 gemini-3.1-pro-preview；Pro 模型需要付費額度，免費金鑰會 429
HIGH_TIER_DEFAULT_MODEL = {"gemini": "gemini-3.1-pro-preview", "anthropic": "claude-sonnet-4-6", "claude_cli": ""}
# 3.1-pro 免費金鑰常 429 → 等 3 秒改用 Flash 深度思考。實測（2026-11-19）gemini-2.5-flash 與 2.5-pro 一樣回 404
# 「不再開放給新使用者」，故不列入預設鏈；gemini-3-flash-preview 實測 22 秒完成五人覆盤，gemini-3.5-flash 為最後備援。
# 舊帳號仍可用 2.5 系列時，可在 HIGH_TIER_MODEL 指定（2.5-flash 會自動帶 thinkingBudget 8192）。
HIGH_TIER_GEMINI_FALLBACKS = ("gemini-3-flash-preview", "gemini-3.5-flash")
HIGH_TIER_429_WAIT_SEC = 3
HIGH_TIER_TIMEOUT_SEC = 420          # 單一引擎上限（claude -p、Anthropic、Gemini Pro）
HIGH_TIER_FLASH_TIMEOUT_SEC = 180    # Flash 備援：深度思考偶爾卡住，不空等
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def high_tier_provider():
    """HIGH_TIER_PROVIDER 未設定時：有 ANTHROPIC_API_KEY → anthropic；這台有已登入的 claude 指令 → claude_cli；其餘 → gemini。"""
    p = (os.getenv("HIGH_TIER_PROVIDER") or "").strip().lower()
    if p in HIGH_TIER_PROVIDERS:
        return p
    if _key("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "claude_cli" if shutil.which("claude") else "gemini"


def high_tier_model(provider=None):
    provider = provider or high_tier_provider()
    custom = (os.getenv("HIGH_TIER_MODEL") or "").strip()
    return custom if custom and provider == high_tier_provider() else HIGH_TIER_DEFAULT_MODEL.get(provider, "")


def _gemini_high(model, system_prompt, user_prompt, json_mode, max_tokens):
    """Gemini 深度思考：2.5 系列給 thinkingBudget 16384，3.x 用 thinkingLevel=high；輸出額度放大、逾時 10 分鐘。"""
    config = {"temperature": 0.4, "maxOutputTokens": max_tokens}
    if json_mode:
        config["responseMimeType"] = "application/json"
    flash = "flash" in model
    if model.startswith("gemini-2.5"):  # 2.5-flash 深度思考 8192 token（免費額度內），2.5-pro 16384
        config["thinkingConfig"] = {"thinkingBudget": 8192 if flash else 16384}
    else:  # 3.x Flash 用 medium，避免深度思考卡住；Pro 用 high
        config["thinkingConfig"] = {"thinkingLevel": "medium" if flash else "high"}
    body = {"systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}], "generationConfig": config}
    headers = {"Content-Type": "application/json", "x-goog-api-key": _key("GEMINI_API_KEY")}
    timeout = HIGH_TIER_FLASH_TIMEOUT_SEC if flash else HIGH_TIER_TIMEOUT_SEC
    for attempt in range(3):
        resp = requests.post(GEMINI_URL.format(model=model), json=body, timeout=timeout, headers=headers)
        if resp.status_code == 400 and "thinkingConfig" in config:  # 模型不接受此思考參數 → 不帶再試
            config.pop("thinkingConfig")
            continue
        if resp.status_code == 429 and "quota" in resp.text.lower():
            break  # 額度用完（免費金鑰沒有 Pro 配額）：重試也沒用，直接換下一個模型
        if resp.status_code in RETRY_STATUS and attempt < 2:
            time.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
            continue
        break
    if resp.status_code != 200:
        raise LLMError(f"{model} HTTP {resp.status_code}：{_scrub(resp.text)}")
    data = resp.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"{model} 無回覆內容：{_scrub(json.dumps(data, ensure_ascii=False))}") from exc
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        raise LLMError(f"{model} 回覆為空（可能思考用盡輸出額度）")
    return text, model


def _anthropic_high(model, system_prompt, user_prompt, json_mode, max_tokens):
    """Anthropic Messages API（REST，不需安裝 SDK）＋延伸思考；模型不支援思考參數時改為不帶再試。"""
    if not _key("ANTHROPIC_API_KEY"):
        raise LLMError("未設定 ANTHROPIC_API_KEY")
    user = user_prompt + ("\n\n只輸出一個 JSON 物件，不要任何說明文字或 Markdown 圍欄。" if json_mode else "")
    body = {"model": model, "max_tokens": max_tokens, "system": system_prompt,
            "messages": [{"role": "user", "content": user}],
            "thinking": {"type": "enabled", "budget_tokens": max(1024, min(16000, max_tokens // 2))}}
    headers = {"x-api-key": _key("ANTHROPIC_API_KEY"), "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    for attempt in range(3):
        resp = requests.post(ANTHROPIC_URL, json=body, headers=headers, timeout=HIGH_TIER_TIMEOUT_SEC)
        if resp.status_code == 400 and "thinking" in body and "thinking" in resp.text:
            body.pop("thinking")
            continue
        if resp.status_code in (429, 500, 529) and attempt < 2:
            time.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)] * 2)
            continue
        break
    if resp.status_code != 200:
        raise LLMError(f"Anthropic {model} HTTP {resp.status_code}：{_scrub(resp.text)}")
    blocks = resp.json().get("content") or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    if not text.strip():
        raise LLMError(f"Anthropic {model} 回覆為空")
    return text, model


def _claude_cli_high(model, system_prompt, user_prompt, json_mode, max_tokens):
    if not shutil.which("claude"):
        raise LLMError("這台主機找不到 claude 指令（claude_cli 只能在已登入 Claude Code 的本機使用）")
    text, _ = _claude_lite(system_prompt, user_prompt, 0.4, json_mode, max_tokens, timeout=HIGH_TIER_TIMEOUT_SEC,
                           model=model or None)
    return text, f"claude -p（本機{('・' + model) if model else ''}）"


HIGH_TIER_CALLERS = {"gemini": _gemini_high, "anthropic": _anthropic_high, "claude_cli": _claude_cli_high}


def call_high_tier_reasoning(prompt, system_prompt="你是嚴謹的機構操盤檢討會主持人。", json_mode=True, max_tokens=24000):
    """👑 高階推理：依 HIGH_TIER_PROVIDER 選引擎，開啟深度思考與大輸出額度；失敗時依序改用 Gemini 高階模型鏈。
    回傳 {"text", "provider", "model", "elapsed_sec", "errors"}；全部失敗丟出 LLMError。"""
    started = time.perf_counter()
    first = high_tier_provider()
    plan = [(first, high_tier_model(first))]
    if first == "gemini":
        plan += [("gemini", m) for m in HIGH_TIER_GEMINI_FALLBACKS if m != plan[0][1]]
    elif has_key("gemini"):
        plan += [("gemini", HIGH_TIER_DEFAULT_MODEL["gemini"])] + [("gemini", m) for m in HIGH_TIER_GEMINI_FALLBACKS]
    errors = []
    for provider, model in plan:
        if provider == "gemini" and not has_key("gemini"):
            errors.append("gemini：未設定金鑰")
            continue
        try:
            text, used = HIGH_TIER_CALLERS[provider](model, system_prompt, prompt, json_mode, max_tokens)
            return {"text": text, "provider": provider, "model": used, "errors": errors,
                    "elapsed_sec": round(time.perf_counter() - started, 1)}
        except (LLMError, requests.RequestException, OSError) as exc:
            errors.append(f"{provider}/{model or 'default'}：{_scrub(exc)[:200]}")
            if "HTTP 429" in str(exc):
                time.sleep(HIGH_TIER_429_WAIT_SEC)  # 限流：稍等再換下一個模型
    raise LLMError("高階推理全部引擎失敗｜" + "｜".join(errors))


def _order(provider):
    if provider in PROVIDERS and provider != "auto":
        # 指定免費引擎失敗時，可改用另一個有金鑰的免費引擎；指定 Claude 則只用 Claude
        return [provider] + ([p for p in FREE_PROVIDERS if p != provider] if provider in FREE_PROVIDERS else [])
    default = os.getenv("DEFAULT_LLM_PROVIDER", "gemini")
    first = [default] if default in PROVIDERS else []
    return list(dict.fromkeys(first + list(FREE_PROVIDERS)))


def call_llm_ex(system_prompt, user_prompt, provider="auto", temperature=0.3, json_mode=False, max_tokens=4096):
    """回傳 {"text", "provider", "model", "elapsed_sec", "errors"}；全部引擎失敗時丟出 LLMError。"""
    errors = []
    started = time.perf_counter()
    for p in _order(provider):
        if not has_key(p):
            errors.append(f"{p}：未設定金鑰")
            continue
        try:
            text, model = CALLERS[p](system_prompt, user_prompt, temperature, json_mode, max_tokens)
            return {"text": text, "provider": p, "model": model, "elapsed_sec": round(time.perf_counter() - started, 2),
                    "errors": errors}
        except (LLMError, requests.RequestException, ValueError, KeyError) as exc:
            errors.append(f"{p}：{_scrub(exc)}")
    raise LLMError("所有引擎皆失敗｜" + "｜".join(errors))


def call_llm(system_prompt: str, user_prompt: str, provider: str = "auto", temperature: float = 0.3) -> str:
    return call_llm_ex(system_prompt, user_prompt, provider, temperature)["text"]


_json_repair = None


def _load_json_repair():
    """載入 json_repair；未安裝時以目前直譯器（Anaconda）自動 pip install 一次。失敗回傳 None。"""
    global _json_repair
    if _json_repair is None:
        try:
            import json_repair
        except ImportError:
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", "-q", "json-repair"], capture_output=True, timeout=180)
                import json_repair
            except (ImportError, OSError, subprocess.TimeoutExpired):
                json_repair = False
        _json_repair = json_repair
    return _json_repair or None


def _heal_json(s):
    """內建修復：字串內未跳脫的換行/Tab 轉成 \\n、補上未閉合的字串與括號、移除結尾多餘逗號。"""
    out, stack, in_str, esc = [], [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            elif ch in "\n\r\t":
                out.append({"\n": "\\n", "\r": "", "\t": "\\t"}[ch])
                continue
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
        out.append(ch)
    text = "".join(out)
    if in_str:
        text += '"'
    text = re.sub(r",\s*$", "", text.rstrip())
    text = re.sub(r",(\s*[}\]])", r"\1", text + "".join(reversed(stack)))
    return text


def safe_parse_json(raw_text):
    """三層防禦解析：① json.loads → ② 萃取 ```json``` 圍欄或最外層 {…} → ③ json_repair／內建修復。
    回傳 dict 或 list；全部失敗才丟出 ValueError（呼叫端會換引擎重試，不會讓戰情室當機）。"""
    text = (raw_text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)(?:```|\Z)", text, flags=re.S)
    candidate = m.group(1).strip() if m else text
    a = candidate.find("{")
    if a < 0:
        a = candidate.find("[")
    b = max(candidate.rfind("}"), candidate.rfind("]"))
    for s in dict.fromkeys([candidate, candidate[a:b + 1] if a >= 0 and b > a else candidate]):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            continue
    body = candidate[a:] if a >= 0 else candidate
    repair = _load_json_repair()
    if repair:
        try:
            obj = repair.loads(body)
            if isinstance(obj, (dict, list)) and obj:
                return obj
        except Exception:  # json_repair 本身失敗時改用內建修復
            pass
    try:
        return json.loads(_heal_json(body))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 三層修復皆失敗：{exc}（開頭：{text[:80]!r}）") from exc


parse_json = safe_parse_json  # 舊名稱相容


# ---------------------------------------------------------------- .env 維護

def ensure_env_defaults():
    """.env 缺少的 LLM 設定補上預設值（不覆蓋既有值），回傳新增的鍵。"""
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    missing = [k for k in ENV_DEFAULTS if not re.search(rf"^{k}=", text, flags=re.M)]
    if missing:
        block = "\n# 多模型免費路由器（tools/llm_router.py）：日常分析走免費 Gemini／Groq，Claude 僅巴董事長深潛用\n"
        block += "".join(f"{k}={ENV_DEFAULTS[k]}\n" for k in missing)
        ENV_PATH.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")
        for k in missing:
            os.environ.setdefault(k, ENV_DEFAULTS[k])
    return missing


def save_env_key(name, value):
    """把金鑰寫入 .env（覆寫同名行或新增）並立即生效。

    不限定開頭（AIza…、AQ.…、gsk_… 皆可）：只要求單行、無空白、長度 > 20 且不是範本佔位字。"""
    if name not in ("GEMINI_API_KEY", "GROQ_API_KEY"):
        raise ValueError("只能設定 GEMINI_API_KEY 或 GROQ_API_KEY")
    value = (value or "").strip()
    if len(value) <= 20 or re.search(r"\s", value) or value.startswith(("你的", "YOUR", "your")):
        raise ValueError("金鑰格式不正確（應為單行、無空白、長度大於 20 的金鑰字串）")
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    if re.search(rf"^{name}=.*$", text, flags=re.M):
        text = re.sub(rf"^{name}=.*$", lambda _: f"{name}={value}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{name}={value}\n"
    ENV_PATH.write_text(text, encoding="utf-8")
    os.environ[name] = value


def status():
    return {"default": os.getenv("DEFAULT_LLM_PROVIDER", "gemini"),
            "gemini": {"key": has_key("gemini"), "model": " → ".join(gemini_models())},
            "groq": {"key": has_key("groq"), "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")},
            "claude_lite": {"key": True, "model": "claude -p（單次、--tools \"\"）"}}


def main():
    parser = argparse.ArgumentParser(description="多模型免費路由器")
    parser.add_argument("--test", action="store_true", help="顯示金鑰狀態並各送一句測試")
    parser.add_argument("--with-claude", action="store_true", help="--test 時也測試 claude_lite（會消耗少量 Claude Token）")
    args = parser.parse_args()
    added = ensure_env_defaults()
    if added:
        print(f"已在 .env 補上：{', '.join(added)}")
    st = status()
    print(f"預設引擎：{st['default']}")
    for p in PROVIDERS:
        print(f"  {p:12s} 金鑰 {'✅' if st[p]['key'] else '❌ 未設定'}｜模型 {st[p]['model']}")
    if not args.test:
        return
    for p in PROVIDERS:
        if p == "claude_lite" and not args.with_claude:
            print("  claude_lite  略過（加 --with-claude 才測試）")
            continue
        if not has_key(p):
            continue
        try:
            r = call_llm_ex("你是測試助理。", "只回覆兩個字：正常", provider=p, max_tokens=20)
            print(f"  {p:12s} ✅ {r['elapsed_sec']} 秒｜{r['text'].strip()[:20]}")
        except LLMError as exc:
            print(f"  {p:12s} ❌ {exc}")


if __name__ == "__main__":
    main()
