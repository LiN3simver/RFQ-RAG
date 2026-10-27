"""BGE-M3 向量化（可中斷續跑），編碼後端可在 torch 與 ONNX 之間切換。"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

from dotenv import load_dotenv

from . import fields
from .db import pool

load_dotenv()

MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
BATCH = int(os.environ.get("EMBED_BATCH", "32"))

BACKEND = os.environ.get("EMBED_BACKEND", "torch").strip().lower()

CHUNK = int(os.environ.get("EMBED_CHUNK", "512"))

THREADS = int(os.environ.get("EMBED_THREADS", str(os.cpu_count() or 4)))
MAX_SEQ = int(os.environ.get("EMBED_MAX_SEQ", "192"))

_HAS_TEXT = "[SQL 已於公開版移除]"


_CACHE = None
_CACHE_LOCK = threading.Lock()


def _model():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    with _CACHE_LOCK:
        if _CACHE is not None:
            return _CACHE
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

        if BACKEND == "onnx":
            from .embed_onnx import OnnxEncoder
            _CACHE = OnnxEncoder(MODEL, max_seq=MAX_SEQ, threads=THREADS)
            return _CACHE

        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(THREADS)
        m = SentenceTransformer(_torch_snapshot(), device="cpu")
        m.max_seq_length = MAX_SEQ
        _CACHE = m
    return _CACHE


_TORCH_IGNORE = ["onnx/*", "imgs/*", "*.jpg",
                 "colbert_linear.pt", "sparse_linear.pt",
                 ".gitattributes", "README.md"]


def _torch_snapshot() -> str:
    if os.environ.get("EMBED_TRIM_DOWNLOAD", "1") == "0":
        return MODEL
    from pathlib import Path
    if Path(MODEL).is_dir():
        return MODEL
    try:
        from huggingface_hub import snapshot_download
        return snapshot_download(MODEL, ignore_patterns=_TORCH_IGNORE)
    except Exception as e:
        print(f"[embed] 精簡下載失敗，改用預設行為：{type(e).__name__}: {e}",
              file=sys.stderr)
        return MODEL


def run(limit: int | None = None, deadline: float | None = None) -> int:
    with pool().connection() as conn:
        (todo,) = conn.execute(
            "[SQL 已於公開版移除]"
        ).fetchone()
    if not todo:
        print("沒有待算的列")
        return 0
    print(f"待算 {todo:,} 列，載入 {MODEL}（{THREADS} threads，"
          f"每塊 {CHUNK} 筆）…", file=sys.stderr)

    model = _model()
    done, t0 = 0, time.time()

    while True:
        with pool().connection() as conn:
            rows = conn.execute(
                "[SQL 已於公開版移除]",
                (CHUNK,),
            ).fetchall()
            if not rows:
                break
            texts = [
                fields.embed_text(dict(zip(fields.SEARCH_SOURCES, r[1:])))
                for r in rows
            ]

            vecs = model.encode(
                texts,
                batch_size=BATCH,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            with conn.cursor() as cur:
                cur.executemany(
                    "[SQL 已於公開版移除]",
                    [(v.tolist(), r[0]) for r, v in zip(rows, vecs)],
                )
            conn.commit()

        done += len(rows)
        rate = done / max(time.time() - t0, 1e-9)
        eta = (todo - done) / rate / 60 if rate else 0
        print(
            f"\r  {done:,}/{todo:,}  {rate:.1f} 列/秒  剩約 {eta:.0f} 分",
            end="", file=sys.stderr, flush=True,
        )
        if limit and done >= limit:
            break
        if deadline and time.time() > deadline:
            print(f"\n時間預算用完，已算 {done:,}/{todo:,}，剩下的留給下一輪",
                  file=sys.stderr, flush=True)
            break

    print(file=sys.stderr)
    return done


def backend_status() -> dict:
    info: dict = {"backend": BACKEND}
    if BACKEND == "onnx":
        from . import embed_onnx
        ok, why = embed_onnx.available()
        info["ok"] = ok
        if not ok:
            info["error"] = why
            return info
    else:
        try:
            import importlib
            for m in ("torch", "sentence_transformers"):
                importlib.import_module(m)
            info["ok"] = True
        except ImportError as e:
            return {**info, "ok": False, "error": f"{type(e).__name__}: {e}"}

    info["loaded"] = _CACHE is not None
    try:
        from huggingface_hub import try_to_load_from_cache
        probe = "onnx/model.onnx" if BACKEND == "onnx" else "config.json"
        info["weights_cached"] = isinstance(try_to_load_from_cache(MODEL, probe), str)
    except Exception:
        info["weights_cached"] = None
    if info.get("weights_cached") is False:
        info["note"] = ("模型還沒下載到這台機器；第一次語意查詢會先抓約 2.3GB。"
                        "這不影響其他功能，也刻意不讓它改變 status。")
    return info


STAGE = "embed"


COMPARE_MIN_COSINE = 0.9999


def compare_backends(queries: list[str] | None = None) -> int:
    import numpy as np

    if queries is None:
        queries = ["MCIO 8X 線長 600mm 以前大概多少錢", "50 ohm single-ended cable",
                   "最近一份 SFF-8643 的報價", "這張單的材料成本？",
                   "labor cost breakdown", "需求量 10K 的線材成本區間"]
    print(f"用 {len(queries)} 題問句比對兩個後端\n")

    from . import embed_onnx
    ok, why = embed_onnx.available()
    if not ok:
        print(f"✗ onnx 後端不可用：{why}")
        return 2

    def _encode(tag: str, build) -> "np.ndarray":
        t0 = time.time()
        m = build()
        t_load = time.time() - t0
        t0 = time.time()
        v = np.asarray(m.encode(queries, batch_size=BATCH,
                                normalize_embeddings=True,
                                show_progress_bar=False))
        print(f"  {tag:6s} 載入 {t_load:5.1f}s  編碼 {time.time() - t0:5.1f}s")
        return v

    def _torch():
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        import torch
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(THREADS)
        m = SentenceTransformer(_torch_snapshot(), device="cpu")
        m.max_seq_length = MAX_SEQ
        return m

    a = _encode("torch", _torch)
    b = _encode("onnx", lambda: embed_onnx.OnnxEncoder(MODEL, max_seq=MAX_SEQ,
                                                       threads=THREADS))

    cos = (a * b).sum(axis=1)
    worst = int(np.argmin(cos))
    print(f"\n餘弦相似度  最低 {cos.min():.6f} / 平均 {cos.mean():.6f} / "
          f"最高 {cos.max():.6f}")
    print(f"  最差的一題：{queries[worst]!r}")
    below = int((cos < COMPARE_MIN_COSINE).sum())
    print(f"  低於門檻 {COMPARE_MIN_COSINE} 的：{below}/{len(cos)} 題")

    if below:
        print("\n✗ 沒過。差異不是 fp32 捨入誤差的等級 —— 先確認分詞、pooling、"
              "正規化哪一環對不上，不要直接調門檻。")
        return 1
    print("\n✓ 過了。這支不驗排序，換後端前還要再跑一次檢索評測。")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="補算 BGE-M3 向量（可中斷續跑）")
    ap.add_argument("--limit", type=int, help="這次最多算幾列（測速用）")
    ap.add_argument("--compare-backends", action="store_true",
                    help="torch vs onnx 逐題比餘弦相似度，不寫資料庫（換後端的關卡）")
    args = ap.parse_args()

    if args.compare_backends:
        sys.exit(compare_backends())

    n = run(limit=args.limit)
    print(f"算了 {n:,} 列")

    with pool().connection() as conn:
        total, has_vec, no_text = conn.execute(
            "[SQL 已於公開版移除]"
        ).fetchone()
    print(f"全庫 {total:,} 列 / 有向量 {has_vec:,} / 無語意文字刻意不算 {no_text:,}")


if __name__ == "__main__":
    main()
