# 赛题通 · 魔搭（ModelScope）创空间 Docker 部署镜像
#
# 与仓库根的 Dockerfile 唯一的差别：代码不在构建上下文里，而是构建时从 Git 仓库克隆，
# 这样创空间只需要放这个 Dockerfile（+ README/ms_deploy.json）就能起服务。
#
#   docker build -f deploy/modelscope/Dockerfile -t saitong-ms .
#   docker run -d -p 7860:7860 -v $PWD/.ms-runtime:/app/.runtime saitong-ms
#
# 换自己的 fork：docker build --build-arg REPO_URL=https://github.com/<你>/saitong.git ...
FROM python:3.13-slim-bookworm

ARG REPO_URL=https://github.com/timbermanQAQ/saitong.git
ARG REPO_REF=main

# git：克隆仓库；libstdc++6 / libgcc-s1：libseekdb.so 与 onnxruntime 的运行库
# ca-certificates：克隆、调用 LLM 接口、抓语雀；tzdata：截止时间按 +08:00 计算
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git libstdc++6 libgcc-s1 tzdata \
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

RUN git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" /app
WORKDIR /app

RUN pip install --no-cache-dir -r requirements.txt

# 语雀快照没进仓库时（fork 时删掉了 data/docs），构建期现抓一次；抓不到就带着空知识库起服务
RUN if ! ls data/docs/*.md >/dev/null 2>&1; then \
        echo "[info] 仓库里没有 data/docs，改用 scripts/fetch_docs.py 从语雀抓取"; \
        python scripts/fetch_docs.py || echo "[warn] 语雀抓取失败：知识库会是空的"; \
    else \
        echo "[info] 使用仓库内的 data/docs 快照"; \
    fi

# 预热 all-MiniLM-L6-v2（86MB）：构建机没有外网时跳过，运行期按 KB_EMBEDDING=auto 再探测，
# 探测失败会自动降级为纯全文检索，服务照常可用。
RUN python scripts/prep_embedding.py \
    || echo "[warn] embedding 模型未预热：运行期将自行探测，拉不到则降级为纯全文检索"

# 把文档索引烘进镜像，容器起来即可提问；挂载空 DATA_DIR 时由 start.sh 现场增量导入。
# 想让镜像更小/构建更快：docker build --build-arg SAITONG_BUILD_INGEST=0 ...
ARG SAITONG_BUILD_INGEST=1
RUN if [ "$SAITONG_BUILD_INGEST" = "1" ]; then \
        python scripts/ingest.py --quiet; \
    else \
        echo "[info] 跳过构建期导入，首次启动时由 scripts/start.sh 导入"; \
    fi \
    && mkdir -p "$DATA_DIR" "$LOG_DIR"

# 只有单进程：seekdb 嵌入式 client 和 PowerContext 子进程都按进程持有，勿开 --workers
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ.get('PORT','7860')+'/healthz', timeout=5).read()"

CMD ["bash", "scripts/start.sh"]
