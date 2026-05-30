"""Tests for frab CLI commands (init-db and seed)."""
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from frab.cli import _sync_db_url, app
from frab.db.models import Exchange, Market

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sync_engine(tmp_path: Path):
    db_url = f"sqlite:///{tmp_path}/test.db"
    return sqlalchemy.create_engine(db_url, future=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_init_db_creates_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("FRAB_DB_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("FRAB_DATA_DIR", str(tmp_path))

    result = runner.invoke(app, ["init-db"])

    assert result.exit_code == 0, result.output
    assert db_path.exists()

    # Inspect created tables via sync engine
    engine = sqlalchemy.create_engine(f"sqlite:///{db_path}", future=True)
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    # Tables are created via Base.metadata.create_all (no alembic_version)
    assert len(table_names) > 0
    assert "exchanges" in table_names
    assert "markets" in table_names
    assert "positions" in table_names


def test_seed_inserts_exchange_and_markets(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("FRAB_DB_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("FRAB_DATA_DIR", str(tmp_path))

    result = runner.invoke(app, ["init-db"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["seed"])
    assert result.exit_code == 0, result.output

    engine = sqlalchemy.create_engine(f"sqlite:///{db_path}", future=True)
    try:
        with Session(engine) as session:
            exchanges = session.execute(select(Exchange)).scalars().all()
            assert len(exchanges) == 1
            assert exchanges[0].name == "hyperliquid"

            markets = session.execute(select(Market)).scalars().all()
            assert len(markets) == 7
            coins = {m.coin for m in markets}
            assert coins == {"BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"}
    finally:
        engine.dispose()


def test_seed_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("FRAB_DB_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("FRAB_DATA_DIR", str(tmp_path))

    runner.invoke(app, ["init-db"])

    result1 = runner.invoke(app, ["seed"])
    assert result1.exit_code == 0, result1.output

    result2 = runner.invoke(app, ["seed"])
    assert result2.exit_code == 0, result2.output

    engine = sqlalchemy.create_engine(f"sqlite:///{db_path}", future=True)
    try:
        with Session(engine) as session:
            exchanges = session.execute(select(Exchange)).scalars().all()
            assert len(exchanges) == 1

            markets = session.execute(select(Market)).scalars().all()
            assert len(markets) == 7
    finally:
        engine.dispose()

    # Second run output should report 0 added
    assert "0 added" in result2.output
    assert "1 skipped" in result2.output  # exchange skipped
    assert "7 skipped" in result2.output  # markets skipped


def test_help_shows_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "init-db" in result.stdout
    assert "seed" in result.stdout
    assert "serve" in result.stdout
    assert "backfill" in result.stdout


def test_backfill_fetches_and_writes(tmp_path, monkeypatch, mocker):
    from frab.exchanges.protocol import FundingTick

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("FRAB_DB_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("FRAB_DATA_DIR", str(tmp_path))

    runner.invoke(app, ["init-db"])
    runner.invoke(app, ["seed"])

    fake_hl = mocker.MagicMock()
    fake_hl.fetch_funding_history = mocker.AsyncMock(side_effect=lambda coin, since_ms: [
        FundingTick(coin=coin, ts_ms=1_747_270_800_000, rate=0.0001, premium=0.0, annualized_pct=0.876),
    ])
    fake_hl.aclose = mocker.AsyncMock()
    mocker.patch("frab.cli.db.HLExchangeReader", return_value=fake_hl)

    result = runner.invoke(app, ["backfill", "--hours", "24", "--coins", "BTC,ETH"])

    assert result.exit_code == 0, result.output
    assert "Backfill complete" in result.output
    assert "BTC: 1 added" in result.output
    assert "ETH: 1 added" in result.output

    engine = sqlalchemy.create_engine(f"sqlite:///{db_path}", future=True)
    try:
        with Session(engine) as session:
            from frab.db.models import FundingRate
            count = session.execute(select(FundingRate)).scalars().all()
            assert len(count) == 2
    finally:
        engine.dispose()


def test_backfill_is_idempotent(tmp_path, monkeypatch, mocker):
    from frab.exchanges.protocol import FundingTick

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("FRAB_DB_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("FRAB_DATA_DIR", str(tmp_path))

    runner.invoke(app, ["init-db"])
    runner.invoke(app, ["seed"])

    fake_hl = mocker.MagicMock()
    fake_hl.fetch_funding_history = mocker.AsyncMock(side_effect=lambda coin, since_ms: [
        FundingTick(coin=coin, ts_ms=1_747_270_800_000, rate=0.0001, premium=0.0, annualized_pct=0.876),
    ])
    fake_hl.aclose = mocker.AsyncMock()
    mocker.patch("frab.cli.db.HLExchangeReader", return_value=fake_hl)

    runner.invoke(app, ["backfill", "--coins", "BTC"])
    result2 = runner.invoke(app, ["backfill", "--coins", "BTC"])

    assert "BTC: 0 added" in result2.output  # second run is no-op


def test_serve_invokes_uvicorn(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("FRAB_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    monkeypatch.setenv("FRAB_DATA_DIR", str(tmp_path))

    fake_app = object()
    build_spy = mocker.patch("frab.server.build_app", return_value=fake_app)
    run_spy = mocker.patch("frab.cli.serve.uvicorn.run")

    result = runner.invoke(app, ["serve", "--host", "127.0.0.1", "--port", "9999", "--coins", "BTC,ETH"])

    assert result.exit_code == 0, result.output
    build_spy.assert_called_once_with(("BTC", "ETH"), dry_run=False)
    run_spy.assert_called_once_with(fake_app, host="127.0.0.1", port=9999, log_level="info")


def test_sync_db_url_strips_aiosqlite():
    assert _sync_db_url("sqlite+aiosqlite:///foo.db") == "sqlite:///foo.db"
    assert _sync_db_url("sqlite+aiosqlite:////abs/path/db.db") == "sqlite:////abs/path/db.db"
