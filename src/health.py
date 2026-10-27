"""/livez、/readyz、/status：存活、可否服務與版本資訊（含 agentkit 雜湊比對）。"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import admission, db

STARTED_AT = datetime.now(timezone.utc)
_T0 = time.monotonic()
_ROOT = Path(__file__).resolve().parents[1]

MANIFEST_NAME = "BUILD_MANIFEST.json"


def tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def combined_hash(files: dict[str, str]) -> str:
    text = "".join(f"{h}  {name}\n" for name, h in sorted(files.items()))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(*args: str, cwd: Path) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:  # noqa: BLE001 —— 容器裡沒有 git 是正常的
        return None


def _git_info(path: Path) -> dict[str, Any] | None:
    commit = _git("rev-parse", "HEAD", cwd=path)
    if not commit:
        return None
    dirty = _git("status", "--porcelain", "--untracked-files=no", cwd=path)
    return {"commit": commit, "dirty": bool(dirty)}


def agentkit_report(agentkit_dir: Path, manifest: dict[str, Any] | None,
                    vendor_dir: Path | None = None) -> dict[str, Any]:
    live = tree_hashes(agentkit_dir)
    rep: dict[str, Any] = {"path": str(agentkit_dir), "sha256": combined_hash(live)}
    if manifest is not None:
        recorded = (manifest.get("agentkit") or {}).get("files") or {}
        changed = sorted(k for k in set(recorded) | set(live)
                         if recorded.get(k) != live.get(k))
        rep["source"] = "vendored"
        rep["manifest"] = manifest
        rep["matches_manifest"] = not changed
        if changed:
            rep["changed_since_vendored"] = changed
    else:
        rep["source"] = "live"
    if vendor_dir is not None and vendor_dir.is_dir():
        vend = tree_hashes(vendor_dir)
        stale = sorted(k for k in set(vend) | set(live) if vend.get(k) != live.get(k))
        rep["vendor_copy"] = {
            "matches_live": not stale,
            **({"differs": stale,
                "fix": "把 ../Agent_Hub_RAG_Frontend/agentkit 重新同步到 vendor/agentkit"} if stale else {}),
        }
    return rep


_version_cache: dict[str, Any] | None = None


def version() -> dict[str, Any]:
    global _version_cache
    if _version_cache is not None:
        return _version_cache
    import agentkit

    ak_dir = Path(agentkit.__file__).resolve().parent
    manifest = None
    mpath = ak_dir / MANIFEST_NAME
    if mpath.is_file():
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            manifest = {"error": f"讀不到 {MANIFEST_NAME}：{type(e).__name__}: {e}"}
    app = (manifest or {}).get("app") or _git_info(_ROOT) or {"commit": "unknown"}
    vendor = _ROOT / "vendor" / "agentkit"
    rep = agentkit_report(ak_dir, manifest,
                          vendor_dir=vendor if vendor.resolve() != ak_dir else None)
    if manifest is None:
        rep["git"] = _git_info(ak_dir.parent)
    _version_cache = {"app": app, "agentkit": rep}
    return _version_cache


def readiness(status: str, detail: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for k in ("error", "note", "llm"):
        if detail.get(k):
            reasons.append(str(detail[k]))
    if detail.get("checking"):
        return False, reasons or ["依賴檢查還沒有結果"]
    return status in ("ready", "degraded"), reasons


def mount(app: Any, spec: Any) -> None:
    from fastapi.responses import JSONResponse

    @app.get("/livez", tags=["ops"])
    async def livez() -> dict[str, Any]:
        return {"status": "alive", "uptime_s": round(time.monotonic() - _T0, 1)}

    @app.get("/readyz", tags=["ops"])
    def readyz():  # noqa: ANN202
        st, detail = spec.status()
        ok, reasons = readiness(st, detail)
        return JSONResponse(
            {"agent_id": spec.agent_id, "ready": ok, "status": st,
             "reasons": reasons, "stale": bool(detail.get("stale"))},
            status_code=200 if ok else 503)

    @app.get("/status", tags=["ops"])
    def status() -> dict[str, Any]:
        st, detail = spec.status()
        ok, reasons = readiness(st, detail)
        return {
            "agent_id": spec.agent_id,
            "ready": ok,
            "status": st,
            "reasons": reasons,
            "started_at": STARTED_AT.isoformat(timespec="seconds"),
            "uptime_s": round(time.monotonic() - _T0, 1),
            "version": version(),
            "admission": admission.stats(),
            "agentkit_gate": gate.stats() if (gate := getattr(spec, "_gate", None)) else None,
            "db": {"budget": db.budget(), "pool": db.pool_stats()},
            "detail": detail,
        }
