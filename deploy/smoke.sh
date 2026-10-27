#!/usr/bin/env bash
# 部署門檻：/readyz 要 200、精確與語意查詢各要有結果、image 內 agentkit 要與版本紀錄一致。
set -uo pipefail
cd "$(dirname "$0")/.."

docker compose exec -T \
    -e SMOKE_EXACT="${T_SMOKE_EXACT:-RFQ-20260511101-001}" \
    -e SMOKE_SEMANTIC="${T_SMOKE_SEMANTIC:-50 ohm single-ended cable}" \
    api python - <<'PY'
import json, os, sys, time, urllib.error, urllib.request

BASE = "http://127.0.0.1:8300"

def call(path, body=None, timeout=10):
    req = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}

def fail(msg):
    print(f"SMOKE_FAIL {msg}")
    sys.exit(1)

try:
    code, ready = call("/readyz")
except Exception as e:
    fail(f"/readyz 連不上：{type(e).__name__}: {e}")
if code != 200:
    fail(f"/readyz {code}：{ready.get('status')} {'；'.join(ready.get('reasons') or [])}")

took = []
for label, q in (("exact", os.environ["SMOKE_EXACT"]),
                 ("semantic", os.environ["SMOKE_SEMANTIC"])):
    t0 = time.monotonic()
    try:
        code, out = call("/a2a/search", {"query": q, "top_k": 3}, timeout=90)
    except Exception as e:
        fail(f"{label} 檢索連不上：{type(e).__name__}: {e}")
    raw = out.get("raw") or {}
    if code != 200 or raw.get("error"):
        fail(f"{label} 檢索失敗（{code}）：{raw.get('error') or out}")
    if not out.get("hits"):
        fail(f"{label} 檢索沒有結果：{q!r}（資料沒進來，或這句在這台的資料裡查不到）")
    took.append(f"{label} {len(out['hits'])} 筆 {time.monotonic() - t0:.1f}s")

code, st = call("/status")
ak = ((st.get("version") or {}).get("agentkit") or {})
if ak.get("source") == "vendored" and ak.get("matches_manifest") is False:
    fail(f"image 裡的 agentkit 跟版本紀錄不一致：{ak.get('changed_since_vendored')}")
app = ((st.get("version") or {}).get("app") or {}).get("commit", "?")[:8]
print(f"SMOKE_OK status={ready.get('status')} app={app} "
      f"agentkit={str(ak.get('sha256', '?'))[:12]} | " + "，".join(took))
PY
