"""產品料號查歷史：比對策略由嚴到寬逐段放寬，全部落空才回報找不到。"""
from __future__ import annotations

import re
from typing import Any

from . import timefilter as tfmod
from .db import pool

PRODUCT_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{8,14}-[0-9A-Za-z]{1,3})"
    r"(?![A-Za-z0-9-])")


def candidates(text: str) -> list[str]:
    out, seen = [], set()
    for m in PRODUCT_RE.finditer(text or ""):
        tok = m.group(1).upper()
        if tok in seen:
            continue
        head = tok.split("-")[0]
        if not re.search(r"\d", head) or len(re.findall(r"[A-Z]", head)) < 1:
            continue
        seen.add(tok)
        out.append(tok)
    return out


_COLS = """
    id, rfq_no, rfq_kind, state, is_primary, part_no, cust_part,
    spec_desc, customer, final_customer, rfw_no, engineer, currency,
    created_on, modified_on, year_qty, moq_a, std_seconds,
    mat_cost, tooling_cost, labor_cost, work_hr_cost, mfg_cost,
    outsource_cost, operating_cost, invest_cost, mp_cost,
    mat_cost_usd, tooling_cost_usd, labor_cost_usd, work_hr_cost_usd,
    mfg_cost_usd, outsource_cost_usd, operating_cost_usd, invest_cost_usd,
    mp_cost_usd, unit_cost_usd, sample_cost_usd
"""

_COST_COLS = ("mat_cost", "labor_cost", "mfg_cost", "tooling_cost",
              "mat_cost_usd", "labor_cost_usd", "mfg_cost_usd",
              "tooling_cost_usd", "unit_cost_usd")

STRATEGIES = ("exact_part", "exact_alias", "prefix", "contains", "normalized")

_STRATEGY_LABEL = {
    "exact_part": "產品料號完全相同",
    "exact_alias": "客戶料號／舊系統料號／回報料號完全相同",
    "prefix": "產品料號以它開頭（欄位裡還接了版次或說明文字）",
    "contains": "產品料號裡面含有它",
    "normalized": "去掉 -／_／. ／空白之後相同",
}


def _rows(cur) -> list[dict]:
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _norm(s: str) -> str:
    return re.sub(r"[-_./\s]", "", (s or "").upper())


def _try(cur, strategy: str, key: str, where_time: list[str],
         args_time: list, limit: int) -> list[dict]:
    if strategy == "exact_part":
        cond, args = "[SQL 已於公開版移除]", [key]
    elif strategy == "exact_alias":
        cond = ("[SQL 已於公開版移除]")
        args = [key, key, key]
    elif strategy == "prefix":
        cond = "[SQL 已於公開版移除]"
        args = [key + "%", "^" + re.escape(key) + "[A-Z0-9]"]
    elif strategy == "contains":
        cond, args = "[SQL 已於公開版移除]", ["%" + key + "%"]
    elif strategy == "normalized":
        cond = "[SQL 已於公開版移除]"
        args = [_norm(key)]
    else:
        return []

    where = [f"({cond})"] + where_time
    cur.execute(
        "[SQL 已於公開版移除]",
        args + list(args_time) + [limit])
    return _rows(cur)


def lookup(part_no: str, tf: tfmod.TimeFilter | None = None,
           limit: int = 30, kind: str | None = None,
           want_doc: bool = False) -> dict[str, Any]:
    key = (part_no or "").strip().upper()
    if not key:
        return {"found": False, "part_no": part_no, "reason": "empty",
                "attempts": []}

    where_time, args_time = tfmod.sql(tf)
    if kind:
        where_time = list(where_time) + ["[SQL 已於公開版移除]"]
        args_time = list(args_time) + [kind.upper()]
    attempts: list[dict[str, Any]] = []
    rows: list[dict] = []
    used = ""

    with pool().connection() as conn, conn.cursor() as cur:
        for strategy in STRATEGIES:
            got = _try(cur, strategy, key, where_time, args_time, limit)
            attempts.append({"strategy": strategy,
                             "label": _STRATEGY_LABEL[strategy],
                             "n": len(got)})
            if got:
                rows, used = got, strategy
                break

        if not rows:
            exists_any = 0
            if where_time:
                cur.execute(
                    "[SQL 已於公開版移除]", (key, key + "%"))
                (exists_any,) = cur.fetchone()
            cur.execute("[SQL 已於公開版移除]")
            (n_parts,) = cur.fetchone()
            return {"found": False, "part_no": key, "attempts": attempts,
                    "indexed_products": n_parts,
                    "filtered_out_by_time": exists_any,
                    "kind": kind, "time": tf.label if tf else ""}

        nos = [r["rfq_no"] for r in rows if r.get("rfq_no")]
        all_docs: dict[str, list[dict]] = {}
        if nos:
            cur.execute(
                "[SQL 已於公開版移除]",
                (nos,))
            for d in _rows(cur):
                all_docs.setdefault(d["rfq_no"], []).append(d)

    total = len(rows)
    n_with_doc = sum(1 for r in rows if all_docs.get(r.get("rfq_no")))
    rows = tfmod.sort_rows(rows, tf)
    no_doc_fallback = False
    if tf and tf.want_one and rows:
        picked = rows[0]
        if want_doc:
            with_doc = [r for r in rows if all_docs.get(r.get("rfq_no"))]
            if with_doc:
                picked = with_doc[0]
            else:
                no_doc_fallback = True
        rows = [picked]

    docs = {r["rfq_no"]: all_docs[r["rfq_no"]]
            for r in rows if all_docs.get(r.get("rfq_no"))}
    return {"found": True, "part_no": key, "matched_by": used,
            "matched_label": _STRATEGY_LABEL[used],
            "n": len(rows), "n_total": total, "attempts": attempts,
            "rows": rows, "docs": docs, "kind": kind,
            "n_total_with_doc": n_with_doc,
            "no_doc_fallback": no_doc_fallback,
            "cost": _cost_summary(rows), "time": tf.label if tf else "",
            "truncated_by_time": bool(tf and tf.want_one and total > 1)}


def _cost_summary(rows: list[dict]) -> dict[str, Any]:
    zero = filled = blank = 0
    with_cost: list[str] = []
    for r in rows:
        vals = [r.get(c) for c in _COST_COLS]
        if all(v is None for v in vals):
            blank += 1
        elif all((v is None or v == 0) for v in vals):
            zero += 1
        else:
            filled += 1
            if r.get("rfq_no"):
                with_cost.append(r["rfq_no"])
    return {"rows_with_cost": filled, "rows_all_zero": zero,
            "rows_unfilled": blank, "rfq_with_cost": with_cost}


def coverage() -> dict[str, Any]:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "[SQL 已於公開版移除]")
        rows, parts = cur.fetchone()
    return {"rows_with_part_no": rows, "distinct_products": parts}
