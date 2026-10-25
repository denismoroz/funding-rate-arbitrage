"""Correct the recorded qty of OPEN spot legs to what the exchange actually delivered.

Why this exists: until the fix, OpenPositionAction recorded a spot buy at the size HL
filled, but HL takes the buy's fee in the bought coin, so the wallet received the
size LESS that fee. HYPE #31: filled 0.2, fee 0.00013439 HYPE, wallet got 0.19986561,
DB said 0.2. (The wallet showed 0.20973794 because 0.00987233 HYPE of the earlier HYPE
#23 close was still in it, which the DB never recorded either: a sub-lot remainder
nobody can sell.) The fixed code records new spot legs net of the fee; this script
brings the rows that are open now to the same footing.

What it does, per OPEN spot position:
  * finds the buy in HL userFills: side B, spot coin, same price, size within 1%,
    within 30 s of the DB fill. Every fill of that order (same oid) is summed;
  * corrected qty = summed size - summed fee paid in the coin (fee tokens other than USDC);
  * proposes positions.qty and the buy row in fills.qty := corrected qty. The fee in
    USDC stays as it is (it is the fee converted at the fill price); a smaller qty
    with that fee is the right way round: tokens received * price + fee = USDC paid.

Nothing else is touched: not state_data.spot_qty (that is the wallet-anchored hedge
size the perp leg was sized from), not closed positions, not the perp legs.

For the reviewer it also prints, per coin, the wallet balance against the corrected sum
of OPEN legs: the difference is the unrecorded remainder of earlier closes.

Safety rules built in:
  * dry-run by default; --apply is required to write;
  * --apply first copies the whole DB to <db>.bak-spotqty-<timestamp> (sqlite backup API);
  * one transaction; a position that cannot be matched to exactly one HL order, or whose
    buy row in fills is not exactly one, is reported and left alone;
  * idempotent: a row already equal to the corrected qty is left alone.

Run on a COPY first, or ON PROD (reads the wallet address from .env unless --address):
    uv run python scripts/correct_spot_leg_qty.py                  # show the plan
    uv run python scripts/correct_spot_leg_qty.py --apply          # write it
    uv run python scripts/correct_spot_leg_qty.py --db /path/to/copy.db --address 0x...
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass

HL_INFO = "https://api.hyperliquid.xyz/info"
MATCH_WINDOW_MS = 30_000
SIZE_TOLERANCE = 0.01          # DB fill qty vs HL fill size
PRICE_TOLERANCE = 1e-6         # entry_price is HL's avgPx: it must agree
USDC_TOKENS = ("USDC", "USD")


def hl_post(body: dict) -> object:
    req = urllib.request.Request(
        HL_INFO, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=30))


def user_fills_since(address: str, since_ms: int) -> list[dict]:
    """All userFills from since_ms on (userFillsByTime pages at 2000 rows)."""
    rows: list[dict] = []
    start = since_ms
    while True:
        batch = hl_post({"type": "userFillsByTime", "user": address, "startTime": start})
        assert isinstance(batch, list)
        rows += batch
        if len(batch) < 2000:
            return rows
        start = int(batch[-1]["time"]) + 1


@dataclass
class Plan:
    position_id: int
    farb_position_id: int | None
    coin: str
    token: str
    old_qty: float
    new_qty: float
    fill_id: int
    old_fill_qty: float
    gross: float
    fee_in_coin: float


def is_spot_coin(hl_coin: str) -> bool:
    return hl_coin.startswith("@") or "/" in hl_coin


def match_order(fills: list[dict], *, ts_ms: int, price: float, qty: float) -> list[dict] | None:
    """All HL fills of the one buy order that matches the DB fill, or None.

    Matching is by time, price and size (the DB stores no oid). If the candidates
    belong to more than one order the match is ambiguous and None is returned."""
    candidates = [
        f for f in fills
        if f["side"] == "B"
        and is_spot_coin(f["coin"])
        and abs(int(f["time"]) - ts_ms) <= MATCH_WINDOW_MS
        and abs(float(f["px"]) - price) <= PRICE_TOLERANCE * max(price, 1.0)
    ]
    by_oid: dict[int, list[dict]] = defaultdict(list)
    for f in candidates:
        by_oid[int(f["oid"])].append(f)
    hits = [
        group for group in by_oid.values()
        if abs(sum(float(f["sz"]) for f in group) - qty) <= max(qty * SIZE_TOLERANCE, 1e-12)
    ]
    return hits[0] if len(hits) == 1 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/frab.db")
    ap.add_argument("--address", default=None, help="wallet address (default: HL_ACCOUNT_ADDRESS from .env)")
    ap.add_argument("--apply", action="store_true", help="write the corrections (default: dry-run)")
    args = ap.parse_args()

    address = args.address
    if address is None:
        from frab.settings import Settings
        address = Settings().hl_account_address
    if not address:
        raise SystemExit("no wallet address: pass --address or set HL_ACCOUNT_ADDRESS")

    con = sqlite3.connect(args.db)
    positions = con.execute(
        "SELECT id, farb_position_id, coin, qty, entry_price FROM positions "
        "WHERE instrument = 'SPOT' AND status = 'OPEN' ORDER BY id"
    ).fetchall()
    if not positions:
        print("no OPEN spot positions")
        return 0

    first_ts = min(
        row[0] for row in con.execute(
            "SELECT MIN(f.ts_ms) FROM fills f JOIN positions p ON p.id = f.position_id "
            "WHERE p.instrument = 'SPOT' AND p.status = 'OPEN'"
        )
    )
    hl_fills = user_fills_since(address, first_ts - 60_000)
    spot_state = hl_post({"type": "spotClearinghouseState", "user": address})
    assert isinstance(spot_state, dict)
    wallet = {b["coin"]: float(b["total"]) for b in spot_state["balances"]}
    print(f"wallet {address}: {len(hl_fills)} userFills since the oldest open spot buy\n")

    plans: list[Plan] = []
    corrected_by_token: dict[str, float] = defaultdict(float)
    for pid, fpid, coin, qty, entry_price in positions:
        buys = con.execute(
            "SELECT id, ts_ms, qty, price FROM fills WHERE position_id = ? AND side = 'long'", (pid,)
        ).fetchall()
        label = f"pos#{pid} (FRAB #{fpid}) {coin}"
        if len(buys) != 1:
            print(f"{label}: {len(buys)} buy rows in fills, expected 1 -- left alone")
            continue
        fill_id, ts_ms, fill_qty, fill_price = buys[0]
        order = match_order(hl_fills, ts_ms=ts_ms, price=fill_price, qty=fill_qty)
        if order is None:
            print(f"{label}: no single HL buy order matches (ts={ts_ms}, px={fill_price}, "
                  f"qty={fill_qty}) -- left alone")
            continue
        gross = sum(float(f["sz"]) for f in order)
        coin_fees = [f for f in order if f["feeToken"].upper() not in USDC_TOKENS]
        tokens = {f["feeToken"] for f in coin_fees}
        if len(tokens) != 1:
            print(f"{label}: fee tokens {sorted(tokens) or 'none in the coin'} -- left alone")
            continue
        token = tokens.pop()
        fee_in_coin = sum(float(f["fee"]) for f in coin_fees)
        plan = Plan(pid, fpid, coin, token, qty, gross - fee_in_coin, fill_id, fill_qty, gross, fee_in_coin)
        plans.append(plan)
        corrected_by_token[token] += plan.new_qty

    changes = [p for p in plans if abs(p.new_qty - p.old_qty) > 1e-12 or abs(p.new_qty - p.old_fill_qty) > 1e-12]
    for p in plans:
        mark = "change" if p in changes else "already correct"
        print(f"pos#{p.position_id} (FRAB #{p.farb_position_id}) {p.coin}  [{mark}]")
        print(f"    HL filled {p.gross:.10g} - fee in {p.token} {p.fee_in_coin:.10g} = {p.new_qty:.10g} received")
        print(f"    positions.qty  {p.old_qty:.10g} -> {p.new_qty:.10g}  ({p.new_qty - p.old_qty:+.3e})")
        print(f"    fills#{p.fill_id}.qty {p.old_fill_qty:.10g} -> {p.new_qty:.10g}")

    print("\nwallet vs the corrected open legs (the gap is the remainder of earlier closes):")
    for token, recorded in sorted(corrected_by_token.items()):
        held = wallet.get(token, 0.0)
        print(f"    {token:<6} wallet {held:.10g}  open legs {recorded:.10g}  unrecorded {held - recorded:+.10g}")

    if not args.apply:
        print(f"\nDRY-RUN -- no changes written ({len(changes)} of {len(plans)} legs would change). "
              "Re-run with --apply to commit.")
        return 0
    if not changes:
        print("\nNothing to change.")
        return 0

    backup = f"{args.db}.bak-spotqty-{int(time.time())}"
    dest = sqlite3.connect(backup)
    con.backup(dest)
    dest.close()
    print(f"\nbackup: {backup}")
    with con:
        for p in changes:
            con.execute("UPDATE positions SET qty = ? WHERE id = ? AND status = 'OPEN'", (p.new_qty, p.position_id))
            con.execute("UPDATE fills SET qty = ? WHERE id = ?", (p.new_qty, p.fill_id))
    print(f"Applied: {len(changes)} legs corrected.")
    print("Rollback if needed (or restore the backup):")
    for p in changes:
        print(f"    UPDATE positions SET qty = {p.old_qty!r} WHERE id = {p.position_id};  "
              f"UPDATE fills SET qty = {p.old_fill_qty!r} WHERE id = {p.fill_id};")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
