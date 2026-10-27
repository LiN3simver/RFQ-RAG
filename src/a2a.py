"""A2A 適配層：把分流、檢索、查價、估價與核對包成 agentkit 的 AgentSpec，掛上 /a2a/*。"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from collections.abc import AsyncIterator
from concurrent.futures import Future
from contextlib import asynccontextmanager
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

_HUB = Path(os.environ.get("AGENTKIT_PATH") or Path(__file__).resolve().parents[2] / "Agent_Hub_RAG_Frontend")
if not (_HUB / "agentkit").is_dir():
    _HUB = Path(__file__).resolve().parents[1] / "vendor"
if _HUB.is_dir() and str(_HUB) not in sys.path:
    sys.path.append(str(_HUB))

from agentkit import AgentSpec, Block, Hit, ToolSpec, build_router  # noqa: E402
from agentkit import registry  # noqa: E402
from agentkit.envelope import EV_DONE, EV_ERROR, EV_META, sse  # noqa: E402

from . import (admission, agent_tools, answer, estimate, fields,  # noqa: E402
               guard, guess, health, intent, lessons, material, pdf_cost,
               product, retrieval)
from .db import pool  # noqa: E402

AGENT_ID = "rfq"

SYNC_STALE_HOURS = float(os.environ.get("SYNC_STALE_HOURS", "100"))


def _search(query: str, top_k: int, filters: dict[str, Any]) -> dict[str, Any]:
    it = intent.parse(query)

    if it["kind"] == intent.KIND_ARCHITECTURE:
        return {"results": [], "intent": it,
                "note": "技術架構說明見 ARCHITECTURE.md（公開版省略執行期文件）"}

    if it["kind"] == intent.KIND_CAPABILITIES:
        return {"query": query, "intent": it, "results": [],
                "capabilities": _capabilities()}

    if it["kind"] == intent.KIND_MATERIAL:
        return {**_material_found(it, top_k), "query": query, "intent": it}

    if it["kind"] == intent.KIND_PRODUCT:
        return {**_product_found(it, top_k), "query": query, "intent": it}

    if it["kind"] == intent.KIND_ESTIMATE:
        return {**_estimate_found(it), "query": query, "intent": it}

    if it["kind"] == intent.KIND_RFQ and it["want_material_detail"]:
        detail = [material.by_rfq(no) for no in it["rfq_nos"]]
        got = [d for d in detail if d.get("found")]
        out: dict[str, Any] = {"query": query, "intent": it, "results": [],
                               "rfq_details": detail}
        if not got:
            cov = material.coverage()
            out["note"] = (
                f"{'、'.join(it['rfq_nos'])} 目前沒有已匯入的成本分析附件。"
                f"（已索引 {cov['docs']} 份、涵蓋 {cov['rfqs_covered']} 張 RFQ。）"
                f"這張單的表單彙總成本欄請改問「的成本」。")
        return out

    if it["kind"] == intent.KIND_RFQ and not it["want_list"]:
        rows = [r for r in (retrieval.by_number(n) for n in it["rfq_nos"]) if r]
        if rows:
            out: dict[str, Any] = _with_sources(
                {"query": query, "results": rows, "intent": it, "exact": True,
                 "parsed": {"rfq_nos": it["rfq_nos"]}})
            if not it["want_form"]:
                out["hint"] = (
                    "（未顯示表單彙總成本欄。要看綠框「原幣成本欄位」與黃框"
                    "「USD欄位(自動計算)」請在問句裡寫明，例如"
                    "「RFQ-xxx 的原幣成本欄位」或「USD field」；"
                    "要看附件 PDF 的逐項材料請問「材料明細」。）")
            if it.get("want_labor"):
                out["hint"] = (out.get("hint") or "") + (
                    "（人工成本另外還有一份：成本分析附件的 B 段有 SMT／組裝／"
                    "委外／其它四列費用，以及預估工站、預計組立總工時(Sec)、"
                    "PPH、工時成本費率、人工效率。要看那份請問"
                    "「成本分析表的人工成本」。）")
            return out
        return {"query": query, "results": [], "intent": it, "exact": True,
                "note": f"查無編號 {'、'.join(it['rfq_nos'])}。"
                        f"（沒有自動列出相近編號；需要的話請說「列出相近的編號」。）"}

    hdr = _header_found(query)
    if hdr:
        return {**hdr, "query": query, "intent": it}

    found = retrieval.search(query, limit=top_k)
    want_materials = bool((found.get("parsed") or {}).get("has_cost_doc"))
    out = _with_sources({**found, "intent": it}, with_materials=want_materials)
    if guess.is_ambiguous(out):
        out["ambiguous"] = True
        out["results"] = []
    return out


_PREFETCHED: ContextVar[tuple[tuple, dict[str, Any], Any] | None] = \
    ContextVar("rfq_prefetched", default=None)

_PREFETCH_LIMITER = anyio.CapacityLimiter(admission.capacity())


def _req_key(query: str, top_k: int, filters: dict | None) -> tuple:
    return (query, top_k, json.dumps(filters or {}, sort_keys=True, default=str))


def _search_entry(query: str, top_k: int, filters: dict[str, Any]) -> dict[str, Any]:
    pre = _PREFETCHED.get()
    if pre is not None and pre[0] == _req_key(query, top_k, filters):
        return pre[1]
    admission.check_query(query)
    with admission.slot():
        return _search(query, top_k, filters)


def _hits_entry(found: dict[str, Any]) -> list[Hit]:
    pre = _PREFETCHED.get()
    if pre is not None and found is pre[1]:
        if isinstance(pre[2], BaseException):
            raise pre[2]
        return pre[2]
    return _hits(found)


@asynccontextmanager
async def prefetch(req: Any) -> AsyncIterator[Any]:
    try:
        admission.check_query(req.query)
        ticket = admission.enter()
    except (admission.Busy, admission.TooLong) as e:
        yield _refusal(e)
        return

    def work() -> tuple[dict[str, Any], Any]:
        with admission.slot(ticket):
            try:
                found = _search(req.query, req.top_k, req.filters or {})
            except Exception as e:   # noqa: BLE001 —— 跟 agentkit._run_search 同一個約定
                found = {"query": req.query, "results": [],
                         "error": f"{type(e).__name__}: {e}"}
            try:
                hits: Any = _hits(found)
            except Exception as e:   # noqa: BLE001
                hits = e
        return found, hits

    try:
        found, hits = await anyio.to_thread.run_sync(work, limiter=_PREFETCH_LIMITER)
    except admission.Busy as e:
        yield _refusal(e)
        return
    finally:
        ticket.release()
    token = _PREFETCHED.set((_req_key(req.query, req.top_k, req.filters), found, hits))
    try:
        yield None
    finally:
        _PREFETCHED.reset(token)


def _refusal(e: Exception) -> Any:
    from fastapi.responses import StreamingResponse

    ref = registry.get(AGENT_ID)
    busy = isinstance(e, admission.Busy)
    events = [
        sse(EV_META, {"agent_id": AGENT_ID, "agent_name": ref.name,
                      "status": "busy" if busy else "rejected"}),
        sse(EV_ERROR, {"error": getattr(e, "user_message", str(e)),
                       "hint": getattr(e, "hint", ""), "busy": busy}),
        sse(EV_DONE, {"llm_available": False}),
    ]
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if busy:
        headers["Retry-After"] = "5"
    return StreamingResponse(iter(events), media_type="text/event-stream",
                             headers=headers)


HEADER_ACCEPT_SIM = 0.60


def _header_found(query: str) -> dict[str, Any] | None:
    text = intent.part_name_query(query)
    if not text:
        return None
    try:
        hit = material.by_part_name(text)
    except Exception as e:      # noqa: BLE001 -- 多出來的一段比對不能弄壞查詢
        print(f"[a2a] 表頭 Part Name 比對失敗，改走一般檢索：{type(e).__name__}: {e}")
        return None
    if not hit.get("found"):
        return None
    if hit["matched_by"] == "similar":
        top = float(hit["docs"][0].get("sim") or 0)
        if top < HEADER_ACCEPT_SIM or hit.get("ambiguous"):
            return None
    return {"results": [], "header_docs": hit}


def _with_sources(out: dict[str, Any], with_materials: bool = False) -> dict[str, Any]:
    src: dict[str, dict[str, Any]] = {}
    for r in out.get("results") or []:
        no = (r.get("rfq_no") or "").strip()
        if not no or no in src:
            continue
        try:
            if with_materials:
                detail = material.by_rfq(no)
                src[no] = ({"docs": detail["docs"], "materials": detail["materials"],
                            "labor": detail["labor"]} if detail.get("found")
                           else {"docs": []})
            else:
                src[no] = {"docs": material.docs_for_rfq(no)}
        except Exception as e:
            src[no] = {"docs": [], "error": f"{type(e).__name__}: {e}"}
    out["sources"] = src
    return out


def _estimate_found(it: dict[str, Any]) -> dict[str, Any]:
    spec = (it.get("estimate_spec") or "").strip()
    if not spec:
        return {"results": [], "estimate": None,
                "note": "要估區間的話，請把產品規格描述一起寫進問句，例如"
                        "「MCIO 8X Straight to MCIO 8X Right Angle L=450mm "
                        "這類以前大概多少錢」。只有「大概多少錢」沒有規格，"
                        "沒有東西可以拿去比對歷史案例。"}
    try:
        est = estimate.estimate(spec)
        sources = _with_sources({"results": est.get("comparables") or []})
        return {"results": [], "estimate": est,
                "sources": sources["sources"]}
    except Exception as e:
        return {"results": [], "estimate": None,
                "note": f"估價索引現在算不出來（{type(e).__name__}: {e}）。"
                        f"這不代表歷史上沒有類似案例，是這台機器上的向量模型"
                        f"叫不動。改用「{spec}」直接檢索仍然查得到相似的單子。"}


def _first_sentence(s: str, cap: int = 110) -> str:
    s = re.sub(r"^【[^】]*】", "", (s or "").strip())
    for sep in ("。", "\n"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return (s[:cap] + "…") if len(s) > cap else s


def _capabilities() -> dict[str, Any]:
    routes: list[dict[str, Any]] = []
    for r in intent.ROUTES:
        checked = []
        for label, prompt in r.examples:
            got = intent.parse(prompt)["kind"]
            checked.append({"label": label, "prompt": prompt,
                            "routes_to": got, "ok": got == r.kind})
        routes.append({"title": r.title, "kind": r.kind, "what": r.what,
                       "trigger": r.trigger, "examples": checked})

    tools = [{"name": t.name, "summary": _first_sentence(t.description),
              "ready": t.ready} for t in TOOLS]

    data: dict[str, Any] = {}
    try:
        data["material_index"] = material.coverage()
    except Exception as e:
        data["material_index"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        with pool().connection() as c:
            total, vec = c.execute(
                "[SQL 已於公開版移除]").fetchone()
        data["rfq_form"] = {"rows": total, "embedded": vec}
    except Exception as e:
        data["rfq_form"] = {"error": f"{type(e).__name__}: {e}"}

    ref = registry.get(AGENT_ID)
    return {"routes": routes, "tools": tools, "data": data,
            "downstream": list(ref.downstream),
            "broken": [f"{c['label']}（宣稱 {r['kind']}，實際走 {c['routes_to']}）"
                       for r in routes for c in r["examples"] if not c["ok"]]}


def _material_found(it: dict[str, Any], top_k: int) -> dict[str, Any]:
    hits = [material.lookup(no, limit=top_k) for no in it["material_nos"]]
    found = [h for h in hits if h.get("found")]
    out: dict[str, Any] = {"materials": hits, "results": []}
    if it["want_similar"]:
        out["similar"] = [material.find_similar(no) for no in it["material_nos"]]
    if found:
        return out
    cov = material.coverage()
    missing = "、".join(it["material_nos"])
    out["note"] = (
        f"查無料號 {missing}。目前已索引 {cov['docs']} 份成本分析表、"
        f"{cov['distinct_parts']} 個相異料號（涵蓋 {cov['rfqs_covered']} 張 RFQ）。"
        f"這顆料可能是還沒匯入的附件，不一定是不存在。"
        f"（沒有自動列出相近料號；需要的話請說「列出相近的料號」。）"
    )
    return out


def _product_found(it: dict[str, Any], top_k: int) -> dict[str, Any]:
    tf = it.get("time")
    want_doc = bool(it.get("want_cost_doc") or it.get("want_material_detail"))
    hits = [product.lookup(no, tf, limit=max(top_k, 30),
                           kind=it.get("rfq_kind"), want_doc=want_doc)
            for no in it["product_nos"]]
    found = [h for h in hits if h.get("found")]
    out: dict[str, Any] = {"products": hits, "results": []}

    if not found:
        cov = product.coverage()
        h = hits[0] if hits else {}
        missing = "、".join(it["product_nos"])
        tried = "、".join(f"{a['label']}" for a in h.get("attempts") or [])
        note = (f"查無產品料號 {missing}。"
                f"已經試過這 {len(h.get('attempts') or [])} 種比對方式都沒有命中："
                f"{tried}。")
        if h.get("filtered_out_by_time"):
            note = (f"產品料號 {missing} 在庫裡是存在的"
                    f"（共 {h['filtered_out_by_time']} 筆），"
                    f"但在你指定的時間範圍（{h.get('time')}）內沒有任何一張單。")
        else:
            note += (f"目前索引了 {cov['distinct_products']} 個相異產品料號、"
                     f"{cov['rows_with_part_no']} 列帶產品料號的單。"
                     f"這顆料可能還沒匯入，不一定是不存在。")
        out["note"] = note
        return out

    if want_doc:
        nos = [no for h in found for no in (h.get("docs") or {})]
        if nos:
            out["rfq_details"] = [material.by_rfq(no) for no in nos]
    return out


_META_KEYS = ("rfq_kind", "state", "customer", "part_no", "rfw_no")


def _meta_line(r: dict) -> str:
    line = " · ".join(str(r[k]) for k in _META_KEYS if r.get(k))
    also = r.get("merged_nos") or []
    if also:
        line += f"　│　同項次另有 {'、'.join(also)}（內容相同，已合併）"
    return line


def _jsonable(row: dict) -> dict:
    return _deep(row)


def _deep(v: Any) -> Any:
    if isinstance(v, Decimal):
        f = float(v)
        return int(f) if f.is_integer() else f
    if isinstance(v, dict):
        return {k: _deep(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_deep(x) for x in v]
    return v


def _content_blocks(row: dict) -> list[Block]:
    text = row.get("require_content") or ""
    if not text:
        return []
    return [Block(kind="text", title="需求內容", text=text)]


def _spec_blocks(row: dict) -> list[Block]:
    specs = row.get("merged_specs")
    if not specs:
        return []
    return [Block(kind="table", title="各單型的規格描述（措辭不同）",
                  head=["單型", "規格描述"], rows=specs)]


def _cost_blocks(row: dict) -> list[Block]:
    green = [[label_l, row.get(local)] for local, _, label_l, _ in fields.COST_ROWS]
    yellow = [[label_u, row.get(usd)] for _, usd, _, label_u in fields.COST_ROWS if usd]
    return [
        Block(kind="kv", title=fields.GREEN_TITLE, tone="green", rows=green),
        Block(kind="kv", title=fields.YELLOW_TITLE, tone="yellow", rows=yellow),
    ]


def _material_table_blocks(src: dict[str, Any]) -> list[Block]:
    mats = src.get("materials")
    if not mats:
        return []
    return [Block(
        kind="table", tone="green",
        title=f"成本分析附件的材料明細（{len(mats)} 項）",
        head=["項目", "材料料號", "材料名稱", "供應商",
              "規格說明", "原幣別", "原幣單價", "匯率",
              "單價(USD)", "用量", "單位", "MOQ",
              "耗損Loss", "總價", "材料交期", "備註", "驗算"],
        rows=[[m.get("seq"), m.get("part_no"), m.get("name"),
               _sup(m), m.get("spec"),
               m.get("currency"), m.get("unit_price_raw"),
               m.get("fx_rate"), m.get("unit_price"),
               m.get("qty"), m.get("unit"), m.get("moq"),
               m.get("loss_pct"), m.get("line_total"),
               m.get("lead_time"), m.get("remark"),
               _CHECK_LABEL.get(m.get("check_status"))]
              for m in mats])]


def _file_url(doc_id: Any) -> str | None:
    if not doc_id:
        return None
    hub = os.environ.get("A2A_HUB_PUBLIC_URL")
    if hub:
        return f"{hub.rstrip('/')}/hub/file/{AGENT_ID}/{doc_id}"
    return f"{registry.get(AGENT_ID).base_url}/a2a/file/{doc_id}"


def _doc_links(docs: list[dict]) -> list[list[str]]:
    out, seen = [], set()
    for d in docs:
        url = _file_url(d.get("id") or d.get("doc_id"))
        if not url or url in seen:
            continue
        seen.add(url)
        v = material.file_version(d)
        name = f"{d['label']}（{v['name']}）" if d.get("label") else v["name"]
        out.append([f"📄 {name}｜{v['text']}", url])
    return out


_CTX_TOP = 5


def _v(x: Any) -> str:
    return "未填" if x is None else str(x)

_CHECK_LABEL = {
    "ok": "已驗算",
    "repaired": "已驗算（修正過小數點）",
    "rescaled": "推測過小數點，僅供參考",
    "unverified": "★ 驗算不符，不可當報價",
    "na": "欄位不齊，無從驗算",
}


def _sup(m: dict[str, Any]) -> str | None:
    v = m.get("supplier")
    if v and m.get("supplier_uncertain"):
        return f"{v}（⚠OCR信心低，請核對PDF）"
    return v


def _material_hits(found: dict[str, Any]) -> list[Hit]:
    hits: list[Hit] = []
    for res in found.get("materials") or []:
        if not res.get("found"):
            continue
        items = res["items"]
        price = res.get("price") or {}
        badges = [[cur, f"{v['min']} ~ {v['max']}（中位 {v['median']}）"]
                  for cur, v in price.items()]
        head = ["來源 RFQ", "附件更新", "材料名稱", "供應商", "規格說明", "原幣別",
                "原幣單價", "匯率", "單價(USD)", "用量", "單位", "MOQ",
                "耗損Loss", "總價", "材料交期", "備註", "驗算"]
        rows = [[
            it.get("rfq_no"), material.file_version(it)["short"],
            it.get("name"), _sup(it),
            it.get("spec"), it.get("currency"), it.get("unit_price_raw"),
            it.get("fx_rate"), it.get("unit_price"), it.get("qty"),
            it.get("unit"), it.get("moq"), it.get("loss_pct"),
            it.get("line_total"), it.get("lead_time"), it.get("remark"),
            _CHECK_LABEL.get(it.get("check_status"), it.get("check_status")),
        ] for it in items]
        first = items[0]
        links = _doc_links([{**it, "id": it.get("doc_id"),
                             "label": it.get("rfq_no")} for it in items])
        hits.append(Hit(
            id=res["part_no"],
            title=first.get("name") or "",
            subtitle=f"出現在 {len(items)} 份成本分析表",
            body=(first.get("spec") or "")[:400],
            badges=badges,
            links=links,
            blocks=[Block(kind="table", title="歷史單價（各次成本分析）",
                          tone="green", head=head, rows=rows)],
            raw=_jsonable(res),
        ))
    for sim in found.get("similar") or []:
        if not sim.get("results"):
            continue
        hits.append(Hit(
            id=f"{sim['part_no']}（相近料號）",
            title="這些是相近的料號，不是你問的那一顆",
            blocks=[Block(kind="table", tone="warn",
                          head=["料號", "出現次數", "品名", "原幣", "最低", "最高"],
                          rows=[[r["part_no"], r["n"], r["name"], r["currency"],
                                 r["price_min"], r["price_max"]]
                                for r in sim["results"]])],
            raw=_jsonable(sim),
        ))
    return hits


_COST_HEAD = ["編號", "單型", "狀態", "建檔", "客戶", "規格描述", "原幣別",
              "材料", "人工", "製造", "模治具", "管銷", "MP",
              "材料(USD)", "人工(USD)", "製造(USD)", "單價(USD)",
              "年需求量", "標準工時(秒)", "附件"]


def _product_hits(found: dict[str, Any]) -> list[Hit]:
    hits: list[Hit] = []
    for res in found.get("products") or []:
        if not res.get("found"):
            continue
        docs = res.get("docs") or {}
        rows = []
        for r in res["rows"]:
            ds = docs.get(r.get("rfq_no")) or []
            rows.append([
                r.get("rfq_no"), r.get("rfq_kind"), r.get("state"),
                str(r.get("created_on") or "")[:10], r.get("customer"),
                (r.get("spec_desc") or "")[:60], r.get("currency"),
                r.get("mat_cost"), r.get("labor_cost"), r.get("mfg_cost"),
                r.get("tooling_cost"), r.get("operating_cost"), r.get("mp_cost"),
                r.get("mat_cost_usd"), r.get("labor_cost_usd"),
                r.get("mfg_cost_usd"), r.get("unit_cost_usd"),
                r.get("year_qty"), r.get("std_seconds"),
                f"{len(ds)} 份" if ds else "無",
            ])
        c = res["cost"]
        scope = f"{res['n_total']} 張（只算 {res['kind']}）" \
            if res.get("kind") else f"{res['n_total']} 張"
        badges = [["開過的單", scope],
                  ["成本欄有值", f"{c['rows_with_cost']} 張"],
                  ["成本欄填 0", f"{c['rows_all_zero']} 張"],
                  ["成本欄從未填寫", f"{c['rows_unfilled']} 張"],
                  ["掛得出成本分析附件", f"{res['n_total_with_doc']} 張"]]
        if res.get("matched_by") != "exact_part":
            badges.append(["比對方式", res.get("matched_label")])
        if res.get("time"):
            badges.append(["時間條件", res["time"]])
        if res.get("truncated_by_time"):
            badges.append(["只顯示 1 張",
                           f"你問的是單一一張；這顆料總共有 {res['n_total']} 張"])
        if res.get("no_doc_fallback"):
            badges.append(["注意",
                           "這顆料的每一張單都沒有成本分析附件，"
                           "所以挑的是時間上最新的那一張"])

        links = _doc_links([{**d, "label": no}
                            for no, ds in docs.items() for d in ds])
        hits.append(Hit(
            id=res["part_no"],
            title=f"產品料號 {res['part_no']}：歷史上開過 {scope}單",
            subtitle=(res["rows"][0].get("spec_desc") or "")[:120],
            badges=badges,
            links=links,
            blocks=[Block(kind="table", tone="green",
                          title="每一張單與它的表單成本欄"
                                "（空白＝沒有人填，0＝填了零）",
                          head=_COST_HEAD, rows=rows)],
            raw=_jsonable({k: v for k, v in res.items() if k != "rows"}),
        ))
    if found.get("note"):
        hits.append(Hit(
            id="查不到",
            title=found["note"],
            blocks=[Block(kind="kv", tone="warn",
                          rows=[["結論", found["note"]]])],
            raw={"note": found["note"],
                 "attempts": [p.get("attempts")
                              for p in found.get("products") or []]},
        ))
    return hits


_LABOR_CHECK_LABEL = {"ok": "算式對得上", "mismatch": "★ 算式對不上",
                      "na": "無從檢查"}

_LABOR_PARAM_LABELS = (
    ("stations", "預估工站"),
    ("total_sec", "組立總工時(Sec)"),
    ("pph", "PPH"),
    ("hr_rate", "工時成本/Hr"),
    ("efficiency_pct", "人工效率%"),
)

_labor_note_cell = pdf_cost.labor_note_residual


def _labor_formula(asm: dict) -> str | None:
    rate, sec = asm.get("hr_rate"), asm.get("total_sec")
    if rate is None or sec is None:
        return None
    cur = asm.get("rate_currency") or ""
    eff = asm.get("efficiency_pct")
    line = f"{rate} {cur}/Hr × {sec} 秒 ÷ 3600"
    if eff is not None:
        line += f" ÷ {eff}%"
    return f"{line} = {asm.get('cost')}"


def _labor_blocks(doc: dict[str, Any], rows: list[dict]) -> list[Block]:
    if not rows:
        return []
    asm = next((r for r in rows if (r.get("total_sec") is not None
                                    or r.get("hr_rate") is not None)), None)
    blocks = [Block(
        kind="table", tone="green",
        title="人工成本明細（B 段）",
        head=(["項次", "人工明細", "費用"]
              + [label for _key, label in _LABOR_PARAM_LABELS]
              + ["幣別", "說明", "驗算"]),
        rows=[[r.get("seq"), r.get("category"), r.get("cost")]
              + [r.get(key) for key, _label in _LABOR_PARAM_LABELS]
              + [r.get("rate_currency"), _labor_note_cell(r),
                 _LABOR_CHECK_LABEL.get(r.get("check_status"))]
              for r in rows])]
    formula = _labor_formula(asm) if asm else None
    blocks.append(Block(kind="kv", tone="yellow", title="人工成本對帳", rows=[
        ["B Total（表上印的）", doc.get("labor_total")],
        ["各列費用之和", doc.get("labor_line_sum")],
        ["對得上嗎", "是" if doc.get("labor_verified") else "否（見下方說明）"],
    ] + ([["組裝費用怎麼算的", formula]] if formula else [])))
    return blocks


def _cost_doc_detail_blocks(src: dict[str, Any], want_labor: bool) -> list[Block]:
    mats = src.get("materials")
    if not mats:
        return []
    doc = (src.get("docs") or [{}])[0]
    mat_blocks = _material_table_blocks(src)
    labor_blocks = _labor_blocks(doc, src.get("labor") or [])
    return (labor_blocks + mat_blocks) if want_labor else (mat_blocks + labor_blocks)


def _header_block(doc: dict[str, Any]) -> Block:
    return Block(
        kind="kv", title="附件表頭（欄位同原始 PDF 第一區塊）", tone="green",
        rows=[[label, doc.get(key)] for key, label in material.HEADER_LABELS])


def _header_hits(found: dict[str, Any]) -> list[Hit]:
    hit = found.get("header_docs") or {}
    if not hit.get("found"):
        return []
    docs = hit.get("docs") or []
    by_doc: dict[Any, list[dict]] = {}
    for m in hit.get("materials") or []:
        by_doc.setdefault(m.get("doc_id"), []).append(m)
    labor_by_doc: dict[Any, list[dict]] = {}
    for r in hit.get("labor") or []:
        labor_by_doc.setdefault(r.get("doc_id"), []).append(r)
    shaped = {
        "intent": found.get("intent"),
        "rfq_details": [
            {"found": True, "rfq_no": d.get("rfq_no") or d.get("file_name"),
             "docs": [d],
             "materials": by_doc.get(d["id"]) or [],
             "labor": labor_by_doc.get(d["id"]) or []}
            for d in docs],
    }
    return _rfq_detail_hits(shaped)


def _rfq_detail_hits(found: dict[str, Any]) -> list[Hit]:
    want_labor = bool((found.get("intent") or {}).get("want_labor"))
    hits: list[Hit] = []
    for det in found.get("rfq_details") or []:
        if not det.get("found"):
            continue
        by_doc: dict[Any, list[dict]] = {}
        for m in det["materials"]:
            by_doc.setdefault(m.get("doc_id"), []).append(m)
        labor_by_doc: dict[Any, list[dict]] = {}
        for r in det.get("labor") or []:
            labor_by_doc.setdefault(r.get("doc_id"), []).append(r)
        for doc in det["docs"]:
            mats = by_doc.get(doc["id"]) or []
            labor = labor_by_doc.get(doc["id"]) or []
            verified = doc.get("totals_verified")
            v = material.file_version(doc)
            hits.append(Hit(
                id=f"{det['rfq_no']}／{v['name']}（{v['text']}）",
                title=(f"成本分析附件的人工成本（B 段 {len(labor)} 列）"
                       if want_labor and labor else
                       f"成本分析附件的材料明細（{len(mats)} 項）"),
                subtitle=(f"檔案{v['text']}｜幣別 {doc.get('currency')}"
                          f"｜抽取方式 {doc.get('source')}"
                          f"｜彙總對帳 {'通過' if verified else '未通過'}"),
                links=_doc_links([doc]),
                badges=([["B Total", doc.get("labor_total")],
                         ["A Total", doc.get("mat_total")],
                         ["合計", doc.get("grand_total")]]
                        if want_labor and labor else
                        [["A Total", doc.get("mat_total")],
                         ["合計", doc.get("grand_total")]]),
                blocks=([] if not want_labor else _labor_blocks(doc, labor)) + [
                    Block(kind="table", title="材料明細（欄位同原始 PDF）",
                          tone="green",
                          head=["項目", "材料料號", "材料名稱", "供應商",
                                "規格說明", "原幣別", "原幣單價", "匯率",
                                "單價(USD)", "用量", "單位", "MOQ",
                                "耗損Loss", "總價", "材料交期", "備註", "驗算"],
                          rows=[[m.get("seq"), m.get("part_no"), m.get("name"),
                                 _sup(m), m.get("spec"),
                                 m.get("currency"), m.get("unit_price_raw"),
                                 m.get("fx_rate"), m.get("unit_price"),
                                 m.get("qty"), m.get("unit"), m.get("moq"),
                                 m.get("loss_pct"), m.get("line_total"),
                                 m.get("lead_time"), m.get("remark"),
                                 _CHECK_LABEL.get(m.get("check_status"))]
                                for m in mats]),
                    _header_block(doc),
                    Block(kind="kv", title="附件彙總", tone="yellow", rows=[
                        ["A 材料", doc.get("mat_total")],
                        ["B 人工", doc.get("labor_total")],
                        ["C 模治具", doc.get("tooling_total")],
                        ["D 其他", doc.get("other_total")],
                        ["製造成本", doc.get("mfg_cost")],
                        ["管銷", doc.get("overhead")],
                        ["合計", doc.get("grand_total")],
                        ["明細總價之和", doc.get("material_line_sum")],
                    ]),
                ] + ([] if want_labor else _labor_blocks(doc, labor)),
                raw=_jsonable({"doc": doc, "materials": mats,
                               "labor": labor}),
            ))
    return hits


_CONF_LABEL = {"high": "高信心", "medium": "中信心", "low": "低信心",
               "none": "沒有可用的歷史案例"}

_EST_SOURCE = ("表單語料表 的表單彙總成本欄（USD 欄）。這不是成本分析附件 PDF "
               "的逐項材料單價，兩者不能互相代換。下方佐證案例若有附件原檔，"
               "連結會掛在這張卡的最底下。")


def _estimate_hits(est: dict[str, Any],
                   sources: dict[str, Any]) -> list[Hit]:
    basis = est.get("basis") or {}
    bands = est.get("estimate") or {}
    conf = est.get("confidence") or "none"
    label = _CONF_LABEL.get(conf, conf)
    n = basis.get("n", 0)

    if bands:
        band_block = Block(
            kind="table", tone="green" if conf == "high" else "yellow",
            title=f"以前做過的 {n} 張類似單子，成本落在這個範圍（美元）",
            head=["成本項", "偏低", "中間", "偏高", "最低", "最高", "幾張單"],
            rows=[[v.get("label"), v.get("p25"), v.get("median"), v.get("p75"),
                   v.get("min"), v.get("max"), v.get("n")]
                  for v in bands.values()])
    else:
        band_block = Block(kind="kv", tone="warn", rows=[
            ["算不出範圍", "找得到的類似單子不到 3 張，算出來的範圍沒有意義，"
                          "所以一個數字都不給"]])

    if conf == "high":
        why = "這幾張單的成本很接近，所以這個範圍比較可信"
    elif bands:
        why = "這幾張單的成本落差不小，範圍只能當找料的起點，不要拿來報價"
    else:
        why = "可以參考的舊單太少"

    badges = [["信心", label], ["參考了", f"{n} 張以前的單子"]]
    if est.get("unavailable"):
        badges.append(["這幾項估不出來",
                       "、".join(u["label"] for u in est["unavailable"])
                       + "（以前的單子幾乎都沒填）"])

    hits = [Hit(
        id=f"成本區間參考：{est.get('query') or ''}",
        title=("這是估價參考，不是報價。"
               + ("可以當成本預估的起點，但仍須由 BOM 與製程工時覆核"
                  if conf == "high" else
                  "這個範圍不可以拿去報價，請走完整的 BOM 估算")),
        subtitle=why,
        badges=badges,
        blocks=[band_block],
        raw=_jsonable({k: v for k, v in est.items() if k != "comparables"}),
    )]

    comps = est.get("comparables") or []
    if comps:
        docs, missing, failed = [], [], []
        for c in comps:
            no = (c.get("rfq_no") or "").strip()
            s = sources.get(no) or {}
            if s.get("error"):
                failed.append(no)
            elif s.get("docs"):
                docs += [{**d, "label": no} for d in s["docs"]]
            else:
                missing.append(no)
        badges = []
        if missing:
            badges.append(["無附件原檔",
                           f"{len(missing)} 筆：{'、'.join(missing[:5])}"
                           + ("…" if len(missing) > 5 else "")])
        if failed:
            badges.append(["附件查詢失敗",
                           f"{len(failed)} 筆：{'、'.join(failed[:5])}"])
        hits.append(Hit(
            id="上面的範圍是從這幾張單子算出來的",
            title="以前做過的類似單子，最像的排在最前面",
            subtitle="成本都是美元，取自這幾張單子上填的成本欄",
            badges=badges,
            links=_doc_links(docs),
            blocks=[Block(
                kind="table", tone="neutral",
                head=["RFQ 編號", "同客戶", "規格描述", "客戶", "產品料號",
                      "年需求量", "材料", "人工", "製造", "管銷", "MP",
                      "標準工時(秒)", "MOQ A"],
                rows=[[c.get("rfq_no"),
                       "是" if c.get("same_customer") else "",
                       c.get("spec_desc"), c.get("customer"), c.get("part_no"),
                       c.get("year_qty")]
                      + [(c.get("costs") or {}).get(col)
                         for col, _l in estimate.COMPONENTS]
                      for c in comps])],
            raw=_jsonable({"comparables": comps}),
        ))
    return hits


def _capability_hits(cap: dict[str, Any]) -> list[Hit]:
    hits: list[Hit] = []
    d = cap.get("data") or {}
    mi = d.get("material_index") or {}
    rf = d.get("rfq_form") or {}
    hits.append(Hit(
        id="我現在有多少資料",
        title="覆蓋範圍（即時查詢，不是寫死的數字）",
        blocks=[Block(kind="kv", tone="good", rows=[
            ["RFQ/S/D 表單語料", rf.get("rows")],
            ["　已算向量", rf.get("embedded")],
            ["成本分析附件（已解析）", mi.get("docs")],
            ["　彙總對帳通過", mi.get("docs_verified")],
            ["　查得到檔案更新時間", mi.get("docs_versioned")],
            ["材料明細列數", mi.get("material_rows")],
            ["相異料號", mi.get("distinct_parts")],
            ["涵蓋 RFQ 張數", mi.get("rfqs_covered")],
            ["可委派的下游 agent", "、".join(cap.get("downstream") or []) or "無"],
        ])],
        raw=d,
    ))
    for r in cap.get("routes") or []:
        hits.append(Hit(
            id=r["title"],
            title=r["what"],
            subtitle=f"觸發條件：{r['trigger']}",
            blocks=[Block(
                kind="table", tone="green", title="照著問就會走這條路",
                head=["快捷鍵", "問句", "實際分流"],
                rows=[[c["label"], c["prompt"],
                       c["routes_to"] if c["ok"] else f"✗ {c['routes_to']}"]
                      for c in r["examples"]])] if r["examples"] else [],
            raw=r,
        ))
    hits.append(Hit(
        id="上游 agent 可以掛的工具",
        title="這些是給其他 agent 呼叫的（工具台也可以手動跑）",
        blocks=[Block(kind="table", tone="yellow",
                      head=["工具", "用途", "可用"],
                      rows=[[t["name"], t["summary"],
                             "是" if t["ready"] else "否"]
                            for t in cap.get("tools") or []])],
        raw={"tools": cap.get("tools")},
    ))
    return hits


def _capability_text(cap: dict[str, Any]) -> str:
    mi = (cap.get("data") or {}).get("material_index") or {}
    rf = (cap.get("data") or {}).get("rfq_form") or {}
    ref = registry.get(AGENT_ID)
    out = [f"我是「{ref.name}」：{ref.role}。目前能做這些事："]
    for i, r in enumerate(cap.get("routes") or [], 1):
        out.append(f"\n{i}. **{r['title']}**")
        out.append(f"   - {r['what']}")
        out.append(f"   - 什麼問法會走這裡：{r['trigger']}")
        if r["examples"]:
            out.append("   - 可以直接問：" +
                       "、".join(f"「{c['prompt']}」" for c in r["examples"]))
    out.append(
        f"\n手上的資料：RFQ/S/D 表單 {rf.get('rows', '?')} 筆；"
        f"成本分析附件已解析 {mi.get('docs', '?')} 份、"
        f"材料明細 {mi.get('material_rows', '?')} 列、"
        f"相異料號 {mi.get('distinct_parts', '?')} 個，"
        f"涵蓋 {mi.get('rfqs_covered', '?')} 張 RFQ。")
    out.append(
        "\n兩件要先知道的限制：\n"
        "- 表單彙總成本欄（綠框／黃框）與附件的逐項材料單價是**兩件事**，"
        "數字不能互相代換。\n"
        "- 掃描件的數字是 OCR 讀的，每一列都標了驗算狀態；"
        "標「驗算不符」的不可以當報價，請點卡片上的 PDF 連結核對。")
    if cap.get("downstream"):
        out.append(f"\n開啟「委派下游」時，我可以再去問："
                   + "、".join(cap["downstream"]))
    if cap.get("broken"):
        out.append("\n★ 自我檢查發現分流與說明對不上（這是 bug，請回報）："
                   + "；".join(cap["broken"]))
    return "\n".join(out)


def _hits(found: dict[str, Any]) -> list[Hit]:
    it = found.get("intent") or {}
    if found.get("capabilities"):
        return _capability_hits(found["capabilities"])
    if found.get("ambiguous"):
        return [Hit(
            id="看不懂這句話",
            title="沒有抓到料號、RFQ／RFS／RFD 編號、成本或時間條件，"
                  "字面與同義詞比對也沒有命中任何關鍵字",
            blocks=[Block(kind="kv", tone="warn", rows=[
                ["建議", "加上料號、RFQ 編號、具體規格描述或時間範圍，"
                         "或問「你可以做什麼」看系統支援的問法"],
            ])],
            raw={"ambiguous": True},
        )]
    if it.get("kind") == intent.KIND_ESTIMATE:
        if found.get("estimate"):
            return _estimate_hits(found["estimate"], found.get("sources") or {})
        return [Hit(id="估不出區間",
                    title=found.get("note") or "",
                    blocks=[Block(kind="kv", tone="warn",
                                  rows=[["為什麼沒有數字", found.get("note")]])],
                    raw={"note": found.get("note")})]
    if found.get("header_docs"):
        return _header_hits(found)
    if it.get("kind") == intent.KIND_MATERIAL:
        return _material_hits(found)
    if it.get("kind") == intent.KIND_PRODUCT:
        return _product_hits(found) + _rfq_detail_hits(found)
    if found.get("rfq_details") is not None:
        return _rfq_detail_hits(found)

    hits: list[Hit] = []
    if found.get("filtered"):
        hits.append(Hit(
            id="查詢條件",
            title="數值條件已轉成 SQL 硬性過濾，下方每一筆都保證符合",
            blocks=[Block(kind="kv", tone="good",
                          rows=retrieval.describe(found.get("parsed") or {}))],
            raw=_jsonable(found.get("parsed") or {}),
        ))
    show_cost = bool(it.get("want_form"))
    src_map = found.get("sources")
    for raw in found.get("results") or []:
        r = _jsonable(raw)
        src = (src_map or {}).get((r.get("rfq_no") or "").strip()) or {}
        links = _doc_links(src.get("docs") or [])
        badges = [["幣別", r.get("currency")]] if r.get("currency") else []
        if not links:
            if src_map is None:
                why = "這條路徑沒有查附件原檔"
            elif src.get("error"):
                why = f"附件查詢失敗（{src['error']}），無法確認有沒有原檔"
            else:
                why = "無成本分析附件原檔，這一筆的內容全部來自 PLM 表單欄位"
            badges.append(["資料來源", why])
        hits.append(Hit(
            id=r.get("rfq_no") or "",
            title=r.get("spec_desc") or "",
            subtitle=_meta_line(r),
            blocks=_content_blocks(r) + _spec_blocks(r)
                   + (_cost_blocks(r) if show_cost else [])
                   + _cost_doc_detail_blocks(src, bool(it.get("want_labor"))),
            badges=badges,
            links=links,
            raw=r,
        ))
    return hits


def _material_context(found: dict[str, Any]) -> str:
    if found.get("note"):
        return found["note"]
    out: list[str] = []
    scope = (found.get("intent") or {}).get("rfq_nos") or []
    if scope:
        out.append(
            f"（使用者的問句裡同時提到 {'、'.join(scope)} 和料號。這裡回的是**這顆料"
            f"在所有歷史成本分析表裡的單價**，不是只有那張單的。每一列都標了來源 "
            f"RFQ，回答時請先講那張單上的那一筆，再說其他單上出現過的價格；"
            f"要看整張單的逐項材料請改問「{scope[0]} 的材料明細」。）")
    for res in found.get("materials") or []:
        if not res.get("found"):
            continue
        out.append(f"■ 料號：{res['part_no']}（出現在 {res['n']} 份成本分析表）")
        for cur, v in (res.get("price") or {}).items():
            out.append(f"- 原幣單價區間（{cur}）："
                       f"最低 {v['min']}／中位 {v['median']}／最高 {v['max']}")
        for it in res["items"][:_CTX_TOP]:
            ver = material.file_version(it)
            out.append(
                f"- 來源 {_v(it.get('rfq_no'))}"
                f"（附件 {ver['name']}，{ver['text']}）："
                f"材料名稱 {_v(it.get('name'))}，"
                f"供應商 {_v(_sup(it))}，"
                f"原幣別 {_v(it.get('currency'))}，"
                f"原幣單價 {_v(it.get('unit_price_raw'))}，"
                f"匯率 {_v(it.get('fx_rate'))}，"
                f"單價(USD) {_v(it.get('unit_price'))}，"
                f"用量 {_v(it.get('qty'))}{it.get('unit') or ''}，"
                f"MOQ {_v(it.get('moq'))}，"
                f"耗損Loss {_v(it.get('loss_pct'))}%，"
                f"總價 {_v(it.get('line_total'))}，"
                f"材料交期 {_v(it.get('lead_time'))}，"
                f"備註 {_v(it.get('remark'))}，"
                f"驗算 {_CHECK_LABEL.get(it.get('check_status'), '未知')}")
        if len(res["items"]) > _CTX_TOP:
            out.append(f"- （另外還有 {len(res['items']) - _CTX_TOP} 次出現沒有"
                       f"列在這裡，完整清單在畫面表格上。**不要**編出沒列出來的"
                       f"那幾次的數字；要講的話就說「其餘 N 次見表格」。）")
        if res["items"]:
            out.append(f"- 規格說明：{_v(res['items'][0].get('spec'))}")
    return "\n".join(out) or "（查無符合的資料）"


def _product_context(found: dict[str, Any]) -> str:
    if found.get("note"):
        return found["note"]
    out: list[str] = []
    for res in found.get("products") or []:
        if not res.get("found"):
            continue
        c = res["cost"]
        scope = f"{res['n_total']} 張 {res['kind']} 單" if res.get("kind") \
            else f"{res['n_total']} 張單"
        out.append(
            f"■ 這是**產品料號** {res['part_no']}（表單的料號欄），"
            f"不是 RFQ 編號、也不是材料料號。已經照這一欄精確查過了，"
            f"命中方式：{res['matched_label']}。"
            f"歷史上共 {scope}"
            + ("（只算這個單型，其他單型不在這次的範圍內）。"
               if res.get("kind") else "。"))
        if res.get("time"):
            out.append(f"- 時間條件：{res['time']}。順序已經依建檔時間排好，"
                       f"下面第一筆就是答案，不要自己挑。")
        if res.get("truncated_by_time"):
            out.append(f"- 下面只列 1 張（使用者問的是單一一張）；"
                       f"這顆料總共 {res['n_total']} 張。")
        if res.get("no_doc_fallback"):
            out.append("- ★ 這顆料的**每一張單都沒有成本分析附件**，"
                       "所以挑的是時間上最新的那一張。回答時要講出來。")
        out.append(
            f"- 成本欄狀況（這三種要分開講，不可以合併成「沒有成本」）："
            f"有填數字的 {c['rows_with_cost']} 張、"
            f"**填 0** 的 {c['rows_all_zero']} 張（0 是有效值，表示確實記成零）、"
            f"**從來沒有人填** 的 {c['rows_unfilled']} 張。")
        if c["rfq_with_cost"]:
            out.append(f"- 成本欄有值的是：{'、'.join(c['rfq_with_cost'][:8])}")
        out.append(f"- 掛得出成本分析附件的：{res['n_total_with_doc']} 張")

        for r in res["rows"][:_CTX_TOP]:
            docs = (res.get("docs") or {}).get(r.get("rfq_no")) or []
            ver = ("、".join(f"{material.file_version(d)['name']}"
                             f"（{material.file_version(d)['text']}）"
                             for d in docs[:2]) if docs else "無附件")
            out.append(
                f"- {_v(r.get('rfq_no'))}（{_v(r.get('rfq_kind'))}，"
                f"建檔 {str(r.get('created_on') or '')[:10]}，"
                f"狀態 {_v(r.get('state'))}）："
                f"客戶 {_v(r.get('customer'))}，"
                f"規格 {(r.get('spec_desc') or '')[:80]}，"
                f"原幣別 {_v(r.get('currency'))}，"
                f"材料成本 {_v(r.get('mat_cost'))}／"
                f"人工 {_v(r.get('labor_cost'))}／"
                f"製造 {_v(r.get('mfg_cost'))}，"
                f"材料成本(USD) {_v(r.get('mat_cost_usd'))}，"
                f"成本分析附件：{ver}")
        if len(res["rows"]) > _CTX_TOP:
            out.append(f"- （還有 {len(res['rows']) - _CTX_TOP} 張沒有列出來，"
                       f"完整清單在畫面表格上；不要編出沒列出來的那幾張的內容。）")
    return "\n".join(out) or "（查無符合的資料）"


def _estimate_context(found: dict[str, Any]) -> str:
    if found.get("note"):
        return found["note"]
    est = found.get("estimate") or {}
    basis = est.get("basis") or {}
    acc = est.get("accuracy") or {}
    conf = est.get("confidence") or "none"

    out = [f"■ 這是「估價參考區間」，不是報價。要估的規格：{est.get('query')}",
           f"- 信心等級：{_CONF_LABEL.get(conf, conf)}"
           f"（實測這一級誤差內 2 倍 {acc.get('誤差內2倍', '?')}、"
           f"中位誤差 {acc.get('中位誤差', '?')}）",
           f"- 一定要照講的結論：{est.get('recommendation')}",
           f"- 依據：最相似的 {basis.get('n')} 筆歷史案例"
           f"（同客戶 {basis.get('n_same_customer')} 筆），"
           f"鄰居成本離散度 {_v(basis.get('spread'))} 倍",
           "- ★ 下面的數字全部照抄，**不要自己加總、換算、或推算任何新數字**"
           "（例如把材料＋人工加起來當總成本）——這份資料的欄位之間沒有那種關係。"]

    bands = est.get("estimate") or {}
    if bands:
        out.append(f"- 成本區間（美元，{basis.get('n')} 筆樣本；"
                   f"每一項都要標 USD。最低／最高在畫面表格上，不必覆述）：")
        for v in bands.values():
            out.append(f"  · {v['label']}：中位 {v['median']}"
                       f"（p25 {v['p25']} ~ p75 {v['p75']}）")
    else:
        out.append("- ★ 可用樣本少於 3 筆，**算不出任何區間**。"
                   "這一題請直接說估不出來，不要自己從佐證案例挑一個數字當答案。")

    if est.get("unavailable"):
        out.append("- 估不出來的欄位（覆蓋率太低，不要編）："
                   + "、".join(u["label"] for u in est["unavailable"]))

    comps = est.get("comparables") or []
    if comps:
        out.append(f"- 佐證案例（區間就是從這 {len(comps)} 筆算出來的，"
                   f"回答時至少點名兩筆編號讓人可以自己去查）：")
        for c in comps[:3]:
            out.append(f"  · {c.get('rfq_no')}（距離 {c.get('distance')}"
                       f"{'，同客戶' if c.get('same_customer') else ''}）"
                       f" {c.get('spec_desc') or ''}"
                       f"｜客戶 {_v(c.get('customer'))}"
                       f"｜材料成本(USD) {_v((c.get('costs') or {}).get('mat_cost_usd'))}")

    out.append(f"- 資料來源：以上數字全部來自 {_EST_SOURCE}")
    src = _source_context(found, title="佐證案例的附件原檔")
    if src:
        out.append(src)
    out.append("【這一題怎麼回答】四到六行就好，依序：①這是參考不是報價 ＋ "
               "信心等級；②主要成本項的中位與 p25~p75（標 USD）；"
               "③一定要照講的結論那一句；④點名兩筆佐證案例編號。"
               "不要逐項覆述所有區間與最低最高 —— 畫面上有完整表格。")
    return "\n".join(out)


def _source_context(found: dict[str, Any], title: str = "資料來源") -> str:
    src = found.get("sources")
    if not src:
        return ""
    lines = [f"（{title}。有原檔的請連檔名與更新時間一起講；"
             f"沒有原檔的要明講「這張單沒有成本分析附件」，不可以略過不提。）"]
    none_at_all: list[str] = []
    for no, s in src.items():
        if s.get("error"):
            lines.append(f"- {no}：附件查詢失敗（{s['error']}），"
                         f"無法確認有沒有原檔 —— 這不等於沒有附件")
        elif s.get("docs"):
            vs = [material.file_version(d) for d in s["docs"][:3]]
            more = f"（另有 {len(s['docs']) - 3} 份）" if len(s["docs"]) > 3 else ""
            lines.append(f"- {no}：" + "、".join(
                f"{v['name']}（{v['text']}）" for v in vs) + more)
        else:
            none_at_all.append(no)
    if len(none_at_all) == 1:
        lines.append(f"- {none_at_all[0]}：沒有成本分析附件原檔，"
                     f"這一筆的內容全部來自 PLM 表單欄位")
    elif none_at_all:
        lines.append(f"- 這 {len(none_at_all)} 筆都**沒有**成本分析附件原檔"
                     f"（內容全部來自 PLM 表單欄位）："
                     + "、".join(none_at_all[:6])
                     + ("…" if len(none_at_all) > 6 else ""))
    return "\n".join(lines)


def _cost_doc_summary_context(found: dict[str, Any]) -> str:
    src = found.get("sources") or {}
    lines: list[str] = []
    for r in found.get("results") or []:
        no = (r.get("rfq_no") or "").strip()
        s = src.get(no) or {}
        mats = s.get("materials")
        if not mats:
            continue
        doc = (s.get("docs") or [{}])[0]
        bad = [m for m in mats if m.get("check_status") in ("unverified", "rescaled")]
        top = sorted((m for m in mats if m.get("line_total") is not None),
                     key=lambda m: m["line_total"], reverse=True)[:3]
        lines.append(f"■ {no}：")
        lines.append(f"  - 材料項數 {len(mats)}｜彙總對帳"
                     f"{'通過' if doc.get('totals_verified') else '未通過'}")
        lines.append(f"  - 彙總：A 材料 {_v(doc.get('mat_total'))}／"
                     f"B 人工 {_v(doc.get('labor_total'))}／"
                     f"合計 {_v(doc.get('grand_total'))}")
        if top:
            lines.append("  - 金額最大的幾項：" + "、".join(
                f"{_v(m.get('part_no'))}（{_sup(m) or '供應商未填'}）"
                f"總價 {_v(m.get('line_total'))}" for m in top))
        if bad:
            lines.append(f"  - ★ 有 {len(bad)} 項驗算沒通過，這幾項不可以當報價。")
        labor = s.get("labor") or []
        if labor:
            lines += [f"  {ln}" for ln in _labor_context_lines(doc, labor)]
    if not lines:
        return ""
    return ("（下面是每一筆結果的成本分析附件摘要。完整逐項材料與人工成本明細"
            "在畫面卡片上，不要逐列覆述，用這裡的彙總數字講結論就好。）\n"
            + "\n".join(lines))


def _labor_context_lines(doc: dict[str, Any], rows: list[dict]) -> list[str]:
    out = ["- 人工成本（附件 PDF 的 B 段，共 %d 列）。這**不是** 表單語料表 "
           "表單上的人工成本欄，兩者不可以互相代換。"
           "完整的逐列表格在畫面卡片上，**不要逐列覆述**，"
           "用下面這幾個數字講結論就好。" % len(rows)]
    out.append(f"- B Total（表上印的）{_v(doc.get('labor_total'))}，"
               f"各列費用之和 {_v(doc.get('labor_line_sum'))}，"
               f"對帳{'通過' if doc.get('labor_verified') else '未通過'}")

    out.append("- 四列各自的費用："
               + "｜".join(f"{r.get('category')} "
                           + (_v(r.get("cost")) if r.get("cost") is not None
                              else "空白（沒填）")
                           for r in rows))
    priced = [r for r in rows if r.get("cost") is not None]
    top = max(priced, key=lambda r: r["cost"], default=None)
    if top is not None:
        out.append(f"- 費用最大的一列：{top.get('category')} "
                   f"{_v(top.get('cost'))}")
    asm = next((r for r in rows if r.get("total_sec") is not None), None)
    if asm:
        out.append(f"- 組裝那一列的工時參數與算式："
                   f"預估工站 {_v(asm.get('stations'))}｜"
                   f"組立總工時 {_v(asm.get('total_sec'))} 秒｜"
                   f"PPH {_v(asm.get('pph'))}｜"
                   f"工時成本 {_v(asm.get('hr_rate'))} "
                   f"{asm.get('rate_currency') or ''}/Hr｜"
                   f"人工效率 {_v(asm.get('efficiency_pct'))}%"
                   f" -> {_labor_formula(asm)}")
    blank = [r.get("category") for r in rows if r.get("cost") is None]
    if blank:
        out.append(f"- 費用**空白**（表上那一格沒填，代表那道工序不做，不是 0）："
                   + "、".join(c or "" for c in blank))
    lt, gt = doc.get("labor_total"), doc.get("grand_total")
    if lt is not None and gt:
        pct = (Decimal(lt) / Decimal(gt) * 100).quantize(Decimal("0.1"))
        out.append(f"- 人工成本佔合計的比例：{_v(lt)} ÷ {_v(gt)} = {pct}%"
                   f"（分母是「合計TOTAL（含攤提）」，含製造成本與管銷）")
    bad = [r for r in rows if r.get("check_status") == "mismatch"]
    if bad:
        out.append("- ★ 有 " + "、".join(r.get("category") or "" for r in bad)
                   + " 這幾列的費用與工時算式對不上（費用 = 工時成本 × "
                     "組立總工時 ÷ 3600 ÷ 人工效率），這幾列**不可以當報價**，"
                     "回答時要講出來。")
    return out


def _header_context(found: dict[str, Any]) -> str:
    hit = found.get("header_docs") or {}
    docs = hit.get("docs") or []
    sim = docs[0].get("sim") if docs else None
    how = hit.get("matched_label") or ""
    if hit.get("matched_by") == "similar" and sim is not None:
        how += f"，相似度 {float(sim):.2f}"
    out = [
        f"■ 這一題是用**成本分析附件的表頭**比對到的，不是用 RFQ 編號、"
        f"也不是用 PLM 表單的規格描述。",
        f"- 比對方式：{how}。命中 {len(docs)} 份附件。",
        f"- 使用者給的字串：{hit.get('query')}",
    ]
    if hit.get("matched_by") == "similar":
        out.append(
            "- ★ 字面上會有出入是**正常的**：這批附件有三成沒有文字層，表頭是"
            "OCR 讀出來的，會把空格吃掉、把 LI 認成 U。系統比對的是去掉空白與"
            "標點之後的字串，相似度已經過門檻，**這就是使用者問的那一份**。"
            "不要跟使用者說「規格描述不一致」或建議他重新查詢。")
    if hit.get("ambiguous"):
        out.append("- ★ 相似度第一名沒有明顯領先第二名，同系列的品名只差一個"
                   "線長。請把命中的幾份都列出來讓使用者確認是哪一份，"
                   "不要替他挑一份。")

    by_doc: dict[Any, list[dict]] = {}
    for m in hit.get("materials") or []:
        by_doc.setdefault(m.get("doc_id"), []).append(m)
    for d in docs:
        v = material.file_version(d)
        out.append("")
        out.append(f"■ {d.get('rfq_no') or '（附件沒有對到 RFQ 編號）'}"
                   f" 的附件 {v['name']}（{v['text']}）")
        out += [f"  - {line}" for line in material.header_lines(d)]
        mats = by_doc.get(d["id"]) or []
        out.append(f"  - 材料明細 {len(mats)} 項；A Total {_v(d.get('mat_total'))}"
                   f"／B Total {_v(d.get('labor_total'))}"
                   f"／合計 {_v(d.get('grand_total'))}"
                   f"｜幣別 {d.get('currency')}｜抽取方式 {d.get('source')}"
                   f"｜彙總對帳{'通過' if d.get('totals_verified') else '未通過'}")
        if not d.get("totals_verified"):
            out.append("  - ★ 這份的「明細總價之和 vs A Total」對不上"
                       f"（明細和 {_v(d.get('material_line_sum'))}），"
                       "彙總數字已經作廢不可當報價，回答時要講出來。")
    out.append("")
    out.append("【這一題怎麼回答】三到六行。①先講這是哪一份附件"
               "（RFQ 編號 + 檔名 + 更新時間）與怎麼比對到的；"
               "②使用者問哪一格就照抄那一格的原文；③材料項數與 A/B/合計；"
               "④對帳沒過就明講不可當報價。完整表格在下方卡片，不要逐列覆述。")
    return "\n".join(out)


def _rfq_detail_context(found: dict[str, Any]) -> str:
    if found.get("note"):
        return found["note"]
    want_labor = bool((found.get("intent") or {}).get("want_labor"))
    want_header = bool((found.get("intent") or {}).get("want_header"))
    out: list[str] = []
    for det in found.get("rfq_details") or []:
        if not det.get("found"):
            continue
        by_doc: dict[Any, list[dict]] = {}
        for m in det["materials"]:
            by_doc.setdefault(m.get("doc_id"), []).append(m)
        labor_by_doc: dict[Any, list[dict]] = {}
        for r in det.get("labor") or []:
            labor_by_doc.setdefault(r.get("doc_id"), []).append(r)
        if len(det["docs"]) > 1:
            out.append(
                f"■ {det['rfq_no']} 有 {len(det['docs'])} 份成本分析附件，"
                f"下面每一份分開列出（最新的排最前面）。回答時**一定要用檔案"
                f"更新時間指明是哪一份**，不要把不同份的數字混在一起。")
        for doc in det["docs"]:
            mats = by_doc.get(doc["id"]) or []
            bad = [m for m in mats
                   if m.get("check_status") in ("unverified", "rescaled")]
            low_conf_sup = [m for m in mats if m.get("supplier_uncertain")]
            suppliers = {m.get("supplier") for m in mats if m.get("supplier")}
            labor = labor_by_doc.get(doc["id"]) or []
            ver = material.file_version(doc)
            out.append(f"■ {det['rfq_no']} 的成本分析附件：{ver['name']}"
                       f"（檔案{ver['text']}）")
            hdr_lines = [f"- {ln}" for ln in material.header_lines(doc)]
            if want_header:
                out += hdr_lines
            if want_labor and labor:
                out += _labor_context_lines(doc, labor)
            elif want_labor:
                out.append("- ★ 這份附件**沒有抽到人工成本（B 段）**。"
                           "多半是它的文字層讀不到那一段（掃描件／字型壞掉）。"
                           "不要用表單的人工成本欄代替回答，照實說這份附件"
                           "的 B 段抽不出來。")
            out.append(f"- 材料項數：{len(mats)}｜供應商家數：{len(suppliers)}"
                       f"｜幣別：{_v(doc.get('currency'))}")
            out.append(f"- 抽取方式：{_v(doc.get('source'))}｜彙總對帳："
                       f"{'通過' if doc.get('totals_verified') else '未通過'}")
            out.append(f"- 彙總：A 材料 {_v(doc.get('mat_total'))}／"
                       f"B 人工 {_v(doc.get('labor_total'))}／"
                       f"C 模治具 {_v(doc.get('tooling_total'))}／"
                       f"合計 {_v(doc.get('grand_total'))}")
            out.append(f"- 明細總價之和：{_v(doc.get('material_line_sum'))}")
            if not want_header:
                out += hdr_lines
            if not want_labor and labor:
                out += _labor_context_lines(doc, labor)

            top = sorted((m for m in mats if m.get("line_total") is not None),
                         key=lambda m: m["line_total"], reverse=True)[:_CTX_TOP]
            if top:
                out.append(f"- 金額最大的 {len(top)} 項（僅供結論引用，"
                           f"完整清單在畫面表格上）：")
                for m in top:
                    out.append(f"  · {_v(m.get('part_no'))} "
                               f"{m.get('name') or ''}"
                               f"（{_sup(m) or '供應商未填'}）"
                               f" 總價 {_v(m.get('line_total'))}")
            if bad:
                out.append(f"- ★ 有 {len(bad)} 項驗算沒通過（"
                           + "、".join(_v(m.get("part_no")) for m in bad[:8])
                           + "），這幾項的數字不可以當報價，回答時要講出來。")
            else:
                out.append("- 全部項目都通過逐列驗算。")
            if low_conf_sup:
                out.append(
                    f"- ★ 有 {len(low_conf_sup)} 項材料的「供應商」欄位（"
                    + "、".join(_v(m.get("part_no")) for m in low_conf_sup[:8])
                    + "）是從掃描件用 OCR 辨識的，這幾個字對辨識模型來說難度"
                      "過高、信心不足（畫面上已標示 ⚠），回答時務必提醒使用者"
                      "這幾項的供應商名稱可能有誤，請以來源 PDF 原文為準，"
                      "不要把它當成確定無誤的事實直接斷言。")
    return "\n".join(out) or "（查無符合的資料）"


_COMPLETE_ROUTES = (intent.KIND_PRODUCT, intent.KIND_MATERIAL,
                    intent.KIND_RFQ, intent.KIND_ESTIMATE)

_CTX_MEMO: dict[str, tuple[str, str]] = {}
_CTX_MEMO_MAX = 32


def context_for(query: str) -> str | None:
    hit = _CTX_MEMO.get(query or "")
    return hit[0] if hit else None


def data_context_for(query: str) -> str | None:
    hit = _CTX_MEMO.get(query or "")
    return hit[1] if hit else None


def _context(query: str, found: dict[str, Any]) -> str:
    it = found.get("intent") or {}
    if found.get("capabilities"):
        return _capability_text(found["capabilities"])

    if found.get("ambiguous"):
        return ("（系統的分流機制沒有在這句話裡找到料號、RFQ 編號、成本或"
                "時間條件，字面與同義詞比對也沒有命中任何關鍵字，所以底下"
                "沒有檢索結果可以看。不要臆測任何具體數字、編號或案例。"
                "請照實告訴使用者系統看不懂這句話，並從下面「系統支援的問法」"
                "裡挑幾個跟他原意接近的例子建議他改問：\n\n"
                + guess.route_catalog() + "）")

    kind = it.get("kind") or intent.KIND_SEARCH
    if found.get("header_docs"):
        context = _header_context(found)
    elif kind == intent.KIND_ESTIMATE:
        context = _estimate_context(found)
    elif kind == intent.KIND_MATERIAL:
        context = _material_context(found)
    elif kind == intent.KIND_PRODUCT:
        context = _product_context(found)
        if found.get("rfq_details"):
            context += "\n\n" + _rfq_detail_context(found)
    elif found.get("rfq_details") is not None:
        context = _rfq_detail_context(found)
    elif found.get("note") and not found.get("results"):
        context = found["note"]
    else:
        asked = tuple(col for col, _op, _v in
                      (found.get("parsed") or {}).get("numeric") or [])
        context = retrieval.as_context(
            found.get("results") or [],
            with_cost=bool(it.get("want_form")) or bool(asked),
            cost_cols=None if it.get("want_form") else asked)
        src = _source_context(found)
        if src:
            context += "\n\n" + src
        mat_summary = _cost_doc_summary_context(found)
        if mat_summary:
            context += "\n\n" + mat_summary
        if found.get("hint"):
            context += "\n\n" + found["hint"]

    if found.get("time_sorted"):
        context += ("\n\n（這一題有時間條件，結果**已經依建檔時間排好**，"
                    "第一筆就是使用者要的那一筆。）")

    head = []
    if kind in _COMPLETE_ROUTES and found.get("intent") is not None:
        head.append("【作答須知】以下資料是系統照使用者的識別碼**精確查出來的**，"
                    "這一題需要的東西已經齊了。\n"
                    "- 不要再呼叫任何工具，直接用下面的內容作答"
                    "（再查一次拿到的是同一份資料，只會多花好幾秒）。\n"
                    "- 簡短作答：條列，通常三到八行。畫面上已經有完整表格，"
                    "不要把每一列再念一遍。")
    tips = lessons.prompt_for(kind)
    if tips:
        head.append(tips)
    data = context
    if head:
        context = "\n\n".join(head) + "\n\n" + context

    if len(_CTX_MEMO) >= _CTX_MEMO_MAX:
        _CTX_MEMO.clear()
    _CTX_MEMO[query or ""] = (context, data)
    return context


def has_data(found: dict[str, Any]) -> bool:
    if found.get("results"):
        return True
    if found.get("estimate"):
        return True
    if (found.get("header_docs") or {}).get("found"):
        return True
    for key in ("materials", "products"):
        if any(x.get("found") for x in (found.get(key) or [])):
            return True
    if any(d.get("found") for d in (found.get("rfq_details") or [])):
        return True
    return False


def _row_count(found: dict[str, Any]) -> int:
    n = len(found.get("results") or [])
    for res in found.get("products") or []:
        n += len(res.get("rows") or [])
    for res in found.get("materials") or []:
        n += len(res.get("items") or [])
    for det in found.get("rfq_details") or []:
        n += len(det.get("materials") or [])
    n += len((found.get("header_docs") or {}).get("materials") or [])
    return n


def _extra_allowed(found: dict[str, Any]) -> tuple[str, ...]:
    n_rows = _row_count(found)
    nums = {str(n_rows), str(len(found.get("results") or []))}
    nums |= {str(i) for i in range(0, min(n_rows, 200) + 1)}
    for res in found.get("products") or []:
        nums |= {str(res.get("n_total") or 0), str(res.get("n") or 0),
                 str(res.get("n_total_with_doc") or 0)}
        nums |= {str(v) for v in (res.get("cost") or {}).values()
                 if isinstance(v, int)}
        kinds: dict[str, int] = {}
        for r in res.get("rows") or []:
            kinds[r.get("rfq_kind") or ""] = kinds.get(r.get("rfq_kind") or "", 0) + 1
        nums |= {str(v) for v in kinds.values()}
    for res in found.get("materials") or []:
        nums |= {str(res.get("n") or 0)}
    for det in found.get("rfq_details") or []:
        nums |= {str(len(det.get("materials") or [])),
                 str(len(det.get("docs") or []))}
    hdr = found.get("header_docs") or {}
    if hdr.get("found"):
        nums |= {str(hdr.get("n") or 0), str(len(hdr.get("docs") or []))}
        for d in hdr.get("docs") or []:
            if d.get("sim") is not None:
                nums.add(f"{float(d['sim']):.2f}")
    return tuple(sorted(nums))


def _answer(query: str, found: dict[str, Any], history: list[dict]) -> Any:
    if found.get("capabilities"):
        return iter([_capability_text(found["capabilities"])])

    if found.get("ambiguous"):
        return guess.suggest(query)

    context = _context(query, found)
    if history:
        prev = "\n".join(
            f"{'使用者' if h['role'] == 'user' else '助理'}：{h['content']}"
            for h in history[-4:] if (h.get("content") or "").strip()
        )
        query = f"（先前對話）\n{prev}\n\n（本次問題）{query}"
    return answer.verified(
        query, context,
        route=(found.get("intent") or {}).get("kind") or intent.KIND_SEARCH,
        has_data=has_data(found), n_rows=_row_count(found),
        extra_allowed=_extra_allowed(found),
        display_context=data_context_for(query),
        system_extra="")


def _sync_summary() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    if not rows:
        return {"stale": None, "note": "公開版不含排程"}
    stages: dict[str, Any] = {}
    worst_hours: float | None = None
    for r in rows:
        hrs = float(r["hours_since_success"]) if r["hours_since_success"] is not None else None
        stages[r["stage"]] = {
            "watermark": r["watermark"],
            "last_success": r["last_success"],
            "hours_since_success": round(hrs, 1) if hrs is not None else None,
            "ok": r["ok"],
            "note": r["note"],
        }
        if hrs is not None and (worst_hours is None or hrs > worst_hours):
            worst_hours = hrs
    return {
        "stages": stages,
        "hours_since_last_success": round(worst_hours, 1) if worst_hours is not None else None,
        "stale": None if worst_hours is None else worst_hours > SYNC_STALE_HOURS,
        "stale_threshold_hours": SYNC_STALE_HOURS,
    }


SCHEMA_CORE = ("表單語料表",)
SCHEMA_OPTIONAL = {
    "附件表": "成本分析附件（料號查價、材料明細）",
    "附件材料明細表": "附件逐項材料",
    "附件人工明細表": "附件人工成本",
    "排程狀態表": "排程紀錄",
    "附件下載佇列": "附件下載紀錄",
}


def _schema_check(c: Any) -> dict[str, Any]:
    names = [*SCHEMA_CORE, *SCHEMA_OPTIONAL]
    rows = c.execute(
        "[SQL 已於公開版移除]",
        (names,)).fetchall()
    missing = [t for t, ok in rows if not ok]
    return {"ok": not missing, "missing": missing,
            "missing_core": [t for t in missing if t in SCHEMA_CORE]}


_status_lock = threading.Lock()
_status_inflight: Future | None = None


def _status() -> tuple[str, dict[str, Any]]:
    global _status_inflight
    with _status_lock:
        fut, owner = _status_inflight, False
        if fut is None:
            fut = _status_inflight = Future()
            owner = True
    if not owner:
        return fut.result()
    try:
        res = _status_impl()
        fut.set_result(res)
        return res
    except BaseException as e:
        fut.set_exception(e)
        raise
    finally:
        with _status_lock:
            _status_inflight = None


def _status_impl() -> tuple[str, dict[str, Any]]:
    try:
        with pool().connection() as c:
            schema = _schema_check(c)
            if schema["missing_core"]:
                return "not_ready", {"db": True, "schema": schema,
                                     "error": f"缺少核心資料表：{'、'.join(schema['missing_core'])}"}
            total, vec = c.execute(
                "[SQL 已於公開版移除]"
            ).fetchone()
        detail: dict[str, Any] = {"rows": total, "embedded": vec, "schema": schema}
    except Exception as e:
        return "not_ready", {"db": False, "error": f"{type(e).__name__}: {e}"}
    try:
        detail["material_index"] = material.coverage()
    except Exception as e:
        detail["material_index"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        detail["sync"] = _sync_summary()
    except Exception as e:
        detail["sync"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        detail["product_index"] = product.coverage()
    except Exception as e:
        detail["product_index"] = {"error": f"{type(e).__name__}: {e}"}
    detail["lessons"] = {"seeded": len(lessons.SEED),
                         "learned": lessons.report()}

    try:
        from . import embed as _embed
        detail["embedder"] = _embed.backend_status()
    except Exception as e:
        detail["embedder"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if detail["embedder"].get("ok") is False:
        return "not_ready", {**detail,
                             "note": "編碼後端不可用，語意檢索與估價會失效："
                                     f"{detail['embedder'].get('error')}"}

    if schema["missing"]:
        lost = "、".join(SCHEMA_OPTIONAL[t] for t in schema["missing"])
        return "degraded", {**detail,
                            "note": f"缺少資料表 {'、'.join(schema['missing'])}，"
                                    f"這些功能不能用：{lost}（資料表要手動補建）"}
    if not os.environ.get("LLM_API_KEY"):
        return "degraded", {**detail, "llm": "LLM_API_KEY 未設定"}
    if vec < total:
        return "degraded", {**detail, "note": f"向量進度 {vec}/{total}，語意通道部分可用"}
    if detail["sync"].get("stale"):
        h = detail["sync"]["hours_since_last_success"]
        return "degraded", {**detail,
                             "note": f"排程已經 {h} 小時沒有成功執行過，"
                                     f"超過門檻 {SYNC_STALE_HOURS} 小時，見 detail.sync"}
    return "ready", detail


def _wrap(t: dict[str, Any]) -> ToolSpec:
    name = t["name"]
    return ToolSpec(
        name=name,
        description=t["description"],
        input_schema=t["input_schema"],
        handler=lambda args, _n=name: _dispatch(_n, args),
    )


def _dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        with admission.slot():
            raw = agent_tools.dispatch(name, args)
    except admission.Busy as e:
        return {"ok": False, "error": e.user_message, "busy": True}
    out = _deep(raw)
    if isinstance(out, dict) and "error" in out:
        return {"ok": False, "error": out["error"], **{k: v for k, v in out.items() if k != "error"}}
    return {"ok": True, "result": out}


TOOLS = [_wrap(t) for t in agent_tools.TOOLS]


EXAMPLES = intent.examples()


SPEC = AgentSpec(
    agent_id=AGENT_ID,
    tools=TOOLS,
    system=agent_tools.SYSTEM_PROMPT,
    examples=EXAMPLES,
    search_fn=_search_entry,
    hits_fn=_hits_entry,
    answer_stream=_answer,
    context_fn=_context,
    status_fn=_status,
    can_delegate=True,
)


def _mount_files(app: Any) -> None:
    from urllib.parse import quote

    from fastapi import HTTPException
    from fastapi.responses import FileResponse, Response

    from . import docroot

    def _inline_disposition(filename: str) -> str:
        quoted = quote(filename)
        if quoted != filename:
            return f"inline; filename*=utf-8''{quoted}"
        return f'inline; filename="{filename}"'

    @app.get("/a2a/file/{doc_id}", tags=["a2a"])
    def cost_file(doc_id: int):
        doc = material.doc_path(doc_id)
        if doc is None:
            raise HTTPException(404, f"查無成本分析附件 #{doc_id}")
        if doc.get("pdf_data") is not None:
            return Response(
                bytes(doc["pdf_data"]), media_type="application/pdf",
                headers={"Content-Disposition":
                         _inline_disposition(doc["file_name"])})
        path = docroot.resolve(doc["path"])
        if not path.is_file():
            v = material.file_version(doc)
            raise HTTPException(
                410, f"檔案已不在原位置：{doc['path']}（找過 {path}）"
                     f"（這份是 {doc.get('rfq_no')} 的 {v['name']}，{v['text']}；"
                     f"明細與版本資訊都還在庫裡，只有原檔不在這台機器上。"
                     f"檔案有搬家的話請改 .env 的 DOC_ROOT，不必動資料庫）")
        return FileResponse(path, media_type="application/pdf",
                            filename=doc["file_name"],
                            content_disposition_type="inline")


def _route_of(found: dict[str, Any]) -> str:
    return (found.get("intent") or {}).get("kind") or intent.KIND_SEARCH


def mount(app: Any) -> bool:
    try:
        router = build_router(SPEC)
        if not guard.mount(app, router, _context, _route_of, prepare=prefetch):
            print("[a2a] ★ 事實核對層沒有掛到 /a2a/message："
                  "「用自己的工具」那條路的回答不會被核對（見 src/guard.py）；"
                  "檢索也會直接在事件迴圈上跑")
        app.include_router(router)
        _mount_files(app)
        health.mount(app, SPEC)
        return True
    except Exception as e:
        print(f"[a2a] 掛載失敗，RFQ-RAG 仍照舊運作：{type(e).__name__}: {e}")
        return False
