"""八通道混合檢索（編號、數值、時間、附件、結構欄位、同義詞、全文、向量）與 RRF 融合。"""
from __future__ import annotations

import re
import threading
from decimal import Decimal

import psycopg

from . import fields, synonyms
from . import timefilter as tfmod
from .db import pool

CAND = 60
RRF_K = 60

WEIGHTS = {
    "id": 6.0,
    "numeric": 4.0,
    "time": 4.0,
    "cost_doc": 4.0,
    "facet": 3.0,
    "synonym": 3.0,
    "lexical": 1.4,
    "vector": 1.0,
}

COLS = """
    id, rfq_no, rfq_kind, parent_no, part_no, spec_desc, report_note,
    require_content, customer, final_customer, rfw_no, engineer, manager,
    buyer, currency, fx_rate, cost_pushed, state, is_primary, created_by, created_on,
    modified_on,
    mat_cost, tooling_cost, labor_cost, work_hr_cost, mfg_cost, outsource_cost,
    operating_cost, invest_cost, std_seconds, mp_cost, moq_a, moq_note,
    mat_cost_usd, tooling_cost_usd, labor_cost_usd, work_hr_cost_usd,
    mfg_cost_usd, outsource_cost_usd, operating_cost_usd, invest_cost_usd,
    mp_cost_usd, moq_a_usd, year_qty, work_hours, unit_cost_usd,
    sample_cost_usd, cost_text
"""


_RFQ_RE = re.compile(
    r"(?<![A-Za-z0-9])(RF[QSD])[-\s]?(\d{6,13})(?:[-\s]?(\d{1,3}))?(?![A-Za-z0-9])",
    re.I)
_PARENT_RE = re.compile(r"(?<!\d)(\d{11})(?!\d)")

_LABEL_TO_COL: dict[str, str] = {}
for _local, _usd, _ll, _lu in fields.COST_ROWS:
    _LABEL_TO_COL[_ll] = _local
    if _usd and _lu:
        _LABEL_TO_COL[_lu] = _usd
_LABEL_TO_COL.update({
    "總工時": "work_hours", "單價": "unit_cost_usd",
    "RFQ單價": "unit_cost_usd", "樣品費用": "sample_cost_usd",
    "匯率": "fx_rate", "年需求量": "year_qty",
})

for _lbl, _col in list(_LABEL_TO_COL.items()):
    _short = re.sub(r"\s*[（(].*?[)）]\s*$", "", _lbl).strip()
    if _short and _short not in _LABEL_TO_COL:
        _LABEL_TO_COL[_short] = _col

_LABELS_BY_LEN = sorted(_LABEL_TO_COL, key=len, reverse=True)

_CMP = [
    (re.compile(r"(大於|超過|高於|>=?|＞)\s*([\d.,]+)"), lambda v: (">", v)),
    (re.compile(r"(小於|低於|少於|<=?|＜)\s*([\d.,]+)"), lambda v: ("<", v)),
    (re.compile(r"(等於|是|＝|=)\s*([\d.,]+)"), lambda v: ("=", v)),
]

_COST_DOC_MENTION_RE = re.compile(r"成本分析(?:表|附件|報表|原檔|pdf)", re.I)
_COST_DOC_NEGATE_RE = re.compile(r"不管|不論|無論")


def parse(q: str, now=None) -> dict:
    out: dict = {"rfq_nos": [], "parents": [], "numeric": [], "facets": {},
                 "text": q, "time": tfmod.parse(q, now)}

    for m in _RFQ_RE.finditer(q):
        kind, mid, seq = m.group(1).upper(), m.group(2), m.group(3)
        out["rfq_nos"].append(f"{kind}-{mid}-{seq.zfill(3)}" if seq else f"{kind}-{mid}")
    for m in _PARENT_RE.finditer(q):
        if not any(m.group(1) in n for n in out["rfq_nos"]):
            out["parents"].append(m.group(1))

    used: list[tuple[int, int]] = []
    hits: list[tuple[int, int, str]] = []
    for label in _LABELS_BY_LEN:
        pos = q.find(label)
        if pos < 0:
            continue
        end = pos + len(label)
        if any(pos < ue and us < end for us, ue in used):
            continue
        used.append((pos, end))
        hits.append((pos, end, _LABEL_TO_COL[label]))
    hits.sort()

    for i, (pos, end, col) in enumerate(hits):
        stop = hits[i + 1][0] if i + 1 < len(hits) else len(q)
        start = hits[i - 1][1] if i else 0
        tail = q[end: min(end + 24, stop)]
        for rx, mk in _CMP:
            m = rx.search(tail)
            if m:
                if m.group(1) == "=" and re.search(
                        r"[A-Za-z0-9]\s*$", tail[:m.start(1)]):
                    continue
                try:
                    op, val = mk(Decimal(m.group(2).replace(",", "")))
                except Exception:
                    break
                out["numeric"].append((col, op, val))
                break
        else:
            head = q[max(start, pos - 6):pos]
            if re.search(r"(有值|有填|不是空)", tail + "｜" + head):
                out["numeric"].append((col, "notnull", None))

    rest = _RFQ_RE.sub(" ", q)
    for kind in ("RFQ", "RFS", "RFD"):
        if re.search(rf"(?<![A-Za-z0-9]){kind}(?![A-Za-z0-9])", rest, re.I):
            out["facets"]["rfq_kind"] = kind
            break
    if re.search(r"(已發行|發行的)", q):
        out["facets"]["state"] = "Released"
    if re.search(r"(作廢|取消)", q):
        out["facets"]["state"] = "Cancelled"
    if re.search(r"(有拋轉|已拋轉)", q):
        out["facets"]["cost_pushed"] = True

    out["has_cost_doc"] = bool(
        _COST_DOC_MENTION_RE.search(q) and not _COST_DOC_NEGATE_RE.search(q))
    return out


_ALLOWED_NUM = set(_LABEL_TO_COL.values())
_ALLOWED_FACET = {"rfq_kind", "state", "cost_pushed"}

_COL_TO_LABEL: dict[str, str] = {}
for _lbl, _c in _LABEL_TO_COL.items():
    if len(_lbl) > len(_COL_TO_LABEL.get(_c, "")):
        _COL_TO_LABEL[_c] = _lbl

_OP_WORD = {">": "大於", "<": "小於", "=": "等於"}
_FACET_LABEL = {"rfq_kind": "單型", "state": "狀態", "cost_pushed": "成本已拋轉"}


def _num_text(v) -> str:
    if isinstance(v, Decimal):
        f = float(v)
        return str(int(f)) if f.is_integer() else str(f)
    return str(v)


def describe(parsed: dict) -> list[list[str]]:
    rows: list[list[str]] = []
    for col, op, val in parsed.get("numeric") or []:
        label = _COL_TO_LABEL.get(col, col)
        if op == "notnull":
            rows.append([label, "有填（不是空白，包含填 0）"])
        else:
            rows.append([label, f"{_OP_WORD.get(op, op)} {_num_text(val)}"])
    for key, val in (parsed.get("facets") or {}).items():
        text = ("是" if val else "否") if isinstance(val, bool) else str(val)
        rows.append([_FACET_LABEL.get(key, key), text])
    if parsed.get("has_cost_doc"):
        rows.append(["成本分析附件", "只留掛得出附件原檔的（沒有原檔的不列入）"])
    rows += tfmod.describe(parsed.get("time"))
    if parsed.get("rfq_nos"):
        rows.append(["指名編號", "、".join(parsed["rfq_nos"])])
    if parsed.get("parents"):
        rows.append(["母單號", "、".join(parsed["parents"])])
    return rows


def _hard_where(p: dict) -> tuple[list[str], list]:
    if p["rfq_nos"]:
        return [], []
    where, args = [], []
    for col, op, val in p["numeric"]:
        if col not in _ALLOWED_NUM:
            continue
        if op == "notnull":
            where.append("[SQL 已於公開版移除]")
        else:
            where.append("[SQL 已於公開版移除]")
            args.append(val)
    for k, v in p["facets"].items():
        if k in _ALLOWED_FACET:
            where.append("[SQL 已於公開版移除]")
            args.append(v)
    twhere, targs = tfmod.sql(p.get("time"))
    where += twhere
    args += targs
    if p.get("has_cost_doc"):
        where.append(
            "[SQL 已於公開版移除]")
    return where, args


def _ch_id(cur, p: dict) -> list[str]:
    hits: list[str] = []
    for no in p["rfq_nos"]:
        cur.execute(
            "[SQL 已於公開版移除]",
            (no, no + "%"),
        )
        hits += [r[0] for r in cur.fetchall()]
    for parent in p["parents"]:
        cur.execute(
            "[SQL 已於公開版移除]",
            (parent, f"%{parent}%"),
        )
        hits += [r[0] for r in cur.fetchall()]
    tokens = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-./]{5,}", p["text"])
              if re.search(r"\d", t)]
    for tok in tokens[:4]:
        cur.execute(
            "[SQL 已於公開版移除]",
            (f"%{tok}%", f"%{tok}%", f"%{tok}%"),
        )
        hits += [r[0] for r in cur.fetchall()]
    return hits


def _ch_numeric(cur, p: dict) -> list[str]:
    if not p["numeric"]:
        return []
    where, args = [], []
    for col, op, val in p["numeric"]:
        if col not in _ALLOWED_NUM:
            continue
        if op == "notnull":
            where.append("[SQL 已於公開版移除]")
        else:
            where.append("[SQL 已於公開版移除]")
            args.append(val)
    if not where:
        return []
    cur.execute(
        "[SQL 已於公開版移除]",
        args,
    )
    return [r[0] for r in cur.fetchall()]


def _ch_time(cur, p: dict) -> list[str]:
    tf = p.get("time")
    where, args = tfmod.sql(tf)
    if not where:
        return []
    order = tfmod.order_by(tf) or "created_on DESC NULLS LAST"
    cur.execute(
        "[SQL 已於公開版移除]", args)
    return [r[0] for r in cur.fetchall()]


def _ch_cost_doc(cur, p: dict) -> list[str]:
    if not p.get("has_cost_doc"):
        return []
    where = ["[SQL 已於公開版移除]"]
    args: list = []
    tf = p.get("time")
    twhere, targs = tfmod.sql(tf, col_prefix="r.")
    where += twhere
    args += targs
    order = tfmod.order_by(tf, col_prefix="r.") or "r.modified_on DESC NULLS LAST"
    cur.execute(
        "[SQL 已於公開版移除]", args)
    return [row[0] for row in cur.fetchall()]


def _ch_facet(cur, p: dict) -> list[str]:
    if not p["facets"]:
        return []
    where = ["[SQL 已於公開版移除]" for k in p["facets"]]
    cur.execute(
        "[SQL 已於公開版移除]",
        list(p["facets"].values()),
    )
    return [r[0] for r in cur.fetchall()]


_TOKEN_RE = re.compile(r"[A-Za-z0-9一-鿿][A-Za-z0-9一-鿿._/-]*")
_STOP = {"的", "有", "是", "哪些", "查詢", "報價單", "多少", "什麼", "資料", "請問", "幫我"}


def _ch_lexical(cur, q: str, extra: list[str] | None = None) -> list[str]:
    toks = [t for t in _TOKEN_RE.findall(q) if t not in _STOP and len(t) > 1]
    extra = extra or []
    safe_extra = [t for t in extra if _TOKEN_RE.fullmatch(t)]
    frags = synonyms.expand_tsq([t for t in safe_extra if t not in toks])[:16]
    all_toks = toks[:12] + frags

    hits: list[str] = []
    if all_toks:
        tsq = " | ".join(all_toks)
        try:
            cur.execute(
                "[SQL 已於公開版移除]",
                (tsq, tsq),
            )
            hits = [r[0] for r in cur.fetchall()]
        except psycopg.OperationalError:
            raise
        except Exception as e:
            cur.connection.rollback()
            print(f"[retrieval] 字面通道的 tsquery 失敗，改用 trigram 兜底："
                  f"{type(e).__name__}: {e}（tsq={tsq[:120]}）")

    probes = (toks[:4] if not extra else (safe_extra[:4] + toks[:2]))
    for tok in probes:
        cur.execute(
            "[SQL 已於公開版移除]",
            (f"%{tok}%",) * 4,
        )
        hits += [r[0] for r in cur.fetchall()]
    return hits


_DF: dict[str, int] | None = None
_DF_LOCK = threading.Lock()
_DF_MAX = 0.08


def _df(cur) -> dict[str, int]:
    global _DF
    if _DF is not None:
        return _DF
    with _DF_LOCK:
        if _DF is not None:
            return _DF
        terms = sorted({t.lower() for v in synonyms.TERMS.values() for t in v
                        if _TOKEN_RE.fullmatch(t)})
        cur.execute("[SQL 已於公開版移除]")
        (total,) = cur.fetchone()
        df: dict[str, int] = {"__total__": total}
        try:
            cur.execute(
                "[SQL 已於公開版移除]",
                (terms,))
            df.update({t: n for t, n in cur.fetchall()})
        except psycopg.OperationalError:
            raise
        except Exception as e:
            cur.connection.rollback()
            print(f"[retrieval] 批次算詞頻失敗，退回逐詞："
                  f"{type(e).__name__}: {e}")
            for t in terms:
                cur.execute("[SQL 已於公開版移除]", (t,))
                (n,) = cur.fetchone()
                df[t] = n
        _DF = df
    return _DF


def _query_concepts(q: str, groups: list[list[str]]) -> list[list[str]]:
    toks = [t for t in _TOKEN_RE.findall(q)
            if t not in _STOP and len(t) > 1 and not re.search(r"[一-鿿]", t)]
    seen = {t.lower() for g in groups for t in g}
    out = list(groups)
    for t in toks:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append([t])
    return out


def _ch_synonym(cur, groups: list[list[str]]) -> list[str]:
    if not groups:
        return []
    df = _df(cur)
    total = df.get("__total__", 1) or 1

    scored: list[tuple[float, str]] = []
    for g in groups:
        frags = synonyms.expand_tsq(g)
        if not frags:
            continue
        worst = max((df.get(t.lower(), 0) for t in g), default=0)
        if worst / total > _DF_MAX:
            continue
        clause = frags[0] if len(frags) == 1 else "(" + " | ".join(frags) + ")"
        scored.append((worst / total, clause))
    if len(scored) < 2:
        return []
    scored.sort()
    picked = [c for _s, c in scored[:6]]

    score = " + ".join("[SQL 已於公開版移除]" for _ in picked)
    or_q = " | ".join(picked)
    try:
        cur.execute(
            "[SQL 已於公開版移除]",
            picked + [or_q, or_q])
        return [r[0] for r in cur.fetchall()]
    except psycopg.OperationalError:
        raise
    except Exception:
        cur.connection.rollback()
        return []


def _ch_vector(cur, q: str, extra: list[str] | None = None) -> list[str]:
    if extra:
        q = q + " " + " ".join(extra[:16])
    cur.execute("[SQL 已於公開版移除]")
    if cur.fetchone() is None:
        return []
    try:
        from .embed import _model
    except Exception:
        return []
    try:
        vec = _model().encode([q], normalize_embeddings=True)[0].tolist()
    except Exception:
        return []
    cur.execute(
        "[SQL 已於公開版移除]",
        (vec,),
    )
    return [r[0] for r in cur.fetchall()]


def _rrf(channels: dict[str, list[str]]) -> list[str]:
    score: dict[str, float] = {}
    for name, ids in channels.items():
        w = WEIGHTS[name]
        seen = set()
        for rank, rid in enumerate(ids):
            if rid in seen:
                continue
            seen.add(rid)
            score[rid] = score.get(rid, 0.0) + w / (RRF_K + rank + 1)
    return sorted(score, key=score.get, reverse=True)


_KIND_PREFIX = re.compile(r"^(?:RFQ|RFS|RFD)-")

_KIND_RANK = {"RFQ": 0, "RFS": 1, "RFD": 2}


def _merge_key(r: dict) -> str:
    no = r.get("rfq_no") or ""
    return _KIND_PREFIX.sub("", no) or f"#id{r.get('id')}"


def _dedupe(rows) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in rows:
        key = _merge_key(r)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)

    def rank(r: dict) -> tuple:
        return (not r.get("is_primary"),
                _KIND_RANK.get(r.get("rfq_kind") or "", 9))

    out = []
    for k in order:
        members = sorted(groups[k], key=rank)
        r = dict(members[0])
        if len(members) > 1:
            r["merged_nos"] = sorted(m["rfq_no"] for m in members[1:]
                                     if m.get("rfq_no"))
            r["merged_kinds"] = sorted(
                {m["rfq_kind"] for m in members if m.get("rfq_kind")},
                key=lambda kd: _KIND_RANK.get(kd, 9))
            specs, seen = [], set()
            for m in members:
                s = (m.get("spec_desc") or "").strip()
                if s and s not in seen:
                    seen.add(s)
                    specs.append([m.get("rfq_kind") or "", s])
            if len(specs) > 1:
                r["merged_specs"] = specs
        out.append(r)
    return out


def search(q: str, limit: int = 8) -> dict:
    p = parse(q)
    groups = synonyms.expand_groups(q)
    extra = synonyms.expand(q)
    with pool().connection() as conn, conn.cursor() as cur:
        channels = {
            "id": _ch_id(cur, p),
            "numeric": _ch_numeric(cur, p),
            "time": _ch_time(cur, p),
            "cost_doc": _ch_cost_doc(cur, p),
            "facet": _ch_facet(cur, p),
            "synonym": _ch_synonym(cur, _query_concepts(q, groups)),
            "lexical": _ch_lexical(cur, q, extra),
            "vector": _ch_vector(cur, q, extra),
        }
        ranked = _rrf(channels)
        hard, hargs = _hard_where(p)
        rows = []
        if ranked:
            sql = "[SQL 已於公開版移除]"
            args: list = [ranked]
            if hard:
                sql += " AND " + " AND ".join(hard)
                args += hargs
            cur.execute(sql, args)
            cols = [d.name for d in cur.description]
            by_id = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
            rows = _dedupe(by_id[i] for i in ranked if i in by_id)
            tf = p.get("time")
            rows = tfmod.sort_rows(rows, tf)
            if tf and tf.want_one:
                rows = rows[:1]
            else:
                rows = rows[:limit]
    tf = p.get("time")
    parsed = {k: v for k, v in p.items() if k not in ("text", "time") and v}
    if tf:
        parsed["time"] = tf.as_dict()
    return {
        "query": q,
        "parsed": parsed,
        "channels": {k: len(v) for k, v in channels.items() if v},
        "filtered": bool(hard),
        "time_sorted": bool(tf and tf.order),
        "time_one": bool(tf and tf.want_one),
        "results": rows,
    }


def by_number(rfq_no: str) -> dict | None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "[SQL 已於公開版移除]",
            (rfq_no,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d.name for d in cur.description], row))


def as_context(rows: list[dict], limit: int = 5,
               with_cost: bool = True,
               cost_cols: tuple[str, ...] | None = None) -> str:
    if not rows:
        return "（查無符合的資料）"
    out = []
    for r in rows[:limit]:
        body = [f"■ 編號：{r['rfq_no']}"]
        for key, label in (
            ("rfq_kind", "類別"), ("state", "狀態"), ("customer", "客戶"),
            ("final_customer", "最終客戶"), ("part_no", "產品料號"),
            ("rfw_no", "RFW送審單"), ("engineer", "負責工程師"),
            ("currency", "原幣幣別"),
            ("created_on", "建檔時間"),
            ("year_qty", "年需求量"), ("std_seconds", "標準工時(秒)"),
            ("work_hours", "總工時"),
        ):
            if r.get(key):
                body.append(f"- {label}：{r[key]}")
        for key, label in (
            ("spec_desc", "規格描述"), ("require_content", "需求內容"),
            ("report_note", "負責人回報備註"),
        ):
            if r.get(key):
                body.append(f"- {label}：{str(r[key]).strip()[:400]}")
        if with_cost:
            body.append(fields.render_cost_lines(r, cost_cols))
        out.append("\n".join(body))
    return "\n\n────────\n\n".join(out)
