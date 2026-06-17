"""Phase F1 migration test — seed 31 XSMOM coins into coin_registry.

Tests:
  1. After upgrade: 38 rows total, original 7 untouched, 31 new rows correct.
  2. After downgrade: back to 7 rows, original 7 unchanged.
  3. New rows have correct risk params (leverage=3, maint_ratio=0.05, active=False).
"""
from __future__ import annotations

import pytest
import sqlite3
import tempfile
import shutil

from pathlib import Path

# Migration revision IDs
_PREV_REVISION = "294489218bcb"
_F1_REVISION = "f1a2b3c4d5e6"

_ORIGINAL_7 = {"BTC", "ETH", "SOL", "HYPE", "PURR", "XPL", "ZEC"}
_NEW_31 = {
    "AAVE", "ADA", "APT", "ARB", "ATOM", "AVAX", "BCH", "BNB", "CRV",
    "DOGE", "DOT", "EIGEN", "ENA", "HMSTR", "INJ", "JTO", "JUP", "LINK",
    "LTC", "NEAR", "PENDLE", "PYTH", "SUI", "TAO", "TON", "TRX", "UNI",
    "WLD", "XLM", "XRP", "ZRO",
}


def _query_coins(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT coin, leverage, maint_ratio, active, validated_at FROM coin_registry ORDER BY coin"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_f1_migration_upgrade_yields_38_rows(tmp_path):
    """After applying the F1 migration, coin_registry has 38 rows."""
    import subprocess, sys

    # Copy the real DB (already at f1 head, so we need a clean copy at prev_revision).
    # Use the temp migtest db which we downgraded back to 7 rows.
    # Actually just run alembic on a temp copy of the real DB at the prior revision.

    # Start from a fresh temp DB and apply both migrations.
    db_file = tmp_path / "test_f1.db"
    env = {
        "FRAB_DB_URL": f"sqlite+aiosqlite:///{db_file}",
        "PATH": "/usr/bin:/bin",
    }
    # Use the project venv alembic
    project_root = Path(__file__).resolve().parents[3]
    alembic_bin = project_root / ".venv" / "bin" / "alembic"

    import os, subprocess
    full_env = {**os.environ, **env}

    # Upgrade to the head of the chain (both migrations in order)
    result = subprocess.run(
        [str(alembic_bin), "upgrade", _F1_REVISION],
        cwd=str(project_root),
        env=full_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"

    rows = _query_coins(str(db_file))
    coins = {r["coin"] for r in rows}

    assert len(rows) == 38, f"Expected 38 rows, got {len(rows)}: {sorted(coins)}"
    assert _ORIGINAL_7 <= coins, f"Original 7 missing: {_ORIGINAL_7 - coins}"
    assert _NEW_31 <= coins, f"New 31 missing: {_NEW_31 - coins}"


def test_f1_migration_original_7_untouched(tmp_path):
    """After F1 upgrade, the original 7 rows have their expected leverage/maint_ratio."""
    import os, subprocess

    db_file = tmp_path / "test_f1b.db"
    env = {**os.environ, "FRAB_DB_URL": f"sqlite+aiosqlite:///{db_file}"}
    project_root = Path(__file__).resolve().parents[3]
    alembic_bin = project_root / ".venv" / "bin" / "alembic"

    result = subprocess.run(
        [str(alembic_bin), "upgrade", _F1_REVISION],
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"

    rows = {r["coin"]: r for r in _query_coins(str(db_file))}

    # Check original 7 are intact
    assert rows["BTC"]["leverage"] == 40
    assert rows["BTC"]["maint_ratio"] == pytest.approx(0.01)
    assert rows["ETH"]["leverage"] == 25
    assert rows["ETH"]["maint_ratio"] == pytest.approx(0.01)
    assert rows["SOL"]["leverage"] == 20
    assert rows["SOL"]["maint_ratio"] == pytest.approx(0.025)
    assert rows["HYPE"]["leverage"] == 10
    assert rows["PURR"]["leverage"] == 3
    assert rows["ZEC"]["leverage"] == 10
    assert rows["XPL"]["leverage"] == 10


def test_f1_migration_new_31_have_fallback_params(tmp_path):
    """New 31 XSMOM rows have leverage=3, maint_ratio=0.05, active=False, validated_at != NULL."""
    import os, subprocess

    db_file = tmp_path / "test_f1c.db"
    env = {**os.environ, "FRAB_DB_URL": f"sqlite+aiosqlite:///{db_file}"}
    project_root = Path(__file__).resolve().parents[3]
    alembic_bin = project_root / ".venv" / "bin" / "alembic"

    result = subprocess.run(
        [str(alembic_bin), "upgrade", _F1_REVISION],
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"

    rows = {r["coin"]: r for r in _query_coins(str(db_file))}

    for coin in _NEW_31:
        assert coin in rows, f"{coin} missing from DB"
        r = rows[coin]
        assert r["leverage"] == 3, f"{coin}: leverage={r['leverage']} != 3"
        assert r["maint_ratio"] == pytest.approx(0.05), f"{coin}: maint_ratio={r['maint_ratio']} != 0.05"
        assert r["active"] == 0, f"{coin}: active={r['active']} should be False/0"
        assert r["validated_at"] is not None, f"{coin}: validated_at should not be NULL"


def test_f1_migration_downgrade_restores_7_rows(tmp_path):
    """After downgrade from F1, exactly the original 7 rows remain."""
    import os, subprocess

    db_file = tmp_path / "test_f1d.db"
    env = {**os.environ, "FRAB_DB_URL": f"sqlite+aiosqlite:///{db_file}"}
    project_root = Path(__file__).resolve().parents[3]
    alembic_bin = project_root / ".venv" / "bin" / "alembic"

    # Upgrade to F1
    r1 = subprocess.run(
        [str(alembic_bin), "upgrade", _F1_REVISION],
        cwd=str(project_root), env=env, capture_output=True, text=True,
    )
    assert r1.returncode == 0, f"upgrade failed:\n{r1.stderr}"

    # Downgrade back to previous revision
    r2 = subprocess.run(
        [str(alembic_bin), "downgrade", _PREV_REVISION],
        cwd=str(project_root), env=env, capture_output=True, text=True,
    )
    assert r2.returncode == 0, f"downgrade failed:\n{r2.stderr}"

    rows = _query_coins(str(db_file))
    coins = {r["coin"] for r in rows}

    assert len(rows) == 7, f"Expected 7 rows after downgrade, got {len(rows)}: {sorted(coins)}"
    assert coins == _ORIGINAL_7, f"Wrong coins after downgrade: {coins}"

    # Verify original 7 are untouched
    by_coin = {r["coin"]: r for r in rows}
    assert by_coin["BTC"]["leverage"] == 40
    assert by_coin["ETH"]["leverage"] == 25
    assert by_coin["SOL"]["leverage"] == 20
