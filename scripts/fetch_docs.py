"""从语雀知识库抓取「2026年OceanBase数据库大赛」目录下的文档，产出与 data/docs 一致的 Markdown 快照。

用法（默认写入 data/docs 并更新 data/manifest.json）：

    python scripts/fetch_docs.py                       # 抓全量
    python scripts/fetch_docs.py --out-dir .tmp/docs --manifest .tmp/manifest.json
    python scripts/fetch_docs.py --root "2026年OceanBase数据库大赛" --sleep 0.4 --limit 5
    python scripts/fetch_docs.py --if-missing   # 已有文档就什么都不做（start.sh / Dockerfile 用）

`ensure_docs()` 是同一件事的函数入口：`scripts/ingest.py` 发现 data/docs 为空时调它现抓。

抓取方式（公开知识库，无需 Token）：
  1. 目录：知识库首页 HTML 里内嵌 `window.appData = JSON.parse(decodeURIComponent("..."))`，
     其中 book.toc 是扁平列表（uuid/parent_uuid/type/title/url/doc_id/level）。
  2. 正文：每篇文档 `<doc_url>/markdown?plain=true&linebreak=false&anchor=false` 直接返回 Markdown。

取第一个顶层节点（默认「2026年OceanBase数据库大赛」，它自身也是一篇文档）之下、下一个顶层节点
之前的文档；没有正文的目录页（如「初赛部分」）跳过。相邻请求之间按 --sleep（默认 0.4 秒）限速。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.kb import infer_track  # noqa: E402  （需在 sys.path 调整之后导入）

UA = "Mozilla/5.0 (X11; Linux x86_64) saitong-fetch-docs/1.0"
APP_DATA_RE = re.compile(r'window\.appData\s*=\s*JSON\.parse\(\s*decodeURIComponent\(\s*"((?:[^"\\]|\\.)*)"\s*\)')
MD_QUERY = {"plain": "true", "linebreak": "false", "anchor": "false"}
DEFAULT_BOOK_URL = "https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio"
DEFAULT_ROOT = "2026年OceanBase数据库大赛"
DOCS_DIR = ROOT / "data" / "docs"
MANIFEST_PATH = ROOT / "data" / "manifest.json"


class FetchError(RuntimeError):
    pass


def _get(url: str, *, timeout: float, referer: str = "") -> str:
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            charset = res.headers.get_content_charset() or "utf-8"
            return res.read().decode(charset, "replace")
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} {url}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchError(f"{type(exc).__name__}: {exc} {url}") from exc


def fetch_toc(book_url: str, *, timeout: float = 30.0) -> list[dict[str, Any]]:
    """读知识库首页，解出 book.toc（扁平列表，保持目录顺序）。"""
    html = _get(book_url.rstrip("/") + "/", timeout=timeout)
    match = APP_DATA_RE.search(html)
    if not match:
        raise FetchError("首页里没有 window.appData JSON.parse(decodeURIComponent(...))，页面结构可能已变")
    payload = json.loads(urllib.parse.unquote(match.group(1)))
    toc = _dig(payload, "book", "toc")
    if isinstance(toc, str):  # 语雀把 toc 又存成了一次 JSON 字符串
        toc = json.loads(toc)
    if not isinstance(toc, list) or not toc:
        raise FetchError("book.toc 为空或结构不符")
    return toc


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def select_docs(toc: list[dict[str, Any]], root_title: str, *, include_root: bool = True) -> list[dict[str, Any]]:
    """取第一个 `root_title` 顶层节点之下的 DOC 条目，遇到下一个顶层节点即停止。

    顶层节点自身也是一篇有正文的文档（现有快照的 00 号文件），默认一并收进来。
    """
    start: int | None = None
    for index, node in enumerate(toc):
        if int(node.get("level") or 0) != 0:
            continue
        if start is None:
            if str(node.get("title") or "").strip() == root_title:
                start = index
        else:
            return _docs_in(toc, start, index, include_root)
    if start is None:
        raise LookupError(f"目录里没有顶层节点「{root_title}」")
    return _docs_in(toc, start, len(toc), include_root)


def _docs_in(toc: list[dict[str, Any]], start: int, stop: int, include_root: bool) -> list[dict[str, Any]]:
    return [item for item in toc[start if include_root else start + 1:stop] if item.get("type") == "DOC"]


def build_path(toc: list[dict[str, Any]], node: dict[str, Any]) -> str:
    """按 parent_uuid 回溯拼出「顶层 / … / 自己」形式的语雀层级路径。"""
    by_uuid = {str(item.get("uuid")): item for item in toc}
    parts: list[str] = []
    current: dict[str, Any] | None = node
    while current is not None and len(parts) < 20:
        parts.append(str(current.get("title") or "").strip())
        current = by_uuid.get(str(current.get("parent_uuid") or ""))
    return " / ".join(part for part in reversed(parts) if part)


def fetch_markdown(doc_url: str, *, timeout: float = 30.0) -> str:
    """取纯 Markdown 正文；目录性的空页（如「初赛部分」）返回空串。"""
    url = doc_url.rstrip("/") + "/markdown?" + urllib.parse.urlencode(MD_QUERY)
    return _get(url, timeout=timeout, referer=doc_url.rstrip("/") + "/")


def render_doc(*, index: int, title: str, path: str, source_url: str, body: str) -> tuple[str, str]:
    """返回 (文件名, 文件内容)，格式与 data/docs 现有快照一致。"""
    slug = source_url.rstrip("/").rsplit("/", 1)[-1]
    front = "\n".join(["---", f"title: {title}", f"path: {path}", f"source_url: {source_url}", "---"])
    name = f"{index:02d}_{slug}.md"
    return name, f"{front}\n\n{body.strip()}\n"


def fetch_book(book_url: str, *, root_title: str, timeout: float, sleep: float,
               limit: int = 0, log=print) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """抓全量正文，返回 (文档条目列表, 跳过记录)。条目含 name/content/title/path/source_url。"""
    toc = fetch_toc(book_url, timeout=timeout)
    wanted = select_docs(toc, root_title)
    if limit:
        wanted = wanted[:limit]
    log(f"目录「{root_title}」下有 {len(wanted)} 篇文档，开始抓取（间隔 {sleep}s）")
    docs: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for position, node in enumerate(wanted):
        slug = str(node.get("url") or "").strip()
        title = str(node.get("title") or "").strip() or slug
        if not slug:
            skipped.append({"title": title, "reason": "目录条目没有 url"})
            continue
        url = f"{book_url.rstrip('/')}/{slug}"
        try:
            body = fetch_markdown(url, timeout=timeout).strip()
        except FetchError as exc:
            skipped.append({"title": title, "reason": str(exc)})
            log(f"  跳过（抓取失败）：{title} · {exc}")
            time.sleep(sleep)
            continue
        if not body:
            skipped.append({"title": title, "reason": "文档没有正文"})
            log(f"  跳过（无正文）：{title}")
            time.sleep(sleep)
            continue
        path = build_path(toc, node)
        name, content = render_doc(index=len(docs), title=title, path=path, source_url=url, body=body)
        docs.append({"name": name, "content": content, "title": title, "chars": len(body),
                     "path": path, "source_url": url})
        log(f"  [{len(docs):>2}] {name}  {title}  {len(body)} 字")
        time.sleep(sleep)
    return docs, skipped


def write_output(docs: list[dict[str, str]], skipped: list[dict[str, str]], *,
                 out_dir: Path, manifest: Path, root_title: str, book_url: str, log=print) -> None:
    """写快照与清单；manifest.json 保持与既有一致的数组格式（file/title/path/source_url/chars）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in sorted(out_dir.glob("*.md")):
        existing.unlink()
    tracks: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for doc in docs:
        (out_dir / doc["name"]).write_text(doc["content"], encoding="utf-8")
        track = infer_track(doc["path"], title=doc["title"])
        tracks[track] = tracks.get(track, 0) + 1
        rows.append({"file": doc["name"], "title": doc["title"], "path": doc["path"],
                     "source_url": doc["source_url"], "chars": doc["chars"]})
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log(f"写入 {len(rows)} 篇到 {out_dir}，清单 {manifest}（跳过 {len(skipped)} 篇）")
    for item in skipped:
        log(f"  未收录：{item['title']} —— {item['reason']}")
    log(f"来源 {book_url.rstrip('/')} 根节点「{root_title}」 · {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    log("赛道分布：" + ", ".join(f"{k}:{v}" for k, v in sorted(tracks.items(), key=lambda kv: -kv[1])))


def has_docs(docs_dir: Path) -> bool:
    """目录里有任意 Markdown 就算有语料（快照不随仓库分发，各处用它判断是否需要现抓）。"""
    return any(docs_dir.glob("*.md"))


def ensure_docs(docs_dir: Path = DOCS_DIR, manifest: Path = MANIFEST_PATH, *,
                book_url: str = DEFAULT_BOOK_URL, root_title: str = DEFAULT_ROOT,
                timeout: float = 30.0, sleep: float = 0.4, quiet: bool = False) -> bool:
    """语料缺失时抓一次；已有则直接返回 True。抓取失败返回 False，由调用方决定怎么报错。"""
    if has_docs(docs_dir):
        return True

    def log(message: str) -> None:
        if not quiet:
            print(message)

    log(f"[fetch_docs] {docs_dir} 里没有文档，从语雀抓取：{book_url}")
    try:
        docs, skipped = fetch_book(book_url, root_title=root_title, timeout=timeout, sleep=sleep, log=log)
    except (FetchError, LookupError) as exc:
        log(f"[fetch_docs] 抓取失败：{exc}")
        return False
    if not docs:
        log("[fetch_docs] 抓到 0 篇，视为失败")
        return False
    write_output(docs, skipped, out_dir=docs_dir, manifest=manifest,
                 root_title=root_title, book_url=book_url, log=log)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取语雀赛题文档到 data/docs")
    parser.add_argument("--book-url", default=DEFAULT_BOOK_URL)
    parser.add_argument("--root", default=DEFAULT_ROOT, help="只取这个顶层节点下的文档")
    parser.add_argument("--out-dir", default=str(DOCS_DIR))
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument("--if-missing", action="store_true",
                        help="目录里已有文档就什么都不做（给 start.sh / Dockerfile 当前置检查用）")
    parser.add_argument("--limit", type=int, default=0, help="只抓前 N 篇（调试用，不影响文件名序号规则）")
    parser.add_argument("--sleep", type=float, default=0.4, help="请求间隔秒数")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    out_dir, manifest = Path(args.out_dir), Path(args.manifest)
    if args.if_missing:
        if has_docs(out_dir):
            if not args.quiet:
                print(f"[fetch_docs] {out_dir} 已有文档，跳过抓取")
            return 0
        return 0 if ensure_docs(out_dir, manifest, book_url=args.book_url, root_title=args.root,
                                timeout=args.timeout, sleep=args.sleep, quiet=args.quiet) else 1

    def log(message: str) -> None:
        if not args.quiet:
            print(message)

    try:
        docs, skipped = fetch_book(args.book_url, root_title=args.root, timeout=args.timeout,
                                   sleep=args.sleep, limit=args.limit, log=log)
    except (FetchError, LookupError) as exc:
        print(f"抓取失败：{exc}", file=sys.stderr)
        return 1
    if not docs:
        print("一篇都没抓到，不写入输出目录", file=sys.stderr)
        return 1
    write_output(docs, skipped, out_dir=out_dir, manifest=manifest,
                 root_title=args.root, book_url=args.book_url, log=log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
