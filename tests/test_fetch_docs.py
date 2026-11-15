"""scripts/fetch_docs.py 的解析/排版测试（全部离线，不碰网络）。

重点是产出格式与 data/docs 现有快照逐字节一致——序号、文件名、front matter 任何一处跑偏，
ingest 的赛道推断和增量哈希都会安静地变味。
"""

from __future__ import annotations

import importlib.util
import json
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FETCH_PATH = ROOT / "scripts" / "fetch_docs.py"

TOC = [
    {"uuid": "a", "parent_uuid": "", "title": "2026年OceanBase数据库大赛", "type": "DOC",
     "url": "ix20uoszxhifnp4b", "level": 0},
    {"uuid": "b", "parent_uuid": "a", "title": "FAQ - 大赛常见问题答疑", "type": "DOC",
     "url": "gp48z4pp0q3zg9b2", "level": 1},
    {"uuid": "c", "parent_uuid": "b", "title": "算分规则", "type": "DOC",
     "url": "dbf6uzmwsuupp7hn", "level": 2},
    {"uuid": "d", "parent_uuid": "a", "title": "初赛部分", "type": "DOC", "url": "pr6qt5z92ophfocf", "level": 1},
    {"uuid": "e", "parent_uuid": "", "title": "中国数据库暑期学校2026", "type": "DOC",
     "url": "bpnhxpu7c7zlx94g", "level": 0},
    {"uuid": "f", "parent_uuid": "e", "title": "不该被抓到", "type": "DOC", "url": "nope", "level": 1},
]


@pytest.fixture(scope="module")
def fd():
    spec = importlib.util.spec_from_file_location("saitong_fetch_docs", FETCH_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_select_docs_stops_at_next_top_level(fd) -> None:
    picked = fd.select_docs(TOC, "2026年OceanBase数据库大赛")
    assert [node["url"] for node in picked] == [
        "ix20uoszxhifnp4b", "gp48z4pp0q3zg9b2", "dbf6uzmwsuupp7hn", "pr6qt5z92ophfocf"]
    # 顶层节点自身也是一篇文档（现有快照的 00 号文件）
    assert picked[0]["title"] == "2026年OceanBase数据库大赛"
    assert all(node["url"] != "nope" for node in picked)
    assert fd.select_docs(TOC, "2026年OceanBase数据库大赛", include_root=False)[0]["url"] == "gp48z4pp0q3zg9b2"
    with pytest.raises(LookupError):
        fd.select_docs(TOC, "不存在的目录")


def test_build_path_follows_parent_uuid(fd) -> None:
    by_url = {node["url"]: node for node in TOC}
    assert fd.build_path(TOC, by_url["dbf6uzmwsuupp7hn"]) == \
        "2026年OceanBase数据库大赛 / FAQ - 大赛常见问题答疑 / 算分规则"


def test_render_doc_matches_committed_snapshot(fd) -> None:
    """拿真实的 data/docs/00_*.md 反推：文件名、front matter 必须逐字节一致。

    正文只保证 rstrip 后相同——语雀导出的旧快照尾部留了 2~3 个空行，脚本收敛成单个换行，
    对切块没有影响（chunk 文本本来就会 strip）。

    快照已不随仓库分发，干净克隆上这一例 skip（不是假通过：有语料的地方照跑）。
    """
    existing = sorted((ROOT / "data" / "docs").glob("00_*.md"))
    if not existing:
        pytest.skip("data/docs 快照不在本地，跑一次 scripts/fetch_docs.py 后即可校验格式")
    source = existing[0]
    text = source.read_text(encoding="utf-8")
    _, front, body = text.split("---\n", 2)
    meta = {}
    for line in front.strip().splitlines():
        key, _, value = line.partition(": ")
        meta[key] = value
    name, rendered = fd.render_doc(index=0, title=meta["title"], path=meta["path"],
                                   source_url=meta["source_url"], body=body.strip())
    assert name == source.name
    assert rendered.split("---\n", 2)[1] == front
    assert rendered.rstrip() == text.rstrip()


def test_fetch_toc_reads_app_data(fd, monkeypatch) -> None:
    payload = {"book": {"id": 1, "toc": TOC}}
    encoded = urllib.parse.quote(json.dumps(payload, ensure_ascii=False), safe="")
    html = f"<html><script>window.appData = JSON.parse(decodeURIComponent(\"{encoded}\"));</script></html>"
    monkeypatch.setattr(fd, "_get", lambda url, **kwargs: html)
    toc = fd.fetch_toc("https://example.com/book")
    assert [node["uuid"] for node in toc] == ["a", "b", "c", "d", "e", "f"]


def test_fetch_toc_accepts_toc_as_json_string(fd, monkeypatch) -> None:
    """语雀有时把 toc 再包一层 JSON 字符串。"""
    payload = {"book": {"toc": json.dumps(TOC, ensure_ascii=False)}}
    encoded = urllib.parse.quote(json.dumps(payload, ensure_ascii=False), safe="")
    html = f'<script>window.appData = JSON.parse(decodeURIComponent("{encoded}"));</script>'
    monkeypatch.setattr(fd, "_get", lambda url, **kwargs: html)
    assert len(fd.fetch_toc("https://example.com/book")) == len(TOC)


def test_fetch_toc_fails_loudly_when_page_layout_changes(fd, monkeypatch) -> None:
    monkeypatch.setattr(fd, "_get", lambda url, **kwargs: "<html>没有 appData</html>")
    with pytest.raises(fd.FetchError):
        fd.fetch_toc("https://example.com/book")


# --------------------------------------------------------------------------- #
# 语料缺失时的自动抓取（data/docs 不再随仓库分发，这条是主路径）
# --------------------------------------------------------------------------- #

def test_has_docs(tmp_path, fd) -> None:
    assert fd.has_docs(tmp_path / "docs") is False          # 目录都不存在
    (tmp_path / "docs").mkdir()
    assert fd.has_docs(tmp_path / "docs") is False
    (tmp_path / "docs" / "00_x.md").write_text("# x", encoding="utf-8")
    assert fd.has_docs(tmp_path / "docs") is True


def test_ensure_docs_does_not_refetch_when_docs_exist(fd, tmp_path, monkeypatch) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "00_a.md").write_text("已有快照", encoding="utf-8")

    def boom(*args, **kwargs):
        raise AssertionError("有语料时不应该发网络请求")

    monkeypatch.setattr(fd, "fetch_book", boom)
    assert fd.ensure_docs(docs, tmp_path / "manifest.json") is True


def test_ensure_docs_returns_false_on_fetch_error(fd, tmp_path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise fd.FetchError("HTTP 403 https://example.com")

    monkeypatch.setattr(fd, "fetch_book", fail)
    assert fd.ensure_docs(tmp_path / "docs", tmp_path / "manifest.json", quiet=True) is False


def test_ensure_docs_writes_snapshot_on_success(fd, tmp_path, monkeypatch) -> None:
    rows = [{"name": "00_a.md", "content": "---\ntitle: A\npath: P\nsource_url: U\n---\n\n正文\n",
             "title": "A", "chars": 2, "path": "P", "source_url": "U"}]
    monkeypatch.setattr(fd, "fetch_book", lambda *a, **k: (rows, []))
    docs, manifest = tmp_path / "docs", tmp_path / "manifest.json"
    assert fd.ensure_docs(docs, manifest, quiet=True) is True
    assert (docs / "00_a.md").read_text(encoding="utf-8").startswith("---")
    assert json.loads(manifest.read_text(encoding="utf-8"))[0]["file"] == "00_a.md"


def test_ingest_ensure_corpus_codes(tmp_path, monkeypatch) -> None:
    """ingest 的返回码：0=可以继续导入，2=抓不到并给出办法。"""
    from scripts import ingest

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "00_a.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(ingest, "ensure_docs", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    assert ingest.ensure_corpus(docs) == 0

    empty = tmp_path / "empty"
    monkeypatch.setattr(ingest, "ensure_docs", lambda *a, **k: True)
    assert ingest.ensure_corpus(empty) == 0
    monkeypatch.setattr(ingest, "ensure_docs", lambda *a, **k: False)
    assert ingest.ensure_corpus(empty) == 2
