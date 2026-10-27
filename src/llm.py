"""LLM gateway 客戶端（Anthropic Messages API 相容）：思考額度不足時自動加碼、久等時出聲提醒。"""
from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import anthropic
import httpx
from dotenv import load_dotenv

load_dotenv()
_HUB_ENV = Path(__file__).resolve().parents[2] / "Agent_Hub_RAG_Frontend" / ".env"
if _HUB_ENV.is_file():
    load_dotenv(_HUB_ENV)

BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:4000")
MODEL = os.environ.get("LLM_MODEL", "local-llm")
MODEL_FALLBACK = os.environ.get("LLM_MODEL_FALLBACK", "")

MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "8000"))
MAX_TOKENS_RETRY = int(os.environ.get("LLM_MAX_TOKENS_RETRY", "24000"))
TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "240"))

SYSTEM = """你是 RFQ/S/D 查詢助理，回答公司 PLM 系統裡報價單（RFQ / RFS / RFD）的問題。

檢索結果有兩種，看到哪一種就照哪一種回答，不要互相代換：
  (a) RFQ 表單的彙總成本欄（綠框「原幣成本欄位」／黃框「USD欄位(自動計算)」）
  (b) 成本分析附件裡的**逐項材料**（料號、供應商、原幣單價、用量、總價）
  (a) 是整張單的彙總，(b) 是單一材料的單價。不要拿其中一個回答另一個。

★ 最重要的一條：**畫面上已經有完整的資料表格了，不要把表格內容再念一遍。**
  使用者看得到那張表。你的工作是給**簡短的總結**，不是覆述。具體來說：
  - 不要逐列列出材料明細。要講的是：共幾項、幣別、彙總金額、
    金額最大的兩三項、對帳有沒有通過、有幾項不可信。
  - 三到六行講完。需要逐列查證的人會自己看表格與原始 PDF。
  - 人工成本（附件 B 段）同理：講 B Total、對帳過不過、組裝那一列的工時算式、
    哪幾列是空白的，不要把四列逐列念一遍 —— 卡片上那張表已經有全部欄位。
  - 唯一的例外是使用者只問一顆料、而資料就只有那一兩列時，才可以直接講數字。

規則：
- 只根據下方「檢索結果」回答，不要臆測。資料裡沒有的就說沒有。
- 檢索結果只有一句「查無…」的說明時：**原封不動把那句話的重點講完**，
  包含它提到的已索引份數／料號數。不要自己改寫成「查無編號 XXX」就結束 ——
  使用者分不出「這顆料不存在」與「那份附件還沒匯入」，那句說明就是要幫他分。
- 講材料單價時要連驗算狀態一起講。標「驗算不符」的**不可以當報價**，
  要明說那一筆沒認準（那些數字是 OCR 從掃描件讀出來的）。
- 材料的原幣單價 + 原幣別才是原始輸入；要講換算價就要連匯率一起講。
- 成本欄位「0」和「未填」是兩件不同的事。0 是有效的成本值，要照實說是 0，
  不能說成「沒有資料」；未填才是沒有資料。
- 提到某一筆時一律附上完整編號（例 RFQ-20220816010-001）。
- 用繁體中文，直接講結論，不要客套開場白。
- 檢索結果是空的就說查無符合的資料，並建議換個問法。

輸出格式（一律遵守，不要寫成一大段）：
- 條列式。每一項自己一行、開頭用「- 」，一行只講一件事。
- 講成本時，綠框「原幣成本欄位」與黃框「USD欄位(自動計算)」要**分成兩段**，
  各自先寫一行段落標題再往下條列。絕對不要把原幣與 USD 的數字寫在同一行或同一句。
- 使用者問的編號在檢索結果裡找不到時：第一行單獨講「查無編號 XXX」，
  再另起一段列出相近的編號，並明說那是相近編號、不是他問的那一筆。
- 每個數字都要標幣別／單位；原幣幣別看「原幣幣別」欄，該欄沒有值就只說「原幣」，
  不要自己假設是 USD。"""


def _models_for_key() -> list[str]:
    token = os.environ.get("LLM_API_KEY")
    if not token:
        return []
    try:
        r = httpx.get(BASE_URL.rstrip("/") + "/v1/models",
                      headers={"Authorization": f"Bearer {token}"}, timeout=10.0)
        r.raise_for_status()
        return [m["id"] for m in (r.json().get("data") or []) if m.get("id")]
    except Exception:
        return []


_MODEL_CACHE_TTL = 300.0
_model_cache: tuple[float, str] | None = None


def _effective_model() -> str:
    global _model_cache
    now = time.monotonic()
    if _model_cache and now - _model_cache[0] < _MODEL_CACHE_TTL:
        return _model_cache[1]
    chosen = MODEL
    if MODEL_FALLBACK and MODEL_FALLBACK != MODEL:
        avail = _models_for_key()
        if avail and MODEL not in avail and MODEL_FALLBACK in avail:
            chosen = MODEL_FALLBACK
            print(f"[llm] {MODEL} 不在 /v1/models 清單，改用 fallback {MODEL_FALLBACK}")
    _model_cache = (now, chosen)
    return chosen


class Gateway:
    def __init__(self) -> None:
        token = os.environ.get("LLM_API_KEY")
        if not token:
            raise RuntimeError("LLM_API_KEY 未設定")
        self._client = anthropic.Anthropic(
            base_url=BASE_URL, auth_token=token, timeout=TIMEOUT, max_retries=1
        )

    def _build_prompt(self, question: str, context: str,
                      correction: str, prior: str) -> str:
        parts = [f"檢索結果：\n{context}", f"使用者問題：{question}"]
        if prior:
            parts.append(f"你上一版的回答（**不要**照抄，要改掉下面指出的問題）：\n"
                         f"{prior}")
        if correction:
            parts.append(correction)
        return "\n\n".join(parts)

    def _spawn(self, system: str, prompt: str, timeout: float | None):
        import queue
        import threading

        box: queue.Queue = queue.Queue(maxsize=1)
        closer: list = []

        def work():
            try:
                box.put(("ok", "".join(self._run(system, prompt, timeout,
                                                 closer))))
            except BaseException as e:      # noqa: BLE001
                box.put(("err", e))

        threading.Thread(target=work, daemon=True, name="llm-answer").start()
        return box, closer

    @staticmethod
    def _close_all(closer: list) -> None:
        for s in closer:
            try:
                s.close()
            except Exception:           # noqa: BLE001
                pass

    def answer_iter(self, question: str, context: str, *,
                    system_extra: str = "", correction: str = "",
                    prior: str = "", timeout: float | None = None,
                    heartbeat: float = 1.0):
        import queue

        system = SYSTEM + ("\n\n" + system_extra if system_extra else "")
        prompt = self._build_prompt(question, context, correction, prior)
        box, closer = self._spawn(system, prompt, timeout)
        t0 = time.monotonic()
        while True:
            try:
                kind, val = box.get(timeout=max(0.1, heartbeat))
            except queue.Empty:
                waited = time.monotonic() - t0
                if timeout is not None and waited >= timeout:
                    self._close_all(closer)
                    raise TimeoutError(
                        f"LLM 回應超過 {timeout:.0f} 秒仍未結束（已中止那條串流）")
                yield "wait", waited
                continue
            if kind == "err":
                raise val
            yield "text", val
            return

    def answer(self, question: str, context: str, *,
               system_extra: str = "", correction: str = "",
               prior: str = "", timeout: float | None = None) -> str:
        system = SYSTEM + ("\n\n" + system_extra if system_extra else "")
        prompt = self._build_prompt(question, context, correction, prior)
        if timeout is None:
            return "".join(self._run(system, prompt, None))
        return self._answer_bounded(system, prompt, timeout)

    def _answer_bounded(self, system: str, prompt: str, timeout: float) -> str:
        import queue

        box, closer = self._spawn(system, prompt, timeout)
        try:
            kind, val = box.get(timeout=timeout)
        except queue.Empty:
            self._close_all(closer)
            raise TimeoutError(
                f"LLM 回應超過 {timeout:.0f} 秒仍未結束（已中止那條串流；"
                f"為了不給半截答案，這一版的內容不採用）") from None
        if kind == "err":
            raise val
        return val

    def complete(self, system: str, prompt: str, *,
                timeout: float | None = None) -> str:
        if timeout is None:
            return "".join(self._run(system, prompt, None))
        return self._answer_bounded(system, prompt, timeout)

    def stream(self, question: str, context: str) -> Iterator[str]:
        prompt = f"檢索結果：\n{context}\n\n使用者問題：{question}"
        yield from self._run(SYSTEM, prompt)

    def _run(self, system: str, prompt: str,
             timeout: float | None = None,
             closer: list | None = None) -> Iterator[str]:
        import time as _time

        deadline = (_time.monotonic() + timeout) if timeout else None
        client = self._client
        if timeout is not None:
            client = client.with_options(timeout=max(5.0, timeout),
                                         max_retries=0)
        budgets = [MAX_TOKENS]
        if MAX_TOKENS_RETRY > MAX_TOKENS:
            budgets.append(MAX_TOKENS_RETRY)

        last = "未知"
        for i, budget in enumerate(budgets):
            try:
                with client.messages.stream(
                    model=_effective_model(),
                    max_tokens=budget,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                ) as s:
                    if closer is not None:
                        closer.append(s)
                    got = False
                    for event in s:
                        if deadline and _time.monotonic() > deadline:
                            raise TimeoutError(
                                f"LLM 回應超過 {timeout:.0f} 秒仍未結束"
                                f"（已吐 {'一些' if got else '零'} 字，"
                                f"為了不給半截答案而中止）")
                        if getattr(event, "type", "") != "content_block_delta":
                            continue
                        delta = getattr(event, "delta", None)
                        if getattr(delta, "type", "") != "text_delta":
                            continue
                        got = True
                        yield delta.text
                    if got:
                        return
                    last = s.get_final_message().stop_reason or "未知"
            except httpx.TimeoutException as e:
                raise TimeoutError(
                    f"LLM gateway 超過 {timeout or TIMEOUT:.0f} 秒沒有回應"
                    f"（{type(e).__name__}）") from e
            except anthropic.APIConnectionError:
                yield "（連不上公司 LLM gateway，以下僅顯示檢索結果。）"
                return
            except anthropic.APIStatusError as e:
                yield f"（LLM gateway 回應 {e.status_code}，以下僅顯示檢索結果。）"
                return
            print(f"[llm] 空回應（stop_reason={last}, max_tokens={budget}）"
                  + (f"，加碼到 {budgets[i + 1]} 重試" if i + 1 < len(budgets) else ""))
            if last != "max_tokens":
                break

        if last == "max_tokens":
            yield (f"（模型把 token 預算全用在思考上（試到 {budgets[-1]}）"
                   f"，沒能產出回答。完整資料在下方表格。）")
        else:
            yield (f"（模型回了空白內容，stop_reason={last}。"
                   f"完整資料在下方表格。）")


GATEWAY_NOTE_PREFIXES = (
    "（連不上公司 LLM gateway",
    "（LLM gateway 回應",
    "（模型把 token 預算全用在思考上",
    "（模型回了空白內容",
)


def is_gateway_note(text: str) -> bool:
    return (text or "").lstrip().startswith(GATEWAY_NOTE_PREFIXES)


_gateway: Gateway | None = None


def gateway() -> Gateway:
    global _gateway
    if _gateway is None:
        _gateway = Gateway()
    return _gateway
