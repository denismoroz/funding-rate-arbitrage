"""Remove doubled rows from funding_accruals.

Why these exist: HL keeps hourly funding payments for about a week, then folds
each UTC day into one userFunding entry stamped 00:00:00.000 with nSamples set
(frab.exchanges.hyperliquid.wire.HLFundingDelta). The loader deduped by ts_ms
only, so every 24 h full sweep stored that summary next to the 24 hourly rows it
sums: ETH #19 had $1.066 in the DB against $0.553 HL paid (cumFunding.sinceOpen).
Rare races also stored the same hourly row twice (same position, ts and amount).

The fixed loader heals OPEN positions on its first full sweep after deploy.
This script also covers closed positions.

Safety rules built in:
  * a 00:00:00.000 row is deleted only if HL's own userFunding (FRAB or XSMOM
    wallet) has a daily summary with that coin, stamp and amount, AND the
    position already holds at least nSamples hourly rows inside that day;
    for old days HL no longer returns, only if 24+ hourly rows of that day
    sum to the row to 6 decimals;
    a summary standing in for a partly missing day is reported, not touched;
  * an exact repeat (same position_id, ts_ms, amount) keeps its first row;
  * the deleted rows are copied to a backup table first;
  * dry-run by default; --apply is required to write.

Run ON PROD (reads the wallet addresses from .env):
    uv run python scripts/dedup_funding_accruals.py            # show the plan
    uv run python scripts/dedup_funding_accruals.py --apply    # delete
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.request
from collections import defaultdict

from frab.settings import Settings

DB = "data/frab.db"
DAY_MS = 86_400_000
HL_INFO = "https://api.hyperliquid.xyz/info"


def user_funding(address: str) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        req = urllib.request.Request(
            HL_INFO,
            data=json.dumps({"type": "userFunding", "user": address, "startTime": start}).encode(),
            headers={"Content-Type": "application/json"},
        )
        batch = json.load(urllib.request.urlopen(req, timeout=30))
        rows += batch
        if len(batch) < 500:
            return rows
        start = int(batch[-1]["time"]) + 1


def main() -> int:
    apply = "--apply" in sys.argv
    settings = Settings()
    summaries: dict[tuple[str, int, float], int] = {}
    for address in (settings.hl_account_address, settings.xsmom_hl_account_address):
        if address is None:
            continue
        for r in user_funding(address):
            d = r["delta"]
            if d.get("nSamples") is not None:
                summaries[(d["coin"], int(r["time"]), round(float(d["usdc"]), 6))] = int(d["nSamples"])
    print(f"HL daily summaries: {len(summaries)}")

    con = sqlite3.connect(DB)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT a.id, a.position_id, p.coin, a.ts_ms, a.amount "
        "FROM funding_accruals a JOIN positions p ON p.id = a.position_id "
        "ORDER BY a.position_id, a.ts_ms, a.id"
    ).fetchall()

    to_delete: list[int] = []
    seen: set[tuple[int, int, float]] = set()
    by_pos: dict[int, list[tuple[int, str, int, float]]] = defaultdict(list)
    repeats = 0
    for rid, pid, coin, ts, amount in rows:
        key = (pid, ts, amount)
        if key in seen:
            to_delete.append(rid)
            repeats += 1
            continue
        seen.add(key)
        by_pos[pid].append((rid, coin, ts, amount))

    dropped: dict[int, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
    partial = []
    for pid, prow in by_pos.items():
        for rid, coin, ts, amount in prow:
            if ts % DAY_MS:
                continue
            day = [a for _, _, t, a in prow if ts < t < ts + DAY_MS]
            n = summaries.get((coin, ts, round(amount, 6)))
            if n is None:
                # HL no longer returns some old summaries; a row is still one
                # when a full day of hours stands next to it and sums to it
                if len(day) >= 24 and round(sum(day), 6) == round(amount, 6):
                    n = 24
                else:
                    continue
            hours = len(day)
            if hours >= n:
                to_delete.append(rid)
                c, s = dropped[pid]
                dropped[pid] = (c + 1, s + amount)
            else:
                partial.append((pid, coin, ts, n, hours))

    for pid, (c, s) in sorted(dropped.items()):
        print(f"pos#{pid:<4} summaries={c:<4} -${s:.6f}")
    print(f"\nexact repeats: {repeats}; summaries next to their hours: "
          f"{sum(c for c, _ in dropped.values())} across {len(dropped)} positions; "
          f"rows to delete: {len(to_delete)}")
    for p in partial:
        print("left alone (day partly missing, summary stands in):", p)

    if not apply:
        print("\nDRY-RUN — no changes written. Re-run with --apply to commit.")
        return 0

    bak = f"funding_accruals_dedup_bak_{int(time.time())}"
    cur.execute(f"CREATE TABLE {bak} AS SELECT * FROM funding_accruals WHERE 0")
    for i in range(0, len(to_delete), 500):
        chunk = to_delete[i:i + 500]
        marks = ",".join("?" * len(chunk))
        cur.execute(f"INSERT INTO {bak} SELECT * FROM funding_accruals WHERE id IN ({marks})", chunk)
        cur.execute(f"DELETE FROM funding_accruals WHERE id IN ({marks})", chunk)
    con.commit()
    print(f"\nApplied. Deleted {len(to_delete)} rows; copies in {bak}.")
    print(f"Rollback if needed: INSERT INTO funding_accruals SELECT * FROM {bak};")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
