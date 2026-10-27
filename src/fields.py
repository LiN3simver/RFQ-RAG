"""表單成本欄位（原幣／USD）的單一定義，以及給 LLM 與全文檢索用的渲染。"""
from __future__ import annotations

from decimal import Decimal

COST_ROWS: list[tuple[str, str | None, str, str | None]] = [
    ("mat_cost",       "mat_cost_usd",       "材料成本",            "材料成本(USD)"),
    ("tooling_cost",   "tooling_cost_usd",   "模治具費用",          "模治具費用(USD)"),
    ("labor_cost",     "labor_cost_usd",     "人工成本",            "人工成本(USD)"),
    ("work_hr_cost",   "work_hr_cost_usd",   "工時成本(原幣$/Hr)",  "工時成本(USD$/Hr)"),
    ("mfg_cost",       "mfg_cost_usd",       "製造成本",            "製費成本(USD)"),
    ("outsource_cost", "outsource_cost_usd", "外包標準成本",        "外包標準成本(USD)"),
    ("operating_cost", "operating_cost_usd", "管銷成本",            "管銷成本(USD)"),
    ("invest_cost",    "invest_cost_usd",    "投資攤提成本",        "投資攤提成本(USD)"),
    ("std_seconds",    None,                 "標準工時(秒)",        None),
    ("mp_cost",        "mp_cost_usd",        "MP",                  "MP (USD)"),
    ("moq_a",          "moq_a_usd",          "MOQ A",               "MOQ A (USD)"),
    ("moq_note",       None,                 "MOQ Note",            None),
]

GREEN_TITLE = "原幣成本欄位"
YELLOW_TITLE = "USD欄位(自動計算)"


def _fmt(v) -> str:
    if v is None:
        return "未填"
    if isinstance(v, (int, float, Decimal)):
        d = Decimal(str(v)).normalize()
        return format(d, "f") if d == d.to_integral_value() else str(d)
    return str(v)


def render_cost_lines(row: dict, only: tuple[str, ...] | None = None) -> str:
    rows = COST_ROWS
    if only:
        keep = set(only)
        rows = [r for r in COST_ROWS if r[0] in keep or (r[1] and r[1] in keep)]
        rows = rows or COST_ROWS
    out = [f"【{GREEN_TITLE}】"]
    out += [f"- {label_l}：{_fmt(row.get(local))}"
            for local, _, label_l, _ in rows]
    yellow = [f"- {label_u}：{_fmt(row.get(usd))}"
              for _, usd, _, label_u in rows if usd]
    if yellow:
        out.append(f"【{YELLOW_TITLE}】")
        out += yellow
    return "\n".join(out)


def cost_text(row: dict) -> str:
    parts = []
    for local, usd, label_l, label_u in COST_ROWS:
        if row.get(local) is not None:
            parts.append(f"{label_l} {_fmt(row[local])}")
        if usd and row.get(usd) is not None:
            parts.append(f"{label_u} {_fmt(row[usd])}")
    return " ".join(parts)


SEARCH_SOURCES = [
    "spec_desc",
    "require_content",
    "part_no",
    "cust_part",
    "customer",
    "final_customer",
    "report_note",
    "moq_note",
]


def search_text(row: dict) -> str:
    parts = [str(row[c]) for c in SEARCH_SOURCES if row.get(c)]
    parts.append(cost_text(row))
    return " ".join(p for p in parts if p).strip()


def embed_text(row: dict) -> str:
    return " ".join(
        str(row[c]).strip() for c in SEARCH_SOURCES if row.get(c)
    ).strip()
