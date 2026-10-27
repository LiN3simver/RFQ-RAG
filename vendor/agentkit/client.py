"""呼叫其他 agent 的 A2A 客戶端（含附件代理與錯誤分類）。"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx

from . import deadline, registry

CARD_TIMEOUT = 6.0
TOOL_TIMEOUT = 90.0
MESSAGE_TIMEOUT = 300.0
FILE_TIMEOUT = 30.0
_CARD_TTL = 10.0

_card_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _url(agent_id: str, path: str) -> str:
    return registry.get(agent_id).base_url.rstrip("/") + path


def _offline(agent_id: str, e: Exception) -> dict[str, Any]:
    ref = registry.get(agent_id)
    return {
        "protocol": "a2a/1",
        "agent_id": agent_id,
        "name": ref.name,
        "role": ref.role,
        "project": ref.project,
        "base_url": ref.base_url,
        "status": "offline",
        "offline": True,
        "error": f"{type(e).__name__}: {e}",
        "capabilities": {"search": False, "chat": False, "tools": False, "delegate": False},
        "tools": [],
        "examples": [],
        "upstream": list(ref.upstream),
        "downstream": list(ref.downstream),
        "hint": f"服務未啟動？請啟動 {ref.project}（埠 {ref.port}）",
    }


def card(agent_id: str, *, fresh: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    if not fresh:
        hit = _card_cache.get(agent_id)
        if hit and now - hit[0] < _CARD_TTL:
            return hit[1]
    try:
        r = httpx.get(_url(agent_id, "/a2a/card"), timeout=CARD_TIMEOUT)
        r.raise_for_status()
        out = r.json()
    except Exception as e:
        out = _offline(agent_id, e)
    _card_cache[agent_id] = (now, out)
    return out


def peer_cards(agent_ids: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    return [card(a) for a in agent_ids if a in registry.BY_ID]


def all_cards(*, fresh: bool = False) -> list[dict[str, Any]]:
    return [card(a.agent_id, fresh=fresh) for a in registry.AGENTS]


def _timed_out(where: str) -> dict[str, Any]:
    e = deadline.Expired(where)
    return {"ok": False, "timeout": True, "error": e.user_message, "hint": e.hint}


def call_tool(
    agent_id: str, tool: str, args: dict[str, Any], *, timeout: float = TOOL_TIMEOUT
) -> dict[str, Any]:
    name = registry.get(agent_id).name
    if deadline.expired():
        return _timed_out(f"呼叫 {name} 之前")
    try:
        r = httpx.post(_url(agent_id, f"/a2a/tool/{tool}"), json=args,
                       timeout=deadline.timeout(timeout), headers=deadline.headers())
        r.raise_for_status()
        out = r.json()
        if isinstance(out, dict) and "ok" in out:
            return out
        return {"ok": True, "result": out}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"{agent_id} 回應 {e.response.status_code}"}
    except Exception as e:
        if deadline.expired():
            return _timed_out(f"等 {name} 回應時")
        return {
            "ok": False,
            "offline": True,
            "error": f"連不上 {name}（{registry.get(agent_id).base_url}）：{e}",
        }


def file_bytes(agent_id: str, doc_id: str) -> tuple[bytes, str, str, int]:
    try:
        r = httpx.get(_url(agent_id, f"/a2a/file/{doc_id}"), timeout=FILE_TIMEOUT)
    except Exception as e:
        ref = registry.get(agent_id)
        raise RuntimeError(f"連不上 {ref.name}（{ref.base_url}）：{type(e).__name__}: {e}") from e
    return (r.content, r.headers.get("content-type", "application/octet-stream"),
            r.headers.get("content-disposition", ""), r.status_code)


def search(agent_id: str, query: str, top_k: int = 10,
           filters: dict[str, Any] | None = None) -> dict[str, Any]:
    if deadline.expired():
        return {"agent_id": agent_id, "query": query, "hits": [],
                "raw": _timed_out(f"查 {registry.get(agent_id).name} 之前")}
    try:
        r = httpx.post(
            _url(agent_id, "/a2a/search"),
            json={"query": query, "top_k": top_k, "filters": filters or {}},
            timeout=deadline.timeout(TOOL_TIMEOUT), headers=deadline.headers(),
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if deadline.expired():
            return {"agent_id": agent_id, "query": query, "hits": [],
                    "raw": _timed_out(f"等 {registry.get(agent_id).name} 回應時")}
        return {"agent_id": agent_id, "query": query, "hits": [],
                "raw": {"error": str(e)}, "offline": True}


def stream_message(
    agent_id: str,
    payload: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> Iterator[tuple[str, dict]]:
    if deadline.current() is not None:
        yield from _stream_with_deadline(agent_id, payload, cancel_event)
        return
    try:
        with httpx.stream(
            "POST", _url(agent_id, "/a2a/message"), json=payload, timeout=MESSAGE_TIMEOUT
        ) as r:
            r.raise_for_status()
            event = ""
            for line in r.iter_lines():
                if cancel_event is not None and cancel_event.is_set():
                    return
                if line.startswith("event: "):
                    event = line[7:].strip()
                elif line.startswith("data: "):
                    try:
                        yield event or "message", json.loads(line[6:])
                    except json.JSONDecodeError:
                        yield "error", {"error": f"無法解析 {agent_id} 的事件內容"}
    except Exception as e:
        ref = registry.get(agent_id)
        yield "error", {
            "error": f"連不上 {ref.name}（{ref.base_url}）：{type(e).__name__}: {e}",
            "offline": True,
            "hint": f"請啟動 {ref.project}（埠 {ref.port}）",
        }
        yield "done", {"llm_available": False}


def _stream_with_deadline(
    agent_id: str, payload: dict[str, Any], cancel_event: threading.Event | None,
) -> Iterator[tuple[str, dict]]:
    import queue

    d = deadline.current()
    ref = registry.get(agent_id)
    q: queue.Queue[tuple[str, Any]] = queue.Queue()
    stop = threading.Event()
    headers = deadline.headers()
    read_timeout = deadline.timeout(MESSAGE_TIMEOUT)

    def reader() -> None:
        try:
            with httpx.stream("POST", _url(agent_id, "/a2a/message"), json=payload,
                              timeout=read_timeout, headers=headers) as r:
                r.raise_for_status()
                event = ""
                for line in r.iter_lines():
                    if stop.is_set():
                        return
                    if line.startswith("event: "):
                        event = line[7:].strip()
                    elif line.startswith("data: "):
                        try:
                            q.put(("ev", (event or "message", json.loads(line[6:]))))
                        except json.JSONDecodeError:
                            q.put(("ev", ("error", {"error": f"無法解析 {agent_id} 的事件內容"})))
        except Exception as e:      # noqa: BLE001 -- 交給下面依情況翻成人話
            q.put(("exc", e))
        finally:
            q.put(("end", None))

    threading.Thread(target=reader, daemon=True, name=f"a2a-stream-{agent_id}").start()
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return
            if d is not None and d.expired():
                yield "error", deadline.error_payload(f"等 {ref.name} 回答時", d.budget)
                yield "done", {"llm_available": False}
                return
            try:
                kind, item = q.get(timeout=1.0)
            except queue.Empty:
                continue
            if kind == "ev":
                yield item
            elif kind == "exc":
                if d is not None and d.expired():
                    yield "error", deadline.error_payload(f"等 {ref.name} 回答時", d.budget)
                else:
                    yield "error", {
                        "error": f"連不上 {ref.name}（{ref.base_url}）：{type(item).__name__}: {item}",
                        "offline": True,
                        "hint": f"請啟動 {ref.project}（埠 {ref.port}）",
                    }
                yield "done", {"llm_available": False}
                return
            else:
                return
    finally:
        stop.set()


NS = "__"


ASK = "ask_"


def ask_agent(agent_id: str, question: str, *, delegate: bool = True,
              timeout: float = MESSAGE_TIMEOUT) -> dict[str, Any]:
    text, hits, trace, errors = "", [], [], []
    for ev, data in stream_message(agent_id, {
        "query": question, "top_k": 8, "history": [], "delegate": delegate,
    }):
        if ev == "delta":
            text += data.get("t") or ""
        elif ev == "results":
            hits = data.get("hits") or []
        elif ev == "trace":
            trace.append(data)
        elif ev == "error":
            errors.append(data.get("error"))
    ref = registry.get(agent_id)
    if not text and errors:
        return {"ok": False, "error": "；".join(str(e) for e in errors), "agent": ref.name}
    return {
        "ok": True,
        "agent": ref.name,
        "answer": text,
        "hits": [{"id": h.get("id"), "title": h.get("title")} for h in hits[:8]],
        "delegated_trace": [
            {"tool": t.get("tool"), "agent": t.get("agent_name"), "ok": t.get("ok")}
            for t in trace if t.get("phase") == "result"
        ],
        **({"warnings": errors} if errors else {}),
    }


def peer_tools(agent_ids: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for aid in agent_ids:
        c = card(aid)
        if c.get("status") == "offline":
            continue
        name = c.get("name", aid)
        for t in c.get("tools") or []:
            if not t.get("ready", True):
                continue
            out.append({
                "name": f"{aid}{NS}{t['name']}"[:64],
                "description": f"[{name}] {t.get('description', '')}",
                "input_schema": t.get("input_schema") or {"type": "object", "properties": {}},
            })
        caps = c.get("capabilities") or {}
        if caps.get("chat") or caps.get("delegate") or caps.get("search"):
            out.append({
                "name": f"{ASK}{aid}"[:64],
                "description": (
                    f"用一句自然語言問「{name}」（{c.get('role', '')}）。"
                    "適用於：問題屬於它的專業範圍、但你不確定要用它的哪一支工具，"
                    "或它需要自己再去問更下游的 agent。"
                    "回傳它的答案與它引用的資料。"
                    + ("★ 這個 agent 目前尚未完全就緒（"
                       + "；".join(c.get("blocked_by") or []) + "），"
                       "它會如實說明哪部分還沒有，不要把它的說明改寫成肯定的答案。"
                       if c.get("blocked_by") else "")
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"question": {
                        "type": "string",
                        "description": "要問它的完整問題（帶上必要的料號與規格，它看不到你的對話）",
                    }},
                    "required": ["question"],
                },
            })
    return out


def split_ns(name: str) -> tuple[str | None, str]:
    if NS in name:
        aid, _, rest = name.partition(NS)
        if aid in registry.BY_ID:
            return aid, rest
    return None, name
