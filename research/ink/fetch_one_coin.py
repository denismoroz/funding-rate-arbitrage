"""
Fetch funding rate history for a single coin from Nado DEX (Ink L2).

Nado = Vertex Protocol reborn on Ink L2 (Kraken's Optimism rollup, chain id 57073).
Previously: archive.prod.vertexprotocol.com (DEAD since 2025-08-14).
Now: archive.prod.nado.xyz

API docs: https://docs.nado.xyz/developer-resources/api/archive-indexer
Funding rates docs: https://docs.nado.xyz/funding-rates

Usage:
    python3 research/ink/fetch_one_coin.py BTC
    python3 research/ink/fetch_one_coin.py ALL [--refill]

Nado Archive API:
    Base:       https://archive.prod.nado.xyz/v1
    Endpoint:   /funding-rates  (exact path TBD — verify against docs)
    Params:     product_id (int), start_time (unix seconds), end_time (unix seconds)
                OR max_time (unix seconds), limit (int)
    Returns:    list of funding rate records, newest → oldest

Data availability:
    Nado Private Alpha launched: 2025-11-20
    Nado Open Beta (public):     2026-01-15
    => Data available from ~2025-11-20
    => "Warm window" 2025-09-01 is PARTIALLY covered (only from ~Nov 20)
    => Hot window pre-2025 is UNAVAILABLE (Nado did not exist)

Funding interval: hourly payments (rate recalculated ~every 20 seconds)
Normalization: funding_rate_per_hour * 24 * 365 * 100 = annualized_pct

IMPORTANT — Spot collateral note:
    On Nado, spot BTC = kBTC (wrapped), spot ETH = wETH (wrapped).
    For Strategy A (long spot + short perp on same venue), verify:
    - kBTC/BTC-PERP basis trade is feasible in unified margin account
    - Nado's risk engine auto-recognizes the hedge
    - SOL spot availability TBD (not confirmed as of 2026-06-04)
"""

# TODO: Before running this script, verify the exact archive endpoint URL for funding rates.
# The archive base URL is confirmed: https://archive.prod.nado.xyz/v1
# But the exact path for historical funding rates needs to be checked at:
#   https://docs.nado.xyz/developer-resources/api/archive-indexer
#
# The Nado Python SDK may provide a simpler interface:
#   pip install nado-python-sdk  (package: nadohq)
#   Docs: https://nadohq.github.io/nado-python-sdk/index.html

import csv
import datetime
import json
import sys
import time
import urllib.request
from pathlib import Path

# ============================================================
# CONFIGURATION — verify these against docs.nado.xyz before use
# ============================================================

ARCHIVE_BASE = "https://archive.prod.nado.xyz/v1"

# TODO: Verify exact endpoint path. Candidates:
#   /funding-rates
#   /products/{product_id}/funding-rates
#   /market/funding-rates
FUNDING_RATE_PATH = "/funding-rates"  # PLACEHOLDER — confirm from docs

# Product IDs on Nado (Vertex legacy format: integer IDs per product)
# TODO: Fetch actual IDs from: GET https://gateway.prod.nado.xyz/v1/contracts
# These are GUESSES based on Vertex ordering (Vertex used: 2=BTC, 4=ETH, 6=SOL, ...)
PRODUCT_IDS = {
    "BTC":  2,   # PLACEHOLDER — verify
    "ETH":  4,   # PLACEHOLDER — verify
    "SOL":  6,   # PLACEHOLDER — verify
    "DOGE": None,  # unknown — fetch from contracts endpoint
    "AVAX": None,  # unknown — fetch from contracts endpoint
    "LINK": None,  # unknown — fetch from contracts endpoint
    "HYPE": None,  # likely not listed on Nado (competitor token)
}

# Data windows
NADO_START = datetime.datetime(2025, 11, 20, 0, 0, 0, tzinfo=datetime.timezone.utc)
WARM_END   = datetime.datetime(2026, 4,   1, 0, 0, 0, tzinfo=datetime.timezone.utc)

COINS = ["BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK"]  # HYPE excluded (likely not listed)

OUT_DIR = Path(__file__).parent / "funding_history"
OUT_DIR.mkdir(exist_ok=True)

FIELDNAMES = ["ts_ms", "coin", "funding_rate_raw", "funding_rate_normalized", "annualized_pct"]

PAGE_SIZE = 1000


def coin_start(coin: str) -> datetime.datetime:
    """All coins start from Nado's launch date."""
    return NADO_START


def get_product_id(coin: str) -> int | None:
    """Return Nado product_id for a coin, or None if unknown."""
    pid = PRODUCT_IDS.get(coin)
    if pid is None:
        print(f"  WARNING: product_id for {coin} is unknown. "
              f"Run: GET https://gateway.prod.nado.xyz/v1/contracts to get the list.")
    return pid


def fetch_contracts() -> dict[str, int]:
    """
    Fetch all contracts from Nado gateway to discover product IDs.
    Returns dict: coin_symbol -> product_id

    TODO: Verify exact response schema against:
        https://docs.nado.xyz/developer-resources/api/gateway/queries/contracts
    """
    url = "https://gateway.prod.nado.xyz/v1/contracts"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "funding-arb-research/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            # TODO: parse actual response schema
            # Expected: {"contracts": [{"product_id": 2, "symbol": "BTC-PERP", ...}]}
            contracts = {}
            for item in data.get("contracts", []):
                symbol = item.get("symbol", "")
                pid = item.get("product_id")
                if symbol.endswith("-PERP") and pid is not None:
                    base = symbol.replace("-PERP", "")
                    contracts[base] = pid
            return contracts
    except Exception as exc:
        print(f"  ERROR fetching contracts: {exc}")
        return {}


def fetch_page(product_id: int, before_ts: int, max_retries: int = 6) -> list[dict]:
    """
    Fetch one page of funding rate history.

    TODO: Verify exact query parameters against docs.nado.xyz
    The Nado/Vertex archive API likely uses:
        - product_id: int
        - max_time or end_time: unix timestamp (seconds)
        - limit: int (max ~1000)
    """
    # TODO: Adjust URL template once exact schema is confirmed
    url = (f"{ARCHIVE_BASE}{FUNDING_RATE_PATH}"
           f"?product_id={product_id}"
           f"&max_time={before_ts}"
           f"&limit={PAGE_SIZE}")

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "funding-arb-research/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                # TODO: Adjust key name based on actual response schema
                # Likely: data["funding_rates"] or data["rates"] or top-level list
                return data.get("funding_rates", data if isinstance(data, list) else [])
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 5 * (2 ** attempt)
                print(f"  RATE-LIMITED pid={product_id} attempt {attempt+1}, sleep {wait}s")
                time.sleep(wait)
            elif e.code == 404:
                print(f"  404 for product_id={product_id}: endpoint path may be wrong.")
                print(f"  Check: {url}")
                return []
            else:
                print(f"  HTTP {e.code} for product_id={product_id} before_ts={before_ts}")
                return []
        except Exception as exc:
            print(f"  ERROR product_id={product_id}: {exc}")
            if attempt < max_retries - 1:
                time.sleep(3)
    return []


def parse_record(coin: str, r: dict) -> dict | None:
    """
    Parse a single funding rate record.

    TODO: Adjust field names based on actual API response schema.
    Nado inherits Vertex format, likely uses:
        - timestamp: int (unix seconds) OR
        - time: int (unix nanoseconds? milliseconds?) — check docs
        - funding_rate_x18: str (18-decimal fixed point) OR
        - rate: float
    """
    try:
        # Try multiple possible field names (resolve after first real API call)
        ts_raw = r.get("timestamp") or r.get("time") or r.get("ts")
        if ts_raw is None:
            return None

        # Convert to ms — handle seconds, ms, ns
        ts_int = int(ts_raw)
        if ts_int > 1e15:          # nanoseconds
            ts_ms = ts_int // 1_000_000
        elif ts_int > 1e12:        # milliseconds
            ts_ms = ts_int
        else:                       # seconds
            ts_ms = ts_int * 1000

        # Rate parsing — Vertex used x18 fixed-point string
        if "funding_rate_x18" in r:
            rate_raw = float(r["funding_rate_x18"]) / 1e18
        elif "rate" in r:
            rate_raw = float(r["rate"])
        elif "funding_rate" in r:
            rate_raw = float(r["funding_rate"])
        else:
            return None

        # Nado: hourly funding payment
        # annualized = rate_per_hour * 24h * 365d * 100%
        annualized = rate_raw * 24 * 365 * 100

        return {
            "ts_ms": ts_ms,
            "coin": coin,
            "funding_rate_raw": round(rate_raw, 10),
            "funding_rate_normalized": round(rate_raw, 10),
            "annualized_pct": round(annualized, 6),
        }
    except Exception:
        return None


def load_existing(coin: str) -> tuple[list[dict], set[int]]:
    path = OUT_DIR / f"{coin}.csv"
    rows: list[dict] = []
    ts_set: set[int] = set()
    if not path.exists():
        return rows, ts_set
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts = int(row["ts_ms"])
            ts_set.add(ts)
            rows.append({
                "ts_ms": ts,
                "coin": row["coin"],
                "funding_rate_raw": float(row["funding_rate_raw"]),
                "funding_rate_normalized": float(row["funding_rate_normalized"]),
                "annualized_pct": float(row["annualized_pct"]),
            })
    return rows, ts_set


def save(coin: str, all_rows: list[dict]) -> int:
    path = OUT_DIR / f"{coin}.csv"
    seen: set[int] = set()
    deduped = []
    for r in sorted(all_rows, key=lambda x: x["ts_ms"]):
        if r["ts_ms"] not in seen:
            seen.add(r["ts_ms"])
            deduped.append(r)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(deduped)
    return len(deduped)


def fetch_coin(coin: str, product_id: int, refill: bool = False) -> int:
    start_dt = coin_start(coin)
    end_dt = WARM_END

    existing_rows, existing_ts = load_existing(coin)
    print(f"{coin} (product_id={product_id}): {len(existing_rows)} existing rows")

    if refill:
        existing_rows = []
        existing_ts = set()
        print(f"  --refill: clearing existing data")

    new_rows: list[dict] = []
    cursor_ts = int(end_dt.timestamp())  # unix seconds, paginating backwards
    total_fetched = 0
    pages = 0
    start_ts = int(start_dt.timestamp())

    print(f"  Fetching {coin} from {start_dt.date()} to {end_dt.date()} ...")

    while cursor_ts > start_ts:
        records = fetch_page(product_id, cursor_ts)
        if not records:
            print(f"  Empty page at cursor_ts={cursor_ts}, stopping")
            break

        pages += 1
        added_this_page = 0

        for r in records:
            parsed = parse_record(coin, r)
            if parsed is None:
                continue
            rec_ts_s = parsed["ts_ms"] // 1000
            if rec_ts_s < start_ts:
                continue
            if rec_ts_s >= int(end_dt.timestamp()):
                continue
            if parsed["ts_ms"] not in existing_ts:
                existing_ts.add(parsed["ts_ms"])
                new_rows.append(parsed)
                added_this_page += 1
            total_fetched += 1

        # Move cursor to the oldest record in this page
        oldest_ts_raw = None
        for r in records:
            ts_raw = r.get("timestamp") or r.get("time") or r.get("ts")
            if ts_raw is not None:
                ts_int = int(ts_raw)
                if ts_int > 1e12:
                    ts_s = ts_int // 1000 if ts_int < 1e15 else ts_int // 1_000_000_000
                else:
                    ts_s = ts_int
                if oldest_ts_raw is None or ts_s < oldest_ts_raw:
                    oldest_ts_raw = ts_s

        if oldest_ts_raw is None or oldest_ts_raw >= cursor_ts:
            print(f"  Cursor did not advance, stopping to avoid infinite loop")
            break

        cursor_ts = oldest_ts_raw - 1

        if pages % 10 == 0:
            total = save(coin, existing_rows + new_rows)
            print(f"  [page {pages}] cursor={datetime.datetime.utcfromtimestamp(cursor_ts).date()} "
                  f"new_rows={len(new_rows)} total={total} (checkpoint)")

        if len(records) < PAGE_SIZE:
            print(f"  Last page ({len(records)} records), reached beginning")
            break

        time.sleep(0.3)

    total = save(coin, existing_rows + new_rows)
    print(f"DONE {coin}: {total} total rows saved ({pages} pages fetched)")
    return total


def main():
    args = sys.argv[1:]
    refill = "--refill" in args
    coin_args = [a for a in args if not a.startswith("--")]

    if not coin_args:
        print("Usage: fetch_one_coin.py COIN [--refill]")
        print(f"  COIN: one of {COINS} or ALL")
        print()
        print("IMPORTANT: Before first run, verify API endpoint by fetching contracts:")
        print("  python3 -c \"import urllib.request, json; "
              "print(json.dumps(json.loads(urllib.request.urlopen("
              "'https://gateway.prod.nado.xyz/v1/contracts').read()), indent=2))\"")
        sys.exit(1)

    coin_input = coin_args[0].upper()
    if coin_input == "ALL":
        targets = COINS
    elif coin_input in COINS:
        targets = [coin_input]
    elif coin_input == "CONTRACTS":
        # Utility: dump all contracts to see product IDs
        print("Fetching contracts from Nado gateway...")
        contracts = fetch_contracts()
        print(json.dumps(contracts, indent=2))
        return
    else:
        print(f"Unknown coin: {coin_input}. Options: {COINS} or ALL or CONTRACTS")
        sys.exit(1)

    # Try to auto-discover product IDs from contracts endpoint
    print("Attempting to discover product IDs from Nado contracts endpoint...")
    discovered = fetch_contracts()
    if discovered:
        print(f"  Discovered {len(discovered)} products: {discovered}")
        for coin, pid in discovered.items():
            if coin in PRODUCT_IDS:
                PRODUCT_IDS[coin] = pid

    for coin in targets:
        pid = get_product_id(coin)
        if pid is None:
            print(f"SKIP {coin}: product_id unknown. "
                  f"Run with 'CONTRACTS' argument first to discover IDs.")
            continue
        fetch_coin(coin, pid, refill=refill)
        if len(targets) > 1:
            time.sleep(1.0)


if __name__ == "__main__":
    main()
