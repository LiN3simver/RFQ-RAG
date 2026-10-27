"""一題的總期限：沿著呼叫鏈往下傳遞剩餘時間。"""
from __future__ import annotations

import os
import time
from contextvars import ContextVar, Token
from typing import Any

HEADER = "X-A2A-Deadline-Ms"
HOP_MARGIN_S = float(os.environ.get("A2A_DEADLINE_HOP_MARGIN_S", "2"))
MIN_LLM_S = float(os.environ.get("A2A_DEADLINE_MIN_LLM_S", "5"))


class Deadline:
    __slots__ = ("budget", "ends")

    def __init__(self, seconds: float) -> None:
        self.budget = max(0.0, seconds)
        self.ends = time.monotonic() + self.budget

    def remaining(self) -> float:
        return self.ends - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def cap(self, seconds: float) -> float:
        return max(0.5, min(seconds, self.remaining()))

    def header_value(self) -> str:
        return str(max(0, int(self.remaining() * 1000)))


def from_header(value: str | None) -> Deadline | None:
    if not value:
        return None
    try:
        ms = float(value)
    except ValueError:
        return None
    return Deadline(ms / 1000 - HOP_MARGIN_S)


_current: ContextVar[Deadline | None] = ContextVar("a2a_deadline", default=None)


def current() -> Deadline | None:
    return _current.get()


def set_current(d: Deadline | None) -> Token:
    return _current.set(d)


def timeout(seconds: float) -> float:
    d = current()
    return seconds if d is None else d.cap(seconds)


def headers() -> dict[str, str]:
    d = current()
    return {} if d is None else {HEADER: d.header_value()}


def expired() -> bool:
    d = current()
    return d is not None and d.expired()


def enough_for_llm() -> bool:
    d = current()
    return d is None or d.remaining() >= MIN_LLM_S


class Expired(Exception):
    def __init__(self, where: str, budget_s: float | None = None) -> None:
        total = f"（{budget_s:.0f} 秒）" if budget_s else ""
        self.where = where
        self.user_message = (f"這一題已經用完時間上限{total}，{where}停止。"
                             "上面已經查到的資料仍然有效。")
        self.hint = ("可以把問題縮小（指定編號、料號或時間範圍）再問一次；"
                     "需要更長的時間請聯絡維護者調整 HUB_MESSAGE_BUDGET_S。")
        super().__init__(self.user_message)


def error_payload(where: str, budget_s: float | None = None) -> dict[str, Any]:
    e = Expired(where, budget_s)
    return {"error": e.user_message, "hint": e.hint, "timeout": True}
