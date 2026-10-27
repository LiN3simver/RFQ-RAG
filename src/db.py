"""資料庫連線池與查詢預算（API／批次兩種 profile）。"""
from __future__ import annotations

import os
import sys
import threading
from typing import Any

from dotenv import load_dotenv
from pgvector.psycopg import register_vector
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool

load_dotenv()

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


PROFILES: dict[str, dict[str, float]] = {
    "api": {
        "connect_timeout_s": 5,
        "pool_wait_s": 10,
        "max_waiting": 16,
        "statement_timeout_ms": 15_000,
        "lock_timeout_ms": 5_000,
        "idle_in_tx_timeout_ms": 60_000,
    },
    "batch": {
        "connect_timeout_s": 15,
        "pool_wait_s": 30,
        "max_waiting": 0,
        "statement_timeout_ms": 0,
        "lock_timeout_ms": 0,
        "idle_in_tx_timeout_ms": 0,
    },
}
_ENV = {
    "connect_timeout_s": "PG_CONNECT_TIMEOUT_S",
    "pool_wait_s": "PG_POOL_WAIT_S",
    "max_waiting": "PG_POOL_MAX_WAITING",
    "statement_timeout_ms": "PG_STATEMENT_TIMEOUT_MS",
    "lock_timeout_ms": "PG_LOCK_TIMEOUT_MS",
    "idle_in_tx_timeout_ms": "PG_IDLE_IN_TX_TIMEOUT_MS",
}
_profile = "batch"


def set_profile(name: str) -> None:
    global _profile
    if name not in PROFILES:
        raise ValueError(f"未知的 profile：{name}（可用：{', '.join(PROFILES)}）")
    if _pool is not None and name != _profile:
        raise RuntimeError("連線池已經建立，profile 改不動了；set_profile 要更早呼叫")
    _profile = name


def budget() -> dict[str, Any]:
    out: dict[str, Any] = {"profile": _profile}
    for k, v in PROFILES[_profile].items():
        raw = os.environ.get(_ENV[k])
        out[k] = float(raw) if raw not in (None, "") else v
    return out


def _conn_kwargs(b: dict[str, Any], *, session_timeouts: bool) -> dict[str, Any]:
    dsn = conninfo_to_dict(os.environ["PG_DSN"])
    kw: dict[str, Any] = {}
    if "connect_timeout" not in dsn and b["connect_timeout_s"]:
        kw["connect_timeout"] = int(b["connect_timeout_s"])
    if session_timeouts:
        opts = [f"-c {guc}={int(b[key])}" for guc, key in (
            ("statement_timeout", "statement_timeout_ms"),
            ("lock_timeout", "lock_timeout_ms"),
            ("idle_in_transaction_session_timeout", "idle_in_tx_timeout_ms"),
        ) if b[key]]
        if opts:
            kw["options"] = " ".join(opts + ([dsn["options"]] if dsn.get("options") else []))
    return kw


def pool() -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        b = budget()
        _pool = ConnectionPool(
            os.environ["PG_DSN"],
            kwargs=_conn_kwargs(b, session_timeouts=True),
            min_size=1,
            max_size=8,
            configure=register_vector,
            check=ConnectionPool.check_connection,
            timeout=b["pool_wait_s"],
            max_waiting=int(b["max_waiting"]),
            open=True,
        )
    return _pool


def pool_stats() -> dict[str, Any] | None:
    if _pool is None:
        return None
    s = _pool.get_stats()
    keep = ("pool_min", "pool_max", "pool_size", "pool_available",
            "requests_waiting", "requests_num", "requests_queued",
            "requests_wait_ms", "requests_errors", "connections_errors",
            "connections_lost", "returns_bad")
    return {k: s[k] for k in keep if k in s}


def connect(autocommit: bool = False):
    import psycopg

    return psycopg.connect(os.environ["PG_DSN"], autocommit=autocommit,
                           **_conn_kwargs(budget(), session_timeouts=False))
