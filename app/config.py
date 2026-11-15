"""集中读取环境变量，向其余模块提供只读配置。"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_COLLECTION = "saitong_kb"
DEFAULT_DOCS_DIR = "data/docs"
DEFAULT_DATA_DIR = ".runtime"
DEFAULT_LOG_DIR = "logs"
DEFAULT_PORT = 7860
DEFAULT_HOST = "0.0.0.0"
DEFAULT_MAX_CHUNK_CHARS = 1200
DEFAULT_TOP_K = 5
DEFAULT_POWERCONTEXT_URL = "http://127.0.0.1:8765"
# 2026-11-18 24:00 北京时间
DEFAULT_DEADLINE = "2026-11-19T00:00:00+08:00"


def _load_dotenv(path: Path) -> None:
    """把 .env 里的键值写入环境（已导出的变量优先，不覆盖）。"""
    if not path.is_file():
        return
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    for key, value in dotenv_values(path).items():
        if value is not None:
            os.environ.setdefault(key, value)


def _as_int(raw: str | None, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """一次进程启动读一次环境，字段全部只读。"""

    def __init__(self, env: Mapping[str, str] | None = None, root: Path | None = None) -> None:
        _load_dotenv((root or PROJECT_ROOT) / ".env")
        source: Mapping[str, str] = env if env is not None else os.environ

        def get(name: str, default: str = "") -> str:
            value = source.get(name)
            return default if value is None or value.strip() == "" else value.strip()

        self.root: Path = Path(root or PROJECT_ROOT).resolve()
        self.data_dir: Path = self._resolve(get("DATA_DIR", DEFAULT_DATA_DIR))
        self.docs_dir: Path = self._resolve(get("DOCS_DIR", DEFAULT_DOCS_DIR))
        self.log_dir: Path = self._resolve(get("LOG_DIR", DEFAULT_LOG_DIR))
        self.host: str = get("HOST", DEFAULT_HOST)
        self.port: int = _as_int(get("PORT"), DEFAULT_PORT)

        self.kb_collection: str = get("KB_COLLECTION", DEFAULT_COLLECTION)
        self.kb_max_chunk_chars: int = _as_int(get("KB_MAX_CHUNK_CHARS"), DEFAULT_MAX_CHUNK_CHARS)
        self.kb_top_k: int = _as_int(get("KB_TOP_K"), DEFAULT_TOP_K)
        # auto：探测默认 ONNX 模型，拉不到就降级全文检索；onnx：强制向量化；fallback：直接降级
        self.kb_embedding: str = get("KB_EMBEDDING", "auto").lower()

        self.llm_base_url: str = get("LLM_BASE_URL")
        self.llm_api_key: str = get("LLM_API_KEY")
        self.llm_model: str = get("LLM_MODEL")
        self.llm_timeout: float = float(_as_int(get("LLM_TIMEOUT"), 30))

        self.powercontext_url: str = (
            get("POWERCONTEXT_URL") or get("POWERCONTEXT_BASE_URL") or DEFAULT_POWERCONTEXT_URL
        ).rstrip("/")
        self.powercontext_autostart: bool = _as_bool(get("POWERCONTEXT_AUTOSTART"), True)
        self.powercontext_binary: str = get("POWERCONTEXT_BIN")
        self.powercontext_ready_timeout: float = float(
            _as_int(get("POWERCONTEXT_READY_TIMEOUT"), 30)
        )
        self.powercontext_data_dir: Path = self._resolve(
            get("POWERCONTEXT_DATA_DIR"), default=self.data_dir / "powercontext"
        )

        self.deadline: datetime = self._parse_deadline(get("DEADLINE", DEFAULT_DEADLINE))

    def _resolve(self, value: str, *, default: Path | None = None) -> Path:
        if not value:
            return Path(default)
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.root / path
        return path

    @staticmethod
    def _parse_deadline(raw: str) -> datetime:
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            moment = datetime.fromisoformat(DEFAULT_DEADLINE)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def seekdb_path(self) -> Path:
        return self.data_dir / "seekdb"

    def days_left(self, now: datetime | None = None) -> int:
        """剩余自然天数；逾期返回负数。"""
        current = now or datetime.now(self.deadline.tzinfo)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        delta = (self.deadline - current).total_seconds() / 86400.0
        if delta > 0:
            return math.ceil(delta - 1e-9)
        return math.floor(delta + 1e-9)

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "data_dir": str(self.data_dir),
            "docs_dir": str(self.docs_dir),
            "log_dir": str(self.log_dir),
            "host": self.host,
            "port": self.port,
            "kb_collection": self.kb_collection,
            "kb_max_chunk_chars": self.kb_max_chunk_chars,
            "kb_top_k": self.kb_top_k,
            "kb_embedding": self.kb_embedding,
            "llm_enabled": self.llm_enabled,
            "llm_model": self.llm_model or None,
            "powercontext_url": self.powercontext_url,
            "powercontext_autostart": self.powercontext_autostart,
            "powercontext_data_dir": str(self.powercontext_data_dir),
            "deadline": self.deadline.isoformat(),
        }


_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings()
    return _SETTINGS


def reset_settings(settings: Settings | None = None) -> Settings:
    """测试注入用：替换（或重建）配置单例。"""
    global _SETTINGS
    _SETTINGS = settings if settings is not None else Settings()
    return _SETTINGS
