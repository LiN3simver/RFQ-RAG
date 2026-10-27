"""入場控管：限制同時執行的檢索／工具數與排隊上限，滿載時立即回「忙碌」。"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

SLOTS = int(os.environ.get("RFQ_MAX_CONCURRENT", "4"))
MAX_WAITING = int(os.environ.get("RFQ_MAX_WAITING", "8"))
WAIT_S = float(os.environ.get("RFQ_QUEUE_WAIT_S", "5"))
MAX_QUERY_CHARS = int(os.environ.get("RFQ_MAX_QUERY_CHARS", "2000"))


class Busy(RuntimeError):
    def __init__(self, waited_s: float, reason: str) -> None:
        self.waited_s = waited_s
        self.reason = reason
        self.user_message = (f"RFQ 查詢目前同時處理 {SLOTS} 筆，已經滿了"
                             f"（{reason}），這一題沒有開始查。")
        self.hint = "請等幾秒再送一次；一直遇到的話請聯絡維護者調整同時查詢上限。"
        super().__init__(self.user_message)


class TooLong(ValueError):
    def __init__(self, n: int) -> None:
        self.user_message = (f"問句太長（{n} 字，上限 {MAX_QUERY_CHARS} 字），"
                             "這一題沒有開始查。")
        self.hint = "請只貼要查的那一段（料號、RFQ 編號或品名），不要整份文件貼進來。"
        super().__init__(self.user_message)


_sem = threading.BoundedSemaphore(SLOTS)
_lock = threading.Lock()
_local = threading.local()
_counts = {"admitted": 0, "running": 0, "waiting": 0, "served": 0,
           "rejected_full": 0, "rejected_timeout": 0, "rejected_too_long": 0}


def capacity() -> int:
    return SLOTS + MAX_WAITING


def check_query(query: str) -> None:
    if len(query or "") > MAX_QUERY_CHARS:
        with _lock:
            _counts["rejected_too_long"] += 1
        raise TooLong(len(query))


class Ticket:
    __slots__ = ("_held", "_done")

    def __init__(self) -> None:
        self._held = False
        self._done = False

    def acquire(self) -> None:
        t0 = time.monotonic()
        if not _sem.acquire(blocking=False):
            with _lock:
                _counts["waiting"] += 1
            try:
                got = _sem.acquire(timeout=WAIT_S)
            finally:
                with _lock:
                    _counts["waiting"] -= 1
            if not got:
                with _lock:
                    _counts["rejected_timeout"] += 1
                self.release()
                raise Busy(time.monotonic() - t0, f"等了 {WAIT_S:g} 秒仍沒有空位")
        self._held = True
        with _lock:
            _counts["running"] += 1

    def release(self) -> None:
        if self._done:
            return
        self._done = True
        with _lock:
            _counts["admitted"] -= 1
            if self._held:
                _counts["running"] -= 1
                _counts["served"] += 1
        if self._held:
            _sem.release()


def enter() -> Ticket:
    with _lock:
        if _counts["admitted"] >= capacity():
            _counts["rejected_full"] += 1
            raise Busy(0.0, f"{SLOTS} 筆在查、{MAX_WAITING} 筆在等")
        _counts["admitted"] += 1
    return Ticket()


@contextmanager
def slot(ticket: Ticket | None = None) -> Iterator[None]:
    depth = getattr(_local, "depth", 0)
    if depth:
        if ticket is not None:
            ticket.release()
        _local.depth = depth + 1
        try:
            yield
        finally:
            _local.depth -= 1
        return

    t = ticket if ticket is not None else enter()
    try:
        t.acquire()
    except BaseException:
        t.release()
        raise
    _local.depth = 1
    try:
        yield
    finally:
        _local.depth = 0
        t.release()


def stats() -> dict[str, Any]:
    with _lock:
        return {"slots": SLOTS, "max_waiting": MAX_WAITING, "wait_s": WAIT_S,
                "max_query_chars": MAX_QUERY_CHARS, **_counts}
