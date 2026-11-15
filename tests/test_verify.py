"""scripts/verify.py 的 URL 构造测试（远端验收 --base-url 才走这条路）。

回归起因：中文队名直接拼进路径时 urllib 抛 UnicodeEncodeError，进程内 TestClient 不会暴露。
"""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

VERIFY_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify.py"


@pytest.fixture(scope="module")
def verify():
    """按文件路径加载验收脚本；隔离它在 import 期写入的 DATA_DIR/LOG_DIR 默认值。"""
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location("saitong_verify", VERIFY_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        yield module
    finally:
        os.environ.clear()
        os.environ.update(saved)


TEAM = "样例队"
ENCODED_TEAM = urllib.parse.quote(TEAM)  # %E6%A0%B7%E4%BE%8B%E9%98%9F


def test_encode_path_quotes_chinese_team(verify) -> None:
    path = verify.encode_path(f"/api/teams/{TEAM}/tracks")
    assert path == f"/api/teams/{ENCODED_TEAM}/tracks"
    assert path.isascii(), "URL 必须纯 ASCII，否则 urllib 会抛 UnicodeEncodeError"
    assert urllib.parse.unquote(path) == f"/api/teams/{TEAM}/tracks"


def test_encode_path_keeps_ascii_and_structure(verify) -> None:
    for path in ("/api/logs/seekdb?limit=20", "/healthz", "/ui/app.js",
                 "/api/teams/abc/blockers/art_1f43/resolve", "/api/teams/team-1/next-steps?top_k=3"):
        assert verify.encode_path(path) == path
    # 已编码的 % 不被二次转义，否则服务端解出来是 %25E6…
    assert verify.encode_path(f"/api/teams/{ENCODED_TEAM}/tracks") == f"/api/teams/{ENCODED_TEAM}/tracks"


def test_raw_chinese_path_is_not_ascii_encodable() -> None:
    """这就是当初的 bug 成因：urllib 最终按 ascii 编码 URL，中文队名会让它抛错（不依赖端口是否在听）。"""
    request = urllib.request.Request(f"http://127.0.0.1/api/teams/{TEAM}", method="GET")
    assert not request.full_url.isascii()
    with pytest.raises(UnicodeEncodeError):
        request.full_url.encode("ascii")


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_remote_client_uses_ascii_urls(verify, monkeypatch) -> None:
    seen: list[str] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001 - 替身签名保持与 urllib 一致
        seen.append(request.full_url if hasattr(request, "full_url") else str(request))
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(verify.urllib.request, "urlopen", fake_urlopen)
    client = verify.RemoteClient("http://127.0.0.1:7861")

    client.request("PUT", f"/api/teams/{TEAM}/tracks", {"track": "seekdb", "topics": ["赛题一"]})
    client.request("POST", f"/api/teams/{TEAM}/blockers/art_0001/resolve")
    client.request("GET", f"/api/teams/{TEAM}/next-steps?top_k=3", None)
    client.get_text(f"/api/teams/{TEAM}")

    assert len(seen) == 4
    for url in seen:
        assert url.isascii(), url
        assert f"/api/teams/{ENCODED_TEAM}" in url
    # 查询串没被破坏
    assert urllib.parse.urlsplit(seen[2]).query == "top_k=3"


def test_read_baseline_counts_come_from_the_instance(verify, monkeypatch) -> None:
    """`--base-url` 模式的运行前基线必须问远端要，不能读本机 logs/。"""
    routes = {
        "/api/health": {"status": "ok", "llm": {"enabled": True}},
        "/api/stats": {"ask_count": 7, "search_count": 3, "team_count": 1},
        "/api/logs/seekdb?limit=1": {"total": 41, "path": "/srv/logs/seekdb_queries.jsonl"},
        "/api/logs/powercontext?limit=1": {"total": 190},
        "/api/logs/llm?limit=1": {"total": 2},
    }
    seen: list[str] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        url = request.full_url
        seen.append(url)
        body = next(json.dumps(row).encode() for path, row in routes.items() if url.endswith(path))
        return _FakeResponse(body)

    monkeypatch.setattr(verify.urllib.request, "urlopen", fake_urlopen)
    before = verify.read_baseline(verify.RemoteClient("http://127.0.0.1:7861"))

    assert (before["ask"], before["search"]) == (7, 3)
    assert before["llm_configured"] is True
    assert [before[c] for c in verify.LOG_CHANNELS] == [41, 190, 2]
    assert before["log_dir"] == "/srv/logs"
    assert all(url.isascii() for url in seen) and len(seen) == 5
