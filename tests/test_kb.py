"""M1 知识库测试：解析、赛道推断、切块、增量导入、混合检索与检索日志。

引擎相关用例共用一个模块级 seekdb 实例（嵌入式启动约几秒，避免每例重建）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import audit
from app.config import PROJECT_ROOT, Settings, reset_settings
from app.kb import (
    ALL_TRACKS,
    MODE_FULLTEXT,
    MODE_HYBRID,
    FallbackEmbeddingFunction,
    KnowledgeBase,
    build_chunks,
    build_ft_terms,
    clean_body,
    embedding_state,
    infer_track,
    overlap_terms,
    pack_text,
    parse_markdown,
    reset_embedding_state,
    split_sections,
)

ROOT = "2026年OceanBase数据库大赛"
MINIMAX_DOC = {
    "name": "aa_minimax.md",
    "title": "MiniMax",
    "path": f"{ROOT} / 赛题 / 初赛部分 / MiniMax",
    "url": "https://example.test/minimax",
    "body": """
> 注意：为方便判题，提交时的分支/commit hash 基于测评平台指定分支。

MiniMax 赛题要求实现一个 SDD 上下文模块。

## 一、题目背景
AI 开发助手修改代码时需要知道当前应遵守哪些要求。

## 二、题目内容
### 2.1 完成一次开发任务
选择一个能运行、有测试的业务工程，定义并完成一项局部修改。

### 2.2 技术结合要求
| 技术 | 必须承担的作用 |
| --- | --- |
| MiniMax | 参与代码生成或修改 |
| seekdb | 存储工程资料或索引 |

## 三、提交材料
作品材料放在参赛仓库的 `minimax/` 目录。评审以平台记录的固定提交为准。

| 材料 | 要求 |
| --- | --- |
| 完整源码 | 上下文模块与接入代码 |
| 运行说明 | 依赖版本、配置步骤、测试命令 |
| 验证样例 | 任务描述、操作步骤、预期结果 |
| 技术使用记录 | MiniMax 开发记录、数据库查询记录 |

## 四、评分与测试
### 4.1 必做项
以下六项全部通过得 20 分，任一项不通过得 0 分。
""",
}

AMD_DOC = {
    "name": "bb_amd.md",
    "title": "AMD",
    "path": f"{ROOT} / 赛题 / 初赛部分 / AMD",
    "url": "https://example.test/amd",
    "body": """
## 环境与部署
在 AMD GPU 机器上部署 seekdb：

```bash
## 这一行在代码块里，不是章节标题
docker run -d --name seekdb oceanbase/seekdb:latest
```

### 提交方式
把分支名和 commit hash 填到测评平台，产物放到 amd 目录。
""",
}

FAQ_DOC = {
    "name": "cc_faq.md",
    "title": "FAQ - 大赛常见问题答疑",
    "path": f"{ROOT} / FAQ - 大赛常见问题答疑",
    "url": "https://example.test/faq",
    "body": """
## 组队
一支队伍最多 5 人，队名提交后不可修改。

## 提交
可以多次提交，评审以平台记录的固定提交为准。
""",
}

SEEKDB_DOC = {
    "name": "dd_seekdb.md",
    "title": "赛题四：全文索引调优",
    "path": f"{ROOT} / 赛题 / 初赛部分 / OceanBase seekdb内核 / 赛题四：全文索引调优",
    "url": "https://example.test/seekdb-ft",
    "body": """
## 题目内容
调优 seekdb 全文索引，提升召回与吞吐。

## 提交材料
提交代码、运行说明与测试结果，放到 seekdb 目录。
""",
}

CARD_DOC = {
    "name": "ee_card.md",
    "title": "企业第八讲：MiniMax",
    "path": f"{ROOT} / 课程 / 企业第八讲：MiniMax",
    "url": "https://example.test/card",
    "body": "[此处为语雀卡片，点击链接查看](https://www.yuque.com/docs/286642472#KgO2W)",
}

CORPUS = [MINIMAX_DOC, AMD_DOC, FAQ_DOC, SEEKDB_DOC, CARD_DOC]


def write_doc(directory: Path, spec: dict) -> Path:
    text = (
        f"---\ntitle: {spec['title']}\npath: {spec['path']}\n"
        f"source_url: {spec['url']}\n---\n\n{spec['body'].strip()}\n"
    )
    path = directory / spec["name"]
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def corpus_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "docs"
    directory.mkdir()
    for spec in CORPUS:
        write_doc(directory, spec)
    return directory


@pytest.fixture()
def doc(corpus_dir: Path):
    return parse_markdown((corpus_dir / MINIMAX_DOC["name"]).read_text(encoding="utf-8"),
                          file_name=MINIMAX_DOC["name"])


# --------------------------------------------------------------------------- #
# 纯函数：解析与赛道
# --------------------------------------------------------------------------- #

def test_parse_markdown_reads_front_matter(corpus_dir: Path) -> None:
    parsed = parse_markdown((corpus_dir / MINIMAX_DOC["name"]).read_text(encoding="utf-8"),
                            file_name=MINIMAX_DOC["name"])
    assert parsed.title == "MiniMax"
    assert parsed.stem == "aa_minimax"
    assert parsed.path.endswith("MiniMax")
    assert parsed.source_url == "https://example.test/minimax"
    assert not parsed.body.startswith("---")
    assert "三、提交材料" in parsed.body


def test_parse_markdown_without_front_matter() -> None:
    parsed = parse_markdown("只有正文的文档\n", file_name="zz_notes.md")
    assert parsed.title == "zz_notes"
    assert parsed.path == "zz_notes"
    assert parsed.source_url == ""


def test_clean_body_drops_html_noise() -> None:
    cleaned = clean_body("提交<font style=\"color:#DF2A3F;\">注意</font>事项<br>见图 ![图](http://x/y.png)")
    assert "<font" not in cleaned and "注意" in cleaned
    assert "![" not in cleaned


def test_clean_body_drops_yuque_card_placeholder() -> None:
    assert clean_body("[此处为语雀卡片，点击链接查看](https://www.yuque.com/docs/286642472#KgO2W)") == ""


def test_card_only_doc_yields_no_chunks() -> None:
    parsed = parse_markdown(
        f"---\ntitle: {CARD_DOC['title']}\npath: {CARD_DOC['path']}\n"
        f"source_url: {CARD_DOC['url']}\n---\n\n{CARD_DOC['body']}\n",
        file_name=CARD_DOC["name"],
    )
    assert build_chunks(parsed) == []
    assert infer_track(parsed.path) == "MiniMax"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"{ROOT}", "通用"),
        (f"{ROOT} / FAQ - 大赛常见问题答疑", "通用"),
        (f"{ROOT} / 赛题", "通用"),
        (f"{ROOT} / 赛题 / 加赛部分 / 算分规则", "通用"),
        (f"{ROOT} / 赛题 / 初赛部分 / MiniMax", "MiniMax"),
        (f"{ROOT} / 赛题 / 初赛部分 / Qoder", "Qoder"),
        (f"{ROOT} / 赛题 / 初赛部分 / AMD", "AMD"),
        (f"{ROOT} / 赛题 / 初赛部分 / Dify", "Dify"),
        (f"{ROOT} / 赛题 / 初赛部分 / 高德地图", "高德地图"),
        (f"{ROOT} / 赛题 / 初赛部分 / 阶跃星辰", "阶跃星辰"),
        (f"{ROOT} / 赛题 / 初赛部分 / 魔搭社区", "魔搭社区"),
        (f"{ROOT} / 赛题 / 初赛部分 / PowerContext", "PowerContext"),
        (f"{ROOT} / 赛题 / 初赛部分 / OceanBase seekdb内核 / 赛题一：AI函数增强", "seekdb"),
        (f"{ROOT} / 赛题 / 初赛部分 / OceanBase seekdb基础事件 / 赛题二：GitHub bug修复", "seekdb"),
        # 生态名优先于通用技术名
        (f"{ROOT} / 赛题 / 初赛部分 / PowerContext / 赛题十一：seekdb 混合检索的单 SQL 候选查询", "PowerContext"),
        (f"{ROOT} / 分享 / AMD  seekdb 部署上报失败修复方案", "AMD"),
        (f"{ROOT} / 课程 / 企业第四讲：dify", "Dify"),
        (f"{ROOT} / 课程 / 企业第七讲：魔搭", "魔搭社区"),
        (f"{ROOT} / 课程 / 企业第八讲：MiniMax", "MiniMax"),
        (f"{ROOT} / 课程 / 第一讲：AI 原生数据库与 seekdb 基础", "seekdb"),
        (f"{ROOT} / 测试 / 算分规则", "通用"),
    ],
)
def test_infer_track(path: str, expected: str) -> None:
    assert infer_track(path) == expected
    assert expected in ALL_TRACKS


# --------------------------------------------------------------------------- #
# 纯函数：切块
# --------------------------------------------------------------------------- #

def test_split_sections_uses_h2_h3(doc) -> None:
    crumbs = [crumb for crumb, _ in split_sections(doc.body)]
    assert [] in crumbs, "第一个标题之前的前言也要成块"
    assert ["三、提交材料"] in crumbs
    assert ["四、评分与测试", "4.1 必做项"] in crumbs


def test_split_sections_ignores_heading_like_lines_inside_code_fence() -> None:
    body = "## 环境与部署\n\n```bash\n## 不是标题\n```\n\n### 提交方式\n填分支名。"
    sections = {">".join(crumb): text for crumb, text in split_sections(body)}
    assert set(sections) == {"环境与部署", "环境与部署>提交方式"}, "围栏内的 # 不应切出新章节"
    assert "## 不是标题" in sections["环境与部署"]
    assert "## 不是标题" not in sections["环境与部署"].split("\n")[0]


def test_build_chunks_metadata_and_ids(doc) -> None:
    chunks = build_chunks(doc, max_chars=1200)
    assert [chunk.id for chunk in chunks[:3]] == ["aa_minimax#000", "aa_minimax#001", "aa_minimax#002"]
    submission = next(chunk for chunk in chunks if chunk.metadata["heading"] == "三、提交材料")
    assert submission.metadata["track"] == "MiniMax"
    assert submission.metadata["source_url"] == "https://example.test/minimax"
    assert submission.metadata["doc_title"] == "MiniMax"
    assert submission.text.startswith("【MiniMax > 三、提交材料】")
    assert "运行说明" in submission.text
    assert len(submission.metadata["content_sha"]) == 16


def test_build_chunks_is_deterministic(doc) -> None:
    assert build_chunks(doc)[3].content_sha == build_chunks(doc)[3].content_sha


def test_pack_text_splits_long_section() -> None:
    body = "\n\n".join(f"第{i}段：" + "内容" * 120 for i in range(6))
    parts = pack_text(body, 500)
    assert len(parts) > 1
    assert all(len(part) <= 500 for part in parts)
    assert "\n\n".join(parts).replace("\n", "") == body.replace("\n", "")


def test_pack_text_hard_splits_single_long_paragraph() -> None:
    parts = pack_text("很" * 1200, 400)
    assert len(parts) >= 3
    assert all(len(part) <= 400 for part in parts)


# --------------------------------------------------------------------------- #
# 纯函数：全文检索词
# --------------------------------------------------------------------------- #

def test_build_ft_terms_mixes_ascii_and_lexicon() -> None:
    assert build_ft_terms("MiniMax 题要提交什么") == ["minimax", "提交"]


def test_build_ft_terms_prefers_longest_match() -> None:
    assert build_ft_terms("提交材料 有哪些要求") == ["提交材料", "要求"]


def test_build_ft_terms_empty_when_nothing_useful() -> None:
    assert build_ft_terms("怎么办啊") == []
    assert build_ft_terms("") == []


# --------------------------------------------------------------------------- #
# seekdb 引擎：导入 / 检索 / 日志
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("kb-engine")
    docs_dir = root / "docs"
    docs_dir.mkdir()
    for spec in CORPUS:
        write_doc(docs_dir, spec)
    settings = Settings(
        env={
            "DATA_DIR": str(root / "runtime"),
            "DOCS_DIR": str(docs_dir),
            "LOG_DIR": str(root / "logs"),
            "KB_COLLECTION": "saitong_kb_test",
        },
        root=PROJECT_ROOT,
    )
    reset_settings(settings)
    base = KnowledgeBase(
        settings=settings,
        docs_dir=docs_dir,
        data_dir=root / "runtime",
        collection="saitong_kb_test",
    )
    report = base.ingest()
    yield base, report, docs_dir, settings
    reset_settings(Settings(env={}, root=PROJECT_ROOT))


def test_first_ingest_reports_counts(engine) -> None:
    base, report, docs_dir, _ = engine
    assert report.docs == len(CORPUS)
    assert report.added == report.chunks > 0
    assert report.updated == 0 and report.unchanged == 0 and report.deleted == 0
    assert sum(report.tracks.values()) == report.chunks
    assert set(report.tracks) <= set(ALL_TRACKS)
    assert report.tracks["MiniMax"] >= 4 and report.tracks["通用"] >= 2
    assert report.skipped_docs == 1, "只有语雀卡片的文档不产生块"
    assert base.count() == report.chunks


def test_ingest_is_incremental_by_hash(engine) -> None:
    base, _, _, _ = engine
    again = base.ingest()
    assert again.added == 0 and again.updated == 0 and again.deleted == 0
    assert again.unchanged == again.chunks == base.count()


def test_ingest_log_line(engine) -> None:
    base, _, _, settings = engine
    rows = audit.read_tail("seekdb", limit=50, settings=settings)
    ingest_rows = [row for row in rows if row.get("event") == "ingest"]
    assert ingest_rows
    latest = ingest_rows[-1]
    assert latest["collection"] == "saitong_kb_test"
    assert latest["chunks"] == base.count()
    assert latest["elapsed_ms"] >= 0


def test_search_finds_submission_material(engine) -> None:
    base, _, _, _ = engine
    wanted = next(
        chunk for chunk in base.chunks_for()
        if chunk.metadata["heading"] == "三、提交材料"
    )
    hits = base.search("MiniMax 题要提交什么", top_k=5)
    assert hits, "混合检索不应为空"
    assert wanted.id in [hit.id for hit in hits]
    top = hits[0]
    assert top.source_url == "https://example.test/minimax"
    assert "运行说明" in top.text
    assert top.to_dict()["track"] == "MiniMax"


def test_search_filters_by_track(engine) -> None:
    base, _, _, _ = engine
    hits = base.search("提交材料 提交方式", tracks=["AMD"], top_k=5)
    assert hits
    assert {hit.track for hit in hits} == {"AMD"}
    assert all(hit.doc_title == "AMD" for hit in hits)


def test_search_track_filter_narrows_results(engine) -> None:
    base, _, _, _ = engine
    minimax = base.search("提交材料", tracks=["MiniMax"], top_k=10)
    seekdb = base.search("提交材料", tracks=["seekdb"], top_k=10)
    assert {hit.track for hit in minimax} == {"MiniMax"}
    assert {hit.track for hit in seekdb} == {"seekdb"}
    assert not {hit.id for hit in minimax} & {hit.id for hit in seekdb}


def test_search_falls_back_to_vector_only_without_terms(engine) -> None:
    base, _, _, settings = engine
    hits = base.search("怎么办啊", top_k=3)
    assert len(hits) <= 3
    row = audit.read_tail("seekdb", limit=1, settings=settings)[0]
    assert row["mode"] == "knn_only"
    assert row["where_document"] is None
    assert row["error"] is None


def test_search_drops_vector_only_noise(engine) -> None:
    """弱向量模型会把一个词都没命中的片段顶进来，这类噪声不返回给用户。"""
    base, _, _, settings = engine
    assert base.search("zzz-不存在的词-qqq", top_k=5) == []
    row = audit.read_tail("seekdb", limit=1, settings=settings)[0]
    assert row["mode"] == "hybrid"
    assert row["n_hits"] == 0 and row["dropped_no_overlap"] > 0


def test_overlap_terms_is_looser_than_ft_terms() -> None:
    assert build_ft_terms("提交材料有哪些") == ["提交材料"], "检索词保持最长覆盖，避免过宽"
    assert set(overlap_terms("提交材料有哪些")) == {"提交材料", "提交", "材料"}
    assert overlap_terms("怎么办啊") == []


def test_search_writes_query_log(engine) -> None:
    base, _, _, settings = engine
    hits = base.search("MiniMax 提交材料", tracks=["MiniMax"], top_k=2)
    row = audit.read_tail("seekdb", limit=1, settings=settings)[0]
    assert row["event"] == "hybrid_search"
    assert row["query"] == "MiniMax 提交材料"
    assert row["ft_terms"][:1] == ["minimax"] and "提交材料" in row["ft_terms"]
    assert row["where"] == {"track": {"$in": ["MiniMax"]}}
    assert row["where_document"]["$or"]
    assert row["tracks"] == ["MiniMax"]
    assert row["n_hits"] == len(hits) == len(row["matched_ids"])
    assert isinstance(row["elapsed_ms"], (int, float)) and row["elapsed_ms"] >= 0
    assert row["matched_ids"] == [hit.id for hit in hits]


def test_search_empty_query_is_logged_as_error(engine) -> None:
    base, _, _, settings = engine
    assert base.search("   ") == []
    row = audit.read_tail("seekdb", limit=1, settings=settings)[0]
    assert row["error"] == "empty_query"


def test_stats(engine) -> None:
    base, _, _, _ = engine
    stats = base.stats()
    assert stats["docs"] == len(CORPUS) - 1, "卡片文档没有块，不计入库内文档数"
    assert stats["chunks"] == base.count()
    assert stats["tracks"]["MiniMax"] >= 3
    assert "MiniMax" in stats["track_docs"]
    assert base.health()["ok"] is True


def test_update_and_delete_docs_only_touches_them(engine) -> None:
    base, _, docs_dir, _ = engine
    original = (docs_dir / MINIMAX_DOC["name"]).read_text(encoding="utf-8")
    (docs_dir / MINIMAX_DOC["name"]).write_text(original + "\n## 五、补充要求\n补交一页技术说明。\n", encoding="utf-8")

    report = base.ingest()
    assert report.added == 1, "新增一个标题只应多一块"
    assert report.updated == 0
    assert report.unchanged == report.chunks - 1

    # 改动已有章节 → 该块 updated，其它块不动
    (docs_dir / MINIMAX_DOC["name"]).write_text(
        original.replace("评审以平台记录的固定提交为准。", "评审以最后一次固定提交为准。"),
        encoding="utf-8",
    )
    second = base.ingest()
    assert second.updated == 1
    assert second.added == 0
    assert second.deleted == 1, "上一步追加的章节被回滚，多余块应清除"

    # 删掉一篇文档 → 它的块全部清除
    before = base.count()
    seekdb_chunks = len([c for c in base.chunks_for() if c.metadata["doc_file"] == "dd_seekdb"])
    (docs_dir / SEEKDB_DOC["name"]).unlink()
    third = base.ingest()
    assert third.deleted == seekdb_chunks
    assert base.count() == before - seekdb_chunks
    assert base.stats()["docs"] == len(CORPUS) - 2  # 卡片文档本就无块
    assert "dd_seekdb" not in {str(meta.get("doc_file", "")) for meta in base._scan_existing().values()}
    assert third.docs == len(CORPUS) - 1

    rebuild = base.ingest(rebuild=True)
    assert rebuild.rebuilt and rebuild.added == rebuild.chunks == base.count()


# --------------------------------------------------------------------------- #
# 离线兜底：embedding 模型不可用时降级纯全文检索
# --------------------------------------------------------------------------- #

def test_fallback_embedding_is_deterministic() -> None:
    vectors = FallbackEmbeddingFunction(dim=64)(["提交材料", "提交材料", "提交方式"])
    assert len(vectors[0]) == 64
    assert vectors[0] == vectors[1], "同一文本必须得到同一伪向量（增量导入依赖确定性）"
    assert vectors[0] != vectors[2]
    assert abs(sum(value * value for value in vectors[0]) - 1.0) < 1e-6


def test_offline_fallback_ingests_and_searches_fulltext_only(
    tmp_path: Path, corpus_dir: Path
) -> None:
    settings = Settings(
        env={
            "DATA_DIR": str(tmp_path / "runtime"),
            "DOCS_DIR": str(corpus_dir),
            "LOG_DIR": str(tmp_path / "logs"),
            "KB_COLLECTION": "saitong_kb_fallback",
            "KB_EMBEDDING": "fallback",
        },
        root=PROJECT_ROOT,
    )
    reset_settings(settings)
    reset_embedding_state()
    try:
        base = KnowledgeBase(
            settings=settings, docs_dir=corpus_dir, data_dir=tmp_path / "runtime",
            collection="saitong_kb_fallback",
        )
        assert base.search_mode == MODE_FULLTEXT
        assert embedding_state(settings)["mode"] == MODE_FULLTEXT

        report = base.ingest()
        assert report.added == report.chunks > 0, "没有模型也要能把库建起来"

        hits = base.search("MiniMax 提交材料", tracks=["MiniMax"], top_k=3)
        assert hits, "全文腿仍要能命中"
        assert {hit.track for hit in hits} == {"MiniMax"}
        assert "运行说明" in hits[0].text
        row = audit.read_tail("seekdb", limit=1, settings=settings)[0]
        assert row["mode"] == "fulltext_only" and row["error"] is None

        # 没有领域词时不给噪声（向量腿此时无语义）
        assert base.search("怎么办啊") == []
        row = audit.read_tail("seekdb", limit=1, settings=settings)[0]
        assert row["mode"] == "fulltext_only_no_terms"

        health = base.health()
        assert health["ok"] and health["search_mode"] == MODE_FULLTEXT
        assert "hash-fallback" in health["embedding"]["model"]
    finally:
        reset_embedding_state()
        reset_settings(Settings(env={}, root=PROJECT_ROOT))


def test_auto_mode_probes_default_embedding_once(tmp_path: Path) -> None:
    """auto 探测结果进程内缓存；无模型机器上应返回 fulltext_only 而不是抛异常。"""
    settings = Settings(env={"DATA_DIR": str(tmp_path), "LOG_DIR": str(tmp_path / "logs"),
                             "KB_EMBEDDING": "auto"}, root=PROJECT_ROOT)
    reset_settings(settings)
    reset_embedding_state()
    try:
        state = embedding_state(settings)
        assert state["mode"] in {MODE_HYBRID, MODE_FULLTEXT}
        assert state["requested"] == "auto"
        assert embedding_state(settings) is state, "同一模式不应重复探测（模型加载很贵）"
        rows = [row for row in audit.read_tail("seekdb", limit=5, settings=settings)
                if row["event"] == "embedding_probe"]
        assert len(rows) == 1
        assert rows[0]["model"]
    finally:
        reset_embedding_state()
        reset_settings(Settings(env={}, root=PROJECT_ROOT))
