"""材料料號查價、附件品名的模糊反查，以及附件的材料／人工明細查詢。"""
from __future__ import annotations

import os
import re
from datetime import datetime
from decimal import Decimal

from .db import pool

PART_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,4}\d{6,9})(?![A-Za-z0-9])")

_ITEM_COLS = """
    m.part_no, m.name, m.supplier, m.supplier_uncertain, m.spec, m.currency,
    m.unit_price_raw, m.fx_rate, m.unit_price, m.qty, m.unit, m.moq,
    m.seq, m.loss_pct, m.line_total, m.lead_time, m.remark,
    m.check_status, m.repaired,
    d.id AS doc_id, d.rfq_no, d.eq_no, d.file_name, d.currency AS doc_currency,
    d.totals_verified, d.source, d.ingested_on,
    d.src_file_name, d.uploaded_on, d.doc_modified_on, d.file_mtime, d.meta_src
"""

_LABOR_COLS = """
    l.seq, l.category, l.note, l.cost, l.stations, l.total_sec, l.pph,
    l.hr_rate, l.rate_currency, l.efficiency_pct, l.check_status
"""

_VER_COLS = ("src_file_name, uploaded_on, doc_modified_on, "
             "file_mtime, meta_src, ingested_on")

_HDR_FIELD_COLS = ("part_name, own_part, cus_part, customer_code, rev, "
                   "quote_reason, doc_date, sales, factory, demand_qty, "
                   "mp_date")

HEADER_LABELS: tuple[tuple[str, str], ...] = (
    ("eq_no", "EQ NO."),
    ("rev", "Rev"),
    ("quote_reason", "報價原因"),
    ("doc_date", "日期"),
    ("customer_code", "Customer code"),
    ("cus_part", "Cus. P/N"),
    ("own_part", "ACME P/N"),
    ("part_name", "Part Name"),
    ("sales", "Sales"),
    ("factory", "製造廠"),
    ("demand_qty", "需求量"),
    ("mp_date", "量產日期"),
)


def header_lines(doc: dict) -> list[str]:
    got = [f"{label}：{doc[key]}" for key, label in HEADER_LABELS
           if doc.get(key)]
    missing = [label for key, label in HEADER_LABELS if not doc.get(key)]
    if missing:
        got.append(f"（表頭這幾格沒有值：{'、'.join(missing)}"
                   f" —— 可能是 PDF 上本來就空著，也可能是這份的文字層／OCR "
                   f"讀不到那一格，兩種都有）")
    return got


_VER_ORDER = "[SQL 已於公開版移除]"

TRUSTED = ("ok", "repaired")


def _rows(cur) -> list[dict]:
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


_TZ = os.environ.get("DISPLAY_TZ") or ""


def _stamp(ts: datetime | None) -> str:
    if ts is None:
        return ""
    if isinstance(ts, str):
        return ts.replace("T", " ")[:16]
    try:
        if _TZ:
            from zoneinfo import ZoneInfo
            ts = ts.astimezone(ZoneInfo(_TZ))
        else:
            ts = ts.astimezone()
    except Exception:
        pass
    return ts.strftime("%Y-%m-%d %H:%M")


def file_version(doc: dict) -> dict:
    name = doc.get("src_file_name") or doc.get("file_name") or "附件"
    plm = doc.get("doc_modified_on") or doc.get("uploaded_on")
    if plm:
        stamp = _stamp(plm)
        return {"name": name, "time": plm, "kind": "plm", "stamp": stamp,
                "text": f"更新 {stamp}", "short": f"更新 {stamp[:10]}"}
    local = doc.get("file_mtime") or doc.get("ingested_on")
    if local:
        stamp = _stamp(local)
        return {"name": name, "time": local, "kind": "local", "stamp": stamp,
                "text": f"下載 {stamp}", "short": f"下載 {stamp[:10]}"}
    return {"name": name, "time": None, "kind": "none", "stamp": "",
            "text": "更新時間不明", "short": "時間不明"}


def lookup(part_no: str, limit: int = 20) -> dict:
    key = (part_no or "").strip().upper()
    if not key:
        return {"found": False, "part_no": part_no, "reason": "empty"}

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "[SQL 已於公開版移除]",
            (key, limit),
        )
        items = _rows(cur)
        if not items:
            cur.execute(
                "[SQL 已於公開版移除]")
            (indexed,) = cur.fetchone()
            return {"found": False, "part_no": key, "indexed_materials": indexed}

    return {"found": True, "part_no": key, "n": len(items),
            "price": _price_range(items), "items": items}


def _price_range(items: list[dict]) -> dict:
    by_cur: dict[str, list[Decimal]] = {}
    for it in items:
        v = it.get("unit_price_raw")
        if v is None or it.get("check_status") not in TRUSTED:
            continue
        by_cur.setdefault(it.get("currency") or "?", []).append(v)
    out = {}
    for cur_, vals in by_cur.items():
        vals.sort()
        out[cur_] = {
            "n": len(vals),
            "min": vals[0],
            "median": _median(vals),
            "max": vals[-1],
        }
    return out


def _median(vals: list[Decimal]) -> Decimal:
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def find_similar(prefix: str, limit: int = 20) -> dict:
    key = (prefix or "").strip().upper()
    if len(key) < 3:
        return {"part_no": prefix, "results": [], "reason": "too_short"}

    if PART_RE.fullmatch(key):
        pattern = f"{re.match(r'[A-Z]+', key).group(0)}%"
        mode = "family"
    else:
        pattern = f"%{key}%"
        mode = "contains"

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "[SQL 已於公開版移除]",
            (pattern, key, limit),
        )
        return {"part_no": key, "mode": mode, "pattern": pattern,
                "results": _rows(cur)}


HEADER_STRATEGIES = ("exact_norm", "own_part", "cus_part",
                     "contains_norm", "similar")

_HDR_STRATEGY_LABEL = {
    "exact_norm": "附件表頭 Part Name 完全相同（忽略空白與標點）",
    "own_part": "附件表頭 ACME P/N 完全相同",
    "cus_part": "附件表頭 Cus. P/N（客戶料號）完全相同",
    "contains_norm": "附件表頭 Part Name 包含它（或它包含表頭）",
    "similar": "附件表頭 Part Name 三連字元相似度比對（OCR 會認錯字，"
               "所以留這一段）",
}

HEADER_SIM_MIN = 0.45
HEADER_SIM_MARGIN = Decimal("0.05")

HEADER_MIN_LEN = 12

_HDR_COLS = """
    id, path, rfq_no, eq_no, file_name, part_name, own_part, cus_part,
    customer_code, rev, quote_reason, doc_date, sales, factory, demand_qty,
    mp_date, currency, template, health, source,
    mat_total, labor_total, tooling_total, other_total, mfg_cost, overhead,
    subtotal, grand_total, moq_a, material_line_sum, totals_verified,
    n_materials, n_labor, labor_total, labor_line_sum, labor_verified
"""


def _hdr_try(cur, strategy: str, key: str, norm: str, limit: int) -> list[dict]:
    if strategy == "exact_norm":
        cond, args = "[SQL 已於公開版移除]", [norm]
    elif strategy == "own_part":
        cond, args = "[SQL 已於公開版移除]", [key]
    elif strategy == "cus_part":
        cond, args = "[SQL 已於公開版移除]", [key]
    elif strategy == "contains_norm":
        if len(norm) < HEADER_MIN_LEN:
            return []
        cond = ("[SQL 已於公開版移除]")
        args = [f"%{norm}%", HEADER_MIN_LEN, norm]
    elif strategy == "similar":
        if len(norm) < HEADER_MIN_LEN:
            return []
        cond = "[SQL 已於公開版移除]"
        args = [norm, HEADER_SIM_MIN]
    else:
        return []

    sim = "[SQL 已於公開版移除]"
    cur.execute(
        "[SQL 已於公開版移除]",
        [norm] + args + [limit])
    return _rows(cur)


def by_part_name(text: str, limit: int = 10) -> dict:
    key = (text or "").strip().upper()
    from .pdf_cost import norm_part_name
    norm = norm_part_name(key)
    if not norm:
        return {"found": False, "query": text, "reason": "empty",
                "attempts": []}

    attempts: list[dict] = []
    rows: list[dict] = []
    used = ""
    with pool().connection() as conn, conn.cursor() as cur:
        for strategy in HEADER_STRATEGIES:
            got = _hdr_try(cur, strategy, key, norm, limit)
            attempts.append({"strategy": strategy,
                             "label": _HDR_STRATEGY_LABEL[strategy],
                             "n": len(got)})
            if got:
                rows, used = got, strategy
                break
        if not rows:
            cur.execute("[SQL 已於公開版移除]")
            docs, with_name = cur.fetchone()
            return {"found": False, "query": text, "attempts": attempts,
                    "indexed_docs": docs, "docs_with_part_name": with_name}

        ids = [r["id"] for r in rows]
        cur.execute(
            "[SQL 已於公開版移除]",
            (ids,))
        materials = _rows(cur)
        cur.execute(
            "[SQL 已於公開版移除]",
            (ids,))
        labor = _rows(cur)

    ambiguous = False
    if used == "similar" and len(rows) > 1:
        top, second = rows[0].get("sim") or 0, rows[1].get("sim") or 0
        ambiguous = (Decimal(str(top)) - Decimal(str(second))) < HEADER_SIM_MARGIN

    return {"found": True, "query": text, "norm": norm,
            "matched_by": used, "matched_label": _HDR_STRATEGY_LABEL[used],
            "n": len(rows), "attempts": attempts, "docs": rows,
            "ambiguous": ambiguous,
            "materials": materials, "labor": labor}


def search_spec(text: str, limit: int = 20) -> dict:
    q = (text or "").strip()
    if len(q) < 2:
        return {"query": text, "results": []}
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "[SQL 已於公開版移除]",
            (f"%{q}%", f"%{q}%", q, limit),
        )
        return {"query": q, "results": _rows(cur)}


def by_rfq(rfq_no: str, limit: int = 200) -> dict:
    key = (rfq_no or "").strip().upper()
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "[SQL 已於公開版移除]",
            (key,),
        )
        docs = _rows(cur)
        if not docs:
            return {"found": False, "rfq_no": key}
        ids = [d["id"] for d in docs]
        cur.execute(
            "[SQL 已於公開版移除]",
            (ids, limit),
        )
        materials = _rows(cur)
        cur.execute(
            "[SQL 已於公開版移除]",
            (ids,),
        )
        return {"found": True, "rfq_no": key, "docs": docs,
                "materials": materials, "labor": _rows(cur)}


def labor_by_rfq(rfq_no: str) -> dict:
    key = (rfq_no or "").strip().upper()
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "[SQL 已於公開版移除]",
            (key,),
        )
        docs = _rows(cur)
        if not docs:
            return {"found": False, "rfq_no": key}
        cur.execute(
            "[SQL 已於公開版移除]",
            ([d["id"] for d in docs],),
        )
        return {"found": True, "rfq_no": key, "docs": docs,
                "labor": _rows(cur)}


def labor_coverage() -> dict:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "[SQL 已於公開版移除]")
        with_labor, verified, docs = cur.fetchone()
        cur.execute(
            "[SQL 已於公開版移除]")
        rows, with_params, ok, mismatch = cur.fetchone()
    return {"docs": docs, "docs_with_labor": with_labor,
            "docs_labor_verified": verified, "labor_rows": rows,
            "rows_with_hours": with_params, "rows_ok": ok,
            "rows_mismatch": mismatch}


def doc_path(doc_id: int) -> dict | None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("[SQL 已於公開版移除]", (doc_id,))
        rows = _rows(cur)
    return rows[0] if rows else None


def docs_for_rfq(rfq_no: str) -> list[dict]:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "[SQL 已於公開版移除]",
            ((rfq_no or "").strip().upper(),))
        return _rows(cur)


def coverage() -> dict:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "[SQL 已於公開版移除]")
        docs, verified, mats, rfqs, versioned = cur.fetchone()
        cur.execute(
            "[SQL 已於公開版移除]")
        (parts,) = cur.fetchone()
    return {"docs": docs, "docs_verified": verified, "material_rows": mats,
            "distinct_parts": parts, "rfqs_covered": rfqs,
            "docs_versioned": versioned}
