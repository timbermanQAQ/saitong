"""M2 队伍档案测试。

契约层用 httpx.MockTransport 断言报文（不依赖服务），
真实子进程用例验证「建档 → 写进度 → 旧记录 forgotten → 重启读回且 scope_id 不变」，
需要本机 powercontext 可执行文件与一个空闲端口，否则自动 skip。
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from app import audit
from app.config import PROJECT_ROOT, Settings, reset_settings
from app.team import (
    KIND_BLOCKER,
    KIND_NOTE,
    KIND_PROGRESS,
    KIND_TRACK_CHOICE,
    SCOPE_SUMMARY,
    MemoryRecord,
    PowerContextClient,
    PowerContextService,
    TeamStore,
    normalize_status,
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("done", "done"),
        ("DONE", "done"),
        ("进行中", "doing"),
        ("wip", "doing"),
        ("blocked", "blocked"),
        ("", "todo"),
        ("乱写", "todo"),
        (None, "todo"),
    ],
)
def test_normalize_status(raw: Any, expected: str) -> None:
    assert normalize_status(raw) == expected


# --------------------------------------------------------------------------- #
# 报文契约（MockTransport）
# --------------------------------------------------------------------------- #

class FakePowerContext:
    """按实测报文最小化实现：幂等键、remember、list、lifecycle。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.scopes: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, str] = {}
        self.records: dict[str, list[dict[str, Any]]] = {}
        self._seq = 0

    def next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq:04d}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, body))

        if path == "/health/live":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v1/scopes" and request.method == "POST":
            key = body["idempotency_key"]
            if key in self.by_key:
                scope_id = self.by_key[key]
                if self.scopes[scope_id]["summary"] != body["summary"]:
                    return httpx.Response(409, json={"error": {"code": "scope_idempotency_conflict",
                                                               "message": "conflict"}})
                return httpx.Response(201, json={**self.scopes[scope_id], "version": 1})
            scope_id = self.next_id("scp_")
            scope = {"scope_id": scope_id, "title": body["title"], "summary": body["summary"]}
            self.scopes[scope_id] = scope
            self.by_key[key] = scope_id
            self.records.setdefault(scope_id, [])
            return httpx.Response(201, json={**scope, "version": 1})
        if path == "/v1/scopes" and request.method == "GET":
            return httpx.Response(200, json={"items": [
                {"scope_id": "scp_default", "title": "Default", "summary": "Default context"},
                *self.scopes.values(),
            ], "next_cursor": None})
        if path == "/v1/memory/remember":
            record = {
                "artifact": {"family": "atomic-memory", "artifact_id": self.next_id("art_"), "revision": 1},
                "kind": body["kind"],
                "text": body["text"],
                "state": "active",
                "state_version": 0,
                "merged_into_id": None,
            }
            self.records.setdefault(body["scope_id"], []).append(record)
            return httpx.Response(200, json={"changed": True, "records": [record]})
        if path == "/v1/atomic-memory/list":
            limit = int(body.get("limit") or 50)
            if limit > 100:
                return httpx.Response(422, json={"error": {"code": "invalid_request", "message": "limit>100"}})
            items = [
                record
                for record in self.records.get(body["scope_id"], [])
                if (not body.get("kind") or record["kind"] == body["kind"])
                and (record["state"] in (body.get("states") or ["active"]))
            ]
            start = int(body.get("cursor") or 0)
            page = items[start:start + limit]
            nxt = str(start + limit) if start + limit < len(items) else None
            return httpx.Response(200, json={"items": page, "next_cursor": nxt})
        if path == "/v1/atomic-memory/lifecycle":
            target = body["target"]["artifact"]
            for record in self.records.get(body["scope_id"], []):
                if record["artifact"]["artifact_id"] == target["artifact_id"]:
                    if record["state_version"] != body["target"]["state_version"]:
                        return httpx.Response(409, json={"error": {"code": "atomic_memory_changed",
                                                                   "message": "stale"}})
                    record["state"] = body["state"]
                    record["state_version"] += 1
                    return httpx.Response(200, json={"changed": True, "records": [record]})
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "gone"}})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})


@pytest.fixture()
def fake(tmp_path: Path) -> Iterator[tuple[TeamStore, FakePowerContext, Settings]]:
    backend = FakePowerContext()
    settings = Settings(
        env={"DATA_DIR": str(tmp_path / "runtime"), "LOG_DIR": str(tmp_path / "logs")},
        root=PROJECT_ROOT,
    )
    reset_settings(settings)
    transport = httpx.MockTransport(backend.handler)
    real_client = httpx.Client

    def client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs.pop("base_url", None)
        kwargs.pop("timeout", None)
        return real_client(transport=transport, base_url="http://testserver", **kwargs)

    httpx.Client = client_factory  # type: ignore[assignment]
    client = PowerContextClient(base_url="http://testserver", settings=settings)
    try:
        yield TeamStore(client, settings=settings), backend, settings
    finally:
        httpx.Client = real_client  # type: ignore[assignment]
        client.close()


def test_scope_request_uses_idempotency_key(fake) -> None:
    store, backend, _ = fake
    first = store.ensure_team("红队")
    assert first["created"] is True
    scope_call = next(call for call in backend.calls if call[1] == "/v1/scopes" and call[0] == "POST")
    assert scope_call[2] == {
        "title": "红队",
        "summary": SCOPE_SUMMARY,
        "idempotency_key": "team:红队",
        "context_references": [],
        "external_references": [],
    }
    again = store.ensure_team("红队")
    assert again["scope_id"] == first["scope_id"]


def test_remember_text_is_json_and_reads_back(fake) -> None:
    store, backend, _ = fake
    scope_id = store.scope_id("红队")
    store.set_track_choices("红队", "MiniMax", ["赛题一", "赛题二"])
    payload = backend.calls[-1][2]
    assert payload["scope_id"] == scope_id
    assert payload["kind"] == KIND_TRACK_CHOICE
    assert json.loads(payload["text"])["topics"] == ["赛题一", "赛题二"]

    profile = store.profile("红队")
    assert profile.tracks == ["MiniMax"]
    assert profile.topics_for("MiniMax") == ["赛题一", "赛题二"]

    teams = store.teams()
    assert [row["name"] for row in teams] == ["红队"], "Default 空间不算队伍"
    assert teams[0]["scope_id"] == scope_id
    assert teams[0]["tracks"] == ["MiniMax"]


def test_list_pages_through_cursor(fake) -> None:
    store, backend, _ = fake
    scope_id = store.scope_id("红队")
    for index in range(130):
        store.client.remember(scope_id, KIND_NOTE, {"i": index})
    records = store.client.list_records(scope_id, kind=KIND_NOTE)
    assert len(records) == 130, "翻页必须取全，否则档案被静默截断"
    assert {int(record.data["i"]) for record in records} == set(range(130))


def test_lifecycle_payload_forgets_previous_progress(fake) -> None:
    store, backend, _ = fake
    store.ensure_team("红队")
    store.set_progress("红队", "MiniMax", "赛题一", "doing")
    store.set_progress("红队", "MiniMax", "赛题一", "done", note="已提交")

    lifecycle = [call for call in backend.calls if call[1] == "/v1/atomic-memory/lifecycle"]
    assert lifecycle, "更新进度必须调用 lifecycle"
    body = lifecycle[-1][2]
    assert body["state"] == "forgotten"
    assert set(body["target"]) == {"artifact", "state_version"}
    assert body["target"]["artifact"]["family"] == "atomic-memory"

    scope_id = store.scope_id("红队")
    records = backend.records[scope_id]
    progress = [r for r in records if r["kind"] == KIND_PROGRESS]
    assert [r["state"] for r in progress] == ["forgotten", "active"]
    profile = store.profile("红队")
    assert profile.status_of("MiniMax", "赛题一") == "done"
    assert len(profile.progress) == 1


def test_blockers_and_notes(fake) -> None:
    store, backend, _ = fake
    store.ensure_team("红队")
    blocker = store.add_blocker("红队", "判题分支不对", track="MiniMax", topic="赛题一")
    assert blocker["resolved"] is False
    resolved = store.resolve_blocker("红队", blocker["id"])
    assert resolved["resolved"] is True
    profile = store.profile("红队")
    assert profile.open_blockers() == []
    assert [record.kind for record in profile.blockers] == [KIND_BLOCKER]

    note = store.add_note("红队", "下周补测试", track="MiniMax")
    assert note["kind"] == KIND_NOTE
    assert profile.latest_note("MiniMax") == ""  # profile 是旧的，需要重取
    assert store.profile("红队").latest_note("MiniMax") == "下周补测试"


def test_unknown_scope_falls_back_to_title_lookup(fake) -> None:
    store, backend, _ = fake
    scope_id = store.scope_id("红队")
    # 幂等键冲突（例如换了 summary）时按 title 找回原有 scope
    backend.scopes[scope_id]["summary"] = "别的摘要"
    assert store.scope_id("红队") == scope_id


def test_powercontext_calls_are_logged(fake) -> None:
    store, _, settings = fake
    store.ensure_team("红队")
    store.set_track_choices("红队", "MiniMax", ["赛题一"])
    store.set_progress("红队", "MiniMax", "赛题一", "doing")
    store.set_progress("红队", "MiniMax", "赛题一", "done")
    rows = audit.read_tail("powercontext", limit=50, settings=settings)
    assert rows
    paths = [row["path"] for row in rows]
    assert "/v1/scopes" in paths and "/v1/memory/remember" in paths
    assert "/v1/atomic-memory/lifecycle" in paths
    for row in rows:
        assert row["event"] == "powercontext"
        assert row["elapsed_ms"] >= 0
        assert row["method"] in {"GET", "POST"}
        assert isinstance(row["request_summary"], dict)
        assert row["ok"] is (row["error"] is None)
        assert "password" not in json.dumps(row)
    assert all(row["path"] != "/health/live" for row in rows), "健康轮询不进业务日志"


def test_error_from_service_is_raised(fake) -> None:
    store, backend, _ = fake
    scope_id = store.scope_id("红队")
    backend.records[scope_id] = []
    client = store.client
    with pytest.raises(Exception) as excinfo:
        client.call("POST", "/v1/atomic-memory/lifecycle",
                    {"scope_id": scope_id,
                     "target": MemoryRecord.from_api(
                         {"artifact": {"artifact_id": "art_missing", "revision": 1},
                          "kind": "progress", "text": "{}", "state": "active", "state_version": 0}
                     ).target,
                     "state": "forgotten"})
    assert excinfo.value.status == 404


# --------------------------------------------------------------------------- #
# 真实子进程：重启读回
# --------------------------------------------------------------------------- #

def _binary_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("powercontext") is not None


@pytest.mark.skipif(not _binary_available(), reason="本机没有 powercontext 包")
def test_live_profile_survives_service_restart(tmp_path: Path) -> None:
    settings = Settings(
        env={
            "DATA_DIR": str(tmp_path / "runtime"),
            "LOG_DIR": str(tmp_path / "logs"),
            "POWERCONTEXT_DATA_DIR": str(tmp_path / "runtime" / "powercontext"),
        },
        root=PROJECT_ROOT,
    )
    reset_settings(settings)
    port = free_port()
    service = PowerContextService(settings=settings, port=port, ready_timeout=60.0)
    client = service.client(timeout=15.0)
    store = TeamStore(client, settings=settings)
    try:
        assert service.start(), "真实服务未就绪：" + str(service.log_path)

        team = store.ensure_team("红队")
        store.set_track_choices("红队", "MiniMax", ["赛题一"])
        store.set_progress("红队", "MiniMax", "赛题一", "doing")
        store.add_blocker("红队", "找不到判题分支", track="MiniMax", topic="赛题一")
        store.set_progress("红队", "MiniMax", "赛题一", "done", note="已推 fix/xxx 分支")

        before = store.profile("红队")
        assert before.status_of("MiniMax", "赛题一") == "done"
        assert len(before.progress) == 1, "旧 progress 已被 lifecycle 置为 forgotten"

        assert service.restart(), "重启后服务未就绪"
        after = store.profile("红队")
        assert after.scope_id == before.scope_id, "重启后 scope_id 必须不变"
        assert after.status_of("MiniMax", "赛题一") == "done"
        assert after.progress[0].data["note"] == "已推 fix/xxx 分支"
        assert len(after.blockers) == 1

        assert client.is_ready()
        assert audit.count_lines("powercontext", settings=settings) > 0
    finally:
        service.stop()
        client.close()
