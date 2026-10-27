"""估價：在同一份向量索引上做 k-NN，以鄰居成本的離散度決定信心等級。"""
from __future__ import annotations

from decimal import Decimal

from .db import pool

COSTED = "is_primary AND mat_cost_usd > 0"

COMPONENTS: list[tuple[str, str]] = [
    ("mat_cost_usd",       "材料成本"),
    ("labor_cost_usd",     "人工成本"),
    ("mfg_cost_usd",       "製造費用"),
    ("operating_cost_usd", "管銷成本"),
    ("mp_cost_usd",        "MP"),
    ("std_seconds",        "標準工時(秒)"),
    ("moq_a_usd",          "MOQ A"),
]

UNAVAILABLE = [("outsource_cost_usd", "外包標準成本"),
               ("tooling_cost_usd",   "模治具費用"),
               ("invest_cost_usd",    "投資攤提成本")]

K = 8
MAX_DIST = 0.32
SPREAD_HIGH = 3.0
SPREAD_MID = 8.0

_TIER = {
    "high":   {"涵蓋": "32.8%", "誤差內2倍": "84.0%", "中位誤差": "1.24 倍"},
    "medium": {"涵蓋": "19.8%", "誤差內2倍": "69.6%", "中位誤差": "1.68 倍"},
    "low":    {"涵蓋": "47.5%", "誤差內2倍": "53.2%", "中位誤差": "1.91 倍"},
}

_NEIGHBOR_COLS = """
    rfq_no, spec_desc, customer, part_no, currency, year_qty, created_on,
    mat_cost_usd, labor_cost_usd, mfg_cost_usd, operating_cost_usd,
    mp_cost_usd, std_seconds, moq_a_usd
"""


def _f(v) -> float | None:
    return float(v) if isinstance(v, (int, float, Decimal)) else None


def neighbors(spec: str, k: int = K, customer: str | None = None) -> list[dict]:
    from .embed import _model
    vec = _model().encode([spec], normalize_embeddings=True)[0].tolist()

    with pool().connection() as conn, conn.cursor() as cur:
        sql = "[SQL 已於公開版移除]"
        cur.execute(sql, (vec, vec, k))
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        if customer:
            cur.execute(
                "[SQL 已於公開版移除]",
                (vec, f"%{customer}%", vec, max(k // 2, 3)),
            )
            seen = {r["rfq_no"] for r in rows}
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                if d["rfq_no"] not in seen:
                    d["same_customer"] = True
                    rows.append(d)

    for r in rows:
        r["distance"] = round(_f(r["distance"]) or 1.0, 4)
        r.setdefault("same_customer", bool(customer and r.get("customer")
                                           and customer.lower() in str(r["customer"]).lower()))
    rows.sort(key=lambda r: r["distance"])
    return rows


def _quantiles(vals: list[float]) -> dict | None:
    v = sorted(x for x in vals if x is not None and x > 0)
    if len(v) < 3:
        return None

    def q(p: float) -> float:
        i = p * (len(v) - 1)
        lo, hi = int(i), min(int(i) + 1, len(v) - 1)
        return v[lo] + (v[hi] - v[lo]) * (i - lo)

    return {"p25": round(q(0.25), 4), "median": round(q(0.5), 4),
            "p75": round(q(0.75), 4), "min": round(v[0], 4),
            "max": round(v[-1], 4), "n": len(v)}


def estimate(spec: str, k: int = K, customer: str | None = None) -> dict:
    nb = neighbors(spec, k=k, customer=customer)
    if not nb:
        return {"query": spec, "confidence": "none", "basis": {"n": 0},
                "estimate": {}, "comparables": [],
                "recommendation": "歷史資料裡找不到有成本明細的類似案例，"
                                  "無法提供參考區間，請走完整 BOM 估算。"}

    mats = [_f(r["mat_cost_usd"]) for r in nb]
    mats = [m for m in mats if m and m > 0]
    spread = (max(mats) / min(mats)) if mats and min(mats) > 0 else None
    best = nb[0]["distance"]

    if spread is None or best >= MAX_DIST or spread >= SPREAD_MID:
        tier = "low"
    elif spread < SPREAD_HIGH:
        tier = "high"
    else:
        tier = "medium"

    est = {}
    for col, label in COMPONENTS:
        qs = _quantiles([_f(r[col]) for r in nb])
        if qs:
            est[col] = {"label": label, **qs}

    t = _TIER[tier]
    if tier == "high":
        rec = (f"高信心：相似案例的成本很集中（最高只有最低的 {spread:.1f} 倍）。"
               f"實測這一區 {t['誤差內2倍']} 落在實際值 2 倍內、"
               f"中位誤差 {t['中位誤差']}。可作為成本預估的起始參考，"
               "但仍須由 BOM 與製程工時估算覆核，不可直接報價。")
    else:
        why = []
        if spread is None:
            why.append("可用的成本樣本不足")
        elif spread >= SPREAD_MID:
            why.append(f"相似案例的成本本身就散（最高是最低的 {spread:.1f} 倍）")
        elif spread >= SPREAD_HIGH:
            why.append(f"相似案例的成本有點散（最高是最低的 {spread:.1f} 倍）")
        if best >= MAX_DIST:
            why.append(f"最接近的案例語意距離 {best:.2f}，並不夠像")
        label = "中信心" if tier == "medium" else "低信心"
        rec = (f"{label}：" + "、".join(why) + "。"
               f"實測這一區只有 {t['誤差內2倍']} 落在 2 倍內、"
               f"中位誤差 {t['中位誤差']}，不要拿這個區間報價，"
               "請走 RD 分析 BOM -> 成本預估 -> 製程工時 的完整估算。"
               "下方案例仍可當作 BOM 的參考起點。")

    return {
        "query": spec,
        "confidence": tier,
        "accuracy": t,
        "basis": {
            "n": len(nb),
            "n_same_customer": sum(1 for r in nb if r.get("same_customer")),
            "spread": round(spread, 2) if spread else None,
            "best_distance": best,
            "pool": "有成本明細的歷史 RFQ",
        },
        "estimate": est,
        "unavailable": [{"col": c, "label": l} for c, l in UNAVAILABLE],
        "comparables": [
            {
                "rfq_no": r["rfq_no"],
                "spec_desc": r["spec_desc"],
                "customer": r["customer"],
                "part_no": r["part_no"],
                "year_qty": _f(r["year_qty"]),
                "distance": r["distance"],
                "same_customer": r.get("same_customer", False),
                "created_on": r["created_on"],
                "costs": {c: _f(r[c]) for c, _l in COMPONENTS},
            }
            for r in nb
        ],
        "recommendation": rec,
    }


def sanity_check(spec: str, proposed: dict, k: int = K) -> dict:
    ref = estimate(spec, k=k)
    flags = []
    for col, val in proposed.items():
        band = ref["estimate"].get(col)
        if band is None or val is None:
            continue
        v = float(val)
        if v > band["max"] * 1.5:
            flags.append({"col": col, "label": band["label"], "value": v,
                          "level": "high",
                          "msg": f"比歷史類似料的最高值 {band['max']} 還高 "
                                 f"{v / band['max']:.1f} 倍"})
        elif band["min"] > 0 and v < band["min"] / 1.5:
            flags.append({"col": col, "label": band["label"], "value": v,
                          "level": "low",
                          "msg": f"比歷史類似料的最低值 {band['min']} 還低 "
                                 f"{band['min'] / v:.1f} 倍" if v > 0
                                 else "為 0 或負值"})
    return {
        "query": spec,
        "confidence": ref["confidence"],
        "ok": not flags,
        "flags": flags,
        "reference": ref["estimate"],
        "comparables": ref["comparables"][:5],
        "note": ("參考區間本身是低信心的，示警僅供參考。"
                 if ref["confidence"] != "high" else
                 "參考區間為高信心，示警值得追查。"),
    }
