"""One-off: archive per-minute price history, thin it in the live DB, VACUUM.

Why: prices gained a row per coin every 30-60 s since 2026-05-28 and was never
cleaned -- ~7.2M rows, ~640 MB of the 800 MB prod DB (2026-10-25). The engine
now thins it hourly (EngineLoop._prune_prices: 7 days per-minute, older one row
per clock-hour); this script does the first, large pass in a maintenance window.

The full per-minute history older than the cutoff is copied first to
data/prices-archive.db (table prices, indexed by coin, ts_ms), which
research/execution/real_fill_audit.py can read instead of prices in frab.db.

Run ON PROD with the engine STOPPED (VACUUM rewrites the file) and after
`alembic upgrade head` (drops the duplicate ix_prices_lookup):
    uv run python scripts/archive_and_thin_prices.py            # show the plan
    uv run python scripts/archive_and_thin_prices.py --apply    # archive, thin, VACUUM
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

DB = "data/frab.db"
ARCHIVE = "data/prices-archive.db"
RETENTION_MS = 7 * 24 * 60 * 60 * 1000  # = frab.engine.loop._PRICE_RETENTION_MS

THIN = (
    "DELETE FROM prices WHERE rowid IN ("
    "  SELECT rowid FROM ("
    "    SELECT rowid, ts_ms, MAX(ts_ms) OVER ("
    "      PARTITION BY exchange_id, coin, ts_ms / 3600000"
    "    ) AS grp_max"
    "    FROM prices WHERE ts_ms < :cutoff"
    "  ) WHERE ts_ms < grp_max)"
)


def mb(path: str) -> str:
    return f"{os.path.getsize(path) / 1048576:,.0f} MB"


def main() -> int:
    apply = "--apply" in sys.argv
    cutoff = int(time.time() * 1000) - RETENTION_MS
    con = sqlite3.connect(DB)
    total, old = con.execute(
        "SELECT count(*), sum(ts_ms < ?) FROM prices", (cutoff,)
    ).fetchone()
    print(f"{DB}: {mb(DB)}; prices rows {total:,}, older than 7 days {old or 0:,}")

    if not apply:
        print("\nDRY-RUN — no changes written. Re-run with --apply (engine stopped).")
        return 0
    if os.path.exists(ARCHIVE):
        print(f"{ARCHIVE} already exists; refusing to archive twice.")
        return 1

    t0 = time.time()
    con.execute(f"ATTACH '{ARCHIVE}' AS arch")
    con.execute("CREATE TABLE arch.prices AS SELECT * FROM main.prices WHERE ts_ms < ?", (cutoff,))
    con.execute("CREATE INDEX arch.ix_prices_coin_ts ON prices (coin, ts_ms)")
    con.commit()
    archived = con.execute("SELECT count(*) FROM arch.prices").fetchone()[0]
    con.execute("DETACH arch")
    if archived != (old or 0):
        print(f"archive holds {archived:,} rows, expected {old:,}; stopping before any delete.")
        return 1
    print(f"archived {archived:,} rows to {ARCHIVE} ({mb(ARCHIVE)}) in {time.time() - t0:.0f}s")

    t0 = time.time()
    con.execute(THIN, {"cutoff": cutoff})
    con.commit()
    left = con.execute("SELECT count(*) FROM prices").fetchone()[0]
    print(f"thinned: {total:,} -> {left:,} rows in {time.time() - t0:.0f}s")

    t0 = time.time()
    con.execute("VACUUM")
    con.close()
    print(f"VACUUM done in {time.time() - t0:.0f}s; {DB}: {mb(DB)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
