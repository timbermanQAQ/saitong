"""M3 接口测试：FastAPI TestClient + 真知识库（tmp DATA_DIR）+ 假 PowerContext。

不依赖 LLM：所有问答走抽取式分支；PowerContext 用 httpx.MockTransport 顶掉。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import AppContext, QaLog, extract_todos, mentioned_tracks, resolve_track_filter
from app.config import PROJECT_ROOT, Settings, reset_settings
from app.kb import KnowledgeBase
from app.main import create_app
from app.team import TeamStore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_team import FakePowerContext  # noqa: E402  复用同一份契约假实现

ROOT = "2026年OceanBase数据库大赛"
MINIMAX = {
    "name": "aa_minimax.md",
    "title": "MiniMax",
    "path": f"{ROOT} / 赛题 / 初赛部分 / MiniMax",
    "url": "https://example.test/minimax",
    "body": """
## 一、题目背景
AI 开发助手需要知道当前工程要遵守哪些要求。

## 三、提交材料
作品材料放在参赛仓库的 `minimax/` 目录。

| 材料 | 要求 |
| --- | --- |
| 完整源码 | 上下文模块与接入代码 |
| 运行说明 | 依赖版本、配置步骤、测试命令 |
| 验证样例 | 任务描述、操作步骤、预期结果 |
| 技术使用记录 | MiniMax 开发记录、数据库查询记录 |

## 四、评分与测试
### 4.2 验收方式
评审按固定提交跑一遍验收脚本。
""",
}

QODER = {
    "name": "bb_qoder.md",
    "title": "Qoder",
    "path": f"{ROOT} / 赛题 / 初赛部分 / Qoder",
    "url": "https://example.test/qoder",
    "body": """
## 提交方式
把分支名和 commit hash 填到测评平台，产物放到 qoder 目录。

## 验收
需要提交运行说明与验证样例，评审按平台记录执行。
""",
}

FAQ = {
    "name": "cc_faq.md",
    "title": "FAQ - 大赛常见问题答疑",
    "path": f"{ROOT} / FAQ - 大赛常见问题答疑",
    "url": "https://example.test/faq",
    "body": """
## 提交
可以多次提交，评审以平台记录的固定提交为准。
""",
}

# AMD 文档故意写得「更像提交材料」：不限定赛道时它会挤掉 MiniMax 的待办（回归用例）
AMD = {
    "name": "dd_amd.md",
    "title": "AMD",
    "path": f"{ROOT} / 赛题 / 初赛部分 / AMD",
    "url": "https://example.test/amd",
    "body": """
## 提交材料
首次启动时出现资源池初始化告警属于正常现象。提交材料必须包含运行说明与验证样例截图，缺一不可。

| 材料 | 要求 |
| --- | --- |
| 完整源码 | GPU 侧适配代码 |
| 运行说明 | 驱动版本、容器启动命令 |
""",
}

CORPUS = [MINIMAX, QODER, FAQ, AMD]


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    root = tmp_path_factory.mktemp("api")
    docs_dir = root / "docs"
    docs_dir.mkdir()
    for spec in CORPUS:
        text = (f"---\ntitle: {spec['title']}\npath: {spec['path']}\n"
                f"source_url: {spec['url']}\n---\n\n{spec['body'].strip()}\n")
        (docs_dir / spec["name"]).write_text(text, encoding="utf-8")

    settings = Settings(
        env={
            "DATA_DIR": str(root / "runtime"),
            "DOCS_DIR": str(docs_dir),
            "LOG_DIR": str(root / "logs"),
            "KB_COLLECTION": "saitong_api_test",
            "LLM_BASE_URL": "",
            "LLM_API_KEY": "",
            "LLM_MODEL": "",
        },
        root=PROJECT_ROOT,
    )
    reset_settings(settings)
    kb = KnowledgeBase(settings=settings, docs_dir=docs_dir, data_dir=root / "runtime",
                       collection="saitong_api_test")
    report = kb.ingest()
    assert report.chunks > 0

    backend = FakePowerContext()
    transport = httpx.MockTransport(backend.handler)
    real_client = httpx.Client

    def client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs.pop("base_url", None)
        kwargs.pop("timeout", None)
        return real_client(transport=transport, base_url="http://testserver", **kwargs)

    httpx.Client = client_factory  # type: ignore[assignment]
    try:
        ctx = AppContext(settings=settings, kb=kb, teams=TeamStore(settings=settings),
                        service=None, qa=QaLog(settings.data_dir / "qa_log.jsonl"))
        app = create_app(context=ctx)
        with TestClient(app) as client:
            yield {"client": client, "backend": backend, "settings": settings, "kb": kb, "ctx": ctx}
    finally:
        httpx.Client = real_client  # type: ignore[assignment]
        ctx.teams.client.close()
        reset_settings(Settings(env={}, root=PROJECT_ROOT))


def make_team(client: TestClient, name: str = "红队",
              tracks: tuple[str, ...] = ("MiniMax", "Qoder")) -> None:
    assert client.post("/api/teams", json={"name": name}).status_code == 200
    for track in tracks:
        response = client.put(f"/api/teams/{name}/tracks", json={"track": track, "topics": ["赛题一"]})
        assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #

def test_mentioned_tracks_and_priority() -> None:
    assert mentioned_tracks("MiniMax 要提交什么") == ["MiniMax"]
    assert set(mentioned_tracks("seekdb 和 高德 的题谁难")) == {"seekdb", "高德地图"}
    assert mentioned_tracks("今天天气") == []

    class Stub:
        tracks = ["MiniMax", "Qoder"]

    assert resolve_track_filter("随便问", None, Stub()) == (["MiniMax", "Qoder"], "team")
    assert resolve_track_filter("MiniMax 提交", None, Stub()) == (["MiniMax"], "question")
    assert resolve_track_filter("MiniMax 提交", ["Dify"], Stub()) == (["Dify"], "request")
    assert resolve_track_filter("随便问", None, None) == ([], "all")


class FakeHit:
    """只需要 SearchHit 暴露的那几个属性。"""

    def __init__(self, text: str, *, doc_title: str = "MiniMax", heading: str = "三、提交材料",
                 track: str = "MiniMax", source_url: str = "https://example.test/minimax",
                 chunk_id: str = "aa#003") -> None:
        self.text = text
        self.doc_title = doc_title
        self.heading = heading
        self.track = track
        self.source_url = source_url
        self.id = chunk_id


def test_extract_todos_from_table_and_prose() -> None:
    table = FakeHit(
        "【MiniMax > 三、提交材料】\n作品材料放在参赛仓库的 minimax 目录。\n"
        "| 材料 | 要求 |\n| --- | --- |\n"
        "| 完整源码 | 上下文模块与接入代码 |\n| 运行说明 | 依赖版本、配置步骤、测试命令 |\n"
    )
    todos = extract_todos([table], limit=5)
    texts = [todo["text"] for todo in todos]
    assert any(todo.startswith("完整源码：") for todo in texts), texts
    assert any(todo.startswith("运行说明：") for todo in texts), texts
    assert not any(todo.startswith("| ---") for todo in texts), "表格分隔线不能当待办"
    assert not any(todo.startswith("【") for todo in texts), "面包屑行不能当待办"
    assert all(todo["source_url"] == "https://example.test/minimax" for todo in todos)

    listed = FakeHit("【Qoder > 提交方式】\n- 把分支名填到测评平台\n1. 产物放到 qoder 目录\n与本题无关的闲聊行\n",
                     doc_title="Qoder", heading="提交方式", track="Qoder",
                     source_url="https://example.test/qoder", chunk_id="bb#000")
    listed_texts = [todo["text"] for todo in extract_todos([listed], limit=5)]
    assert listed_texts == ["把分支名填到测评平台", "产物放到 qoder 目录"], listed_texts

    background = FakeHit("【MiniMax > 一、题目背景】\n这里只做背景介绍，讲的是理念。\n", heading="一、题目背景")
    assert extract_todos([background], limit=3) == [], "背景章节的散文不该变成待办"
    assert len(extract_todos([table, listed], limit=2)) == 2, "limit 必须生效"


# --------------------------------------------------------------------------- #
# 运维接口
# --------------------------------------------------------------------------- #

def test_health_and_tracks(harness) -> None:
    client = harness["client"]
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["seekdb"]["ok"] and health["seekdb"]["chunks"] > 0
    assert health["powercontext"]["ok"] is True
    assert health["llm"] == {"enabled": False, "model": None}
    assert isinstance(health["days_left"], int)

    listing = client.get("/api/tracks").json()
    names = [row["name"] for row in listing["tracks"]]
    assert {"MiniMax", "Qoder", "seekdb", "通用"} <= set(names)
    by_name = {row["name"]: row["chunks"] for row in listing["tracks"]}
    assert by_name["MiniMax"] >= 3 and by_name["Qoder"] >= 2
    assert client.get("/healthz").json()["seekdb"]["chunks"] == listing["chunks"]


# --------------------------------------------------------------------------- #
# 队伍档案
# --------------------------------------------------------------------------- #

def test_team_crud_and_profile(harness) -> None:
    client = harness["client"]
    created = client.post("/api/teams", json={"name": "红队"}).json()
    assert created["created"] is True and created["scope_id"]
    assert client.post("/api/teams", json={"name": "红队"}).json()["created"] is False

    client.put("/api/teams/红队/tracks", json={"track": "MiniMax", "topics": ["赛题一", "赛题二"]})
    client.put("/api/teams/红队/tracks", json={"track": "Qoder", "topics": ["赛题三"]})

    progress = client.put("/api/teams/红队/progress",
                          json={"track": "MiniMax", "topic": "赛题一", "status": "doing"})
    assert progress.json()["status"] == "doing"
    done = client.put("/api/teams/红队/progress",
                      json={"track": "MiniMax", "topic": "赛题一", "status": "已完成", "note": "已提交"})
    body = done.json()
    assert body["status"] == "done"
    assert body["replaced"] == 1, "旧 progress 必须被 lifecycle 置为 forgotten"
    assert len(body["progress"]) == 1

    blocker = client.post("/api/teams/红队/blockers",
                          json={"text": "判题分支找不到", "track": "MiniMax", "topic": "赛题二"}).json()
    blocker_id = blocker["blocker"]["id"]
    profile = client.get("/api/teams/红队").json()
    assert profile["tracks"] == ["MiniMax", "Qoder"]
    assert profile["topics"] == {"MiniMax": ["赛题一", "赛题二"], "Qoder": ["赛题三"]}
    assert len(profile["blockers"]) == 1

    resolved = client.post(f"/api/teams/红队/blockers/{blocker_id}/resolve").json()
    assert resolved["blocker"]["resolved"] is True
    assert client.get("/api/teams/红队").json()["blockers"][0]["resolved"] is True
    assert client.post("/api/teams/红队/blockers/xxx/resolve").status_code == 404
    assert client.get("/api/teams/不存在的队").status_code == 404


# --------------------------------------------------------------------------- #
# 检索与问答
# --------------------------------------------------------------------------- #

def test_search_returns_sources(harness) -> None:
    result = harness["client"].post("/api/search", json={"q": "提交材料", "tracks": ["MiniMax"]}).json()
    assert result["hits"], "混合检索应有命中"
    top = result["hits"][0]
    assert top["track"] == "MiniMax"
    assert top["source_url"] == "https://example.test/minimax"
    assert "运行说明" in top["text"]
    assert harness["client"].post("/api/search", json={"q": ""}).status_code == 422


def test_ask_uses_team_tracks_when_question_is_generic(harness) -> None:
    client = harness["client"]
    make_team(client, "蓝队", ("MiniMax", "Qoder"))
    response = client.post("/api/ask", json={"question": "提交材料有哪些", "team": "蓝队"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "extractive"
    assert body["track_basis"] == "team"
    assert body["filter_tracks"] == ["MiniMax", "Qoder"]
    assert body["used_tracks"] == ["MiniMax", "Qoder"]
    assert body["sources"], "无 LLM 也必须给出来源"
    assert all(source["source_url"].startswith("https://example.test/") for source in body["sources"])
    assert all(source["n"] == index for index, source in enumerate(body["sources"], start=1))
    assert "运行说明" in body["answer"]
    assert "未配置 LLM" in (body["warning"] or "")


def test_ask_named_track_beats_profile(harness) -> None:
    client = harness["client"]
    make_team(client, "橙队", ("MiniMax", "Qoder"))
    body = client.post("/api/ask", json={"question": "Qoder 的提交方式是什么", "team": "橙队"}).json()
    assert body["track_basis"] == "question"
    assert body["used_tracks"] == ["Qoder"]
    assert {source["track"] for source in body["sources"]} == {"Qoder"}

    explicit = client.post("/api/ask", json={"question": "提交材料有哪些",
                                             "tracks": ["Qoder"]}).json()
    assert explicit["track_basis"] == "request"
    assert explicit["used_tracks"] == ["Qoder"]

    unknown = client.post("/api/ask", json={"question": "提交材料", "team": "没这支队伍"})
    assert unknown.status_code == 404
    empty = client.post("/api/ask", json={"question": "   "})
    assert empty.status_code == 422


def test_next_steps_lists_todos_with_links(harness) -> None:
    client = harness["client"]
    make_team(client, "绿队", ("MiniMax",))
    client.put("/api/teams/绿队/progress", json={"track": "MiniMax", "topic": "赛题一", "status": "done"})
    client.put("/api/teams/绿队/progress",
               json={"track": "MiniMax", "topic": "赛题二", "status": "blocked"})
    body = client.get("/api/teams/绿队/next-steps").json()
    assert body["team"] == "绿队"
    assert isinstance(body["days_left"], int)
    assert body["overdue"] == (body["days_left"] < 0)
    topics = {item["topic"]: item for item in body["items"]}
    assert "赛题一" not in topics, "已完成赛题不再出现在下一步"
    assert topics["赛题二"]["status"] == "blocked"
    todos = topics["赛题二"]["todos"]
    assert todos, "未完成赛题必须给出待办"
    assert all(todo["source_url"].startswith("https://example.test/") for todo in todos)
    assert any("运行说明" in todo["text"] or "验证样例" in todo["text"] for todo in todos)
    assert any("验收" in " ".join(item["queries"]) for item in body["items"])
    assert client.get("/api/teams/没这队/next-steps").status_code == 404


def test_next_steps_todos_stay_within_their_own_track(harness) -> None:
    """回归：MiniMax 赛题的待办不能引用 AMD 文档（检索必须按该赛题赛道过滤）。"""
    client = harness["client"]
    make_team(client, "灰队", ("MiniMax", "AMD"))
    client.put("/api/teams/灰队/tracks", json={"track": "AMD", "topics": ["赛题九"]})
    client.put("/api/teams/灰队/progress",
               json={"track": "MiniMax", "topic": "赛题一", "status": "doing"})

    body = client.get("/api/teams/灰队/next-steps", params={"top_k": 5}).json()
    items = {item["topic"]: item for item in body["items"]}
    assert set(items) >= {"赛题一", "赛题九"}

    for item in body["items"]:
        assert item["search_tracks"] == [item["track"]], "检索必须带上该赛题的赛道过滤"
        assert item["todos"], f"{item['track']}/{item['topic']} 应有待办"
        for todo in item["todos"]:
            assert todo["track"] == item["track"], (
                f"待办引用了别的赛道：{item['track']}/{item['topic']} → {todo}")

    minimax = items["赛题一"]
    assert {todo["doc_title"] for todo in minimax["todos"]} == {"MiniMax"}
    assert not any("资源池" in todo["text"] for todo in minimax["todos"]), "AMD 的句子混进来了"
    assert {todo["source_url"] for todo in items["赛题九"]["todos"]} == {"https://example.test/amd"}

    # 检索侧日志可核对：每条「提交材料」查询都只限定一个赛道
    logs = client.get("/api/logs/seekdb", params={"limit": 80}).json()["items"]
    step_rows = [row for row in logs
                 if row["event"] == "hybrid_search" and row["query"].endswith("提交材料")]
    assert step_rows
    for row in step_rows:
        assert row["tracks"], "next-steps 的检索必须限定赛道"
        assert len(row["tracks"]) == 1
        assert row["where"] == {"track": {"$in": row["tracks"]}}


def test_stats_and_logs(harness) -> None:
    client = harness["client"]
    make_team(client, "紫队", ("MiniMax",))
    for question in ("提交材料有哪些", "提交材料有哪些", "验收方式是什么"):
        assert client.post("/api/ask", json={"question": question, "team": "紫队"}).status_code == 200
    client.post("/api/search", json={"q": "判题分支"})

    stats = client.get("/api/stats").json()
    assert stats["ask_count"] >= 3
    assert stats["team_count"] >= 1 and "紫队" in stats["teams"]
    assert stats["chunks"] == harness["kb"].count()
    hot = stats["hot_questions"]
    assert hot and hot[0]["question"] == "提交材料有哪些" and hot[0]["count"] >= 2
    assert stats["modes"]["extractive"] >= 3
    assert stats["track_chunks"]["MiniMax"] >= 3

    qa_rows = json.loads(harness["settings"].data_dir.joinpath("qa_log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert qa_rows["event"] == "search"
    assert "紫队" not in (harness["settings"].data_dir / "qa_log.jsonl").read_text(encoding="utf-8"), \
        "问答记录不得包含队名等个人信息"

    seekdb_logs = client.get("/api/logs/seekdb", params={"limit": 5}).json()
    assert seekdb_logs["total"] >= 1
    assert seekdb_logs["items"][-1]["event"] in {"hybrid_search", "ingest"}
    pc_logs = client.get("/api/logs/powercontext", params={"limit": 200}).json()
    assert pc_logs["items"], "档案写操作必须留日志"
    paths = {row["path"] for row in pc_logs["items"]}
    assert {"/v1/scopes", "/v1/memory/remember", "/v1/atomic-memory/list"} <= paths
    assert all(row["elapsed_ms"] >= 0 and row["method"] in {"GET", "POST"} for row in pc_logs["items"])
    assert client.get("/api/logs/nope").status_code == 422
