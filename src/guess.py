"""模糊問句的偵測與追問建議。"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from . import intent

GUESS_PREFIX = ("（這句話系統判斷不出明確的查詢意圖，"
                "以下是猜測與建議問法，不是查詢結果。）")


def is_guess(text: str) -> bool:
    return (text or "").lstrip().startswith(GUESS_PREFIX)


def is_ambiguous(found: dict[str, Any]) -> bool:
    it = found.get("intent") or {}
    if it.get("kind") != intent.KIND_SEARCH:
        return False
    parsed = found.get("parsed") or {}
    if parsed.get("numeric") or parsed.get("facets") or parsed.get("time"):
        return False
    channels = found.get("channels") or {}
    return not (set(channels) - {"vector"})


def route_catalog() -> str:
    lines = []
    for r in intent.ROUTES:
        eg = "、".join(f"「{q}」" for _label, q in r.examples[:2])
        lines.append(f"- {r.title}：{r.trigger}" + (f"（例：{eg}）" if eg else ""))
    return "\n".join(lines)


GUESS_SYSTEM = """你是 RFQ/S/D 查詢助理的「問法客服」。

使用者剛剛問的這句話，系統的分流機制判斷不出來要查什麼——沒有抓到料號、
RFQ 編號、成本數值條件、時間條件，字面與同義詞比對也完全沒有命中，
代表這句話可能跟這套系統的資料範圍無關，或者只是措辭系統認不出來。

你的工作**不是回答這句話**——手上沒有任何檢索到的資料，硬答等於編造。
你的工作是：
  1. 用一句話講你猜使用者可能想問什麼；如果真的看不出來，就老實說看不出來。
  2. 從下面「系統支援的問法」裡，挑 2~4 個跟使用者原意最接近的例子，
     改寫成使用者可能真正想問的版本（如果使用者的問句裡有具體的詞彙，
     可以套進例子裡；沒有就照例子的形式給）。

規則：
- 不要引用任何檢索結果，不要編造料號、金額、日期、編號——手上沒有這些東西。
- 建議的問法只能是「系統支援的問法」清單裡列出的形式，不要發明清單以外的功能。
- 如果使用者的問句其實已經寫得很清楚（只是系統剛好沒命中），要老實講
  「這句話看起來夠清楚，可能是系統一時沒認出來，可以試著更直接地寫」，
  不要硬掰一個牽強的猜測。
- 用繁體中文，三到六行，條列式，不要客套開場白。"""


def suggest(query: str) -> Iterator[str]:
    from .llm import gateway, is_gateway_note

    prompt = f"系統支援的問法：\n{route_catalog()}\n\n使用者問句：{query}"
    try:
        text = gateway().complete(GUESS_SYSTEM, prompt)
    except Exception as e:      # noqa: BLE001 -- gateway 掛了不該讓查詢跟著掛
        yield (f"（LLM gateway 出錯（{type(e).__name__}），暫時沒辦法幫你猜"
               f"這句話的意思。可以試著加上料號、RFQ 編號、具體規格描述，"
               f"或問「你可以做什麼」看系統支援的問法。）")
        return
    if is_gateway_note(text):
        yield text
        return
    yield GUESS_PREFIX + "\n\n" + text
