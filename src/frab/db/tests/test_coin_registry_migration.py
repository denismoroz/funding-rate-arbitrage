"""Migration test for coin_registry — applies migration to a fresh SQLite DB
and asserts all 7 seeded rows are exactly correct.

Uses alembic.command.upgrade() against a file-based sqlite temp DB so the
full migration path (including op.bulk_insert) is exercised end-to-end,
independent of the ORM init_db path used by other tests.
"""
from __future__ import annotations

import os
import tempfile

import pytest
import sqlalchemy as sa
from alembic import command as alembic_command
from alembic.config import Config


# ── Expected ground truth (mirrors the migration seed exactly) ───────────────
_EXPECTED_ROWS = {
    "BTC":  {"leverage": 40, "maint_ratio": 0.010, "spot_token": "UBTC",  "bridge_safe": True,  "active": True,  "position_size_usd": None, "sz_decimals": None},
    "ETH":  {"leverage": 25, "maint_ratio": 0.010, "spot_token": "UETH",  "bridge_safe": True,  "active": True,  "position_size_usd": None, "sz_decimals": None},
    "HYPE": {"leverage": 10, "maint_ratio": 0.025, "spot_token": "HYPE",  "bridge_safe": True,  "active": True,  "position_size_usd": None, "sz_decimals": None},
    "PURR": {"leverage":  3, "maint_ratio": 0.025, "spot_token": "PURR",  "bridge_safe": True,  "active": True,  "position_size_usd": None, "sz_decimals": None},
    "SOL":  {"leverage": 20, "maint_ratio": 0.025, "spot_token": "USOL",  "bridge_safe": True,  "active": True,  "position_size_usd": None, "sz_decimals": None},
    "XPL":  {"leverage": 10, "maint_ratio": 0.025, "spot_token": "XPL",   "bridge_safe": True,  "active": False, "position_size_usd": None, "sz_decimals": None},
    "ZEC":  {"leverage": 10, "maint_ratio": 0.025, "spot_token": "ZEC",   "bridge_safe": True,  "active": False, "position_size_usd": None, "sz_decimals": None},
}


def _make_alembic_cfg(db_path: str) -> Config:
    """Build an Alembic Config pointing at a fresh SQLite file.

    Directory layout (repo-relative):
      src/frab/db/tests/   <-- __file__ lives here
      4 levels up          <-- repo root (where alembic.ini lives)

    We do NOT pass the ini file path to Config() so that alembic skips its
    fileConfig() call in env.py. fileConfig() resets the root logger (level
    WARN, adds a StreamHandler) which breaks pytest's caplog in tests that
    run after this one in the same process. Instead we set only the options
    that alembic.command.upgrade() needs.
    """
    repo_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    )
    # Build a Config without an ini file to avoid fileConfig() pollution.
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(repo_root, "src/frab/db/migrations"))
    cfg.set_main_option("prepend_sys_path", repo_root)
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option(
        "sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}"
    )
    return cfg


@pytest.fixture
def migrated_db(tmp_path):
    """Temp SQLite DB upgraded to head via alembic migrations."""
    db_path = str(tmp_path / "test_migration.db")
    cfg = _make_alembic_cfg(db_path)
    alembic_command.upgrade(cfg, "head")
    yield db_path


def test_coin_registry_row_count(migrated_db):
    """Exactly 7 rows seeded."""
    engine = sa.create_engine(f"sqlite:///{migrated_db}")
    with engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM coin_registry")).scalar()
    engine.dispose()
    assert count == 7, f"Expected 7 seeded rows, got {count}"


def test_coin_registry_seed_correctness(migrated_db):
    """All 7 rows match the ground-truth spec exactly."""
    engine = sa.create_engine(f"sqlite:///{migrated_db}")
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT coin, leverage, maint_ratio, spot_token, bridge_safe, active,"
                "       position_size_usd, sz_decimals, validated_at"
                "  FROM coin_registry"
                " ORDER BY coin"
            )
        ).mappings().all()
    engine.dispose()

    assert len(rows) == 7, f"Expected 7 rows, got {len(rows)}"

    for row in rows:
        coin = row["coin"]
        assert coin in _EXPECTED_ROWS, f"Unexpected coin in seed: {coin!r}"
        expected = _EXPECTED_ROWS[coin]

        assert row["leverage"] == expected["leverage"], \
            f"{coin}: leverage mismatch: {row['leverage']} != {expected['leverage']}"
        assert abs(row["maint_ratio"] - expected["maint_ratio"]) < 1e-9, \
            f"{coin}: maint_ratio mismatch: {row['maint_ratio']} != {expected['maint_ratio']}"
        assert row["spot_token"] == expected["spot_token"], \
            f"{coin}: spot_token mismatch: {row['spot_token']} != {expected['spot_token']}"
        assert bool(row["bridge_safe"]) == expected["bridge_safe"], \
            f"{coin}: bridge_safe mismatch"
        assert bool(row["active"]) == expected["active"], \
            f"{coin}: active mismatch: {row['active']} != {expected['active']}"
        assert row["position_size_usd"] is None, \
            f"{coin}: position_size_usd should be NULL, got {row['position_size_usd']}"
        assert row["sz_decimals"] is None, \
            f"{coin}: sz_decimals should be NULL, got {row['sz_decimals']}"

        # validated_at must be non-NULL (seeded rows are pre-validated)
        assert row["validated_at"] is not None, \
            f"{coin}: validated_at should be non-NULL (seeded row is pre-validated)"
        assert isinstance(row["validated_at"], int), \
            f"{coin}: validated_at should be int epoch ms, got {type(row['validated_at'])}"
        assert row["validated_at"] > 0, \
            f"{coin}: validated_at should be a positive epoch ms"


def test_coin_registry_active_flags(migrated_db):
    """Active universe = 5 coins; inactive = 2 (ZEC, XPL)."""
    engine = sa.create_engine(f"sqlite:///{migrated_db}")
    with engine.connect() as conn:
        active = {
            row[0]
            for row in conn.execute(
                sa.text("SELECT coin FROM coin_registry WHERE active = 1")
            )
        }
        inactive = {
            row[0]
            for row in conn.execute(
                sa.text("SELECT coin FROM coin_registry WHERE active = 0")
            )
        }
    engine.dispose()

    assert active == {"BTC", "ETH", "SOL", "HYPE", "PURR"}
    assert inactive == {"ZEC", "XPL"}


def test_coin_registry_downgrade_drops_table(tmp_path):
    """downgrade -1 removes the coin_registry table."""
    db_path = str(tmp_path / "test_downgrade.db")
    cfg = _make_alembic_cfg(db_path)
    alembic_command.upgrade(cfg, "head")

    # Verify table exists
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        tables_before = {
            row[0]
            for row in conn.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    engine.dispose()
    assert "coin_registry" in tables_before

    # Downgrade one step
    alembic_command.downgrade(cfg, "-1")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        tables_after = {
            row[0]
            for row in conn.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    engine.dispose()
    assert "coin_registry" not in tables_after
