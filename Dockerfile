FROM python:3.12.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir uv==0.11.19
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --python /usr/local/bin/python3.12
# 在构建阶段下载并固定 Embedding 权重，运行容器时不再因第一条语义请求临时下载。
ENV HF_HOME=/opt/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1
RUN mkdir -p "$HF_HOME" && \
    /app/.venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('Qwen/Qwen3-Embedding-0.6B')"
COPY services ./services
COPY shared ./shared
COPY prompts ./prompts

USER 65532:65532
ENTRYPOINT ["/app/.venv/bin/uvicorn"]
