"""回答流程：生成 → 確定性核對 → 帶錯誤退件重寫；都沒過就直接列出檢索結果。"""
from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

from . import lessons, verify

MAX_ATTEMPTS = int(os.environ.get("ANSWER_MAX_ATTEMPTS", "5"))
PATIENCE_S = float(os.environ.get("ANSWER_PATIENCE_S", "20"))
PATIENCE_REPEAT_S = float(os.environ.get("ANSWER_PATIENCE_REPEAT_S", "30"))
HARD_LIMIT_S = float(os.environ.get("ANSWER_HARD_LIMIT_S", "0"))

FALLBACK_CHARS = 2600


DEADLINE_MARGIN_S = 3.0


def hard_limit() -> float | None:
    limits = [HARD_LIMIT_S] if HARD_LIMIT_S > 0 else []
    d = _request_deadline()
    if d is not None:
        limits.append(max(1.0, d.remaining() - DEADLINE_MARGIN_S))
    return min(limits) if limits else None


def _request_deadline():
    try:
        from agentkit import deadline
    except ImportError:
        return None
    return deadline.current()


PATIENCE_PREFIX = "（已經等了 "


def is_patience(text: str) -> bool:
    return (text or "").lstrip().startswith(PATIENCE_PREFIX)


def patience_note(waited: float, attempt: int) -> str:
    more = f"，這是第 {attempt} 次產生" if attempt > 1 else ""
    return (f"（已經等了 {waited:.0f} 秒{more}，模型還在整理答案，請耐心等候。"
            f"公司 LLM gateway 忙碌時單題可能要一分鐘以上——"
            f"檢索結果已經查好了，就在下方卡片，不必等這段文字才能看。）\n\n")


FALLBACK_PREFIXES = ("（模型的回答連續", "（時間預算", "（LLM gateway 出錯")


def is_fallback(text: str) -> bool:
    return (text or "").lstrip().startswith(FALLBACK_PREFIXES)


def _fallback(context: str, violations: list[verify.Violation],
              attempts: int, elapsed: float, reason: str) -> str:
    head = (f"（模型的回答連續 {attempts} 次沒有通過事實核對"
            if reason == "verify" else
            f"（時間預算 {elapsed:.0f} 秒用完，還沒有拿到通過核對的回答")
    why = ("；最後一次的問題：" + "；".join(v.detail for v in violations[:3])
           if violations else "")
    return (f"{head}{why}。"
            f"為了不給出沒有依據的數字，以下**直接列出檢索到的原始資料**，"
            f"未經模型改寫。完整內容見下方卡片。）\n\n"
            + context.strip()[:FALLBACK_CHARS])


def verified(question: str, context: str, *, route: str,
             has_data: bool, n_rows: int = 0,
             extra_allowed: tuple[str, ...] = (),
             deadline: float | None = None,
             display_context: str | None = None,
             system_extra: str | None = None,
             on_event: Any = None) -> Iterator[str]:
    from .llm import gateway, is_gateway_note

    t0 = time.monotonic()
    limit = deadline if deadline is not None else hard_limit()
    shown = display_context if display_context is not None else context

    def emit(**kw):
        if on_event:
            try:
                on_event({"route": route, **kw})
            except Exception:      # noqa: BLE001 -- 觀測不能弄壞回答
                pass

    if not has_data:
        emit(phase="no_data", attempts=0, seconds=0.0)
        yield shown.strip() or "查不到符合的資料。"
        return

    if system_extra is None:
        system_extra = lessons.prompt_for(route)
    prior = ""
    correction = ""
    violations: list[verify.Violation] = []
    recorded: set[str] = set()
    attempt = 0

    next_notice = PATIENCE_S

    while attempt < MAX_ATTEMPTS:
        attempt += 1
        try:
            text = ""
            for kind, val in gateway().answer_iter(
                    question, context, system_extra=system_extra,
                    correction=correction, prior=prior,
                    timeout=(None if limit is None
                             else max(1.0, limit - (time.monotonic() - t0)))):
                if kind == "wait":
                    waited = time.monotonic() - t0
                    if waited >= next_notice:
                        emit(phase="patience", attempts=attempt, seconds=waited)
                        yield patience_note(waited, attempt)
                        next_notice = waited + PATIENCE_REPEAT_S
                    continue
                text = val
        except TimeoutError as e:
            emit(phase="timeout", attempts=attempt, error=str(e),
                 seconds=time.monotonic() - t0)
            yield _fallback(shown, violations, attempt,
                            time.monotonic() - t0, "deadline")
            return
        except Exception as e:      # noqa: BLE001
            emit(phase="error", attempts=attempt, error=f"{type(e).__name__}: {e}",
                 seconds=time.monotonic() - t0)
            yield (f"（LLM gateway 出錯（{type(e).__name__}），"
                   f"以下直接列出檢索結果。）\n\n"
                   + shown.strip()[:FALLBACK_CHARS])
            return

        if is_gateway_note(text):
            emit(phase="gateway_note", attempts=attempt,
                 seconds=time.monotonic() - t0)
            yield text
            return

        violations = verify.check(text, shown, question,
                                  has_data=has_data,
                                  extra_allowed=extra_allowed)
        elapsed = time.monotonic() - t0
        emit(phase="checked", attempts=attempt, seconds=elapsed,
             violations=[v.code for v in violations])

        if not violations:
            yield text
            return

        for v in violations:
            if v.code in recorded:
                continue
            recorded.add(v.code)
            lessons.record(route, v.code, verify.generic_fix(v),
                           question[:200])

        prior = text
        correction = verify.summarize(violations)
        print(f"[answer] 第 {attempt} 次沒過核對（{route}）："
              + "；".join(v.detail for v in violations[:3]))

    emit(phase="give_up", attempts=attempt, seconds=time.monotonic() - t0,
         violations=[v.code for v in violations])
    yield _fallback(shown, violations, attempt,
                    time.monotonic() - t0, "verify")
