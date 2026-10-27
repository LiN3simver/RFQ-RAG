"""成本分析 PDF 匯入：解析、對帳、寫入附件與明細；可中斷續跑、可設時間預算。"""
from __future__ import annotations

import argparse
import collections
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import docroot, pdf_cost
from .db import pool

_FILE_RFQ_RE = re.compile(r"(RF[QSD])[-_ ]?(\d{11})[-_ ]?(\d{3})", re.I)

_FETCH_PREFIX_RE = re.compile(r"^([0-9A-Fa-f]{32})_")

_SUMMARY_COLS = {
    "mat": "mat_total", "labor": "labor_total", "tooling": "tooling_total",
    "other": "other_total", "mfg": "mfg_cost", "overhead": "overhead",
    "subtotal": "subtotal", "grand": "grand_total", "moq_a": "moq_a",
}


def rfq_from_name(name: str) -> str | None:
    m = _FILE_RFQ_RE.search(name)
    return f"{m.group(1).upper()}-{m.group(2)}-{m.group(3)}" if m else None


def stratum(path: str) -> str:
    import fitz

    try:
        doc = fitz.open(path)
    except Exception:
        return "error"
    try:
        best = ("", -1, 0)
        for pg in doc:
            t = pg.get_text()
            a = sum(1 for x in pdf_cost.ANCHORS if x in t)
            if (a, len(t)) > (best[1], best[2]):
                best = (t, a, len(t))
    finally:
        doc.close()
    text, anchors, chars = best
    if anchors >= pdf_cost.MIN_ANCHORS:
        return "text_ok"
    if chars < pdf_cost.MIN_CHARS:
        return "no_text"
    return pdf_cost.health(text)


def stratified(paths: list[str], n: int) -> list[str]:
    if n >= len(paths):
        return paths
    buckets: dict[str, list[str]] = collections.defaultdict(list)
    for i, p in enumerate(sorted(paths), 1):
        buckets[stratum(p)].append(p)
        if i % 100 == 0:
            print(f"  分層 {i}/{len(paths)}", flush=True)

    total = sum(len(v) for v in buckets.values())
    picked: list[str] = []
    for key in sorted(buckets, key=lambda k: -len(buckets[k])):
        group = buckets[key]
        take = max(1, round(n * len(group) / total))
        take = min(take, len(group), max(0, n - len(picked)))
        if take <= 0:
            continue
        step = len(group) / take
        picked += [group[int(i * step)] for i in range(take)]
    return sorted(picked)


def _done(cur) -> dict[str, tuple[int, float]]:
    cur.execute("[SQL 已於公開版移除]")
    return {r[0]: (r[1], float(r[2] or 0)) for r in cur.fetchall()}


def _fetch_meta(cur, path: str) -> dict | None:
    m = _FETCH_PREFIX_RE.match(os.path.basename(path))
    if not m:
        return None
    cur.execute(
        "[SQL 已於公開版移除]", (m.group(1).upper(),),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"file_id": m.group(1).upper(), "rfq_no": row[0], "filename": row[1],
            "created_on": row[2], "modified_on": row[3]}


def save(cur, path: str, res: dict, parse_ms: int) -> int:
    st = os.stat(path)
    name = os.path.basename(path)
    s = res.get("summary") or {}
    ids = res.get("ids") or {}
    fetch = _fetch_meta(cur, path)
    rfq_no = rfq_from_name(name)
    row = {
        "path": docroot.to_key(path),
        "file_name": name,
        "rfq_no": (fetch["rfq_no"] if fetch else None) or rfq_no or ids.get("rfq_no"),
        "rfq_no_src": "doc_fetch" if (fetch and fetch["rfq_no"]) else
                      ("filename" if rfq_no else
                       ("content" if ids.get("rfq_no") else None)),
        "eq_no": ids.get("eq_no"),
        "template": s.get("template"),
        "health": res.get("health"),
        "source": res.get("source"),
        "pages": len(res.get("pages") or []),
        "ocr_pages": res.get("ocr_pages") or 0,
        "skipped_pages": res.get("skipped_pages") or 0,
        "currency": s.get("currency"),
        "material_line_sum": s.get("material_line_sum"),
        "line_total_missing": s.get("line_total_missing"),
        "totals_verified": s.get("totals_verified"),
        "rows_ok": s.get("rows_ok"),
        "rows_repaired": s.get("rows_repaired"),
        "rows_unverified": s.get("rows_unverified"),
        "n_materials": len(res.get("materials") or []),
        "file_size": st.st_size,
        "file_mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc),
        "parse_ms": parse_ms,
    }
    row.update({col: s.get(key) for key, col in _SUMMARY_COLS.items()})

    hdr = res.get("header") or {}
    for col in ("part_name", "own_part", "cus_part", "customer_code", "rev",
                "quote_reason", "doc_date", "sales", "factory", "demand_qty",
                "mp_date"):
        row[col] = hdr.get(col)
    row["part_name_norm"] = pdf_cost.norm_part_name(hdr.get("part_name")) or None

    lab = res.get("labor") or {}
    row["n_labor"] = len(lab.get("rows") or [])
    row["labor_line_sum"] = lab.get("line_sum")
    row["labor_verified"] = lab.get("verified")
    if row.get("labor_total") is None and lab.get("total") is not None:
        row["labor_total"] = lab["total"]

    if fetch:
        row["file_id"] = fetch["file_id"]
        row["src_file_name"] = fetch["filename"]
        row["uploaded_on"] = fetch["created_on"]
        row["doc_modified_on"] = fetch["modified_on"]
        row["meta_src"] = "doc_fetch"

    cols = list(row)
    cur.execute(
        "[SQL 已於公開版移除]",
        [row[c] for c in cols],
    )
    doc_id = cur.fetchone()[0]

    cur.execute("[SQL 已於公開版移除]", (doc_id,))
    mcols = ["doc_id", "seq", "part_no", "name", "supplier",
             "supplier_uncertain", "spec", "currency",
             "unit_price_raw", "fx_rate", "unit_price", "qty", "unit", "moq",
             "loss_pct", "line_total", "lead_time", "remark",
             "check_status", "repaired"]
    src = {"check_status": "check"}
    cur.executemany(
        "[SQL 已於公開版移除]",
        [[doc_id, i] + [m.get(src.get(c, c)) for c in mcols[2:]]
         for i, m in enumerate(res.get("materials") or [], 1)],
    )

    cur.execute("[SQL 已於公開版移除]", (doc_id,))
    lcols = ["doc_id", "seq", "category", "note", "cost", "stations",
             "total_sec", "pph", "hr_rate", "rate_currency", "efficiency_pct",
             "check_status"]
    cur.executemany(
        "[SQL 已於公開版移除]",
        [[doc_id, i] + [r.get(src.get(c, c)) for c in lcols[2:]]
         for i, r in enumerate(lab.get("rows") or [], 1)],
    )
    return doc_id


def unlink_plm(cur, doc_id: int) -> None:
    cur.execute(
        "[SQL 已於公開版移除]", (doc_id,))


def run(directory: str, sample: int | None, force: bool, no_ocr: bool,
        deadline: float | None = None) -> collections.Counter:
    paths = sorted(str(p) for p in Path(directory).glob("*.pdf"))
    if not paths:
        print(f"{directory} 裡沒有 PDF")
        return collections.Counter()
    print(f"{directory}：{len(paths)} 份 PDF")

    if sample:
        print(f"分層抽樣 {sample} 份…")
        paths = stratified(paths, sample)
        print(f"抽出 {len(paths)} 份")

    with pool().connection() as conn:
        with conn.cursor() as cur:
            seen = {} if force else _done(cur)
        stats = collections.Counter()
        t_all = time.time()
        for i, p in enumerate(paths, 1):
            if deadline and time.time() > deadline:
                stats["deadline_stop"] += 1
                print(f"時間預算用完，停在 {i}/{len(paths)}，剩下的留給下一輪",
                      flush=True)
                break
            st = os.stat(p)
            prev = seen.get(docroot.to_key(p))
            if prev and prev[0] == st.st_size and abs(prev[1] - st.st_mtime) < 1:
                stats["skipped_done"] += 1
                continue
            t0 = time.time()
            try:
                res = pdf_cost.parse(p, ocr=not no_ocr)
            except Exception as e:
                stats["error"] += 1
                print(f"[{i}/{len(paths)}] ERROR {os.path.basename(p)} "
                      f"{type(e).__name__}: {e}", flush=True)
                continue
            ms = round((time.time() - t0) * 1000)
            is_fetched = bool(_FETCH_PREFIX_RE.match(os.path.basename(p)))
            with conn.cursor() as cur:
                doc_id = save(cur, p, res, ms)
                if prev and not is_fetched:
                    unlink_plm(cur, doc_id)
                    stats["plm_unlinked"] += 1
            conn.commit()
            n = len(res["materials"])
            stats["docs"] += 1
            stats["materials"] += n
            stats[f"source_{res['source'] or 'none'}"] += 1
            if res["summary"].get("totals_verified"):
                stats["verified"] += 1
            print(f"[{i}/{len(paths)}] {os.path.basename(p):34} "
                  f"{res['health']:12} {str(res['source']):8} "
                  f"材料 {n:3}  對帳 {str(res['summary'].get('totals_verified')):5} "
                  f"{ms/1000:5.1f}s", flush=True)

    el = time.time() - t_all
    print(f"\n完成 {el/60:.1f} 分鐘")
    for k, v in sorted(stats.items()):
        print(f"  {k:18} {v}")
    return stats


STAGE = "ingest_pdf"


def main() -> None:
    ap = argparse.ArgumentParser(description="成本分析 PDF 匯入")
    ap.add_argument("--dir", required=True, help="放 PDF 的目錄")
    ap.add_argument("--sample", type=int, help="只做 N 份（分層抽樣，可重現）")
    ap.add_argument("--force", action="store_true", help="已匯入的也重做")
    ap.add_argument("--no-ocr", action="store_true", help="跳過 OCR（只吃文字層）")
    ap.add_argument("--budget-min", type=float,
                    help="最多跑幾分鐘就停，剩下的留給下一輪（排程用）")
    a = ap.parse_args()

    deadline = time.time() + a.budget_min * 60 if a.budget_min else None
    run(a.dir, a.sample, a.force, a.no_ocr, deadline)


if __name__ == "__main__":
    main()
