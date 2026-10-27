"""讓模型自跑工具迴圈的回答也經過核對：攔截 SSE 的 delta 事件，核對通過才放行。"""
from __future__ import annotations

import json
import re
from typing import Any

import anyio

from fastapi import Request
from fastapi.responses import StreamingResponse

from agentkit import MessageReq
from agentkit.envelope import EV_DELTA, EV_DONE, EV_RESULTS, EV_TRACE
from agentkit.envelope import sse as _sse_text

from . import answer as ansmod
from . import verify

_HOLD = (EV_DELTA,)

_CHECK_LIMITER = anyio.CapacityLimiter(8)

_CODE_ZH = {
    "placeholder": "有佔位符",
    "ungrounded_number": "數字沒有出處",
    "ungrounded_id": "編號沒有出處",
    "contradiction": "自相矛盾",
    "empty": "沒有內容",
}

_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*|\s*```$")
_PROSE_KEYS = ("note", "summary", "answer", "text", "conclusion", "explanation")
_FIELD_LABELS = {
    "total_drawings": "工程圖數量",
    "total_lines": "BOM 總筆數",
    "max_level": "最深階層",
    "semi_count": "半成品數量",
    "file": "檔名",
    "file_modified_on": "更新時間",
    "lvl": "階層",
    "parent": "母件",
    "child": "子件",
    "qty_eff": "用量",
    "is_semi_by_structure": "依結構判定為半成品",
    "is_semi_by_type": "依類型判定為半成品",
    "part_type": "料件類型",
    "drawing_no": "工程圖號",
}
_SECTION_LABELS = {
    "summary": "摘要",
    "drawings_list": "工程圖",
    "bom_summary": "BOM 摘要",
    "lines": "BOM 明細",
}

def _scalar(v: Any) -> str:
    if v is None:
        return "未填"
    if v is True:
        return "是"
    if v is False:
        return "否"
    return str(v)


def _label(key: str) -> str:
    return _FIELD_LABELS.get(key, _SECTION_LABELS.get(key, key.replace("_", " ")))


def _flatten_json(obj: Any, depth: int = 0) -> str:
    pad = "  " * depth
    lines: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in _PROSE_KEYS and isinstance(v, str) and v.strip():
                lines.append(f"{pad}{v.strip()}")
        for k, v in obj.items():
            if k.lower() in _PROSE_KEYS and isinstance(v, str):
                continue
            if isinstance(v, dict):
                lines.append(f"{pad}{_SECTION_LABELS.get(k, _label(k))}")
                lines.append(_flatten_json(v, depth + 1))
            elif isinstance(v, list):
                lines.append(_flatten_list(k, v, depth))
            else:
                lines.append(f"{pad}{_label(k)}：{_scalar(v)}")
    elif isinstance(obj, list):
        lines.append(_flatten_list("", obj, depth))
    else:
        lines.append(f"{pad}{_scalar(obj)}")
    return "\n".join(ln for ln in lines if ln.strip())


def _flatten_list(key: str, items: list, depth: int) -> str:
    pad = "  " * depth
    label = _SECTION_LABELS.get(key, _label(key)) if key else "項目"
    if not items:
        return f"{pad}{label}：（空）"
    if all(not isinstance(x, (dict, list)) for x in items):
        return f"{pad}{label}：" + "、".join(_scalar(x) for x in items)
    lines = [f"{pad}{label}（共 {len(items)} 筆）"]
    for i, x in enumerate(items, 1):
        if isinstance(x, dict):
            flat = {k: v for k, v in x.items() if not isinstance(v, (dict, list))}
            deep = {k: v for k, v in x.items() if isinstance(v, (dict, list))}
            fields: list[str] = []
            if "lvl" in flat:
                fields.append(f"第 {_scalar(flat.pop('lvl'))} 階")
            parent = flat.pop("parent", None)
            child = flat.pop("child", None)
            if parent is not None and child is not None:
                fields.append(f"母件 {_scalar(parent)} → 子件 {_scalar(child)}")
            elif parent is not None:
                fields.append(f"母件：{_scalar(parent)}")
            elif child is not None:
                fields.append(f"子件：{_scalar(child)}")
            fields.extend(f"{_label(k)}：{_scalar(v)}" for k, v in flat.items())
            inline = "；".join(fields)
            lines.append(f"{pad}- {inline}" if inline else f"{pad}-")
            if deep:
                lines.append(_flatten_json(deep, depth + 2))
        else:
            lines.append(f"{pad}- {_scalar(x)}")
    return "\n".join(lines)


def unwrap_json_answer(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return text
    s = _FENCE_RE.sub("", s).strip()
    i = min((p for p in (s.find("{"), s.find("[")) if p >= 0), default=-1)
    if i < 0:
        return text
    head, rest = s[:i].strip(), s[i:]
    try:
        obj, end = json.JSONDecoder().raw_decode(rest)
    except ValueError:
        return text
    if not isinstance(obj, (dict, list)):
        return text
    if len(rest[:end]) < 0.6 * len(s):
        return text
    body = _flatten_json(obj)
    if not body.strip():
        return text
    tail = rest[end:].strip()
    return "\n\n".join(x for x in (head, body, tail) if x)

_ALWAYS_SOURCE_DEPENDENT = frozenset({"ungrounded_number", "ungrounded_id"})


def _is_source_dependent(v: Any, has_data: bool | None) -> bool:
    if v.code in _ALWAYS_SOURCE_DEPENDENT:
        return True
    if v.code == "contradiction":
        return has_data is False
    return False

_FRAME_RE = re.compile(rb"event:\s*(?P<ev>[^\n]+)\ndata:\s*(?P<data>.*?)\n\n",
                       re.S)


def sse(event: str, data: Any) -> bytes:
    return _sse_text(event, data).encode("utf-8")


def _own_agent_id() -> str:
    from .a2a import AGENT_ID

    return AGENT_ID


def _parse_frames(buf: bytes):
    pos = 0
    while True:
        m = _FRAME_RE.search(buf, pos)
        if not m:
            break
        pos = m.end()
        ev = m.group("ev").decode("utf-8", "replace").strip()
        try:
            data = json.loads(m.group("data").decode("utf-8", "replace"))
        except Exception:       # noqa: BLE001 -- 解不出來就當成不透明的框
            data = None
        yield ev, data, buf[m.start():m.end()]
    if pos < len(buf):
        yield "", None, buf[pos:]


class Checker:
    def __init__(self, query: str, context_fn, route_fn) -> None:
        self.query = query
        self._context_fn = context_fn
        self._route_fn = route_fn
        self.found: dict[str, Any] = {}
        self.parts: list[str] = []
        self.tail = b""
        self.tool_facts: list[str] = []
        self.peer_agents: set[str] = set()

    def feed(self, chunk: bytes):
        buf, self.tail = self.tail + chunk, b""
        out = bytearray()
        for ev, data, raw in _parse_frames(buf):
            if not ev:
                self.tail = raw
                continue
            if ev == EV_RESULTS and isinstance(data, dict):
                self.found = data.get("raw") or {}
                out += raw
            elif ev == EV_TRACE and isinstance(data, dict):
                if (data.get("phase") == "result" and data.get("ok")
                        and not data.get("repeated")):
                    if preview := str(data.get("preview") or "").strip():
                        self.tool_facts.append(preview)
                    if (aid := data.get("agent")) and aid != _own_agent_id():
                        self.peer_agents.add(str(data.get("agent_name") or aid))
                out += raw
            elif ev in _HOLD and isinstance(data, dict):
                self.parts.append(str(data.get("t") or ""))
            elif ev == EV_DONE:
                out += self.finish()
                out += raw
            else:
                out += raw
        return bytes(out)

    def flush(self) -> bytes:
        out = bytearray(self.tail)
        self.tail = b""
        out += self.finish()
        return bytes(out)

    def finish(self) -> bytes:
        if not self.parts:
            return b""
        text = "".join(self.parts).strip()
        self.parts = []
        if not text:
            return b""

        unwrapped = unwrap_json_answer(text)
        was_json = unwrapped != text
        if was_json:
            print("[guard] 答案被包成 JSON，已就地攤平成文字")
            text = unwrapped

        try:
            from .a2a import (_extra_allowed, _row_count, context_for,
                              data_context_for, has_data)
            context = context_for(self.query) \
                or self._context_fn(self.query, self.found) or ""
            facts = data_context_for(self.query) or context
            route = self._route_fn(self.found)
            hd = has_data(self.found)
            violations = verify.check(
                text, facts, self.query, has_data=hd,
                extra_allowed=(*_extra_allowed(self.found), *self.tool_facts))
        except Exception as e:      # noqa: BLE001
            print(f"[guard] 核對失敗，原樣放行：{type(e).__name__}: {e}")
            return sse(EV_DELTA, {"t": text})

        if not violations:
            return sse(EV_DELTA, {"t": text})

        if self.peer_agents:
            soft = [v for v in violations if _is_source_dependent(v, hd)]
            hard = [v for v in violations if not _is_source_dependent(v, hd)]
            who = "、".join(sorted(self.peer_agents))
            print(f"[guard] 這一輪有下游參與（{who}），不退件重寫。"
                  f"核對不到的：{'；'.join(v.detail for v in soft[:3]) or '無'}"
                  + (f"；仍要提醒的：{'；'.join(v.detail for v in hard[:3])}" if hard else ""))
            note = ""
            if hard:
                note = "（回答內容可能有矛盾或未完成，請以來源卡片為準。）\n\n"
            return sse(EV_DELTA, {"t": note + text})

        print("[guard] 工具迴圈的回答沒過核對（" + route + "）："
              + "；".join(v.detail for v in violations[:3]))
        fixed = "".join(
            c for c in ansmod.verified(
                self.query, context, route=route, has_data=hd,
                n_rows=_row_count(self.found),
                extra_allowed=(*_extra_allowed(self.found), *self.tool_facts),
                display_context=data_context_for(self.query),
                system_extra="")
            if not ansmod.is_patience(c))
        kinds = sorted({v.code for v in violations})
        note = (f"（第一版回答有 {len(violations)} 處對不上檢索結果"
                f"（{'、'.join(_CODE_ZH.get(k, k) for k in kinds)}），"
                f"已自動重寫並通過核對。）\n\n")
        return sse(EV_DELTA, {"t": note + fixed})


def mount(app: Any, router: Any, context_fn, route_fn, prepare=None) -> bool:
    orig = None
    for r in getattr(router, "routes", []):
        if getattr(r, "path", "") == "/a2a/message" and \
                "POST" in (getattr(r, "methods", None) or set()):
            orig = r.endpoint
            break
    if orig is None:
        return False

    @app.post("/a2a/message", tags=["a2a"])
    async def guarded_message(req: MessageReq, request: Request):   # noqa: ANN202
        if prepare is None:
            resp = await orig(req, request)
        else:
            async with prepare(req) as early:
                if early is not None:
                    return early
                resp = await orig(req, request)
        if req.no_llm or not isinstance(resp, StreamingResponse):
            return resp

        checker = Checker(req.query, context_fn, route_fn)
        inner = resp.body_iterator

        async def gen():
            import asyncio

            async def step() -> bytes | None:
                try:
                    chunk = await it.__anext__()
                except StopAsyncIteration:
                    return None
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                return await anyio.to_thread.run_sync(checker.feed, chunk,
                                                      limiter=_CHECK_LIMITER)

            loop = asyncio.get_running_loop()
            t0 = loop.time()
            next_notice = ansmod.PATIENCE_S
            it = inner.__aiter__()
            pending = None
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(step())
                done, _ = await asyncio.wait({pending}, timeout=1.0)
                if not done:
                    waited = loop.time() - t0
                    if waited >= next_notice:
                        next_notice = waited + ansmod.PATIENCE_REPEAT_S
                        yield sse(EV_DELTA,
                                  {"t": ansmod.patience_note(waited, 1)})
                    continue
                try:
                    out = pending.result()
                finally:
                    pending = None
                if out is None:
                    break
                if out:
                    yield out
            rest = await anyio.to_thread.run_sync(checker.flush,
                                                  limiter=_CHECK_LIMITER)
            if rest:
                yield rest

        headers = {k: v for k, v in resp.headers.items()
                   if k.lower() not in ("content-length", "content-type")}
        return StreamingResponse(gen(), media_type=resp.media_type,
                                 headers=headers)

    return True
