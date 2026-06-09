#!/usr/bin/env python3
"""One-off: scrub stale-cash equity "bumps" from historical equity_snapshots.

Background
----------
total_equity = cash + spot_value. On a delta-neutral open the spot buy hits
spot_value immediately, but the offsetting USDC debit only lands when the next
wallet_snapshot is written (~30 min lag). During that gap cash is stale-high
while spot is already counted, so total_equity inflates ("bump") then snaps
back when the wallet snapshot catches up.

The live fix (ledger._compute_position_values) now GATES each spot leg: it is
excluded while its opened_at > the latest cash wallet_snapshot ts. This script
applies the exact same gating retroactively to rows already persisted.

For each SPOT position P:
  catchup_ts = first cash (USDC/USDT) wallet_snapshot with ts_ms >= P.opened_at
  For every equity_snapshot row in [P.opened_at, catchup_ts) (same strategy):
      subtract P.qty * P.entry_price from total_equity AND spot_value
  (rows in that window are exactly the rows the new gate would have excluded).

Subtraction uses P.qty*entry_price (the open-time notional). Over the short
bump window mark ~= entry, so any residual is sub-dollar; the visible step is
removed and the curve becomes continuous.

Usage:
    python scripts/scrub_equity_bumps.py            # dry-run (no writes)
    python scripts/scrub_equity_bumps.py --apply    # backup + apply
"""
from __future__ import annotations

import sqlite3
import sys
import time

DB = "data/frab.db"
CASH = ("USDC", "USDT")


def main() -> int:
    apply = "--apply" in sys.argv
    con = sqlite3.connect(DB)
    cur = con.cursor()

    positions = cur.execute(
        """
        SELECT p.id, p.coin, p.qty, p.entry_price, p.opened_at, f.strategy_id,
               (SELECT min(w.ts_ms) FROM wallet_snapshots w
                 WHERE w.coin IN ('USDC','USDT') AND w.ts_ms >= p.opened_at) AS catchup
        FROM positions p
        JOIN farb_positions f ON f.id = p.farb_position_id
        WHERE p.instrument = 'SPOT'
        ORDER BY p.opened_at
        """
    ).fetchall()

    plan = []
    total_rows = 0
    for pid, coin, qty, entry, opened_at, strat, catchup in positions:
        if catchup is None:
            continue
        bump = qty * entry
        n = cur.execute(
            "SELECT count(*) FROM equity_snapshots "
            "WHERE strategy_id=? AND ts_ms>=? AND ts_ms<?",
            (strat, opened_at, catchup),
        ).fetchone()[0]
        if n == 0:
            continue
        win_min = (catchup - opened_at) / 60000.0
        plan.append((pid, coin, strat, opened_at, catchup, bump, n))
        total_rows += n
        print(
            f"pos#{pid:<3} {coin:<5} strat={strat} "
            f"window={win_min:5.1f}min rows={n:<4} bump=-${bump:,.2f}"
        )

    print(f"\n{len(plan)} positions, {total_rows} equity_snapshot rows to adjust.")

    if not apply:
        print("\nDRY-RUN — no changes written. Re-run with --apply to commit.")
        return 0

    bak = f"equity_snapshots_bak_{int(time.time())}"
    cur.execute(f"CREATE TABLE {bak} AS SELECT * FROM equity_snapshots")
    print(f"\nBackup table created: {bak}")

    for pid, coin, strat, opened_at, catchup, bump, n in plan:
        cur.execute(
            "UPDATE equity_snapshots "
            "SET total_equity = total_equity - ?, spot_value = spot_value - ? "
            "WHERE strategy_id=? AND ts_ms>=? AND ts_ms<?",
            (bump, bump, strat, opened_at, catchup),
        )
    con.commit()
    print(f"Applied. Adjusted {total_rows} rows across {len(plan)} windows.")
    print(f"Rollback if needed: DELETE FROM equity_snapshots; "
          f"INSERT INTO equity_snapshots SELECT * FROM {bak};")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
