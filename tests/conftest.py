"""测试公用装置：路径注入 + 隔离的 DATA_DIR / LOG_DIR。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings, reset_settings  # noqa: E402


@pytest.fixture()
def isolated_settings(tmp_path: Path) -> Settings:
    """把配置指向 tmp_path，避免测试写进仓库的 .runtime/ 与 logs/。"""
    env = {
        "DATA_DIR": str(tmp_path / "runtime"),
        "DOCS_DIR": str(tmp_path / "docs"),
        "LOG_DIR": str(tmp_path / "logs"),
        "KB_COLLECTION": "saitong_kb_test",
    }
    return reset_settings(Settings(env=env, root=ROOT))


@pytest.fixture(autouse=True)
def _restore_settings() -> None:
    yield
    reset_settings(Settings(env={}, root=ROOT))
