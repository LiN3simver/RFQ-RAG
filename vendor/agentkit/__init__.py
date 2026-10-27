"""agentkit：多個 agent 共用的 A2A 契約與 FastAPI 路由。"""
from __future__ import annotations

from . import client, delegate, envelope, gateway, registry
from .envelope import PROTOCOL, Block, Hit, ToolSpec, sse, tool_err, tool_ok
from .server import AgentSpec, MessageReq, build_router

__all__ = [
    "AgentSpec",
    "Block",
    "Hit",
    "MessageReq",
    "PROTOCOL",
    "ToolSpec",
    "build_router",
    "client",
    "delegate",
    "envelope",
    "gateway",
    "registry",
    "sse",
    "tool_err",
    "tool_ok",
]
