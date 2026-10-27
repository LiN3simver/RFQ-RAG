"""FastAPI 入口，只掛 /a2a/*。公開版僅展示架構，不含資料與 SQL，無法實際查詢。"""
from __future__ import annotations

from fastapi import FastAPI

from src import db

db.set_profile("api")

app = FastAPI(title="RFQ-RAG — RFQ/S/D")


try:
    from src import a2a

    a2a.mount(app)
except Exception as _e:  # pragma: no cover
    print(f"[a2a] 未掛載（{type(_e).__name__}: {_e}）")
