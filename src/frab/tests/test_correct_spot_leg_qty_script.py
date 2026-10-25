"""Tests for scripts/correct_spot_leg_qty.py (HL calls patched, SQLite on disk)."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[3] / "scripts" / "correct_spot_leg_qty.py"
_spec = importlib.util.spec_from_file_location("correct_spot_leg_qty", _PATH)
script = importlib.util.module_from_spec(_spec)
sys.modules["correct_spot_leg_qty"] = script
_spec.loader.exec_module(script)

ADDRESS = "0xabc"
HYPE_BUY_MS = 1_791_478_870_396            # the DB fill, placed 5 s before HL's


def _hl_fill(*, oid, sz, px, fee, token, t=HYPE_BUY_MS + 4_600, coin="@107", side="B"):
    return {"oid": oid, "sz": str(sz), "px": str(px), "fee": str(fee), "feeToken": token,
            "coin": coin, "side": side, "time": t}


HYPE_FILL = _hl_fill(oid=568976683026, sz=0.2, px=83.908, fee=0.00013439, token="HYPE")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "frab.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE positions (id INTEGER PRIMARY KEY, farb_position_id INTEGER, coin TEXT,
            instrument TEXT, status TEXT, qty REAL, entry_price REAL);
        CREATE TABLE fills (id INTEGER PRIMARY KEY, position_id INTEGER, ts_ms INTEGER,
            side TEXT, qty REAL, price REAL, fee REAL);
        INSERT INTO positions VALUES (236, 31, 'HYPE', 'SPOT', 'OPEN', 0.2, 83.908);
        INSERT INTO fills VALUES (221, 236, 1791478870396, 'long', 0.2, 83.908, 0.01127639612);
        -- the perp leg, a closed spot leg and a sell must never be touched
        INSERT INTO positions VALUES (237, 31, 'HYPE', 'PERP', 'OPEN', 0.21, 83.776);
        INSERT INTO fills VALUES (222, 237, 1791478882152, 'short', 0.21, 83.776, 0.0076);
        INSERT INTO positions VALUES (30, 23, 'HYPE', 'SPOT', 'CLOSED', 0.19, 72.902);
        INSERT INTO fills VALUES (33, 30, 1780336984228, 'long', 0.19, 72.902, 0.00012767);
        INSERT INTO fills VALUES (216, 30, 1791414064859, 'short', 0.18, 88.077, 0.0106);
    """)
    con.commit()
    con.close()
    return path


def _run(monkeypatch, db, *extra, hl_fills=(HYPE_FILL,), wallet=None):
    monkeypatch.setattr(script, "user_fills_since", lambda address, since_ms: list(hl_fills))
    balances = wallet if wallet is not None else {"HYPE": "0.20973794"}
    monkeypatch.setattr(script, "hl_post", lambda body: {
        "balances": [{"coin": c, "total": t} for c, t in balances.items()]})
    monkeypatch.setattr(sys, "argv", ["correct_spot_leg_qty", "--db", str(db), "--address", ADDRESS, *extra])
    return script.main()


def _rows(db):
    con = sqlite3.connect(db)
    try:
        return (con.execute("SELECT id, qty FROM positions ORDER BY id").fetchall(),
                con.execute("SELECT id, qty FROM fills ORDER BY id").fetchall())
    finally:
        con.close()


def test_dry_run_prints_before_and_after_and_writes_nothing(monkeypatch, db, capsys):
    before = _rows(db)

    assert _run(monkeypatch, db) == 0

    out = capsys.readouterr().out
    assert "pos#236 (FRAB #31) HYPE  [change]" in out
    assert "positions.qty  0.2 -> 0.19986561" in out
    assert "fills#221.qty 0.2 -> 0.19986561" in out
    assert "HYPE   wallet 0.20973794  open legs 0.19986561  unrecorded +0.00987233" in out
    assert "DRY-RUN" in out
    assert _rows(db) == before
    assert not list(db.parent.glob("*.bak-spotqty-*"))


def test_apply_backs_up_then_corrects_only_the_open_spot_leg_and_its_buy_row(monkeypatch, db):
    before_positions, before_fills = _rows(db)

    assert _run(monkeypatch, db, "--apply") == 0

    positions, fills = _rows(db)
    assert dict(positions)[236] == pytest.approx(0.19986561)
    assert dict(fills)[221] == pytest.approx(0.19986561)
    # everything else is byte for byte as it was
    assert [p for p in positions if p[0] != 236] == [p for p in before_positions if p[0] != 236]
    assert [f for f in fills if f[0] != 221] == [f for f in before_fills if f[0] != 221]
    # the backup holds the old values
    (backup,) = db.parent.glob("frab.db.bak-spotqty-*")
    con = sqlite3.connect(backup)
    assert con.execute("SELECT qty FROM positions WHERE id = 236").fetchone() == (0.2,)
    assert con.execute("SELECT qty FROM fills WHERE id = 221").fetchone() == (0.2,)
    con.close()


def test_apply_is_idempotent(monkeypatch, db, capsys):
    _run(monkeypatch, db, "--apply")
    after_first = _rows(db)
    capsys.readouterr()

    assert _run(monkeypatch, db, "--apply") == 0

    out = capsys.readouterr().out
    assert "[already correct]" in out
    assert "Nothing to change." in out
    assert _rows(db) == after_first
    assert len(list(db.parent.glob("frab.db.bak-spotqty-*"))) == 1


def test_fills_of_one_order_are_summed(monkeypatch, db):
    pieces = [
        _hl_fill(oid=9, sz=0.15, px=83.908, fee=0.0001, token="HYPE"),
        _hl_fill(oid=9, sz=0.05, px=83.908, fee=0.00003, token="HYPE"),
        _hl_fill(oid=10, sz=0.2, px=83.908, fee=0.5, token="HYPE", t=HYPE_BUY_MS + 60_000),  # too late
    ]

    _run(monkeypatch, db, "--apply", hl_fills=pieces)

    assert dict(_rows(db)[0])[236] == pytest.approx(0.2 - 0.00013)


def test_unmatched_position_is_reported_and_left_alone(monkeypatch, db, capsys):
    before = _rows(db)
    other_coin_price = _hl_fill(oid=5, sz=0.2, px=70.0, fee=0.0001, token="HYPE")

    _run(monkeypatch, db, "--apply", hl_fills=[other_coin_price])

    out = capsys.readouterr().out
    assert "no single HL buy order matches" in out
    assert _rows(db) == before


def test_ambiguous_match_is_left_alone(monkeypatch, db, capsys):
    before = _rows(db)
    twins = [
        _hl_fill(oid=1, sz=0.2, px=83.908, fee=0.0001, token="HYPE"),
        _hl_fill(oid=2, sz=0.2, px=83.908, fee=0.0002, token="HYPE"),
    ]

    _run(monkeypatch, db, "--apply", hl_fills=twins)

    assert "no single HL buy order matches" in capsys.readouterr().out
    assert _rows(db) == before


def test_sell_fills_and_perp_fills_are_not_candidates(monkeypatch, db, capsys):
    before = _rows(db)
    wrong = [
        _hl_fill(oid=1, sz=0.2, px=83.908, fee=0.01, token="USDC", side="A"),
        _hl_fill(oid=2, sz=0.2, px=83.908, fee=0.01, token="USDC", coin="HYPE"),   # a perp fill
    ]

    _run(monkeypatch, db, "--apply", hl_fills=wrong)

    assert "no single HL buy order matches" in capsys.readouterr().out
    assert _rows(db) == before


def test_fee_paid_in_usdc_leaves_the_leg_alone(monkeypatch, db, capsys):
    before = _rows(db)
    usdc_fee = _hl_fill(oid=3, sz=0.2, px=83.908, fee=0.0113, token="USDC")

    _run(monkeypatch, db, "--apply", hl_fills=[usdc_fee])

    assert "none in the coin" in capsys.readouterr().out
    assert _rows(db) == before


def test_leg_with_two_buy_rows_is_left_alone(monkeypatch, db, capsys):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO fills VALUES (300, 236, 1791478871000, 'long', 0.01, 83.9, 0.0)")
    con.commit()
    con.close()
    before = _rows(db)

    _run(monkeypatch, db, "--apply")

    assert "2 buy rows in fills, expected 1" in capsys.readouterr().out
    assert _rows(db) == before


def test_no_open_spot_positions(monkeypatch, db, capsys):
    con = sqlite3.connect(db)
    con.execute("UPDATE positions SET status = 'CLOSED'")
    con.commit()
    con.close()

    assert _run(monkeypatch, db) == 0

    assert "no OPEN spot positions" in capsys.readouterr().out
