"""LLM gateway 客戶端（tool use），含 JSON 信封攤平與探測快取。"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from . import deadline

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:4000")
MODEL = os.environ.get("LLM_MODEL", "local-llm")
MODEL_FALLBACK = os.environ.get("LLM_MODEL_FALLBACK", "")
MAX_TOKENS = int(os.environ.get("A2A_MAX_TOKENS", os.environ.get("LLM_MAX_TOKENS", "9000")))
TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "240"))
MAX_STEPS = int(os.environ.get("A2A_MAX_STEPS", "5"))


OWNER_ADMIN = "gateway 管理者"
OWNER_LOCAL = "Agent_Hub 的 .env"
NEEDS_NEW_KEY = {"auth", "quota", "no_model_access"}

_PROBE_TTL = float(os.environ.get("LLM_PROBE_TTL", "60"))
_PROBE_TIMEOUT = float(os.environ.get("LLM_PROBE_TIMEOUT", "10"))
_probe_cache: tuple[float, dict[str, Any]] | None = None


def _diag(kind: str, title: str, detail: str, fix: str, owner: str,
          raw: str = "", models: list[str] | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "ok": kind == "ok",
        "title": title,
        "detail": detail,
        "fix": fix,
        "owner": owner,
        "needs_new_key": kind in NEEDS_NEW_KEY,
        "model": _model_cache[1] if _model_cache else MODEL,
        "base_url": BASE_URL,
        "models": models or [],
        "raw": raw,
    }


def models_for_key() -> list[str]:
    key = os.environ.get("LLM_API_KEY")
    if not key:
        return []
    try:
        import httpx

        r = httpx.get(BASE_URL.rstrip("/") + "/v1/models",
                      headers={"Authorization": f"Bearer {key}"},
                      timeout=deadline.timeout(10.0))
        r.raise_for_status()
        return [m["id"] for m in (r.json().get("data") or []) if m.get("id")]
    except Exception:
        return []


_MODEL_CACHE_TTL = 300.0
_model_cache: tuple[float, str] | None = None


def _effective_model() -> str:
    global _model_cache
    now = time.monotonic()
    if _model_cache and now - _model_cache[0] < _MODEL_CACHE_TTL:
        return _model_cache[1]
    chosen = MODEL
    if MODEL_FALLBACK and MODEL_FALLBACK != MODEL:
        avail = models_for_key()
        if avail and MODEL not in avail and MODEL_FALLBACK in avail:
            chosen = MODEL_FALLBACK
            print(f"[gateway] {MODEL} 不在 /v1/models 清單，改用 fallback {MODEL_FALLBACK}")
    _model_cache = (now, chosen)
    return chosen


def classify(err: BaseException | str) -> dict[str, Any]:
    raw = err if isinstance(err, str) else f"{type(err).__name__}: {err}"
    low = raw.lower()
    code = getattr(err, "status_code", None) if isinstance(err, BaseException) else None

    if code == 401 or "invalid api key" in low or "authenticationerror" in low:
        return _diag("auth", "API key 無效或已被撤銷",
                     "gateway 回 401，目前設定的這把 key 連認證都過不了。",
                     "跟 gateway 管理者換一把新的 API key，填進 .env 的 "
                     "LLM_API_KEY 後重啟 Hub 與各 agent。", OWNER_ADMIN, raw)

    if code == 429 or "rate limit" in low or "too many requests" in low:
        return _diag("rate_limit", "呼叫太密集，被 gateway 限流",
                     "gateway 回 429。這是每分鐘的頻率上限，不是總量用完。",
                     "等幾十秒再送一次。★ 這種情況換新 key 沒有用。",
                     OWNER_ADMIN, raw)

    if any(w in low for w in ("budget", "quota", "exceeded", "insufficient", "credit")):
        return _diag("quota", "這把 key 的額度用完了",
                     "gateway 的訊息裡提到額度／預算已用盡 —— "
                     "這才是「用到一定次數就要換一把 key」的那種情況。",
                     "跟 gateway 管理者要一把新的 API key（或請他把額度加回來），"
                     "換進 .env 的 LLM_API_KEY 後重啟 Hub 與各 agent。",
                     OWNER_ADMIN, raw)

    if code == 404 or "unknown model" in low:
        avail = models_for_key()
        return _diag("unknown_model", f"gateway 不認識模型名稱 {_effective_model()}",
                     "gateway 回 404 unknown model —— 這個名字在它的模型表裡"
                     "根本不存在，多半是打錯字。",
                     ("先請 gateway 管理者給正確的模型名稱。" if not avail
                      else "把 Agent_Hub/.env 的 LLM_MODEL 改成 " + "、".join(avail[:10])
                           + " 其中之一，換過的專案都會一起生效。"),
                     OWNER_LOCAL if avail else OWNER_ADMIN, raw, avail)

    if "invalid model name" in low or (code == 400 and "model" in low):
        avail = models_for_key()
        if avail:
            return _diag("wrong_model", f"這把 key 不能用 {_effective_model()}，但還有別的可以用",
                         "gateway 回 400 說這個模型名不被接受，"
                         "而 /v1/models 仍列得出其他模型。",
                         "把 Agent_Hub/.env 的 LLM_MODEL 改成 " + "、".join(avail[:10])
                         + " 其中之一後重啟 Hub 與各 agent。", OWNER_LOCAL, raw, avail)
        return _diag("no_model_access", "這把 API key 名下沒有任何可用模型",
                     f"gateway 認得 {_effective_model()}（亂打的模型名會回 404，這裡回的是 400），"
                     "但 /v1/models 對這把 key 回的是空清單 —— key 還活著、"
                     "模型授權卻被收走了（額度到期，或管理者改了這把 key 的模型綁定）。",
                     "跟 gateway 管理者要一把新的 API key，或請他把這顆模型重新綁"
                     "回這把 key。★ 改 Agent_Hub/.env 的 LLM_MODEL 沒有用，"
                     "這把 key 一顆模型都沒有。",
                     OWNER_ADMIN, raw)

    if any(w in low for w in ("connect", "timeout", "timed out", "unreachable")):
        return _diag("offline", "連不上 LLM gateway 或逾時",
                     f"打 {BASE_URL} 的時候連線失敗或逾時。",
                     "確認 gateway 服務有起來、且這台機器連得到那個網段。",
                     OWNER_ADMIN, raw)

    if "max_tokens" in low:
        return _diag("token_budget", "模型的思考佔滿了 token 預算",
                     f"這顆模型的 thinking 區塊算進 max_tokens，目前是 {MAX_TOKENS}。",
                     "調高 .env 的 A2A_MAX_TOKENS 後重啟服務。", OWNER_LOCAL, raw)

    return _diag("other", "LLM gateway 回了未分類的錯誤", raw or "（沒有錯誤訊息）",
                 "把下面的原始訊息貼給 gateway 管理者。", OWNER_ADMIN, raw)


def diagnose(*, fresh: bool = False) -> dict[str, Any]:
    global _probe_cache

    if not os.environ.get("LLM_API_KEY"):
        return _diag("not_configured", "LLM 尚未設定", "LLM_API_KEY 未設定",
                     "在 Agent_Hub 的 .env 補上 LLM_API_KEY 後重啟 Hub。", OWNER_LOCAL)
    try:
        import anthropic  # noqa: F401
    except Exception as e:
        return _diag("not_configured", "anthropic SDK 不可用", str(e),
                     "在 Hub 的 .venv 裝上 anthropic 套件。", OWNER_LOCAL)

    now = time.monotonic()
    if not fresh and _probe_cache and now - _probe_cache[0] < _PROBE_TTL:
        return _probe_cache[1]

    try:
        import anthropic

        model = _effective_model()
        anthropic.Anthropic(
            base_url=BASE_URL, auth_token=os.environ["LLM_API_KEY"],
            timeout=deadline.timeout(_PROBE_TIMEOUT), max_retries=0,
        ).messages.create(
            model=model, max_tokens=16,
            messages=[{"role": "user", "content": "ping"}],
        )
        out = _diag("ok", "LLM 可用", f"{model} @ {BASE_URL}", "", "")
    except BaseException as e:  # noqa: BLE001 -- 狀態檢查不該把服務弄掛
        if deadline.expired():
            return _deadline_diag("檢查 LLM 是否可用時", f"{type(e).__name__}: {e}")
        out = classify(e)
    _probe_cache = (now, out)
    return out


_probe_lock = threading.Lock()
_probe_done: threading.Event | None = None


def _probe_in_background() -> threading.Event:
    global _probe_done
    with _probe_lock:
        if _probe_done is not None and not _probe_done.is_set():
            return _probe_done
        done = _probe_done = threading.Event()

    def run() -> None:
        try:
            diagnose(fresh=True)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True, name="llm-probe").start()
    return done


def diagnose_quick(wait: float) -> dict[str, Any] | None:
    if not os.environ.get("LLM_API_KEY"):
        return diagnose()
    now = time.monotonic()
    cache = _probe_cache
    if cache and now - cache[0] < _PROBE_TTL:
        return cache[1]
    done = _probe_in_background()
    if cache:
        return cache[1]
    done.wait(max(0.0, wait))
    return _probe_cache[1] if _probe_cache else None


def available() -> tuple[bool, str]:
    d = diagnose()
    if d["ok"]:
        return True, f"{_effective_model()} @ {BASE_URL}"
    return False, f"{d['title']}：{d['detail']}"


_client: Any = None


def client() -> Any:
    global _client
    if _client is None:
        import anthropic

        token = os.environ.get("LLM_API_KEY")
        if not token:
            raise RuntimeError("LLM_API_KEY 未設定")
        _client = anthropic.Anthropic(
            base_url=BASE_URL, auth_token=token, timeout=TIMEOUT, max_retries=1
        )
    return _client


def _assistant_content(blocks: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in blocks:
        d = b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else dict(b)
        if d.get("type") == "text" and not (d.get("text") or "").strip():
            continue
        out.append(d)
    return out


def _strip_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = [b for b in c if b.get("type") != "thinking"] or [
                {"type": "text", "text": "（略）"}
            ]
        out.append({**m, "content": c})
    return out


_CALL_WRAPPERS = ("<tool_call>", "</tool_call>", "<tool_use>", "</tool_use>",
                  "```json", "```tool_code", "```")


def _iter_json_objects(text: str) -> Iterator[tuple[int, int, Any]]:
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        j = i
        in_str = esc = False
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j < n and depth == 0:
            try:
                obj = json.loads(text[i:j + 1])
            except Exception:  # noqa: BLE001 -- 不是 JSON 就當普通文字
                obj = None
            if isinstance(obj, dict):
                yield i, j + 1, obj
                i = j + 1
                continue
        i += 1


def _as_tool_call(obj: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    fn = obj.get("function") if isinstance(obj.get("function"), dict) else obj
    name = fn.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    args = fn.get("arguments")
    if args is None:
        args = fn.get("input")
    if args is None:
        args = fn.get("parameters")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:  # noqa: BLE001
            return None
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None

    if obj.get("type") == "function" or isinstance(obj.get("function"), dict):
        return name.strip(), args
    if set(obj) <= {"name", "arguments", "input", "parameters", "id", "type"}:
        return name.strip(), args
    return None


def extract_text_tool_calls(text: str) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    spans: list[tuple[int, int]] = []
    for s, e, obj in _iter_json_objects(text):
        c = _as_tool_call(obj)
        if c:
            calls.append(c)
            spans.append((s, e))
    if not calls:
        markup_call = _as_markup_tool_call(text)
        return ("", [markup_call]) if markup_call else (text, [])

    keep, prev = [], 0
    for s, e in spans:
        keep.append(text[prev:s])
        prev = e
    keep.append(text[prev:])
    clean = "".join(keep)
    for w in _CALL_WRAPPERS:
        clean = clean.replace(w, "")
    return clean.strip(), calls


def _as_markup_tool_call(text: str) -> tuple[str, dict[str, Any]] | None:
    rows: list[str] = []
    for raw in html.unescape(text).splitlines():
        row = re.sub(r"\\([_\\])", r"\1", raw).rstrip().rstrip("\\").strip()
        if row:
            rows.append(row)
    if not rows:
        return None

    saw_call = saw_function = in_args = False
    outer_name = function_name = ""
    args: dict[str, Any] = {}
    for row in rows:
        bare = re.sub(r"[\s_-]+", " ", row.lower()).strip()
        if bare == "tool call":
            saw_call = True
            continue
        if bare in {"function", "arguments"}:
            in_args = bare == "arguments"
            continue
        if "：" in row:
            key, value = (part.strip() for part in row.split("：", 1))
        elif ":" in row:
            key, value = (part.strip() for part in row.split(":", 1))
        else:
            return None
        norm_key = re.sub(r"[\s-]+", "_", key.lower())
        if in_args:
            try:
                parsed = json.loads(value)
            except Exception:  # noqa: BLE001 -- 一般字串參數
                parsed = value
            args[norm_key] = parsed
        elif norm_key in {"tool_name", "tool"}:
            outer_name = value
        elif norm_key == "name":
            function_name = value
        elif norm_key == "type":
            saw_function = value.lower() == "function"
        elif norm_key == "id":
            continue
        else:
            return None

    name = function_name or outer_name
    if not (saw_call and saw_function and name and in_args):
        return None
    if outer_name and function_name and outer_name != function_name:
        return None
    return name, args


_ANSWER_KEYS = ("summary", "answer", "text", "content", "response", "reply", "message", "result")
_EXTRA_KEYS = (("source", "來源"), ("sources", "來源"), ("note", "註"),
               ("caveat", "註"), ("warning", "注意"))

_UNI_ESC = re.compile(r"\\u([0-9a-fA-F]{4})")


def _decode_unicode_escapes(s: str) -> str:
    return _UNI_ESC.sub(lambda m: chr(int(m.group(1), 16)), s)


def unwrap_json_answer(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = s[3:].lstrip()
        if s[:4].lower() == "json":
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return _decode_unicode_escapes(text) if "\\u" in text else text

    try:
        obj = json.loads(s)
    except Exception:  # noqa: BLE001 -- 不是合法 JSON 就當普通文字
        return _decode_unicode_escapes(text) if "\\u" in text else text
    if not isinstance(obj, dict):
        return text

    body = next((obj[k].strip() for k in _ANSWER_KEYS
                 if isinstance(obj.get(k), str) and obj[k].strip()), None)
    if body is None:
        return _decode_unicode_escapes(text) if "\\u" in text else text

    parts = [body]
    for key, label in _EXTRA_KEYS:
        v = obj.get(key)
        if isinstance(v, list):
            v = "、".join(str(x) for x in v if x)
        if isinstance(v, str) and v.strip() and v.strip() not in body:
            parts.append(f"{label}：{v.strip()}")
    return "\n".join(parts)


class _RecoveredToolUse:
    type = "tool_use"

    def __init__(self, id: str, name: str, input: dict[str, Any]) -> None:  # noqa: A002
        self.id, self.name, self.input = id, name, input

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


def _tool_use_block(block_id: str, name: str, args: dict[str, Any]) -> Any:
    try:
        from anthropic.types import ToolUseBlock

        return ToolUseBlock(id=block_id, name=name, input=args, type="tool_use")
    except Exception:  # noqa: BLE001 -- SDK 版本不合就用自己的替身
        return _RecoveredToolUse(block_id, name, args)


_PLAYBACK_CHUNK = 24


def stream_step(
    system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> Iterator[tuple[str, Any]]:
    import anthropic

    if not deadline.enough_for_llm():
        yield "error", _deadline_diag("呼叫模型之前")
        return

    kwargs: dict[str, Any] = {
        "model": _effective_model(),
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools

    def _client_for_call() -> Any:
        c = client()
        d = deadline.current()
        return c if d is None else c.with_options(timeout=d.cap(TIMEOUT), max_retries=0)

    def _run(kw: dict[str, Any]) -> Iterator[tuple[str, Any]]:
        msg = _client_for_call().messages.create(**kw)

        texts: list[str] = []
        kept: list[Any] = []
        recovered: list[tuple[str, dict[str, Any]]] = []
        for b in msg.content:
            if getattr(b, "type", "") != "text":
                kept.append(b)
                continue
            clean, calls = extract_text_tool_calls(b.text or "")
            recovered.extend(calls)
            if not clean.strip():
                continue
            clean = unwrap_json_answer(clean)
            texts.append(clean)
            try:
                kept.append(b.model_copy(update={"text": clean}))
            except Exception:  # noqa: BLE001 -- 改不動就留原樣，別讓串流掛掉
                kept.append(b)

        if recovered:
            for i, (name, args) in enumerate(recovered):
                kept.append(_tool_use_block(f"txt_{getattr(msg, 'id', 'x')}_{i}", name, args))
            print(f"[gateway] 從文字救回 {len(recovered)} 個工具呼叫："
                  f"{', '.join(n for n, _ in recovered)}")
            try:
                msg.content = kept
            except Exception as e:  # noqa: BLE001
                print(f"[gateway] 救回的工具呼叫寫不回 msg.content：{type(e).__name__}: {e}")

        for text in texts:
            for i in range(0, len(text), _PLAYBACK_CHUNK):
                yield "text", text[i:i + _PLAYBACK_CHUNK]
        yield "final", msg

    try:
        yield from _run(kwargs)
    except anthropic.APIStatusError as e:
        if e.status_code == 400 and any(
            isinstance(m.get("content"), list) for m in messages
        ):
            try:
                yield from _run({**kwargs, "messages": _strip_thinking(messages)})
                return
            except Exception as e2:
                yield "error", classify(e2)
                return
        yield "error", classify(e)
    except anthropic.APIConnectionError as e:
        if deadline.expired():
            yield "error", _deadline_diag("等模型回答時", f"{type(e).__name__}: {e}")
            return
        yield "error", _diag(
            "offline", "連不上 LLM gateway", f"打不到 {BASE_URL}。",
            "確認 gateway 服務有起來、且這台機器連得到那個網段。",
            OWNER_ADMIN, f"{type(e).__name__}: {e}",
        )
    except Exception as e:
        yield "error", classify(e)


def _deadline_diag(where: str, raw: str = "") -> dict[str, Any]:
    e = deadline.Expired(where)
    return {**_diag("deadline", "這一題的時間上限到了", e.user_message, e.hint,
                    OWNER_LOCAL, raw), "timeout": True}


def empty_answer_note(final: Any) -> str | None:
    if getattr(final, "stop_reason", None) == "max_tokens":
        return (
            f"（模型的思考佔滿了 {MAX_TOKENS} 個 token 預算，沒能產出回答。"
            "資料仍在上方；調高 A2A_MAX_TOKENS 後重試。）"
        )
    return None
