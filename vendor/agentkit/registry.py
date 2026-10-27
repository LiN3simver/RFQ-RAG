"""agent 名冊：各 agent 的 id、位址與能力。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentRef:
    agent_id: str
    name: str
    role: str
    port: int
    project: str
    downstream: tuple[str, ...] = ()
    upstream: tuple[str, ...] = ()
    native_ui: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def base_url(self) -> str:
        env = os.environ.get(f"A2A_{self.agent_id.upper()}_URL")
        return env or f"http://127.0.0.1:{self.port}"


AGENTS: tuple[AgentRef, ...] = (
    AgentRef(
        agent_id="bom",
        name="Bom Agent",
        role="依規格建構新產品 BOM：決定要哪些槽位、各多少用量",
        port=8500,
        project="Bom-RAG",
        downstream=("material", "partrag", "car8d"),
        tags=("BOM", "槽位", "用量"),
    ),
    AgentRef(
        agent_id="material",
        name="材料建議 Agent",
        role="每個 BOM 槽位放哪一個具體料號（元件層規格比對）",
        port=8400,
        project="material-agent",
        upstream=("bom",),
        tags=("選料", "元件", "替代料"),
    ),
    AgentRef(
        agent_id="partrag",
        name="相似搜尋建議 Agent",
        role="成品線材語料的相似料號檢索",
        port=8000,
        project="Part-RAG",
        upstream=("bom", "rfq"),
        tags=("相似料號", "工程圖", "PLM 欄位"),
    ),
    AgentRef(
        agent_id="rfq",
        name="產品估價 Agent",
        role="RFQ/S/D 歷史紀錄查詢與成本區間參考",
        port=8300,
        project="RFQ-RAG",
        downstream=("bom", "car8d", "partrag"),
        tags=("RFQ", "歷史成本", "合理性檢核"),
    ),
    AgentRef(
        agent_id="car8d",
        name="8D 客訴單 Agent",
        role="客訴抱怨單(8D) 查詢：單號/料號/客戶反查、客訴是否成立、責任歸屬、D3~D8 對策",
        port=8700,
        project="8D-Report-RAG",
        downstream=(),
        upstream=("bom", "rfq"),
        tags=("客訴", "8D", "CAR", "品質異常", "根本原因"),
    ),
)

BY_ID: dict[str, AgentRef] = {a.agent_id: a for a in AGENTS}

HUB_PORT = int(os.environ.get("A2A_HUB_PORT", "8600"))


def get(agent_id: str) -> AgentRef:
    if agent_id not in BY_ID:
        raise KeyError(f"未知的 agent_id：{agent_id}（可用：{', '.join(BY_ID)}）")
    return BY_ID[agent_id]


def peers_of(agent_id: str) -> tuple[AgentRef, ...]:
    return tuple(BY_ID[p] for p in get(agent_id).downstream if p in BY_ID)
