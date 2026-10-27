"""失敗記憶：以「路徑＋違規類型」為鍵記錄核對失敗並放進下一次的 prompt，含程式內種子教訓。"""
from __future__ import annotations

import threading
import time
from typing import Any

from .db import pool

MAX_INJECT = 4
_TTL = 60.0

ANY = "*"

SEED: tuple[dict[str, str], ...] = (
    {
        "route": "product",
        "code": "contradiction",
        "rule": "使用者給的是**產品料號**（AX52K8D61930-3 這種：字母開頭、"
                "十幾個英數、接 '-' 再一兩碼）時，不可以說「查無編號」。"
                "那不是 RFQ 編號，它在 表單的 part_no 欄 這一欄，"
                "檢索結果已經照它查過了。要講的是「這顆產品開過 N 張單」。",
    },
    {
        "route": "product",
        "code": "zero_vs_blank",
        "rule": "成本欄「填 0」與「沒有人填」是兩件事，一定要分開講。"
                "檢索結果會直接告訴你哪一種（『成本欄全部填 0』／『從未填寫』），"
                "照它講，不要合併成「沒有成本資料」。",
    },
    {
        "route": ANY,
        "code": "placeholder",
        "rule": "任何情況都不可以用 X／N／TBD／<金額> 代替一個數字。"
                "沒有那個數字就說「這一欄沒有資料」。",
    },
    {
        "route": ANY,
        "code": "ungrounded_number",
        "rule": "數字只能從「檢索結果」照抄（可以四捨五入）。"
                "覆蓋率、已索引份數、筆數這種統計值尤其不可以憑印象講 —— "
                "檢索結果沒寫就不要講那個數字。",
    },
    {
        "route": ANY,
        "code": "ungrounded_id",
        "rule": "編號、料號、附件檔名只能引用檢索結果裡真的出現過的。"
                "不要為了讓答案完整而組一個看起來合理的編號。",
    },
    {
        "route": "material",
        "code": "fallback_to_similar",
        "rule": "材料料號查不到就是查不到，不可以改回相近的料號或相似的 RFQ 充數。"
                "要說「查無此料號」並把檢索結果裡的已索引份數一起講完。",
    },
    {
        "route": ANY,
        "code": "time_guess",
        "rule": "問句有「最近／最新／最早／某年某月」時，順序已經由系統依"
                "建檔時間排好了（檢索結果會註明）。第一筆就是答案，"
                "不要自己從清單裡挑一筆說它是最新的。系統沒有註明排序時，"
                "不可以宣稱任何一筆是「最新的」。",
    },
)


class _Store:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}


_mem = _Store()
_lock = threading.Lock()
_cache: dict[str, tuple[float, list[dict]]] = {}
_ddl_done = False
_db_ok = True


def _ensure_table(cur) -> None:
    global _ddl_done
    if _ddl_done:
        return
    cur.execute("[SQL 已於公開版移除]")
    _ddl_done = True


def record(route: str, code: str, rule: str, sample_q: str = "") -> None:
    global _db_ok
    key = (route, code)
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute("[SQL 已於公開版移除]", (route, code, rule, sample_q or None))
        _db_ok = True
    except Exception as e:      # noqa: BLE001 -- 見上面那則說明
        _db_ok = False
        print(f"[lessons] 寫不進資料庫，改記在記憶體：{type(e).__name__}: {e}")
        with _lock:
            row = _mem.rows.setdefault(
                key, {"route": route, "code": code, "rule": rule,
                      "sample_q": sample_q, "hits": 0})
            row["hits"] += 1
            row["rule"] = rule
    with _lock:
        _cache.pop(route, None)
        _cache.pop(ANY, None)


def learned(route: str) -> list[dict]:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(route)
        if hit and now - hit[0] < _TTL:
            return hit[1]

    rows: list[dict] = []
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute("[SQL 已於公開版移除]", (route, ANY))
            rows = [{"route": r[0], "code": r[1], "rule": r[2], "hits": r[3]}
                    for r in cur.fetchall()]
    except Exception:
        with _lock:
            rows = [dict(r) for r in _mem.rows.values()
                    if r["route"] in (route, ANY)]
            rows.sort(key=lambda r: r["hits"], reverse=True)

    seeds = [{**s, "hits": -1, "seed": True}
             for s in SEED if s["route"] in (route, ANY)]
    seen = {(s["route"], s["code"]) for s in seeds}
    merged = seeds + [r for r in rows if (r["route"], r["code"]) not in seen]

    def rank(r: dict) -> tuple:
        return (r["route"] == ANY,
                not r.get("seed"),
                -int(r.get("hits") or 0))
    out = sorted(merged, key=rank)
    with _lock:
        _cache[route] = (now, out)
    return out


def prompt_for(route: str, limit: int = MAX_INJECT) -> str:
    rows = learned(route)[:limit]
    if not rows:
        return ""
    lines = ["【以前在這類問題上犯過的錯，這一次不要再犯】"]
    lines += [f"- {r['rule']}" for r in rows]
    return "\n".join(lines)


def report() -> list[dict]:
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute("[SQL 已於公開版移除]")
            return [{"route": r[0], "code": r[1], "rule": r[2], "hits": r[3],
                     "sample_q": r[4], "last_seen": r[5]}
                    for r in cur.fetchall()]
    except Exception as e:      # noqa: BLE001
        return [{"error": f"{type(e).__name__}: {e}",
                 "note": "記憶庫讀不到，種子教訓仍然有效（見 lessons.SEED）"}]


def forget(route: str, code: str) -> bool:
    try:
        with pool().connection() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute("[SQL 已於公開版移除]", (route, code))
            n = cur.rowcount
        with _lock:
            _cache.clear()
        return bool(n)
    except Exception:
        with _lock:
            _mem.rows.pop((route, code), None)
            _cache.clear()
        return False
