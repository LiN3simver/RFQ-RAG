"""build_router：把 AgentSpec 變成 /a2a/* 路由，含入場控管與依賴檢查快取。"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import anyio
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from . import deadline, registry
from .envelope import (
    EV_DELTA,
    EV_DONE,
    EV_ERROR,
    EV_META,
    EV_RESULTS,
    PROTOCOL,
    Hit,
    ToolSpec,
    sse,
    tool_err,
    tool_ok,
)

Status = str

STATUS_DEADLINE = float(os.environ.get("A2A_STATUS_DEADLINE", "2.5"))
STATUS_TTL = float(os.environ.get("A2A_STATUS_TTL", "15"))
STATUS_MAX_STALE = float(os.environ.get("A2A_STATUS_MAX_STALE", "60"))
STATUS_STUCK = float(os.environ.get("A2A_STATUS_STUCK", "30"))

SEARCH_CONCURRENCY = int(os.environ.get("A2A_SEARCH_CONCURRENCY", "4"))
SEARCH_MAX_WAITING = int(os.environ.get("A2A_SEARCH_MAX_WAITING", "8"))
SEARCH_WAIT_S = float(os.environ.get("A2A_SEARCH_WAIT_S", "5"))


@dataclass
class AgentSpec:
    agent_id: str

    tools: list[ToolSpec] = field(default_factory=list)

    system: str = ""

    examples: list[dict[str, str]] = field(default_factory=list)

    search_fn: Callable[[str, int, dict], dict[str, Any]] | None = None
    hits_fn: Callable[[dict[str, Any]], list[Hit]] | None = None
    answer_stream: Callable[[str, dict[str, Any], list[dict]], Iterator[str]] | None = None

    context_fn: Callable[[str, dict[str, Any]], str] | None = None

    status_fn: Callable[[], tuple[Status, dict[str, Any]]] | None = None
    blocked_by: list[str] = field(default_factory=list)

    can_delegate: bool = False

    _status_cache: tuple[float, tuple[Status, dict[str, Any]]] | None = None
    _status_probe: tuple[float, threading.Event] | None = field(default=None, repr=False)
    _status_lock: threading.Lock = field(default_factory=threading.Lock,
                                         repr=False, compare=False)
    _gate: Any = field(default=None, repr=False, compare=False)

    def ref(self) -> registry.AgentRef:
        return registry.get(self.agent_id)

    def tool(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)

    def status(self) -> tuple[Status, dict[str, Any]]:
        if self.status_fn is None:
            return "ready", {}

        with self._status_lock:
            now = time.monotonic()
            cache = self._status_cache
            if cache and now - cache[0] < STATUS_TTL:
                return cache[1]
            probe = self._status_probe
            if probe is None:
                probe = self._status_probe = (now, threading.Event())
                threading.Thread(target=self._run_probe, args=(probe,), daemon=True,
                                 name=f"a2a-status-{self.agent_id}").start()

        started, done = probe
        if done.wait(STATUS_DEADLINE):
            return self._status_cache[1]  # type: ignore[index]  # _run_probe 先寫快取才 set

        now = time.monotonic()
        running = now - started
        if running >= STATUS_STUCK:
            return "not_ready", {
                "checking": True, "stuck": True, "stuck_s": round(running, 1),
                "note": f"依賴檢查已經 {running:.0f}s 沒有回應（上限 {STATUS_STUCK:g}s），"
                        "判定卡住；那一次結束之前不會再開新的檢查",
            }
        cache = self._status_cache
        if cache and now - cache[0] <= STATUS_MAX_STALE:
            prev_st, prev_detail = cache[1]
            return prev_st, {**prev_detail, "stale": True, "stale_s": round(now - cache[0], 1),
                             "note": f"依賴檢查超過 {STATUS_DEADLINE}s 未回應，這是上一次的結果"}
        return "degraded", {
            "checking": True,
            "note": f"依賴檢查超過 {STATUS_DEADLINE}s 未回應（資料庫沒開時會這樣），"
                    "背景仍在檢查，稍後重新整理即可看到結果",
        }

    def _run_probe(self, probe: tuple[float, threading.Event]) -> None:
        try:
            v = self.status_fn()  # type: ignore[misc]
        except Exception as e:
            v = ("not_ready", {"error": f"{type(e).__name__}: {e}"})
        with self._status_lock:
            self._status_cache = (time.monotonic(), v)
            if self._status_probe is probe:
                self._status_probe = None
        probe[1].set()


class MessageReq(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=100)
    filters: dict[str, Any] = Field(default_factory=dict)
    history: list[dict[str, str]] = Field(default_factory=list)
    delegate: bool = False
    use_tools: bool = True
    peers: list[str] = Field(default_factory=list)
    no_llm: bool = False


class SearchReq(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=100)
    filters: dict[str, Any] = Field(default_factory=dict)


def _run_search(spec: AgentSpec, query: str, top_k: int, filters: dict) -> dict[str, Any]:
    if spec.search_fn is None:
        st, detail = spec.status()
        return {
            "query": query,
            "results": [],
            "unavailable": True,
            "status": st,
            "note": "此 agent 尚未提供檢索能力",
            "blocked_by": spec.blocked_by,
            "detail": detail,
        }
    try:
        return spec.search_fn(query, top_k, filters or {})
    except Exception as e:
        return {"query": query, "results": [], "error": f"{type(e).__name__}: {e}"}


def _to_hits(spec: AgentSpec, found: dict[str, Any]) -> list[dict[str, Any]]:
    if spec.hits_fn is None:
        return [
            Hit(id=str(r.get("id", i)), title=str(r.get("id", i)), raw=r).to_dict()
            for i, r in enumerate(found.get("results") or [])
        ]
    try:
        return [h.to_dict() for h in spec.hits_fn(found)]
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        print(f"[a2a] hits_fn 失敗，退回原始列：{msg}")
        return [
            Hit(
                id=str(r.get("item_number") or r.get("rfq_no") or i),
                title="（此 agent 的卡片渲染失敗，以下為原始欄位）",
                subtitle=msg,
                raw=r,
            ).to_dict()
            for i, r in enumerate(found.get("results") or [])
        ]


class Busy(Exception):
    def __init__(self, agent_name: str, slots: int, reason: str) -> None:
        self.user_message = (f"{agent_name} 目前同時處理 {slots} 筆查詢，已經滿了"
                             f"（{reason}），這一題沒有開始查。")
        self.hint = ("請等幾秒再送一次；一直遇到的話請聯絡維護者調整同時查詢上限"
                     "（A2A_SEARCH_CONCURRENCY）。")
        super().__init__(self.user_message)


class _Gate:
    def __init__(self, agent_name: str, slots: int | None = None,
                 max_waiting: int | None = None, wait_s: float | None = None) -> None:
        self.agent_name = agent_name
        self.slots = SEARCH_CONCURRENCY if slots is None else slots
        self.max_waiting = SEARCH_MAX_WAITING if max_waiting is None else max_waiting
        self.wait_s = SEARCH_WAIT_S if wait_s is None else wait_s
        self._slots = anyio.CapacityLimiter(self.slots)
        self._threads = anyio.CapacityLimiter(self.slots)
        self.waiting = 0
        self.counts = {"served": 0, "rejected_full": 0, "rejected_timeout": 0}

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        if self._slots.available_tokens < 1 and self.waiting >= self.max_waiting:
            self.counts["rejected_full"] += 1
            raise Busy(self.agent_name, self.slots, f"另有 {self.max_waiting} 筆在等")
        dl = deadline.current()
        wait = self.wait_s if dl is None else max(0.0, min(self.wait_s, dl.remaining()))
        self.waiting += 1
        try:
            with anyio.fail_after(wait):
                await self._slots.acquire()
        except TimeoutError:
            self.counts["rejected_timeout"] += 1
            if dl is not None and dl.expired():
                raise deadline.Expired("排隊等候查詢時") from None
            raise Busy(self.agent_name, self.slots,
                       f"等了 {self.wait_s:g} 秒仍沒有空位") from None
        finally:
            self.waiting -= 1
        try:
            return await anyio.to_thread.run_sync(fn, *args, limiter=self._threads)
        finally:
            self._slots.release()
            self.counts["served"] += 1

    def stats(self) -> dict[str, Any]:
        return {"slots": self.slots, "max_waiting": self.max_waiting, "wait_s": self.wait_s,
                "running": self.slots - int(self._slots.available_tokens),
                "waiting": self.waiting, **self.counts}


def _search_and_hits(spec: AgentSpec, query: str, top_k: int,
                     filters: dict) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    found = _run_search(spec, query, top_k, filters)
    return found, _to_hits(spec, found)


def _refusal_stream(ref: registry.AgentRef, e: Busy | deadline.Expired) -> Any:
    from fastapi.responses import StreamingResponse

    busy = isinstance(e, Busy)
    err: dict[str, Any] = {"error": e.user_message, "hint": e.hint}
    err["busy" if busy else "timeout"] = True
    events = [
        sse(EV_META, {"agent_id": ref.agent_id, "agent_name": ref.name,
                      "status": "busy" if busy else "timeout"}),
        sse(EV_ERROR, err),
        sse(EV_DONE, {"llm_available": False}),
    ]
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if busy:
        headers["Retry-After"] = "5"
    return StreamingResponse(iter(events), media_type="text/event-stream", headers=headers)


def _adopt_deadline(request: Request) -> deadline.Deadline | None:
    dl = deadline.from_header(request.headers.get(deadline.HEADER))
    deadline.set_current(dl)
    return dl


_STATUS_THREADS = anyio.CapacityLimiter(16)


def build_router(spec: AgentSpec) -> APIRouter:
    router = APIRouter(prefix="/a2a", tags=["a2a"])
    ref = spec.ref()
    gate = spec._gate = _Gate(ref.name)

    async def status_async() -> tuple[Status, dict[str, Any]]:
        return await anyio.to_thread.run_sync(spec.status, limiter=_STATUS_THREADS)

    @router.get("/card")
    async def card() -> dict[str, Any]:
        st, detail = await status_async()
        return {
            "protocol": PROTOCOL,
            "agent_id": ref.agent_id,
            "name": ref.name,
            "role": ref.role,
            "project": ref.project,
            "base_url": ref.base_url,
            "native_ui": ref.native_ui,
            "tags": list(ref.tags),
            "status": st,
            "detail": detail,
            "blocked_by": spec.blocked_by if st != "ready" else [],
            "capabilities": {
                "search": spec.search_fn is not None,
                "chat": spec.answer_stream is not None,
                "tools": any(t.ready for t in spec.tools),
                "delegate": spec.can_delegate,
            },
            "upstream": list(ref.upstream),
            "downstream": list(ref.downstream),
            "tools": [t.public() for t in spec.tools],
            "examples": spec.examples,
            "system": spec.system,
        }

    @router.get("/health")
    async def health() -> dict[str, Any]:
        st, detail = await status_async()
        return {"agent_id": ref.agent_id, "status": st, "detail": detail}

    @router.get("/tools")
    def tools() -> dict[str, Any]:
        return {
            "agent_id": ref.agent_id,
            "tools": [t.public() for t in spec.tools],
            "system": spec.system,
        }

    @router.post("/tool/{name}")
    async def call_tool(name: str, request: Request,
                        body: dict[str, Any] | None = None) -> dict[str, Any]:
        _adopt_deadline(request)
        t = spec.tool(name)
        if t is None:
            return tool_err(
                f"未知的工具：{name}",
                available=[x.name for x in spec.tools],
            )
        if not t.ready:
            return tool_err(
                f"工具 {name} 尚未可用",
                reason=t.not_ready_reason,
                blocked_by=spec.blocked_by,
                status="not_ready",
            )

        def execute() -> dict[str, Any]:
            t0 = time.perf_counter()
            try:
                out = t.handler(body or {})
            except Exception as e:
                return tool_err(f"{type(e).__name__}: {e}")
            ms = round((time.perf_counter() - t0) * 1000)
            if isinstance(out, dict) and "ok" in out:
                return {**out, "ms": ms}
            return {**tool_ok(out), "ms": ms}

        if deadline.expired():
            e = deadline.Expired("呼叫工具前")
            return tool_err(e.user_message, timeout=True, hint=e.hint)
        try:
            return await gate.run(execute)
        except Busy as e:
            return tool_err(e.user_message, busy=True, hint=e.hint)
        except deadline.Expired as e:
            return tool_err(e.user_message, timeout=True, hint=e.hint)

    @router.post("/search")
    async def search(req: SearchReq, request: Request) -> dict[str, Any]:
        _adopt_deadline(request)
        try:
            found, hits = await gate.run(_search_and_hits, spec, req.query,
                                         req.top_k, req.filters)
        except (Busy, deadline.Expired) as e:
            flag = "busy" if isinstance(e, Busy) else "timeout"
            found, hits = {"query": req.query, "results": [], flag: True,
                           "error": e.user_message, "hint": e.hint}, []
        return {
            "agent_id": ref.agent_id,
            "query": req.query,
            "hits": hits,
            "raw": found,
        }

    @router.get("/peers")
    def peers() -> dict[str, Any]:
        from .client import peer_cards

        return {"agent_id": ref.agent_id, "peers": peer_cards(ref.downstream)}

    @router.post("/message")
    async def message(req: MessageReq, request: Request):
        from fastapi.responses import StreamingResponse

        dl = _adopt_deadline(request)
        if dl is not None and dl.expired():
            return _refusal_stream(ref, deadline.Expired("開始查詢前"))

        try:
            found, hits = await gate.run(_search_and_hits, spec, req.query,
                                         req.top_k, req.filters)
        except (Busy, deadline.Expired) as e:
            return _refusal_stream(ref, e)

        cancel_event = threading.Event()

        async def watch_disconnect():
            try:
                while not cancel_event.is_set():
                    if await request.is_disconnected():
                        cancel_event.set()
                        return
                    await asyncio.sleep(0.25)
            except Exception:
                pass

        watcher = asyncio.ensure_future(watch_disconnect())

        def gen() -> Iterator[str]:
            st, _ = spec.status()
            delegating = bool(req.delegate and spec.can_delegate)
            has_tools = any(t.ready for t in spec.tools)
            tool_loop = delegating or bool(req.use_tools and has_tools)
            yield sse(EV_META, {
                "agent_id": ref.agent_id,
                "agent_name": ref.name,
                "status": st,
                "retrieval_query": found.get("query", req.query),
                "delegating": delegating,
                "tool_mode": tool_loop and not delegating,
            })
            yield sse(EV_RESULTS, {"hits": hits, "raw": found})

            if req.no_llm:
                yield sse(EV_DONE, {"llm_available": False, "skipped": "no_llm"})
                return

            if not deadline.enough_for_llm():
                yield sse(EV_ERROR, deadline.error_payload("產生回答前"))
                yield sse(EV_DONE, {"llm_available": False, "skipped": "deadline"})
                return

            got = False
            try:
                if tool_loop:
                    from .delegate import stream_delegated

                    for ev, payload in stream_delegated(spec, req, found):
                        if cancel_event.is_set():
                            break
                        if ev == EV_DELTA and payload.get("t"):
                            got = True
                        yield sse(ev, payload)
                elif spec.answer_stream is not None:
                    stream = spec.answer_stream(req.query, found, req.history)
                    try:
                        for chunk in stream:
                            if cancel_event.is_set():
                                break
                            if deadline.expired():
                                yield sse(EV_ERROR, deadline.error_payload("產生回答途中"))
                                break
                            if chunk:
                                got = True
                                yield sse(EV_DELTA, {"t": chunk})
                    finally:
                        stream.close()
                else:
                    yield sse(EV_DELTA, {"t": _no_llm_note(spec, st)})
            except Exception as e:
                if not cancel_event.is_set():
                    payload: dict[str, Any] = {
                        "error": getattr(e, "user_message", None) or f"{type(e).__name__}: {e}"
                    }
                    diag = getattr(e, "diag", None)
                    if isinstance(diag, dict):
                        payload["diag"] = diag
                    hint = getattr(e, "hint", None)
                    if hint:
                        payload["hint"] = str(hint)
                    if isinstance(e, deadline.Expired):
                        payload["timeout"] = True
                    yield sse(EV_ERROR, payload)

            if not cancel_event.is_set():
                yield sse(EV_DONE, {"llm_available": got})

        def gen_and_cleanup() -> Iterator[str]:
            try:
                yield from gen()
            finally:
                cancel_event.set()
                watcher.cancel()

        return StreamingResponse(
            gen_and_cleanup(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


def _no_llm_note(spec: AgentSpec, status: Status) -> str:
    ref = spec.ref()
    if status == "not_ready":
        why = "；".join(spec.blocked_by) or "尚未實作"
        return f"（{ref.name} 尚未就緒：{why}。已在上方回報現況，未捏造任何結果。）"
    return "（此 agent 未提供文字摘要，以上為檢索結果。）"
