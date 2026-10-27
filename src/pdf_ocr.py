"""掃描頁文字還原：RapidOCR 認字、PDF 向量框線定欄位，讀短的儲存格單獨裁切重掃。"""
from __future__ import annotations

import os
from functools import lru_cache

DPI = int(os.environ.get("OCR_DPI", "300"))

MIN_SCORE = float(os.environ.get("OCR_MIN_SCORE", "0.5"))

CELL_RESCAN_SCORE = float(os.environ.get("OCR_CELL_RESCAN_SCORE", "0.75"))
CELL_RESCAN_DPI = int(os.environ.get("OCR_CELL_RESCAN_DPI", "600"))
CELL_RESCAN_MIN_WIDTH = 20.0
CELL_RESCAN_MAX_PER_PAGE = int(os.environ.get("OCR_CELL_RESCAN_MAX", "80"))

CELL_RESCAN_MIN_COVERAGE = float(os.environ.get("OCR_CELL_RESCAN_COVERAGE", "0.85"))

CELL_RESCAN_COVERAGE_MIN_WIDTH = float(
    os.environ.get("OCR_CELL_RESCAN_COVERAGE_WIDTH", "60"))

CELL_CONFIDENT_SCORE = float(os.environ.get("OCR_CELL_CONFIDENT_SCORE", "0.90"))

Word = tuple[float, float, float, float, str, float]


@lru_cache(maxsize=1)
def _engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def available() -> bool:
    try:
        _engine()
        return True
    except Exception:
        return False


def words(path: str, page_no: int, dpi: int = DPI) -> list[Word]:
    import fitz
    import numpy as np

    doc = fitz.open(path)
    try:
        pm = doc[page_no].get_pixmap(dpi=dpi)
        img = np.frombuffer(pm.samples, dtype=np.uint8).reshape(
            pm.height, pm.width, pm.n)
    finally:
        doc.close()

    res, _ = _engine()(img)
    scale = dpi / 72.0
    out: list[Word] = []
    for box, text, score in res or []:
        if score < MIN_SCORE or not (text or "").strip():
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append((min(xs) / scale, min(ys) / scale,
                    max(xs) / scale, max(ys) / scale, text.strip(), score))
    return out


def page_text(ws: list[Word]) -> str:
    if not ws:
        return ""
    items = sorted(ws, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines: list[list[Word]] = [[items[0]]]
    for w in items[1:]:
        cur = lines[-1]
        cy = (w[1] + w[3]) / 2
        ref_cy = sum((x[1] + x[3]) / 2 for x in cur) / len(cur)
        ref_h = sum(x[3] - x[1] for x in cur) / len(cur)
        if abs(cy - ref_cy) <= ref_h * 0.6:
            cur.append(w)
        else:
            lines.append([w])
    return "\n".join(
        " ".join(w[4] for w in sorted(line, key=lambda w: w[0]))
        for line in lines
    )


def fill_tables(pl_page, ws: list[Word], path: str | None = None,
               page_no: int | None = None
               ) -> tuple[list[list[list[str | None]]],
                          list[list[list[bool]]]]:
    fitz_page = None
    doc = None
    if path is not None and page_no is not None:
        import fitz

        doc = fitz.open(path)
        fitz_page = doc[page_no]
    budget = [CELL_RESCAN_MAX_PER_PAGE]
    try:
        out: list[list[list[str | None]]] = []
        unc: list[list[list[bool]]] = []
        for table in pl_page.find_tables():
            rows: list[list[str | None]] = []
            urows: list[list[bool]] = []
            for row in table.rows:
                cells = row.cells
                matched = [_match_words(c, ws) if c else None for c in cells]
                row_has_content = any(matched)
                texts: list[str | None] = []
                confs: list[bool] = []
                for c, m in zip(cells, matched):
                    if c:
                        t, u = _cell_text(c, m, fitz_page, budget, row_has_content)
                    else:
                        t, u = None, False
                    texts.append(t)
                    confs.append(u)
                rows.append(texts)
                urows.append(confs)
            if rows:
                out.append(rows)
                unc.append(urows)
        return out, unc
    finally:
        if doc is not None:
            doc.close()


def _match_words(cell, ws: list[Word]) -> list[Word]:
    x0, y0, x1, y1 = cell
    return [w for w in ws
            if x0 - 1 <= (w[0] + w[2]) / 2 <= x1 + 1
            and y0 - 1 <= (w[1] + w[3]) / 2 <= y1 + 1]


def _cell_text(cell, got: list[Word], page=None, budget: list[int] | None = None,
               row_has_content: bool = True) -> tuple[str | None, bool]:
    x0, y0, x1, y1 = cell
    text = None
    orig_score = None
    if got:
        got = sorted(got, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
        text = " ".join(w[4] for w in got).strip() or None
        orig_score = min(w[5] for w in got)
    orig_text = text

    width = x1 - x0
    if page is None or not budget or budget[0] <= 0 or width < CELL_RESCAN_MIN_WIDTH:
        return text, False

    if got:
        low_score = min(w[5] for w in got) < CELL_RESCAN_SCORE
        low_coverage = False
        if width >= CELL_RESCAN_COVERAGE_MIN_WIDTH:
            coverage = (max(w[2] for w in got) - min(w[0] for w in got)) / width
            low_coverage = coverage < CELL_RESCAN_MIN_COVERAGE
        needs_rescan = low_score or low_coverage
    elif width >= CELL_RESCAN_COVERAGE_MIN_WIDTH:
        needs_rescan = True
    elif row_has_content:
        needs_rescan = True
    else:
        needs_rescan = False

    if needs_rescan:
        budget[0] -= 1
        better = _rescan_cell(page, x0, y0, x1, y1)
        if better and (text is None or _stripped_len(better) >= _stripped_len(text)):
            text = better

    _PLACEHOLDERS = {"/", "-", "－", "—", "."}
    uncertain = False
    if needs_rescan and (text or "").strip() not in _PLACEHOLDERS:
        if _stripped_len(text) <= 1:
            uncertain = True
        elif (text == orig_text and orig_score is not None
              and orig_score < CELL_CONFIDENT_SCORE):
            uncertain = True
    return text, uncertain


def _stripped_len(s: str) -> int:
    return len("".join((s or "").split()))


def _rescan_cell(page, x0: float, y0: float, x1: float, y1: float) -> str | None:
    import numpy as np

    padx, pady = 1.0, 1.5
    rect = _fitz_rect(x0 - padx, y0 - pady, x1 + padx, y1 + pady)
    try:
        pm = page.get_pixmap(dpi=CELL_RESCAN_DPI, clip=rect)
        img = np.frombuffer(pm.samples, dtype=np.uint8).reshape(
            pm.height, pm.width, pm.n)
        res, _ = _engine()(img)
    except Exception:      # noqa: BLE001 -- 重掃是加分項，壞了不能拖垮整頁
        return None
    items = [(min(p[0] for p in box), text.strip(), score)
             for box, text, score in (res or [])
             if score >= CELL_RESCAN_SCORE and (text or "").strip()]
    if not items:
        return None
    items.sort(key=lambda t: t[0])
    return " ".join(t for _, t, _ in items)


def _fitz_rect(x0: float, y0: float, x1: float, y1: float):
    import fitz

    return fitz.Rect(x0, y0, x1, y1)
