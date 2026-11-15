"""可核对的 JSONL 审计日志：seekdb 检索、PowerContext 调用、LLM 生成各一个通道。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings, get_settings

CHANNELS: dict[str, str] = {
    "seekdb": "seekdb_queries.jsonl",
    "powercontext": "powercontext_calls.jsonl",
    "llm": "llm_calls.jsonl",
}

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(channel: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(channel)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[channel] = lock
        return lock


def log_path(channel: str, settings: Settings | None = None) -> Path:
    if channel not in CHANNELS:
        raise KeyError(f"unknown log channel: {channel}")
    cfg = settings or get_settings()
    return cfg.log_dir / CHANNELS[channel]


def log_event(channel: str, *, settings: Settings | None = None, **fields: Any) -> dict[str, Any]:
    """追加一行日志并返回写入内容；写盘失败不影响业务流程。"""
    payload: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
    payload.update(fields)
    path = log_path(channel, settings)
    with _lock_for(channel):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:  # 日志不可写时只标记，不抛给调用方
            payload["log_error"] = f"{type(exc).__name__}: {exc}"
    return payload


def read_tail(channel: str, limit: int = 50, settings: Settings | None = None) -> list[dict[str, Any]]:
    """读取最近若干行日志，供 /api/logs 自查。"""
    path = log_path(channel, settings)
    if not path.is_file():
        return []
    lines: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                lines.append(line)
            if len(lines) > limit:
                del lines[: len(lines) - limit]
    records: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            records.append({"ts": None, "raw": line, "parse_error": True})
    return records


def count_lines(channel: str, settings: Settings | None = None) -> int:
    path = log_path(channel, settings)
    if not path.is_file():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())
