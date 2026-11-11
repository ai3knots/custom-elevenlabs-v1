"""The Alembic history must build exactly the schema the models describe, and be reversible.

Runs against a throwaway SQLite file, never a real database. The tests are synchronous because Alembic's
async `env.py` starts its own event loop.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command
from src.db.base import Base

ROOT = Path(__file__).resolve().parent.parent
UNUSED_TABLES = {"agent_config_versions", "experiments"}


def _config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {name for (name,) in rows if name != "alembic_version"}


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "migrations.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    return path


def test_there_is_a_single_head() -> None:
    assert len(ScriptDirectory.from_config(_config()).get_heads()) == 1


def test_upgrade_builds_the_schema_the_models_describe(database: Path) -> None:
    command.upgrade(_config(), "head")
    assert _tables(database) == set(Base.metadata.tables)


def test_the_unused_tables_are_gone_after_upgrade_and_back_after_downgrade(database: Path) -> None:
    config = _config()
    command.upgrade(config, "19ddfd7201e0")
    assert _tables(database) >= UNUSED_TABLES

    command.upgrade(config, "head")
    assert not UNUSED_TABLES & _tables(database)

    command.downgrade(config, "19ddfd7201e0")
    assert _tables(database) >= UNUSED_TABLES
