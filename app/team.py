"""队伍档案：PowerContext 1.5.0 HTTP 客户端、子进程托管与档案语义。

字段名以实测报文为准（见 docs/DESIGN.md §15），不猜：
- POST /v1/scopes            → 201 {scope_id,title,summary,...,version}
- POST /v1/memory/remember   → 200 {changed,records:[{artifact,kind,text,state,state_version,merged_into_id}]}
- POST /v1/atomic-memory/list     → 200 {items:[record],next_cursor}
- POST /v1/atomic-memory/lifecycle → 200 {changed,records:[record]}，state 只接受 "forgotten"
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

from . import audit
from .config import Settings, get_settings

KIND_TRACK_CHOICE = "track-choice"
KIND_PROGRESS = "progress"
KIND_BLOCKER = "blocker"
KIND_NOTE = "note"

SCOPE_SUMMARY = "2026 OceanBase 数据库大赛队伍档案（赛题通）"
DEFAULT_TIMEOUT = 10.0
LIST_PAGE_SIZE = 100
PROGRESS_STATUSES = ("todo", "doing", "blocked", "done")
_HEALTH_PATH = "/health/live"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_status(status: str) -> str:
    value = str(status or "").strip().lower()
    aliases = {
        "": "todo",
        "tbd": "todo",
        "todo": "todo",
        "未开始": "todo",
        "待办": "todo",
        "doing": "doing",
        "ing": "doing",
        "wip": "doing",
        "进行中": "doing",
        "blocked": "blocked",
        "block": "blocked",
        "阻塞": "blocked",
        "done": "done",
        "finish": "done",
        "finished": "done",
        "complete": "done",
        "已完成": "done",
    }
    return aliases.get(value, value if value in PROGRESS_STATUSES else "todo")


class PowerContextError(RuntimeError):
    """PowerContext 调用失败（未就绪、契约不符、scope 不存在等）。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        path: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.path = path

    def as_dict(self) -> dict[str, Any]:
        return {"error": str(self), "status": self.status, "code": self.code, "path": self.path}


@dataclass
class MemoryRecord:
    """一条 atomic-memory，`data` 是 text 里 JSON 的结构化内容。"""

    kind: str
    text: str
    artifact_id: str
    revision: int
    state: str
    state_version: int
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.artifact_id

    @property
    def ts(self) -> str:
        return str(self.data.get("ts") or "")

    @property
    def target(self) -> dict[str, Any]:
        return {
            "artifact": {"family": "atomic-memory", "artifact_id": self.artifact_id, "revision": self.revision},
            "state_version": self.state_version,
        }

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "MemoryRecord":
        artifact = raw.get("artifact") or {}
        text = str(raw.get("text") or "")
        try:
            parsed = json.loads(text)
            data = parsed if isinstance(parsed, dict) else {"value": parsed}
        except (json.JSONDecodeError, TypeError):
            data = {"text": text}
        return cls(
            kind=str(raw.get("kind") or ""),
            text=text,
            artifact_id=str(artifact.get("artifact_id") or ""),
            revision=int(artifact.get("revision") or 1),
            state=str(raw.get("state") or "active"),
            state_version=int(raw.get("state_version") or 0),
            data=data,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.artifact_id, "kind": self.kind, "state": self.state, **self.data}


class PowerContextClient:
    """PowerContext 的最小 HTTP 封装：每次调用写一行审计日志。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        settings: Settings | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = (base_url or self.settings.powercontext_url).rstrip("/")
        self.timeout = timeout
        self._http: httpx.Client | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 底层
    @property
    def http(self) -> httpx.Client:
        with self._lock:
            if self._http is None:
                self._http = httpx.Client(base_url=self.base_url, timeout=self.timeout)
            return self._http

    def close(self) -> None:
        with self._lock:
            if self._http is not None:
                self._http.close()
                self._http = None

    def call(self, method: str, path: str, payload: dict[str, Any] | None = None,
             *, params: dict[str, Any] | None = None, log: bool = True,
             expect_status: Iterable[int] | None = None) -> Any:
        expected = set(expect_status or {200, 201})
        body = payload if payload is not None else (params or {})
        started = time.perf_counter()
        status: int | None = None
        data: Any = None
        error: str | None = None
        try:
            response = self.http.request(method, path, json=payload, params=params)
            status = response.status_code
            try:
                data = response.json()
            except ValueError:
                data = None
            if status not in expected:
                error = _api_error_message(data) or f"HTTP {status}"
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            if log:
                audit.log_event(
                    "powercontext",
                    settings=self.settings,
                    event="powercontext",
                    method=method,
                    path=path,
                    request_summary=_summarize(body),
                    status_code=status,
                    elapsed_ms=elapsed_ms,
                    ok=error is None,
                    error=error,
                    scope_id=body.get("scope_id"),
                    kind=body.get("kind"),
                )
        if error is not None:
            code = None
            if isinstance(data, dict):
                code = ((data.get("error") or {}).get("code")) or None
            raise PowerContextError(error, status=status, code=code, path=path)
        return data

    # ------------------------------------------------------------------ 健康
    def probe(self) -> dict[str, Any]:
        """服务是否就绪，不抛异常、不写日志（健康轮询用）。"""
        try:
            data = self.call("GET", _HEALTH_PATH, log=False)
        except PowerContextError as exc:
            return {"ok": False, "url": self.base_url, "error": str(exc)}
        except Exception as exc:  # pragma: no cover - 防御性
            return {"ok": False, "url": self.base_url, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "url": self.base_url, "status": data}

    def is_ready(self) -> bool:
        try:
            return bool((self.call("GET", _HEALTH_PATH, log=False) or {}).get("status"))
        except PowerContextError:
            return False

    def server_info(self) -> dict[str, Any]:
        data = self.call("GET", "/v1/server-info", log=False)
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ scope
    def list_scopes(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = self.call("GET", "/v1/scopes", params=params) or {}
            items.extend(data.get("items") or [])
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return items

    def ensure_scope(self, name: str) -> str:
        """幂等建档：重复的 idempotency_key + 相同参数 → 201 同一个 scope_id；
        参数不同 → 409 scope_idempotency_conflict，退化为按 title 查找已有 scope。
        """
        title = str(name).strip()
        if not title:
            raise PowerContextError("队名不能为空")
        payload = {"title": title, "summary": SCOPE_SUMMARY, "idempotency_key": f"team:{title}",
                   "context_references": [], "external_references": []}
        try:
            data = self.call("POST", "/v1/scopes", payload)
            scope_id = str((data or {}).get("scope_id") or "")
            if scope_id:
                return scope_id
        except PowerContextError as exc:
            if exc.code != "scope_idempotency_conflict" and exc.status != 409:
                raise
        known = self.find_scope(title)
        if known:
            return known
        raise PowerContextError(f"无法为队伍「{title}」定位档案空间", status=409,
                                code="scope_not_found", path="/v1/scopes")

    def find_scope(self, name: str) -> str | None:
        title = str(name).strip()
        for scope in self.list_scopes():
            if str(scope.get("title") or "") == title:
                return str(scope.get("scope_id") or "")
        return None

    # ------------------------------------------------------------- 记忆读写
    def remember(self, scope_id: str, kind: str, data: dict[str, Any] | str, *,
                 reason: str = "") -> MemoryRecord:
        payload = data if isinstance(data, dict) else {"text": str(data)}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=False)
        body: dict[str, Any] = {"scope_id": scope_id, "kind": kind, "text": text}
        if reason:
            body["reason"] = reason
        result = self.call("POST", "/v1/memory/remember", body) or {}
        records = result.get("records") or []
        if not records:
            raise PowerContextError("remember 未返回记录", path="/v1/memory/remember")
        return MemoryRecord.from_api(records[0])

    def list_records(self, scope_id: str, *, kind: str | None = None,
                     states: Iterable[str] = ("active",)) -> list[MemoryRecord]:
        wanted = [str(state) for state in states if state] or None
        records: list[MemoryRecord] = []
        cursor: str | None = None
        for _ in range(50):
            body: dict[str, Any] = {"scope_id": scope_id, "limit": LIST_PAGE_SIZE}
            if kind:
                body["kind"] = kind
            if wanted:
                body["states"] = wanted
            if cursor:
                body["cursor"] = cursor
            page = self.call("POST", "/v1/atomic-memory/list", body) or {}
            records.extend(MemoryRecord.from_api(item) for item in page.get("items") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        return records

    def forget(self, record: MemoryRecord, *, scope_id: str) -> bool:
        """置为 forgotten；已被并发改动作废（409 atomic_memory_changed）视为无需处理。"""
        try:
            result = self.call(
                "POST",
                "/v1/atomic-memory/lifecycle",
                {"scope_id": scope_id, "target": record.target, "state": "forgotten"},
            )
        except PowerContextError as exc:
            if exc.code == "atomic_memory_changed" or exc.status in (404, 409):
                return False
            raise
        return bool((result or {}).get("changed"))

    def records_for(self, scope_id: str, kind: str, *, match=None,
                    states: Iterable[str] = ("active",)) -> list[MemoryRecord]:
        found = self.list_records(scope_id, kind=kind, states=states)
        if match is None:
            return found
        return [record for record in found if match(record)]


def _api_error_message(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message") or ""
            details = err.get("details")
            if isinstance(details, dict):
                errors = details.get("errors")
                if isinstance(errors, list) and errors:
                    first = errors[0]
                    loc = ".".join(str(part) for part in (first.get("loc") or [])[1:])
                    message = f"{message}（{loc}: {first.get('msg')}）" if loc else message
            return f"{code}: {message}" if code else str(message)
        if isinstance(err, str):
            return err
    return ""


def _preview(value: Any, limit: int = 200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _summarize(body: dict[str, Any]) -> dict[str, Any]:
    summary = {key: _preview(value) for key, value in body.items()
               if key in {"title", "summary", "idempotency_key", "kind", "text", "state", "reason",
                          "states", "limit", "cursor", "target", "params"}}
    return summary


class PowerContextService:
    """本仓库自带的 PowerContext 子进程：拉起、就绪等待、优雅退出。"""

    def __init__(self, *, settings: Settings | None = None, host: str = "127.0.0.1",
                 port: int | None = None, ready_timeout: float = 30.0) -> None:
        self.settings = settings or get_settings()
        self.host = host
        self.port = int(port if port is not None else _url_port(self.settings.powercontext_url, 8765))
        self.ready_timeout = ready_timeout
        self.process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def data_dir(self) -> Path:
        return self.settings.powercontext_data_dir

    @property
    def log_path(self) -> Path:
        return self.settings.log_dir / "powercontext_server.log"

    def _targets_local_loopback(self) -> bool:
        try:
            host = httpx.URL(self.settings.powercontext_url).host or ""
        except httpx.InvalidURL:
            return True
        return host in {"127.0.0.1", "localhost", "::1", ""}

    def command(self) -> list[str]:
        tail = ["server", "run", "--no-env-file", "--host", self.host, "--port", str(self.port)]
        binary = find_binary(self.settings)
        if binary:
            return [binary, *tail]
        # 没有 console script 时直接用导入入口（powercontext.cli.app 里没有 __main__ 守卫）
        return [sys.executable, "-c", "from powercontext.cli.app import main; main()", *tail]

    def client(self, **kwargs: Any) -> PowerContextClient:
        return PowerContextClient(base_url=kwargs.pop("base_url", self.base_url),
                                  settings=self.settings, **kwargs)

    def wait_ready(self, *, timeout: float | None = None, client: PowerContextClient | None = None) -> bool:
        deadline = time.monotonic() + (timeout if timeout is not None else self.ready_timeout)
        probe = client or self.client(timeout=2.0)
        try:
            while time.monotonic() < deadline:
                if probe.is_ready():
                    return True
                proc = self.process
                if proc is not None and proc.poll() is not None:
                    return False
                time.sleep(0.25)
        finally:
            if client is None:
                probe.close()
        return False

    def start(self, *, force: bool = False) -> bool:
        """外部已有服务时只等待就绪；否则自己 spawn 一个子进程。"""
        with self._lock:
            probe = self.client(timeout=2.0)
            try:
                externally_ready = probe.is_ready()
            finally:
                probe.close()
            if externally_ready and not force and self.process is None:
                return True
            if self.process is not None and self.process.poll() is None:
                return True
            if not self._targets_local_loopback():
                # POWERCONTEXT_URL 指向远端：本进程不该再起一个本地服务
                return self.wait_ready()
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["POWERCONTEXT_HOME"] = str(self.data_dir)
            handle = self.log_path.open("ab")
            try:
                self.process = subprocess.Popen(
                    self.command(), stdout=handle, stderr=subprocess.STDOUT, env=env,
                    cwd=str(self.settings.root),
                )
            finally:
                handle.close()
            audit.log_event("powercontext", settings=self.settings, event="server_spawn",
                            command=self.command(), data_dir=str(self.data_dir),
                            base_url=self.base_url, pid=self.process.pid if self.process else None)
            return self.wait_ready()

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            proc = self.process
            if proc is None or proc.poll() is not None:
                self.process = None
                return
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
            self.process = None
            audit.log_event("powercontext", settings=self.settings, event="server_stop", pid=proc.pid)

    def restart(self) -> bool:
        """重启自己拉起的子进程；外部服务只等待就绪（数据在磁盘，档案不丢）。"""
        if self.process is None:
            return self.start()
        self.stop()
        _wait_port_free(self.host, self.port, timeout=8.0)
        return self.start(force=True)


def _url_port(url: str, default: int) -> int:
    try:
        parsed = httpx.URL(url)
        return parsed.port or default
    except (httpx.InvalidURL, ValueError):
        return default


def _wait_port_free(host: str, port: int, *, timeout: float = 5.0) -> bool:
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex((host, port)) != 0:
                return True
        time.sleep(0.2)
    return False


def find_binary(settings: Settings | None = None) -> str | None:
    """定位 powercontext 可执行文件：显式配置 → PATH → 同解释器 bin 目录。"""
    cfg = settings or get_settings()
    candidates = [cfg.powercontext_binary, shutil.which("powercontext"),
                  str(Path(sys.executable).with_name("powercontext"))]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


# =========================================================================== #
# 档案语义层
# =========================================================================== #

_STATUS_RANK = {"done": 0, "blocked": 3, "doing": 2, "todo": 1}


@dataclass
class TeamProfile:
    name: str
    scope_id: str
    choices: list[MemoryRecord] = field(default_factory=list)
    progress: list[MemoryRecord] = field(default_factory=list)
    blockers: list[MemoryRecord] = field(default_factory=list)
    notes: list[MemoryRecord] = field(default_factory=list)

    @property
    def tracks(self) -> list[str]:
        seen: dict[str, None] = {}
        for record in list(self.choices) + list(self.progress):
            track = str(record.data.get("track") or "")
            if track:
                seen.setdefault(track, None)
        return list(seen)

    def topics_for(self, track: str) -> list[str]:
        topics: dict[str, None] = {}
        for choice in self.choices:
            if str(choice.data.get("track") or "") != track:
                continue
            for topic in choice.data.get("topics") or []:
                if topic:
                    topics.setdefault(str(topic), None)
        for record in self.progress:
            if str(record.data.get("track") or "") == track and record.data.get("topic"):
                topics.setdefault(str(record.data.get("topic")), None)
        return list(topics)

    def status_of(self, track: str, topic: str) -> str:
        """同一赛题多条进度记录取「离完成最远」的那个（阻塞优先于进行中优先于待办）。"""
        statuses = [
            normalize_status(record.data.get("status"))
            for record in self.progress
            if str(record.data.get("track") or "") == track and str(record.data.get("topic") or "") == topic
        ]
        if not statuses:
            return "todo"
        return max(statuses, key=lambda status: _STATUS_RANK.get(status, 1))

    def progress_for(self, track: str, topic: str) -> list[dict[str, Any]]:
        return [
            record.to_dict()
            for record in self.progress
            if str(record.data.get("track") or "") == track and str(record.data.get("topic") or "") == topic
        ]

    def open_blockers(self, track: str = "", topic: str = "") -> list[MemoryRecord]:
        return [
            record
            for record in self.blockers
            if not record.data.get("resolved")
            and (not track or str(record.data.get("track") or "") == track)
            and (not topic or str(record.data.get("topic") or "") == topic)
        ]

    def latest_note(self, track: str = "", topic: str = "") -> str:
        for record in reversed(self.notes):
            if (not track or str(record.data.get("track") or "") == track) and (
                not topic or str(record.data.get("topic") or "") == topic
            ):
                return str(record.data.get("text") or "")
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "team": self.name,
            "scope_id": self.scope_id,
            "tracks": self.tracks,
            "topics": {track: self.topics_for(track) for track in self.tracks},
            "choices": [record.to_dict() for record in self.choices],
            "progress": [record.to_dict() for record in self.progress],
            "blockers": [record.to_dict() for record in self.blockers],
            "notes": [record.to_dict() for record in self.notes],
            "updated_at": max((record.ts for record in self.all_records()), default=""),
        }

    def all_records(self) -> Iterable[MemoryRecord]:
        return [*self.choices, *self.progress, *self.blockers, *self.notes]


class TeamStore:
    """上层只认队名与语义方法，不感知 PowerContext 报文。"""

    def __init__(self, client: PowerContextClient | None = None,
                 *, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client

    @property
    def client(self) -> PowerContextClient:
        if self._client is None:
            self._client = PowerContextClient(settings=self.settings)
        return self._client

    # ---------------------------------------------------------------- 队伍
    def ensure_team(self, name: str) -> dict[str, Any]:
        title = _clean_name(name)
        existed = self.client.find_scope(title) is not None
        scope_id = self.client.ensure_scope(title)
        return {"name": title, "scope_id": scope_id, "created": not existed}

    def scope_id(self, name: str) -> str:
        """未建档则自动建档，保证所有写入路径都能落地。"""
        return self.client.ensure_scope(_clean_name(name))

    def teams(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for scope in self.client.list_scopes():
            title = str(scope.get("title") or "").strip()
            summary = str(scope.get("summary") or "")
            if not title or str(scope.get("summary") or "") != SCOPE_SUMMARY:
                continue
            scope_id = str(scope.get("scope_id") or "")
            records = self._records(scope_id, KIND_TRACK_CHOICE) + self._records(scope_id, KIND_PROGRESS)
            tracks = sorted({str(record.data.get("track") or "") for record in records if record.data.get("track")})
            rows.append({
                "name": title,
                "scope_id": scope_id,
                "tracks": tracks,
                "updated_at": max((record.ts for record in records), default=""),
            })
        rows.sort(key=lambda row: row["name"])
        return rows

    def profile(self, name: str) -> TeamProfile:
        scope_id = self.client.find_scope(_clean_name(name))
        if not scope_id:
            raise KeyError(f"队伍不存在：{name}")
        return TeamProfile(
            name=_clean_name(name),
            scope_id=scope_id,
            choices=self._records(scope_id, KIND_TRACK_CHOICE),
            progress=self._records(scope_id, KIND_PROGRESS),
            blockers=self._records(scope_id, KIND_BLOCKER),
            notes=self._records(scope_id, KIND_NOTE),
        )

    def profile_or_none(self, name: str) -> TeamProfile | None:
        try:
            return self.profile(name)
        except KeyError:
            return None

    def _records(self, scope_id: str, kind: str) -> list[MemoryRecord]:
        records = self.client.list_records(scope_id, kind=kind, states=("active",))
        return sorted(records, key=lambda record: (record.ts, record.artifact_id))

    # ---------------------------------------------------------------- 写入
    def set_track_choices(self, name: str, track: str, topics: Iterable[str] | None = None) -> dict[str, Any]:
        track = str(track).strip()
        if not track:
            raise ValueError("赛道不能为空")
        scope_id = self.scope_id(name)
        wanted = [str(topic).strip() for topic in (topics or []) if str(topic).strip()]
        for record in self._records(scope_id, KIND_TRACK_CHOICE):
            if str(record.data.get("track") or "") == track:
                self.client.forget(record, scope_id=scope_id)
        if not wanted:
            return {"team": _clean_name(name), "track": track, "removed": True}
        payload = {"track": track, "topics": wanted, "active": True, "ts": _now_iso()}
        record = self.client.remember(scope_id, KIND_TRACK_CHOICE, payload, reason="选择赛道")
        return {"team": _clean_name(name), "track": track, "topics": wanted, "removed": False,
                "record": record.to_dict()}

    def set_progress(self, name: str, track: str, topic: str, status: str,
                     note: str = "") -> dict[str, Any]:
        topic = str(topic).strip()
        if not topic:
            raise ValueError("赛题不能为空")
        scope_id = self.scope_id(name)
        track = str(track).strip() or _first_track(self._records(scope_id, KIND_TRACK_CHOICE))
        same_topic = lambda record: str(record.data.get("topic") or "") == topic  # noqa: E731
        forgotten = [
            record.to_dict()
            for record in self.client.records_for(scope_id, KIND_PROGRESS, match=same_topic)
            if self.client.forget(record, scope_id=scope_id)
        ]
        payload = {"track": track, "topic": topic, "status": normalize_status(status),
                   "note": str(note or ""), "ts": _now_iso()}
        record = self.client.remember(scope_id, KIND_PROGRESS, payload, reason=f"更新进度：{topic}")
        return {"team": _clean_name(name), "progress": record.to_dict(),
                "forgotten": forgotten, "replaced": len(forgotten)}

    def add_blocker(self, name: str, text: str, *, track: str = "",
                    topic: str = "") -> dict[str, Any]:
        text = str(text).strip()
        if not text:
            raise ValueError("卡点描述不能为空")
        scope_id = self.scope_id(name)
        payload = {"track": str(track or ""), "topic": str(topic or ""), "text": text,
                   "resolved": False, "ts": _now_iso()}
        record = self.client.remember(scope_id, KIND_BLOCKER, payload, reason="记录卡点")
        return record.to_dict()

    def resolve_blocker(self, name: str, blocker_id: str) -> dict[str, Any]:
        scope_id = self.scope_id(name)
        matches = [
            record
            for record in self._records(scope_id, KIND_BLOCKER)
            if record.artifact_id == blocker_id or record.artifact_id.startswith(blocker_id)
        ]
        if not matches:
            raise KeyError(f"卡点不存在或已解决：{blocker_id}")
        record = matches[0]
        payload = {**record.data, "resolved": True, "resolved_at": _now_iso()}
        updated = self.client.remember(scope_id, KIND_BLOCKER, payload, reason="标记卡点已解决")
        self.client.forget(record, scope_id=scope_id)
        return updated.to_dict()

    def add_note(self, name: str, text: str, *, track: str = "",
                 topic: str = "") -> dict[str, Any]:
        text = str(text).strip()
        if not text:
            raise ValueError("备注不能为空")
        scope_id = self.scope_id(name)
        payload = {"track": str(track or ""), "topic": str(topic or ""), "text": text, "ts": _now_iso()}
        record = self.client.remember(scope_id, KIND_NOTE, payload, reason="自由备注")
        return record.to_dict()

    # ---------------------------------------------------------------- 派生
    def unfinished(self, profile: TeamProfile) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for track in profile.tracks:
            for topic in profile.topics_for(track):
                status = profile.status_of(track, topic)
                if status == "done":
                    continue
                items.append({
                    "track": track,
                    "topic": topic,
                    "status": status,
                    "note": profile.latest_note(track, topic)
                    or next((str(r.data.get("note") or "") for r in reversed(profile.progress)
                             if str(r.data.get("topic") or "") == topic), ""),
                    "blockers": [record.to_dict() for record in profile.open_blockers(track, topic)],
                })
        return items


def _clean_name(name: str) -> str:
    return str(name or "").strip()


def _first_track(records: Iterable[MemoryRecord]) -> str:
    for record in records:
        track = str(record.data.get("track") or "")
        if track:
            return track
    return ""
