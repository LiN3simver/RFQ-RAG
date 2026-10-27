"""不需要 torch 的查詢編碼器：BGE-M3 官方 ONNX + onnxruntime + tokenizers。"""
from __future__ import annotations

import os
from pathlib import Path

_ONNX_FILES = ["onnx/*", "tokenizer.json"]


def _snapshot(repo: str) -> Path:
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(repo, allow_patterns=_ONNX_FILES))


class OnnxEncoder:
    def __init__(self, repo: str, max_seq: int = 192, threads: int | None = None):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        d = _snapshot(repo)

        self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tok.enable_truncation(max_length=max_seq)
        pad_id = self.tok.token_to_id("<pad>")
        self.tok.enable_padding(pad_id=pad_id, pad_token="<pad>")

        so = ort.SessionOptions()
        if threads:
            so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(
            str(d / "onnx" / "model.onnx"),
            sess_options=so,
            providers=["CPUExecutionProvider"],
        )
        self.max_seq_length = max_seq

    def encode(self, texts, batch_size: int = 32,
               normalize_embeddings: bool = True, **_ignored):
        import numpy as np

        if isinstance(texts, str):
            texts = [texts]
        out = []
        for i in range(0, len(texts), batch_size):
            enc = self.tok.encode_batch(texts[i:i + batch_size])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            msk = np.array([e.attention_mask for e in enc], dtype=np.int64)
            (vecs,) = self.sess.run(
                ["sentence_embedding"],
                {"input_ids": ids, "attention_mask": msk},
            )
            out.append(vecs)
        vecs = np.concatenate(out, axis=0) if len(out) > 1 else out[0]
        if normalize_embeddings:
            n = np.linalg.norm(vecs, axis=1, keepdims=True)
            vecs = vecs / np.clip(n, 1e-12, None)
        return vecs.astype("float32")


def available() -> tuple[bool, str]:
    missing = []
    for mod in ("onnxruntime", "tokenizers", "huggingface_hub", "numpy"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        return False, f"缺少套件：{', '.join(missing)}"
    return True, "ok"


__all__ = ["OnnxEncoder", "available"]


def main() -> None:  # pragma: no cover
    ok, why = available()
    print(f"onnx 後端可用：{ok}（{why}）")
    if not ok:
        raise SystemExit(1)

    repo = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
    print(f"載入 {repo}（第一次要下載約 2.28GB 到 HF_HOME={os.environ.get('HF_HOME') or '預設'}）…")
    enc = OnnxEncoder(repo, max_seq=int(os.environ.get("EMBED_MAX_SEQ", "192")))
    v = enc.encode(["FFC 30P 130mm"])
    norm = float((v[0] ** 2).sum()) ** 0.5
    print(f"維度 {v.shape}，範數 {norm:.6f}")
    if abs(norm - 1.0) > 1e-4:
        print("✗ 範數不是 1，正規化沒生效")
        raise SystemExit(1)
    print("✓ 這條路可以走。要換過去之前先跑："
          "python -m src.embed --compare-backends")


if __name__ == "__main__":  # pragma: no cover
    main()
