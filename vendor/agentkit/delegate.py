"""委派迴圈：模型呼叫本地工具或下游 agent，含重複呼叫收回與步數預算。"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from typing import Any

from . import client as peer_client
from . import gateway, registry
from .envelope import EV_DELTA, EV_ERROR, EV_RESULTS, EV_TRACE

RESULT_CHARS = 6000
PREVIEW_CHARS = 400
MAX_CONSECUTIVE_FAILS = int(os.environ.get("A2A_MAX_CONSECUTIVE_FAILS", "3"))

MAX_STEPS_DELEGATE = int(os.environ.get("A2A_MAX_STEPS_DELEGATE", "0"))


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"…（截斷，原長 {len(s)} 字）"


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


_TRIMMABLE = ("results", "nearby", "roles", "comparables", "values", "hits", "breakdown")


def _fit(v: Any, budget: int) -> str:
    s = _dumps(v)
    if len(s) <= budget:
        return s
    if isinstance(v, dict):
        for key in _TRIMMABLE:
            items = v.get(key)
            if isinstance(items, list) and len(items) > 1:
                keep = max(1, len(items) // 2)
                return _fit(
                    {**v, key: items[:keep],
                     f"{key}_truncated": f"原有 {len(items)} 筆，只給前 {keep} 筆"},
                    budget,
                )
    return _clip(s, budget)


PREFETCH_CHARS = int(os.environ.get("A2A_PREFETCH_CHARS", "3000"))


def _topic_hit(query: str, tags: list[str]) -> list[str]:
    q = query or ""
    ql = q.lower()
    out: list[str] = []
    for raw in tags:
        t = (raw or "").strip()
        if len(t) < 2:
            continue
        if t.isascii():
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(t.lower())}(?![A-Za-z0-9])", ql):
                out.append(t)
        elif t in q:
            out.append(t)
    return out


def _flatten_hit(h: dict[str, Any]) -> str:
    parts = [f"【{h.get('id') or ''}】{h.get('title') or ''}".strip()]
    if sub := h.get("subtitle"):
        parts.append(str(sub))
    if body := h.get("body"):
        parts.append(str(body))
    for b in h.get("blocks") or []:
        kind = b.get("kind")
        title = b.get("title") or ""
        if kind == "kv":
            rows = "；".join(
                f"{k}={'未填' if v is None else v}" for k, v in (b.get("rows") or []))
            if rows:
                parts.append(f"{title}：{rows}")
        elif kind == "text":
            if txt := (b.get("text") or "").strip():
                parts.append(f"{title}：{txt}")
        elif kind == "table":
            head = "/".join(str(x) for x in (b.get("head") or []))
            rows = " | ".join(
                "/".join(str(x) for x in r) for r in (b.get("rows") or [])[:12])
            if rows:
                parts.append(f"{title}（{head}）：{rows}")
    return "\n".join(p for p in parts if p)


def _prefetch_peers(
    query: str, peer_ids: list[str]
) -> tuple[list[dict[str, Any]], str, list[tuple[str, str, list[dict[str, Any]]]]]:
    traces: list[dict[str, Any]] = []
    chunks: list[str] = []
    cards: list[tuple[str, str, list[dict[str, Any]]]] = []
    matched: list[tuple[str, dict[str, Any], list[str]]] = []
    for aid in peer_ids:
        c = peer_client.card(aid)
        if c.get("status") == "offline" or not (c.get("capabilities") or {}).get("search"):
            continue
        if tags := _topic_hit(query, c.get("tags") or []):
            matched.append((aid, c, tags))
    if not matched:
        return traces, "", cards

    budget = max(600, PREFETCH_CHARS // len(matched))
    for aid, c, tags in matched:
        name = c.get("name", aid)
        t0 = time.perf_counter()
        try:
            out = peer_client.search(aid, query, top_k=3)
        except Exception as e:      # noqa: BLE001 -- 事前動作失敗不該讓整題掛掉
            traces.append({"phase": "prefetch", "agent": aid, "agent_name": name,
                           "tags": tags, "ok": False, "error": f"{type(e).__name__}: {e}"})
            continue
        ms = round((time.perf_counter() - t0) * 1000)
        hits = out.get("hits") or []
        traces.append({
            "phase": "prefetch", "agent": aid, "agent_name": name, "tags": tags,
            "ok": not out.get("offline"), "hits": len(hits), "ms": ms,
            "reason": f"問句命中「{'、'.join(tags)}」，先取 {name} 的資料",
        })
        head = (f"\n\n【我先替你向「{name}」查過了】"
                f"（這一題提到「{'、'.join(tags)}」，那是它的主題，我自己連這種欄位都沒有）")
        if hits:
            for h in hits:
                if isinstance(h, dict):
                    h.setdefault("badges", []).insert(0, ["來源", name])
                    h.setdefault("why", []).insert(
                        0, f"由「{name}」提供（問句提到「{'、'.join(tags)}」，系統自動查詢）")
            cards.append((aid, name, hits))
            body = "\n\n".join(_flatten_hit(h) for h in hits)
            chunks.append(f"{head}\n{_clip(body, budget)}\n"
                          f"★ 回答時必須用上面這份資料。**不可以**說「我的檢索結果裡沒有"
                          f"{'／'.join(tags)}的欄位所以無法確認」—— 已經問過了，資料就在上面。"
                          f"需要更完整的細節再呼叫 `{aid}{peer_client.NS}*` 的工具。")
        else:
            chunks.append(f"{head}\n它也查不到（回 0 筆）。"
                          f"★ 所以要說的是「已經問過 {name}，它的語料裡也沒有」，"
                          f"**不可以**說「我沒有這個欄位所以不知道」。")
    return traces, "".join(chunks), cards


def _peer_notes(peer_ids: list[str]) -> str:
    lines: list[str] = []
    for aid in peer_ids:
        c = peer_client.card(aid)
        st = c.get("status", "unknown")
        head = (f"- {c.get('name', aid)}（取資料用 `{aid}{peer_client.NS}*`，"
                f"問一句話用 `{peer_client.ASK}{aid}`，狀態 {st}）：{c.get('role', '')}")
        lines.append(head)
        if tags := [t for t in (c.get("tags") or []) if t]:
            lines.append(f"  它的主題關鍵詞：{'、'.join(tags)}")
        if st == "offline":
            lines.append(f"  ★ 這個 agent 現在沒有啟動。它的工具不在你的工具清單裡，"
                         f"需要它的問題要直說「{c.get('name', aid)} 未啟動，這部分無法回答」。")
            continue
        if c.get("blocked_by"):
            lines.append(f"  ★ 尚未就緒：{'；'.join(c['blocked_by'])}。"
                         f"它回 not_ready 時不要自己編答案。")
        sysmsg = (c.get("system") or "").strip()
        if sysmsg:
            lines.append("  它自己的限制（必須遵守）：")
            lines += [f"    {ln}" for ln in _clip(sysmsg, 900).splitlines() if ln.strip()]
    return "\n".join(lines)


def build_system(spec: Any, peer_ids: list[str]) -> str:
    own = (spec.system or "").strip()
    notes = _peer_notes(peer_ids)
    ref = registry.get(spec.agent_id)
    parts = [own] if own else [f"你是「{ref.name}」：{ref.role}。"]
    if notes:
        parts.append(
            "你可以呼叫下面這些下游 agent 的工具來補足自己沒有的資料。\n"
            f"{notes}\n\n"
            "呼叫規則：\n"
            "- ★ 先看自己手上的資料。上面「我自己的檢索結果」或自己的工具就足以回答時，"
            "**直接回答，不要呼叫任何下游** —— 委派不是每題都要做的儀式，"
            "多問一輪要多花好幾秒與好幾倍 token，答案還可能被無關的資料帶偏。\n"
            "- 只有在缺的那塊資料自己真的沒有時才委派，而且只問缺的那一塊，"
            "不要把整題轉包出去。\n"
            "- ★★ 但「自己手上有資料」指的是**能回答這一題的資料**，"
            "不是「碰巧也有這個料號／編號」。上面每個下游都列了它的主題關鍵詞，"
            "那些主題是你**連欄位都沒有**的領域 —— 問題落在那些主題上時，"
            "不論你自己查到多少東西，都要呼叫對應的下游。\n"
            "  自己查到了那個料號、但要問的是別人的主題 -> 照樣要問，"
            "然後把兩邊的結果合起來回答。\n"
            "  ★ 這種情況**絕對不要**寫成「我的檢索結果裡沒有這個欄位，所以無法確認」"
            "—— 那不是誠實，是漏問。真的問過而對方也沒有，才可以那樣說。\n"
            "- 需要別人的資料就直接呼叫工具，不要猜。\n"
            f"- 知道要哪一筆資料 -> 用 `<agent>{peer_client.NS}<工具>`；"
            f"問題屬於對方專業但不確定用哪支工具 -> 用 `{peer_client.ASK}<agent>` "
            "把整句問題交給它（它看不到你的對話，問題要自帶料號與規格）。\n"
            "- 工具回 {\"ok\": false} 或 not_ready 時，如實說明是哪一個 agent、"
            "為什麼拿不到，**絕對不要用猜的料號或數字補位**。\n"
            "- 引用下游的結果時要說是哪個 agent 提供的，方便使用者自己去查。"
        )
    else:
        parts.append(
            "你現在是**獨立模式**：只有自己的工具，沒有任何其他 agent 可以呼叫。\n"
            "- 需要計算或查表就用自己的工具，不要憑印象給數字。\n"
            "- 缺的那塊資料要別的 agent 才有時，直說「這部分需要 ○○ Agent，"
            "目前沒有開放委派」，不要自己補一個。"
        )
    parts.append(
        "工具呼叫的紀律（違反的話使用者會完全看不到答案）：\n"
        "- 只能呼叫工具清單裡列出的名字，不要自己發明工具（例如 `summarize`——"
        "沒有這種工具，摘要是你自己讀資料後直接用文字回答的事，不是呼叫工具）。\n"
        "- ★ 拿到足夠回答的資料就**立刻用文字作答**，不要再呼叫任何工具。"
        "工具呼叫的次數有上限，用完了你就沒有機會講話，使用者會拿到一片空白。\n"
        "- ★ 同一支工具不要反覆改寫查詢重試。第一次查到了就用第一次的結果；"
        "查不到就直接說「查不到」並說明是哪裡沒有資料，換個寫法再查通常不會有新東西。\n"
        "- 工具回的資料不完整時，就用已經有的部分作答並講明哪一塊缺，"
        "不要為了補齊而一直查下去。\n"
        "- ★ 答案要寫成**給人看的段落文字**，不可以包成 JSON、不要輸出 "
        "{\"summary\": …} 這種信封，也不要用 \\u 逃逸中文。直接寫字就好。"
    )
    parts.append("回答用繁體中文，直接講結論，不要客套開場白。")
    return "\n\n".join(parts)


def _resolve_bare(spec: Any, tool: str, allowed: list[str]) -> str | None:
    hits = [aid for aid in allowed
            if any(t.get("name") == tool and t.get("ready", True)
                   for t in (peer_client.card(aid).get("tools") or []))]
    return hits[0] if len(hits) == 1 else None


def _dispatch(spec: Any, name: str, args: dict[str, Any],
              peers_on: bool = True,
              allowed: list[str] | None = None) -> tuple[str, dict[str, Any]]:
    if not peers_on and (name.startswith(peer_client.ASK) or peer_client.split_ns(name)[0]):
        return spec.agent_id, {
            "ok": False,
            "error": "目前是獨立模式（使用者沒有勾選「委派下游」），不能呼叫其他 agent。"
                     "只能用自己的工具，或直說這部分要對方才有。",
        }
    if name.startswith(peer_client.ASK):
        aid = name[len(peer_client.ASK):]
        if aid not in registry.BY_ID:
            return spec.agent_id, {"ok": False, "error": f"未知的 agent：{aid}"}
        if aid not in registry.get(spec.agent_id).downstream:
            return spec.agent_id, {
                "ok": False,
                "error": f"{aid} 不是 {spec.agent_id} 的下游，不能呼叫（避免 A→B→A 繞回來）",
                "allowed": list(registry.get(spec.agent_id).downstream),
            }
        q = (args.get("question") or "").strip()
        if not q:
            return aid, {"ok": False, "error": "question 不可為空"}
        return aid, peer_client.ask_agent(aid, q)

    aid, tool = peer_client.split_ns(name)
    if aid is None:
        t = spec.tool(tool)
        if t is None:
            if peers_on and (peer := _resolve_bare(spec, tool, allowed or [])):
                out = peer_client.call_tool(peer, tool, args)
                if isinstance(out, dict):
                    out = {**out, "note_from_hub":
                           f"你呼叫的 `{tool}` 少了命名空間，已當成 "
                           f"`{peer}{peer_client.NS}{tool}` 執行。"
                           f"下次請寫完整的名字。"}
                return peer, out
            own_names = sorted(x.name for x in spec.tools if x.ready)
            return spec.agent_id, {
                "ok": False,
                "error": f"未知的工具：{name}（不存在，不要再呼叫這個名字）",
                "available_tools": own_names,
            }
        if not t.ready:
            return spec.agent_id, {"ok": False, "error": f"工具 {tool} 尚未可用",
                                   "reason": t.not_ready_reason}
        try:
            out = t.handler(args)
        except Exception as e:
            return spec.agent_id, {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return spec.agent_id, out if isinstance(out, dict) and "ok" in out else {"ok": True, "result": out}
    return aid, peer_client.call_tool(aid, tool, args)


def _forced_answer(spec: Any, req: Any, own_ctx: str,
                   collected: list[tuple[str, str]]) -> Iterator[tuple[str, dict]]:
    lines = [req.query, ""]
    if own_ctx.strip():
        lines.append(own_ctx.strip())
    if collected:
        lines.append("以下是為了回答這一題、已經查到的資料：")
        per = max(600, RESULT_CHARS // max(1, len(collected)))
        for name, body in collected:
            lines.append(f"\n【{name} 的結果】\n{_clip(body, per)}")
    else:
        lines.append("（沒有任何工具成功回傳資料。）")
    lines.append(
        "\n請直接用繁體中文把上面那個問題的答案寫出來。\n"
        "★ 你現在沒有任何工具可以呼叫，也不要輸出 JSON —— 只能寫文字。\n"
        "★ 資料裡有的就照實講；資料不足的部分明講是哪一塊缺，不要自己補數字。"
    )

    ref = registry.get(spec.agent_id)
    own = (spec.system or "").strip() or f"你是「{ref.name}」：{ref.role}。"
    system = (own + "\n\n現在是**收尾**：資料都查完了，你的工作只剩下用文字把結論講清楚。"
                    "不要呼叫工具，不要輸出 JSON，用繁體中文直接講結論。")

    got = False
    for kind, payload in gateway.stream_step(system, [{"role": "user", "content": "\n".join(lines)}], None):
        if kind == "text":
            got = True
            yield EV_DELTA, {"t": payload}
        elif kind == "error":
            if isinstance(payload, dict):
                yield EV_ERROR, {"error": f"{payload['title']}：{payload['detail']}",
                                 "hint": payload["fix"], "diag": payload,
                                 **({"timeout": True} if payload.get("timeout") else {})}
            else:
                yield EV_ERROR, {"error": payload}
            return
    if not got:
        yield EV_DELTA, {
            "t": "（模型跑完了工具但始終沒有產出文字答案。"
                 "上方軌跡與下方卡片是已經查到的真實資料，請直接參考。）"
        }


def stream_delegated(spec: Any, req: Any, found: dict[str, Any]) -> Iterator[tuple[str, dict]]:
    ok, detail = gateway.available()
    if not ok:
        yield EV_DELTA, {"t": f"（LLM gateway 不可用：{detail}。以上為檢索結果，未呼叫任何工具。）"}
        return

    ref = registry.get(spec.agent_id)
    peers_on = bool(getattr(req, "delegate", False) and getattr(spec, "can_delegate", False))
    allowed = [p for p in ref.downstream if not req.peers or p in req.peers] if peers_on else []
    tools = [t.for_model() for t in spec.tools if t.ready] + peer_client.peer_tools(allowed)
    system = build_system(spec, allowed)
    max_steps = MAX_STEPS_DELEGATE if (peers_on and MAX_STEPS_DELEGATE) else gateway.MAX_STEPS

    own_ctx = ""
    hits = found.get("results") or []
    ctx_fn = getattr(spec, "context_fn", None)
    if ctx_fn is not None:
        try:
            text = (ctx_fn(req.query, found) or "").strip()
        except Exception as e:  # noqa: BLE001 -- 背景講不出來也要能繼續跑工具
            text = ""
            print(f"[delegate] context_fn 失敗，退回 results：{type(e).__name__}: {e}")
        if text:
            own_ctx = f"\n\n我自己的檢索結果：\n{_clip(text, RESULT_CHARS)}"
    if not own_ctx and hits:
        own_ctx = (f"\n\n我自己的檢索結果（{len(hits)} 筆）：\n"
                   f"{_fit({'results': hits[:8]}, RESULT_CHARS)}")
    elif not own_ctx and found.get("unavailable"):
        own_ctx = f"\n\n（我自己沒有可用的語料：{found.get('note', '')}）"

    peer_ctx = ""
    if allowed:
        pre_traces, peer_ctx, pre_cards = _prefetch_peers(req.query, allowed)
        for t in pre_traces:
            yield EV_TRACE, {"step": 0, **t}
        for aid, name, hits in pre_cards:
            yield EV_RESULTS, {
                "hits": hits,
                "raw": {"prefetched_from": aid, "query": req.query},
                "append": True,
                "agent": aid,
                "agent_name": name,
            }

    messages: list[dict[str, Any]] = []
    for h in req.history[-8:]:
        if h.get("role") in ("user", "assistant") and (h.get("content") or "").strip():
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": req.query + own_ctx + peer_ctx})

    seen: set[str] = set()
    collected: list[tuple[str, str]] = []
    answered = False
    consecutive_fails = 0

    repeats_by_tool: dict[str, int] = {}
    MAX_REPEATS = 2

    step = 0
    while step < max_steps:
        step += 1
        final = None
        for kind, payload in gateway.stream_step(system, messages, tools):
            if kind == "text":
                if (payload or "").strip():
                    answered = True
                yield EV_DELTA, {"t": payload}
            elif kind == "error":
                if isinstance(payload, dict):
                    yield EV_ERROR, {
                        "error": f"{payload['title']}：{payload['detail']}",
                        "hint": payload["fix"],
                        "diag": payload,
                        **({"timeout": True} if payload.get("timeout") else {}),
                    }
                else:
                    yield EV_ERROR, {"error": payload}
                return
            else:
                final = payload

        if final is None:
            return

        calls = [b for b in final.content if getattr(b, "type", "") == "tool_use"]
        if not calls:
            note = gateway.empty_answer_note(final)
            if note:
                yield EV_DELTA, {"t": note}
                return
            if not answered:
                yield from _forced_answer(spec, req, own_ctx + peer_ctx, collected)
            return
        messages.append({"role": "assistant", "content": gateway._assistant_content(final.content)})

        results: list[dict[str, Any]] = []
        for c in calls:
            args = c.input if isinstance(c.input, dict) else {}
            yield EV_TRACE, {"step": step, "phase": "call", "id": c.id,
                             "tool": c.name, "args": args}
            t0 = time.perf_counter()
            sig = f"{c.name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
            repeated = sig in seen
            if repeated:
                aid, out = spec.agent_id, {
                    "ok": False,
                    "error": "你剛才已經用一模一樣的參數呼叫過這個工具了，結果就在上面。"
                             "不要重複呼叫：改用已經拿到的資料直接作答，"
                             "或是換一組真的不同的參數。"
                             "★ 問句還有沒有回答到的部分，就去問那一部分。",
                }
            else:
                seen.add(sig)
                aid, out = _dispatch(spec, c.name, args, peers_on, allowed)
            ms = round((time.perf_counter() - t0) * 1000)
            body = _dumps(out)
            yield EV_TRACE, {
                "step": step, "phase": "result", "id": c.id, "tool": c.name,
                "agent": aid, "agent_name": registry.get(aid).name if aid in registry.BY_ID else aid,
                "ok": bool(out.get("ok")), "offline": bool(out.get("offline")),
                "repeated": repeated,
                "ms": ms, "preview": _clip(body, PREVIEW_CHARS),
            }
            if out.get("ok"):
                consecutive_fails = 0
                repeats_by_tool.pop(c.name, None)
                collected.append((c.name, body))
            elif repeated:
                n = repeats_by_tool[c.name] = repeats_by_tool.get(c.name, 0) + 1
                if n >= MAX_REPEATS and not c.name.startswith(peer_client.ASK):
                    kept = [t for t in tools if t.get("name") != c.name]
                    if len(kept) < len(tools):
                        tools = kept
                        yield EV_TRACE, {
                            "step": step, "phase": "drop_tool", "tool": c.name,
                            "reason": f"同一組參數重複呼叫 {n} 次，資料已在手上；"
                                      "把這支工具收回，剩下的步數留給問句還沒"
                                      "回答到的部分",
                            "tools_left": len(tools),
                        }
            else:
                consecutive_fails += 1
            results.append({
                "type": "tool_result",
                "tool_use_id": c.id,
                "content": _fit(out, RESULT_CHARS),
                **({"is_error": True} if not out.get("ok") else {}),
            })

        if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
            yield EV_TRACE, {
                "step": step, "phase": "abort",
                "reason": f"連續 {consecutive_fails} 次工具呼叫失敗，停止嘗試並直接作答",
            }
            break

        if not tools:
            yield EV_TRACE, {
                "step": step, "phase": "abort",
                "reason": "所有工具都因為重複呼叫被收回，資料已取得，直接作答",
            }
            break

        if step == max_steps - 1:
            results.append({
                "type": "text",
                "text": "（這是最後一次可以呼叫工具的機會，下一輪就要交答案了。"
                        "請用目前手上的資料直接把結論寫出來。）",
            })
        messages.append({"role": "user", "content": results})

    yield from _forced_answer(spec, req, own_ctx + peer_ctx, collected)
