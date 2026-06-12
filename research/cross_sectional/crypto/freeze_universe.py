"""
Freeze the reproducible HL crypto universe to a static JSON snapshot.

WHY: cryptodata.universe() ranks the live HL perp universe by 24h volume and
applies liquidity/history/freshness filters. The volume ranking DRIFTS day to
day (coins cross the MIN_VOL_USD line, a candidate flips in/out — observed 35↔34
membership). A backtest MUST be deterministic: identical inputs → identical
numbers on rerun. So we call universe() ONCE, snapshot the sorted coin list to
universe.json (with the snapshot date + filter params for provenance), and the
package loads THIS frozen list, never a live universe() call.

Run once:  PYTHONPATH=... python freeze_universe.py
Rerun only intentionally (the snapshot is the contract; changing it changes the
backtest). Reruns of THIS script may differ if the live volume ranking moved —
that is exactly why we freeze.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import cryptodata

OUT = Path(__file__).parent / "universe.json"


def main() -> None:
    coins = cryptodata.universe()  # sorted alphabetically, reproducible-from-filters
    snapshot = {
        "snapshot_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "n_coins": len(coins),
        "coins": coins,
        "filters": {
            "MIN_VOL_USD": cryptodata.MIN_VOL_USD,
            "MIN_HISTORY_DAYS": cryptodata.MIN_HISTORY_DAYS,
            "MAX_NAN_FRAC": cryptodata.MAX_NAN_FRAC,
            "MAX_STALE_DAYS": cryptodata.MAX_STALE_DAYS,
            "CANDIDATE_POOL": cryptodata.CANDIDATE_POOL,
            "HISTORY_START": str(cryptodata.HISTORY_START.date()),
        },
    }
    OUT.write_text(json.dumps(snapshot, indent=2))
    print(f"Frozen universe ({len(coins)} coins) → {OUT}")
    print(", ".join(coins))


if __name__ == "__main__":
    main()
