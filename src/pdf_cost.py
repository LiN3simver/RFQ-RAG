"""成本分析表解析：表頭、材料明細（算式對帳與小數點修正）、人工成本與彙總欄。"""
from __future__ import annotations

import collections
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from . import pdf_ocr


ANCHORS = ["成本", "材料", "Total", "匯率", "單價", "PCS", "RMB", "USD", "工時"]

MIN_CHARS = 200
MIN_ANCHORS = 6


_COMMA_DEC_RE = re.compile(r"^(-?\d+),(\d+)$")


def _fix_comma(s: str) -> str:
    m = _COMMA_DEC_RE.match(s)
    if m and len(m.group(2)) != 3:
        return f"{m.group(1)}.{m.group(2)}"
    return s.replace(",", "")


_CJK_RE = re.compile(r"[一-鿿]")
_DIGIT_RE = re.compile(r"\d")
MIN_DIGIT_RATIO = 0.15


def health(text: str) -> str:
    t = text.strip()
    if len(t) < MIN_CHARS:
        return "scanned"
    if sum(1 for a in ANCHORS if a in text) >= MIN_ANCHORS:
        return "ok"
    digit_ratio = len(_DIGIT_RE.findall(t)) / len(t)
    if digit_ratio < MIN_DIGIT_RATIO:
        return "corrupt"
    return "cjk_missing" if not _CJK_RE.search(t) else "other"


def _num(v) -> Decimal | None:
    if v is None:
        return None
    s = str(v).strip().replace("¥", "").replace("$", "")
    s = s.replace("US", "").replace("RMB", "").replace("NT", "").strip()
    s = _fix_comma(s)
    if not s or s in {"/", "-", "—"} or s.startswith("#"):
        return None
    s = s.rstrip("%")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


_MAT_HEADER = ("材料料號", "供應商")
_SPEC_KEYS = ("規格說明", "規格", "零件材質")
_NAME_KEYS = ("材料名稱", "材料名", "零件品名")

_S2T = str.maketrans({
    "号": "號", "应": "應", "商": "商", "说": "說", "规": "規", "单": "單",
    "价": "價", "损": "損", "总": "總", "项": "項", "别": "別", "数": "數",
    "汇": "匯", "费": "費", "称": "稱", "备": "備", "注": "註", "过": "過",
    "质": "質", "东": "東", "币": "幣", "样": "樣", "计": "計", "两": "兩",
    "间": "間", "长": "長", "线": "線", "电": "電", "铜": "銅", "镀": "鍍",
    "钢": "鋼", "胶": "膠", "缩": "縮", "厂": "廠", "标": "標", "准": "準",
})


def _norm(s: str | None) -> str:
    return (s or "").replace("\n", "").replace(" ", "").translate(_S2T)


def _find_col(header: list, keys: tuple, exclude: tuple = ()) -> int | None:
    for i, h in enumerate(header):
        if i in exclude:
            continue
        if any(k in _norm(h) for k in keys):
            return i
    return None


_HEAD_SIGNALS = (
    r"料號",
    r"材料名|零件品名",
    r"供.{0,2}商",
    r"規格|材質",
    r"匯率|汇率",
    r"用量",
    r"MOQ",
    r"交期",
    r"原.{0,2}單價|原幣別",
    r"耗損|耗损|L[O0]S",
)
_HEAD_MIN = 4


def _is_mat_header(row: list) -> bool:
    j = _norm("".join(_norm(c) for c in row))
    u = j.upper()
    hits = sum(1 for p in _HEAD_SIGNALS if re.search(p, j) or re.search(p, u))
    if hits >= _HEAD_MIN:
        return True
    return "MOQ" in u and bool(re.search(r"L[O0]S", u))


_MOQ_OFFSETS = {"no": -10, "name": -9, "sup": -8, "spec": -7, "cur": -6,
                "raw": -5, "fx": -4, "up": -3, "qty": -2, "unit": -1,
                "loss": 1, "tot": 2, "lead": 3, "note": 4}


def material_table(page, tables: list | None = None,
                   uncertain: list | None = None) -> list[dict]:
    out: list[dict] = []
    _tables = list(page.extract_tables() if tables is None else tables)
    _uncertain = uncertain if uncertain is not None else [None] * len(_tables)
    for tb, tbu in zip(_tables, _uncertain):
        if not tb or len(tb) < 2:
            continue
        hi = next((i for i, r in enumerate(tb) if _is_mat_header(r)), None)
        if hi is None:
            continue
        header = [(c or "").replace("\n", "") for c in tb[hi]]
        body = tb[hi + 1:]
        ubody = tbu[hi + 1:] if tbu is not None else [None] * len(body)
        i_no = _find_col(header, ("材料料號", "零件品名", "料號"))
        i_name = _find_col(header, _NAME_KEYS)
        i_sup = _find_col(header, ("供應商",))
        i_spec = _find_col(header, _SPEC_KEYS)
        i_cur = _find_col(header, ("原幣別",))
        i_raw = _find_col(header, ("原幣單價", "零件成本"))
        i_fx = _find_col(header, ("匯率",))
        i_up = _find_col(header, ("單價(USD)", "單價USD"))
        if i_up is None:
            i_up = _find_col(
                header, ("單價",),
                exclude=tuple(x for x in (i_raw, i_cur) if x is not None))
        i_qty = _find_col(header, ("用量",))
        i_unit = _find_col(header, ("單位",))
        i_loss = _find_col(header, ("耗損", "損耗"))
        i_tot = _find_col(header, ("總價", "小計"))
        i_lead = _find_col(header, ("交期",))
        i_note = _find_col(header, ("備註", "備考"))

        i_moq = _find_col(header, ("MOQ",))
        if i_moq is not None:
            def _off(cur, key):
                if cur is not None:
                    return cur
                j = i_moq + _MOQ_OFFSETS[key]
                return j if 0 <= j < len(header) else None
            i_no, i_name = _off(i_no, "no"), _off(i_name, "name")
            i_sup, i_spec = _off(i_sup, "sup"), _off(i_spec, "spec")
            i_cur, i_raw = _off(i_cur, "cur"), _off(i_raw, "raw")
            i_fx, i_up = _off(i_fx, "fx"), _off(i_up, "up")
            i_qty, i_unit = _off(i_qty, "qty"), _off(i_unit, "unit")
            i_loss, i_tot = _off(i_loss, "loss"), _off(i_tot, "tot")
            i_lead, i_note = _off(i_lead, "lead"), _off(i_note, "note")

            if i_up is not None and i_up == i_raw:
                j = i_moq + _MOQ_OFFSETS["up"]
                i_up = j if 0 <= j < len(header) and j != i_raw else None

        def cell(row, idx):
            if idx is None or idx >= len(row):
                return None
            v = row[idx]
            return (v or "").replace("\n", " ").strip() or None

        for row, urow in zip(body, ubody):
            if not any((c or "").strip() for c in row):
                continue
            part = cell(row, i_no)
            spec = cell(row, i_spec)
            name = cell(row, i_name)
            total = _num(cell(row, i_tot))
            if not (part or spec or name or total):
                continue
            sup_unc = bool(i_sup is not None and urow is not None
                          and i_sup < len(urow) and urow[i_sup])
            out.append({
                "part_no": part, "name": name, "supplier": cell(row, i_sup),
                "supplier_uncertain": sup_unc,
                "spec": spec, "currency": cell(row, i_cur),
                "unit_price_raw": _num(cell(row, i_raw)),
                "fx_rate": _num(cell(row, i_fx)),
                "unit_price": _num(cell(row, i_up)),
                "qty": _num(cell(row, i_qty)), "unit": cell(row, i_unit),
                "moq": cell(row, i_moq), "loss_pct": _num(cell(row, i_loss)),
                "line_total": total,
                "lead_time": cell(row, i_lead), "remark": cell(row, i_note),
            })
        if out:
            break
    return out


_SCALES = [Decimal(10) ** k for k in (-4, -3, -2, -1, 1, 2, 3, 4)]
_ROW_TOL = Decimal("0.02")
_ABS_TOL = Decimal("0.0002")

_FIX_FIELDS = ("line_total", "unit_price_raw", "unit_price", "qty", "loss_pct")

_FIX_ORDER = ("fx_rate",) + _FIX_FIELDS


def _close(a: Decimal | None, b: Decimal | None) -> bool | None:
    if a is None or b is None:
        return None
    diff = abs(a - b)
    if diff <= _ABS_TOL:
        return True
    return diff / max(abs(a), abs(b), Decimal("0.0001")) <= _ROW_TOL


def _identities(m: dict) -> tuple[bool | None, bool | None]:
    r, f, u = m.get("unit_price_raw"), m.get("fx_rate"), m.get("unit_price")
    e1 = _close(u, r / f) if (r is not None and u is not None and f) else None
    q, t = m.get("qty"), m.get("line_total")
    loss = m.get("loss_pct") or Decimal(0)
    e2 = (_close(t, u * q * (1 + loss / 100))
          if (u is not None and q is not None and t is not None) else None)
    return e1, e2


def _consistent(e1: bool | None, e2: bool | None) -> bool:
    return (e1 is not None or e2 is not None) and e1 is not False and e2 is not False


def _doc_fx(materials: list[dict]) -> Decimal | None:
    counts = collections.Counter(m["fx_rate"] for m in materials
                                 if m.get("fx_rate"))
    if not counts:
        return None
    fx, n = counts.most_common(1)[0]
    return fx if n >= 3 and n >= len(counts) else None


def reconcile(materials: list[dict]) -> list[dict]:
    fx = _doc_fx(materials)
    for m in materials:
        e1, e2 = _identities(m)
        if e1 is None and e2 is None:
            m["check"] = "na"
            continue
        if _consistent(e1, e2):
            m["check"] = "ok"
            continue

        sols: list[tuple[str, Decimal]] = [
            (field, m[field] * s)
            for field in _FIX_FIELDS
            for s in _SCALES
            if m.get(field) not in (None, 0)
            and _consistent(*_identities({**m, field: m[field] * s}))
        ]
        if fx is not None and m.get("fx_rate") != fx \
                and _consistent(*_identities({**m, "fx_rate": fx})):
            sols.append(("fx_rate", fx))
        if not sols:
            m["check"] = "unverified"
            continue

        field, val = min(sols, key=lambda fv: (_FIX_ORDER.index(fv[0]),
                                               abs(fv[1] - (m[fv[0]] or 0))))
        m["check"] = "repaired" if len(sols) == 1 else "rescaled"
        m["repaired"] = f"{field}:{m.get(field)}->{val}"
        m[field] = val
    return materials


_CUR = r"(?:US\$|USD|NT\$|NTD|RMB|¥|\$)?\s*"
_N = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
_TOTALS: list[tuple[str, str]] = [
    ("mat",       rf"A[\s_]*Total[:：]?\s*{_CUR}{_N}"),
    ("labor",     rf"B[\s_]*Total[:：]?\s*{_CUR}{_N}"),
    ("tooling",   rf"C[\s_]*Total[:：]?\s*{_CUR}{_N}"),
    ("other",     rf"D[\s_]*Total[:：]?\s*{_CUR}{_N}"),
    ("mfg",       rf"製造成本[^\n]{{0,18}}?{_CUR}{_N}"),
    ("overhead",  rf"管[銷销][^\n]{{0,18}}?{_CUR}{_N}"),
    ("subtotal",  rf"成本小[計计][^\n]{{0,24}}?{_CUR}{_N}"),
    ("grand",     rf"合[計计]TOTAL[^\n]{{0,20}}?{_CUR}{_N}"),
    ("moq_a",     rf"MOQ\s*A[^\n]{{0,40}}?{_CUR}{_N}\s*$"),
]

_TOTALS_PARTNER: list[tuple[str, str]] = [
    ("mat",   rf"Material_Total[:：]?\s*{_CUR}{_N}"),
    ("labor", rf"Labor_Total\([^)]*\)\s*=\s*{_CUR}{_N}"),
    ("grand", rf"產品總成本[^\n]{{0,20}}?{_CUR}{_N}"),
    ("price", rf"Unit\s*Price[^\n]{{0,40}}?{_CUR}{_N}\s*/pc"),
]


_LABOR_SEC_RE = re.compile(r"B\s*[.．、:：]?\s*人工成本|人工成本\s*\(\s*labor", re.I)
_LABOR_END_RE = re.compile(r"C\s*[.．、:：]?\s*(?:模|治具|設備|设备|Tooling)"
                           r"|C\s*[\s_]*Total", re.I)
_LABOR_SPAN = 1200

_LABOR_CATS: tuple[tuple[str, re.Pattern], ...] = (
    ("SMT", re.compile(r"SMT", re.I)),
    ("組裝(Assembly)",
     re.compile(r"(?:[組组][裝装]\s*(?:\(\s*Assembly\s*\))?|Assembly)", re.I)),
    ("委外成本", re.compile(r"委外(?:成本)?")),
    ("其它", re.compile(r"其[它他]")),
)

_LABOR_PARAMS: tuple[tuple[str, re.Pattern], ...] = (
    ("stations", re.compile(r"[預预][估計计]工站[^\d\n]{0,6}" + _N)),
    ("total_sec", re.compile(r"[組组]立[總总]工[時时][^\d\n]{0,8}" + _N)),
    ("pph", re.compile(r"PPH[^\d\n]{0,6}" + _N, re.I)),
    ("hr_rate", re.compile(r"工[時时]成本[^\d\n]{0,14}" + _N)),
    ("efficiency_pct", re.compile(r"人[工力]效率[^\d\n]{0,6}" + _N)),
)

LABOR_PARAM_KEYS: tuple[str, ...] = tuple(k for k, _ in _LABOR_PARAMS)

_NOTE_PARAM_RE = re.compile(
    r"(?:[預预][估計计]|工站|[組组]立|[總总]工[時时]|Sec|PPH|工[時时]成本|"
    r"人[工力]效率|US\$?D?|RMB|NTD?|Hr|[\d.,%$¥()（）/\s：:])+", re.I)


def labor_note_residual(row: dict) -> str | None:
    note = row.get("note")
    if not note:
        return None
    if not any(row.get(k) is not None for k in LABOR_PARAM_KEYS):
        return note
    return _NOTE_PARAM_RE.sub(" ", note).strip() or None


_LABOR_COST_RE = re.compile(rf"{_CUR}{_N}\s*$")
_LABOR_TOTAL_RE = re.compile(rf"B[\s_]*Total[:：]?\s*{_CUR}{_N}", re.I)

_LABOR_HEAD_RE = re.compile(r"人工明[細细].{0,6}[說说]明.{0,6}[費费]用")

_LABOR_FREE_MAX = 20


def _labor_free_label(line: str, cost_match: re.Match | None) -> str | None:
    s = (line[:cost_match.start()] if cost_match else line).strip()
    if not s or len(s) > _LABOR_FREE_MAX:
        return None
    return s.split()[0] if s.split() else None


_SEC_PER_HR = Decimal(3600)


def _labor_block(text: str) -> str | None:
    text = unicodedata.normalize("NFKC", text)
    m = _LABOR_SEC_RE.search(text)
    if not m:
        return None
    seg = text[m.end():]
    e = _LABOR_END_RE.search(seg)
    return seg[:e.start()] if e else seg[:_LABOR_SPAN]


def labor_detail(text: str) -> dict:
    seg = _labor_block(text)
    if seg is None:
        return {"found": False, "rows": [], "total": None}

    rows: list[dict] = []
    total = None
    for line in seg.splitlines():
        s = line.strip()
        if not s:
            continue
        mt = _LABOR_TOTAL_RE.search(s)
        if mt:
            total = _num(mt.group(1))
            break
        if _LABOR_HEAD_RE.search(s):
            continue
        cat = next((c for c, r in _LABOR_CATS if r.search(s)), None)
        if cat is not None and any(r["category"] == cat for r in rows):
            continue
        mc = _LABOR_COST_RE.search(s)
        cost = _num(mc.group(1)) if mc else None
        if cat is None:
            cat = _labor_free_label(s, mc)
            if cat is None or cost is None:
                continue
        row: dict = {"category": cat, "cost": cost,
                     "note": _labor_note(s, cat, mc)}
        if cat == "組裝(Assembly)":
            for key, pat in _LABOR_PARAMS:
                pm = pat.search(s)
                if pm:
                    row[key] = _num(pm.group(1))
            row["rate_currency"] = _rate_currency(s)
        rows.append(row)
    return {"found": True, "rows": rows, "total": total}


_RATE_CUR_RE = re.compile(r"工[時时]成本\s*[（(]\s*(US|USD|RMB|NT|NTD|CNY|¥|\$)")
_RATE_CUR_MAP = {"US": "USD", "USD": "USD", "$": "USD", "RMB": "RMB",
                 "¥": "RMB", "CNY": "RMB", "NT": "NTD", "NTD": "NTD"}


def _rate_currency(line: str) -> str | None:
    m = _RATE_CUR_RE.search(line)
    return _RATE_CUR_MAP.get(m.group(1).upper()) if m else None


def _labor_note(line: str, cat: str, cost_match: re.Match | None) -> str | None:
    s = line[:cost_match.start()] if cost_match else line
    pat = next((r for c, r in _LABOR_CATS if c == cat), None)
    s = pat.sub("", s, count=1) if pat else s.replace(cat, "", 1)
    return re.sub(r"\s+", " ", s).strip(" |:：") or None


_LABOR_TOL = Decimal("0.02")


def verify_labor(labor: dict) -> dict:
    rows = labor.get("rows") or []
    for r in rows:
        r["check"] = _labor_check(r)
    costs = [r["cost"] for r in rows if r.get("cost") is not None]
    line_sum = sum(costs, Decimal(0)) if costs else None
    total = labor.get("total")
    labor["line_sum"] = line_sum
    if total is None or line_sum is None:
        labor["verified"] = False
        return labor
    labor["verified"] = bool(_close_tol(total, line_sum, _LABOR_TOL))
    return labor


def _labor_check(row: dict) -> str:
    cost = row.get("cost")
    rate, sec = row.get("hr_rate"), row.get("total_sec")
    pph, eff = row.get("pph"), row.get("efficiency_pct")
    if cost is None or rate is None or (sec is None and not pph):
        return "na"
    if sec is not None:
        want = rate * sec / _SEC_PER_HR
        if eff:
            want = want / (eff / 100)
        if _close_tol(cost, want, _LABOR_TOL):
            return "ok"
    if pph and _close_tol(cost, rate / pph, _LABOR_TOL):
        return "ok"
    return "mismatch"


def _close_tol(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    diff = abs(a - b)
    if diff <= _ABS_TOL:
        return True
    return diff / max(abs(a), abs(b), Decimal("0.0001")) <= tol


def _currency(text: str) -> str | None:
    if re.search(r"合[計计]TOTAL[^\n]{0,20}US\$", text) or "(USD)" in text:
        return "USD"
    if re.search(r"合[計计]TOTAL[^\n]{0,20}¥", text) or "RMB¥" in text:
        return "RMB"
    if "USD/RMB" in text or "RMB/USD" in text:
        return "RMB"
    return None


def summary_totals(text: str) -> dict:
    partner = "合作廠成本評估表" in text or "Material_Total" in text
    pats = _TOTALS_PARTNER if partner else _TOTALS
    out: dict = {"currency": _currency(text),
                 "template": "partner" if partner else "acme"}
    for key, pat in pats:
        m = re.search(pat, text, re.M)
        if m:
            out[key] = _num(m.group(1))
    return out


TOL_TEXT = Decimal("0.02")
TOL_OCR = Decimal("0.05")


def verify_totals(summary: dict, materials: list[dict],
                  tol: Decimal = TOL_TEXT) -> dict:
    line_sum = sum((m["line_total"] for m in materials
                    if m.get("line_total") is not None), Decimal(0))
    out = dict(summary)
    out["material_line_sum"] = line_sum if materials else None
    out["line_total_missing"] = sum(1 for m in materials
                                    if m.get("line_total") is None)
    checks = collections.Counter(m.get("check") or "na" for m in materials)
    out["rows_ok"] = checks["ok"]
    out["rows_repaired"] = checks["repaired"] + checks["rescaled"]
    out["rows_unverified"] = checks["unverified"]
    mat = summary.get("mat")
    if mat is None or not materials or line_sum == 0:
        out["totals_verified"] = False
        return out
    ratio = abs(mat - line_sum) / max(line_sum, Decimal("0.0001"))
    out["totals_verified"] = ratio < tol
    if not out["totals_verified"]:
        for k in ("mat", "labor", "tooling", "other", "mfg",
                  "overhead", "subtotal", "grand", "moq_a", "price"):
            out.pop(k, None)
    return out


_EQ_RE = re.compile(r"(?<![A-Za-z0-9])((?:SA-EQ|SB-EQ)\d+)(?![A-Za-z0-9])", re.I)
_RFQ_RE = re.compile(r"(?<![A-Za-z0-9])(RF[QSD]-\d{11}-\d{3})(?![A-Za-z0-9])", re.I)


def header_ids(text: str) -> dict:
    eq = _EQ_RE.search(text)
    rfq = _RFQ_RE.search(text)
    return {"eq_no": eq.group(1).upper() if eq else None,
            "rfq_no": rfq.group(1).upper() if rfq else None}


_HDR_LABELS: tuple[tuple[str, str], ...] = (
    ("customer_code", r"Customer\s*code\s*[:：.．]?"),
    ("customer_info", r"Customer\s*info\w*\s*[:：.．]?"),
    ("affiliate",     r"Affiliate\s*info\w*\s*[:：.．]?"),
    ("own_part",     r"ACME\s*(?:Part\s*N[O0]\s*\.?|P\s*/\s*N)\s*[:：.．]?"),
    ("cus_part",      r"Cus\s*\.?\s*P\s*/\s*N\s*[:：.．]?"),
    ("part_name",     r"Part\s*Name\s*[:：.．]?"),
    ("rfq_no",        r"RFQ\s*N[O0]\s*\.?\s*[:：.．]?"),
    ("eq_no",         r"EQ\s*N[O0]\s*\.?\s*[:：.．]?"),
    ("rev",           r"(?<![A-Za-z0-9_])Rev(?![A-Za-z0-9_])\s*[:：.．]?"),
    ("quote_reason",  r"[^\s:：]{0,2}原因\s*[:：.．]?"),
    ("mp_date",       r"量[產产]\s*日期\s*[:：.．]?"),
    ("doc_date",      r"日期\s*[:：.．]?"),
    ("sales",         r"Sales\s*[:：.．]?"),
    ("factory",       r"[製制]造.?\s*[:：.．]"),
    ("demand_qty",    r"需求量\s*[:：.．]?"),
    ("summary_note",  r"摘要\s*[:：.．]?"),
    ("fx_label",      r"[匯汇匿]率\s*[（(]?\s*(?:RMB|RVB)?\s*/?\s*[UL]SD?\s*[)）]?\s*[:：]?"),
    ("fx_pair",       r"(?:USD|NTD|RMB|CNY)\s*/\s*(?:USD|NTD|RMB|CNY)"
                      r"(?![A-Za-z0-9])"),
)

_HDR_ANY_RE = re.compile("|".join(f"(?P<{k}>{p})" for k, p in _HDR_LABELS), re.I)

_HDR_END_RE = re.compile(r"A\s*[.．、]?\s*材料成本|材料料號|材料料号|"
                         r"零件品名|A\s*Total", re.I)

_HDR_EMPTY = {"", "/", "-", "—", "－", ":", "：", ".", "N/A", "NA"}

_HDR_JUNK_RE = re.compile(r"^(?:USD|NTD?|RMB|CNY)\s*/", re.I)

_HDR_NUMERIC_JUNK_RE = re.compile(r"^[\d.,\s/]+$")
_HDR_NEEDS_LETTER = ("part_name", "own_part", "cus_part")


def _hdr_value(s: str, key: str = "") -> str | None:
    v = re.sub(r"\s+", " ", (s or "").strip(" \t|:：.．、/\\")).strip()
    if key in ("sales", "factory"):
        v = re.split(r"[:：]|\s\d", v)[0].strip()
    if v in _HDR_EMPTY or len(v) < 1 or _HDR_JUNK_RE.match(v):
        return None
    if key in _HDR_NEEDS_LETTER and _HDR_NUMERIC_JUNK_RE.match(v):
        return None
    return v[:300]


def header_fields(text: str) -> dict:
    text = unicodedata.normalize("NFKC", text or "")
    end = _HDR_END_RE.search(text)
    head = text[:end.start()] if end else text[:1500]

    out: dict = {}
    for line in head.splitlines():
        hits = [(m.start(), m.end(), m.lastgroup) for m in _HDR_ANY_RE.finditer(line)]
        for i, (_s, e, key) in enumerate(hits):
            stop = hits[i + 1][0] if i + 1 < len(hits) else len(line)
            if key in ("customer_info", "affiliate", "summary_note",
                       "fx_label", "fx_pair"):
                continue
            if key in ("rfq_no", "eq_no"):
                continue
            if out.get(key) is not None:
                continue
            v = _hdr_value(line[e:stop], key)
            if v is not None:
                out[key] = v
    return out


_NORM_KEEP_RE = re.compile(r"[^0-9A-Z一-鿿]+")


def norm_part_name(s: str | None) -> str:
    return _NORM_KEEP_RE.sub("", (s or "").upper())


MIN_ANCHORS_OCR = 4

GLYPH_IMAGE_MIN = 20
SCAN_AREA_MIN = 0.5

_HEALTH_ORDER = {"ok": 0, "cjk_missing": 1, "other": 2, "corrupt": 3,
                 "scanned": 4}


def page_plan(path: str) -> list[str]:
    import fitz

    out: list[str] = []
    doc = fitz.open(path)
    try:
        for pg in doc:
            if len(pg.get_text().strip()) >= MIN_CHARS:
                out.append("text")
                continue
            page_area = abs(pg.rect.width * pg.rect.height) or 1.0
            infos = pg.get_image_info()
            biggest = max(
                (abs(x["bbox"][2] - x["bbox"][0])
                 * abs(x["bbox"][3] - x["bbox"][1]) / page_area
                 for x in infos), default=0.0)
            out.append("ocr" if len(infos) >= GLYPH_IMAGE_MIN
                       or biggest >= SCAN_AREA_MIN else "skip")
    finally:
        doc.close()
    return out


def parse(path: str, ocr: bool = True) -> dict:
    import pdfplumber

    from . import pdf_fontfix

    plan = page_plan(path)
    res: dict = {"path": path, "materials": [], "summary": {}, "ids": {},
                 "header": {},
                 "labor": {"found": False, "rows": [], "total": None},
                 "pages": [], "health": "scanned", "text": "",
                 "ocr_pages": 0, "skipped_pages": 0, "source": None}
    texts: list[str] = []
    use_ocr = ocr and pdf_ocr.available()

    with pdf_fontfix.patched_fonts(path) as fix:
        res["fontfix"] = sorted(fix.tables)
        with pdfplumber.open(path) as pdf:
            for i, pg in enumerate(pdf.pages):
                kind = plan[i] if i < len(plan) else "text"
                if kind == "skip":
                    res["pages"].append({"page": i, "source": None,
                                         "health": "drawing"})
                    res["skipped_pages"] += 1
                    continue

                text = pg.extract_text() or ""
                st = health(text)
                source, tables, tables_unc = "text", None, None

                if st == "scanned":
                    if not (kind == "ocr" and use_ocr):
                        res["pages"].append({"page": i, "source": None,
                                             "health": "scanned"})
                        res["skipped_pages"] += 1
                        continue
                    ws = pdf_ocr.words(path, i)
                    otext = pdf_ocr.page_text(ws)
                    if sum(1 for a in ANCHORS if a in otext) < MIN_ANCHORS_OCR:
                        res["pages"].append({"page": i, "source": "ocr",
                                             "health": "not_cost"})
                        res["skipped_pages"] += 1
                        res["ocr_pages"] += 1
                        continue
                    text, st, source = otext, health(otext), "ocr"
                    tables, tables_unc = pdf_ocr.fill_tables(pg, ws, path, i)
                    res["ocr_pages"] += 1
                elif fix.tables and st != "corrupt":
                    source = "fontfix"

                if (not res["materials"] and source != "ocr" and use_ocr
                        and kind != "skip" and st in ("corrupt", "cjk_missing")):
                    ws = pdf_ocr.words(path, i)
                    otext = pdf_ocr.page_text(ws)
                    if sum(1 for a in ANCHORS if a in otext) >= MIN_ANCHORS_OCR:
                        otables, otables_unc = pdf_ocr.fill_tables(pg, ws, path, i)
                        if material_table(pg, otables, otables_unc):
                            text, st, source, tables, tables_unc = (
                                otext, health(otext), "ocr", otables, otables_unc)
                    res["ocr_pages"] += 1

                texts.append(text)
                res["pages"].append({"page": i, "source": source,
                                     "health": st})
                if not res["materials"]:
                    res["materials"] = reconcile(
                        material_table(pg, tables, tables_unc))
                    if res["materials"]:
                        res["source"] = source
                if not res["ids"].get("eq_no"):
                    res["ids"] = header_ids(text)
                for k, v in header_fields(text).items():
                    res["header"].setdefault(k, v)

    res["text"] = "\n".join(texts).strip()
    if res["text"]:
        res["summary"] = verify_totals(
            summary_totals(res["text"]), res["materials"],
            tol=TOL_OCR if res["source"] == "ocr" else TOL_TEXT)
        res["labor"] = verify_labor(labor_detail(res["text"]))
    kept = [p["health"] for p in res["pages"]
            if p["health"] in _HEALTH_ORDER]
    res["health"] = min(kept, key=lambda s: _HEALTH_ORDER[s]) if kept else "scanned"
    return res
