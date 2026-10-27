"""A2A 資料形狀：Hit、Block、ToolSpec 與 SSE 事件。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

PROTOCOL = "a2a/1"

EV_META = "meta"
EV_RESULTS = "results"
EV_TRACE = "trace"
EV_DELTA = "delta"
EV_TITLE = "title"
EV_ERROR = "error"
EV_DONE = "done"

Tone = Literal["neutral", "green", "yellow", "warn", "good"]


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@dataclass
class Block:
    kind: Literal["kv", "text", "table"]
    title: str = ""
    tone: Tone = "neutral"
    rows: list[list[Any]] = field(default_factory=list)
    head: list[str] = field(default_factory=list)
    text: str = ""


@dataclass
class Hit:
    id: str
    title: str = ""
    subtitle: str = ""
    body: str = ""
    score: float | None = None
    why: list[str] = field(default_factory=list)
    badges: list[list[str]] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    links: list[list[str]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Any = None
    ready: bool = True
    not_ready_reason: str = ""

    def public(self) -> dict[str, Any]:
        d = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "ready": self.ready,
        }
        if not self.ready and self.not_ready_reason:
            d["not_ready_reason"] = self.not_ready_reason
        return d

    def for_model(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def tool_ok(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def tool_err(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}
