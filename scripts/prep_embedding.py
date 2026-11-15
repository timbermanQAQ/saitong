"""把默认 embedding 模型（all-MiniLM-L6-v2 ONNX）预热到本地缓存。

Docker 构建期调用一次，模型就烘进镜像层；容器里没有外网时也不会现下载。
构建环境同样离线时会失败：`|| true` 让镜像继续构建，运行期由 kb.py 降级为纯全文检索。

用法：
    python scripts/prep_embedding.py           # 预热并打印结果
    python scripts/prep_embedding.py --check   # 只检查，不触发下载
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.kb import (  # noqa: E402
    EMBEDDING_MODEL,
    MODE_HYBRID,
    default_embedding_function,
    embedding_state,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="预热 seekdb 默认 embedding 模型")
    parser.add_argument("--check", action="store_true", help="只做一次探测，不额外样例编码")
    args = parser.parse_args(argv)

    cfg = get_settings()
    started = time.perf_counter()
    if args.check:
        state = embedding_state(cfg)
        ok = state["mode"] == MODE_HYBRID
        print(f"embedding 状态：{state['mode']}（model={state['model']}"
              f"{'，detail=' + str(state['detail']) if state['detail'] else ''}）")
        return 0 if ok else 1

    try:
        function = default_embedding_function()
        vectors = function(["赛题通：MiniMax 题要提交什么"])
    except Exception as exc:  # 构建环境离线
        print(f"[warn] 模型预热失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("[warn] 运行期将降级为纯全文检索（KB_EMBEDDING=auto），或显式设 fallback", file=sys.stderr)
        return 1
    print(f"模型 {EMBEDDING_MODEL} 已就绪：dim={len(vectors[0])}，"
          f"耗时 {time.perf_counter() - started:.1f}s，缓存目录 ~/.cache/pyseekdb/onnx_models")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
