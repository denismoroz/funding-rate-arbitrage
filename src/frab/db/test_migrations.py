"""Tests for Alembic migrations: upgrade/downgrade round-trip and metadata parity."""
import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import Base

# Locate the project root relative to this file: src/frab/db/test_migrations.py
# -> parents[0] = db/, [1] = frab/, [2] = src/, [3] = project root
PROJECT_ROOT = Path(__file__).resolve().parents[3]

EXPECTED_TABLES = {
    "exchanges",
    "markets",
    "funding_rates",
    "prices",
    "strategies",
    "signals",
    "positions",
    "fills",
    "equity_snapshots",
    "events",
}


def _make_alembic_config(tmp_db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "src/frab/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{tmp_db_path}")
    return cfg


async def _get_table_names(db_path: Path) -> set[str]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    try:
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    finally:
        await engine.dispose()
    return names


def _sync_get_table_names(db_path: Path) -> set[str]:
    return asyncio.run(_get_table_names(db_path))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_initial_migration_creates_all_tables(tmp_path, mocker):
    tmp_db = tmp_path / "test_migrations.db"
    cfg = _make_alembic_config(tmp_db)

    spy = mocker.spy(command, "upgrade")
    command.upgrade(cfg, "head")
    spy.assert_called_once()

    table_names = _sync_get_table_names(tmp_db)

    # alembic_version table is also present — filter it out
    app_tables = table_names - {"alembic_version"}
    assert app_tables == EXPECTED_TABLES


def test_downgrade_drops_all_tables(tmp_path):
    tmp_db = tmp_path / "test_downgrade.db"
    cfg = _make_alembic_config(tmp_db)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    table_names = _sync_get_table_names(tmp_db)

    # After full downgrade only alembic_version may remain
    app_tables = table_names - {"alembic_version"}
    assert app_tables == set()


def test_metadata_matches_migration(tmp_path):
    tmp_db = tmp_path / "test_meta.db"
    cfg = _make_alembic_config(tmp_db)

    command.upgrade(cfg, "head")

    table_names = _sync_get_table_names(tmp_db)

    app_tables = table_names - {"alembic_version"}
    metadata_tables = set(Base.metadata.tables.keys())
    assert app_tables == metadata_tables
