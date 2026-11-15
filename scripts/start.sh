#!/usr/bin/env bash
# 赛题通容器入口：准备目录 → 增量导入知识库 → 前台启动 uvicorn。
#
# PowerContext 子进程由应用自己拉起（app/main.py + POWERCONTEXT_AUTOSTART），
# 这里不再重复启动，避免两个进程抢同一个端口。
#
# 可用环境变量见 .env.example；本地直接跑：
#   PYTHON=/home/claude/mmvenv/bin/python bash scripts/start.sh
set -Eeuo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."

PYTHON="${PYTHON:-python}"
export PORT="${PORT:-7860}"
export HOST="${HOST:-0.0.0.0}"
DATA_DIR="${DATA_DIR:-.runtime}"
LOG_DIR="${LOG_DIR:-logs}"
export DATA_DIR LOG_DIR

log() { printf '[start.sh] %s\n' "$*"; }
fail() { printf '[start.sh] %s\n' "$*" >&2; }

mkdir -p "$DATA_DIR" "$LOG_DIR" "${POWERCONTEXT_DATA_DIR:-$DATA_DIR/powercontext}"

# 语料快照不随仓库分发：没有 data/docs 就先从语雀抓（ingest 里也有同样的兜底，这里只为把话说明白）
DOCS_DIR="${DOCS_DIR:-data/docs}"
if ! ls "$DOCS_DIR"/*.md >/dev/null 2>&1; then
    log "$DOCS_DIR 为空：先跑 scripts/fetch_docs.py 从语雀抓取（约 2 分钟）"
    "$PYTHON" scripts/fetch_docs.py --if-missing \
        || fail "语料抓取失败（容器可能没有外网？）：知识库会是空的，可在有网机器上抓好后挂进 $DOCS_DIR"
fi

if [[ "${SKIP_INGEST:-0}" == "1" ]]; then
    log "SKIP_INGEST=1：跳过导入（索引若不存在，检索会是空的）"
else
    log "导入 data/docs → seekdb（按内容哈希增量，未变化时几秒）"
    "$PYTHON" scripts/ingest.py --quiet || fail "导入失败：检索将不可用，先修复后再重启"
fi

# 只为把「向量检索 or 纯全文」写进启动日志，失败不影响服务启动
log "embedding：$("$PYTHON" scripts/prep_embedding.py --check 2>/dev/null || echo '探测失败，按 KB_EMBEDDING=auto 运行期再试')"

# 单进程：seekdb 嵌入式 client 与 PowerContext 子进程都按进程持有，别加 --workers
log "启动 FastAPI：http://$HOST:$PORT  （PowerContext 日志：$LOG_DIR/powercontext_server.log）"
exec "$PYTHON" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --log-level info
