#!/usr/bin/env python3
"""把 data/docs 导入 seekdb 知识库（按内容哈希增量更新）。

语料快照不随仓库分发：data/docs 为空时先自动跑 scripts/fetch_docs.py 从语雀抓取（约 2 分钟）。

用法：
    python scripts/ingest.py                                  # 增量导入
    python scripts/ingest.py --rebuild                        # 清库重建
    python scripts/ingest.py --dry-run                        # 只切块不写库
    python scripts/ingest.py --query "MiniMax 题要提交什么" --tracks MiniMax
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.kb import ALL_TRACKS, KnowledgeBase, build_chunks, infer_track, read_doc  # noqa: E402
from scripts.fetch_docs import ensure_docs, has_docs  # noqa: E402


def ensure_corpus(docs_dir: Path) -> int:
    """快照不随仓库分发：data/docs 空着就先从语雀抓一次；抓不到给出可操作的报错，返回退出码。"""
    if has_docs(docs_dir):
        return 0
    print(f"[ingest] {docs_dir} 里没有文档（语料快照不入库，按需抓取），尝试从语雀抓取…", file=sys.stderr)
    if ensure_docs(docs_dir, docs_dir.parent / "manifest.json"):
        return 0
    print("[ingest] 抓取失败，可选办法：\n"
          "  1) 在有外网的机器上跑 `python scripts/fetch_docs.py`，把生成的 data/docs 带过来；\n"
          f"  2) 用 --docs <目录> 指向已有的 Markdown 快照；\n"
          f"  3) 容器里挂卷：-v $PWD/data/docs:/app/data/docs。", file=sys.stderr)
    return 2


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="赛题通知识库导入脚本")
    parser.add_argument("--docs", default=None, help="Markdown 目录，默认取 DOCS_DIR / data/docs")
    parser.add_argument("--data-dir", default=None, help="seekdb 数据根目录，默认取 DATA_DIR / .runtime")
    parser.add_argument("--collection", default=None, help="collection 名，默认 KB_COLLECTION / saitong_kb")
    parser.add_argument("--max-chunk-chars", type=int, default=None, help="单块最大字符数，默认 1200")
    parser.add_argument("--rebuild", action="store_true", help="删除 collection 后全量重建")
    parser.add_argument("--dry-run", action="store_true", help="只解析与切块，不写 seekdb")
    parser.add_argument("--query", default=None, help="导入后立即执行一次检索（验证用）")
    parser.add_argument("--tracks", nargs="*", default=None, help="检索时限定赛道，如 MiniMax AMD")
    parser.add_argument("--top-k", type=int, default=5, help="检索返回条数")
    parser.add_argument("--quiet", action="store_true", help="只打印结果摘要")
    return parser.parse_args(argv)


def dry_run(docs_dir: Path, max_chars: int) -> int:
    stems = sorted(docs_dir.glob("*.md"))
    if not stems:
        print(f"未找到文档：{docs_dir}", file=sys.stderr)
        return 1
    total = 0
    tracks: dict[str, int] = {}
    for path in stems:
        doc = read_doc(path)
        chunks = build_chunks(doc, max_chars=max_chars)
        total += len(chunks)
        for chunk in chunks:
            track = str(chunk.metadata["track"])
            tracks[track] = tracks.get(track, 0) + 1
        print(f"  {doc.file_name:<32} {len(chunks):>3} 块  [{infer_track(doc.path, title=doc.title)}] {doc.title}")
    print(f"\n[dry-run] {len(stems)} 篇文档 → {total} 块，未写入 seekdb")
    print("[dry-run] 赛道分布：" + ", ".join(f"{k}:{v}" for k, v in sorted(tracks.items(), key=lambda kv: -kv[1])))
    return 0


def show_search(kb: KnowledgeBase, query: str, tracks: list[str] | None, top_k: int) -> None:
    unknown = [t for t in (tracks or []) if t not in ALL_TRACKS]
    if unknown:
        print(f"注意：以下赛道名不在推断结果中，过滤后可能为空 → {unknown}", file=sys.stderr)
    hits = kb.search(query, tracks=tracks, top_k=top_k)
    print(f"\n检索：{query}" + (f"  赛道={tracks}" if tracks else ""))
    if not hits:
        print("  无命中")
        return
    for rank, hit in enumerate(hits, 1):
        print(f"  {rank}. [{hit.score if hit.score is not None else float('nan'):.4f}] "
              f"{hit.track} · {hit.doc_title} › {hit.heading}  ({hit.id})")
        print(f"     {hit.snippet(160)}")
        print(f"     {hit.source_url}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    kb = KnowledgeBase(
        docs_dir=Path(args.docs) if args.docs else None,
        data_dir=Path(args.data_dir) if args.data_dir else None,
        collection=args.collection,
        max_chunk_chars=args.max_chunk_chars,
    )
    if (code := ensure_corpus(kb.docs_dir)):
        return code
    if args.dry_run:
        return dry_run(kb.docs_dir, kb.max_chunk_chars)

    progress = None if args.quiet else (lambda message: print(message, file=sys.stderr))
    print(f"导入 {kb.docs_dir} → {kb.seekdb_path} (collection={kb.collection_name})")
    report = kb.ingest(rebuild=args.rebuild, progress=progress)
    print(report.summary())
    print(f"现存块数：{kb.count()}")

    if args.query:
        show_search(kb, args.query, args.tracks, args.top_k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
