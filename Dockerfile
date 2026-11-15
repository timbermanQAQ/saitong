# 赛题通 · 单容器镜像：FastAPI + 嵌入式 seekdb 知识库 + PowerContext 队伍档案
#
#   docker build -t saitong .
#   docker run -d -p 7860:7860 -v $PWD/.docker-runtime:/app/.runtime saitong
#   浏览器打开 http://127.0.0.1:7860
#
# 本地开发不需要镜像：PYTHON=/home/claude/mmvenv/bin/python bash scripts/start.sh
FROM python:3.13-slim-bookworm

# libstdc++6 / libgcc-s1：libseekdb.so 与 onnxruntime 的运行库（libaio 由 pylibseekdb 自带）
# ca-certificates：调用 OpenAI 兼容 LLM 接口；tzdata：截止时间按 +08:00 计算
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 libgcc-s1 tzdata \
    && rm -rf /var/lib/apt/lists/*

# HOME 指向 /app：embedding 模型缓存落在 $HOME/.cache/pyseekdb，随镜像走，与运行用户无关
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/app \
    HOST=0.0.0.0 \
    PORT=7860 \
    DATA_DIR=/app/.runtime \
    LOG_DIR=/app/logs \
    POWERCONTEXT_AUTOSTART=1

WORKDIR /app

# 依赖单独一层，改代码不会触发重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY tests ./tests
COPY data ./data
COPY docs ./docs
COPY README.md .env.example ./

# 语料快照不随仓库分发（data/docs 已 gitignore）：构建上下文里没有就现抓一次
RUN if ! ls data/docs/*.md >/dev/null 2>&1; then \
        echo "[info] data/docs 为空：构建期从语雀抓取（scripts/fetch_docs.py）"; \
        python scripts/fetch_docs.py; \
    else \
        echo "[info] 使用构建上下文里的 data/docs 快照"; \
    fi

# 预热 all-MiniLM-L6-v2（86MB）：构建机没有外网时跳过，运行期按 KB_EMBEDDING=auto 再探测，
# 探测失败会自动降级为纯全文检索，服务照常可用。
RUN python scripts/prep_embedding.py \
    || echo "[warn] embedding 模型未预热：运行期将自行探测，拉不到则降级为纯全文检索"

# 把 59 篇文档的索引烘进镜像，容器起来即可提问；挂载空 DATA_DIR 时由 start.sh 现场增量导入。
# 想让镜像更小/构建更快：docker build --build-arg SAITONG_BUILD_INGEST=0 .
ARG SAITONG_BUILD_INGEST=1
RUN if [ "$SAITONG_BUILD_INGEST" = "1" ]; then \
        python scripts/ingest.py --quiet; \
    else \
        echo "[info] 跳过构建期导入，首次启动时由 scripts/start.sh 导入"; \
    fi \
    && mkdir -p "$DATA_DIR" "$LOG_DIR"

# 只有单进程：seekdb 嵌入式 client 和 PowerContext 子进程都按进程持有，勿开 --workers
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ.get('PORT','7860')+'/healthz', timeout=5).read()"

CMD ["bash", "scripts/start.sh"]
