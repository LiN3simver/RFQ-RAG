# 展示用：說明映像檔的拆分方式。公開版不含資料與 SQL，建出來也無法實際查詢。
#
# 兩個 target：
#   api       常駐服務。查詢編碼走 BGE-M3 官方 ONNX + onnxruntime，不裝 torch，
#             也不裝 PDF/OCR 套件（重型 import 都寫在函式裡），映像檔約 541MB。
#   pipeline  批次（PDF 解析、向量化），是 api 的超集，與 api 共用底層 layer。
# api 放在最後：不指定 --target 時拿到的是精簡版。

FROM python:3.12-slim AS base
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libstdc++6 curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt
ENV EMBED_BACKEND=onnx
COPY main.py ./
COPY src ./src
COPY vendor/agentkit ./agentkit
ENV AGENTKIT_PATH=/app


FROM base AS pipeline
COPY requirements-pipeline.txt ./
RUN pip install --no-cache-dir -r requirements-pipeline.txt
CMD ["python", "-m", "src.ingest_pdf", "--help"]


FROM base AS api
EXPOSE 8300
# /readyz 不能服務時回 503（/a2a/health 一律回 200，不能拿來當健康檢查）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS --max-time 4 http://127.0.0.1:8300/readyz || exit 1
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8300"]
