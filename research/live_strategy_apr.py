"""
Live strategy performance metrics — reads a frab SQLite snapshot and computes
APR, profit, occupied capital, and equity-curve data.

Designed to be importable as a library (compute_strategy_metrics) and
runnable as a CLI.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
SECONDS_PER_YEAR = 365.25 * 86400
EQUITY_CURVE_MAX_POINTS = 500
APR_MEANINGFUL_MIN_SECONDS = 30 * 86400

# Cash-flow detection threshold: a wallet USDC jump larger than this that is
# not explained by fills in the same 10-second window is treated as an
# external deposit or withdrawal.
CASH_FLOW_THRESHOLD_USD = 5.0


def _ts_to_iso(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.isoformat()


def _ts_to_msk_str(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=MSK)
    return dt.strftime("%Y-%m-%d %H:%M:%S MSK")


def _window_result(
    profit_usd: float | None,
    avg_occupied_usd: float | None,
    window_seconds: int,
    available: bool,
) -> dict:
    if not available or profit_usd is None or avg_occupied_usd is None:
        return {
            "profit_usd": None,
            "return_pct": None,
            "apr_pct": None,
            "apr_meaningful": False,
            "avg_occupied_usd": None,
            "window_seconds": window_seconds,
            "available": False,
        }

    if avg_occupied_usd > 0:
        r = profit_usd / avg_occupied_usd
        apr = ((1 + r) ** (SECONDS_PER_YEAR / window_seconds) - 1) * 100
        return_pct = r * 100
    else:
        apr = 0.0
        return_pct = 0.0

    return {
        "profit_usd": profit_usd,
        "return_pct": return_pct,
        "apr_pct": apr,
        "apr_meaningful": window_seconds >= APR_MEANINGFUL_MIN_SECONDS,
        "avg_occupied_usd": avg_occupied_usd,
        "window_seconds": window_seconds,
        "available": True,
    }


def _detect_external_cash_flows(con: sqlite3.Connection) -> list[dict]:
    """Detect external deposits/withdrawals from wallet_snapshots.

    A deposit/withdrawal is a jump in the USDC hl_account_total wallet balance
    that is not explained by spot fills in a ±60-minute window.  The 60-minute
    tolerance is necessary because the wallet snapshot can lag the fill by up to
    ~30 minutes in the observed data.

    Heuristic: if the sum of absolute spot-fill values within ±60 min of the
    wallet jump is >= 50% of the jump magnitude, the jump is position-related
    and is not flagged as an external flow.
    """
    rows = con.execute(
        """
        SELECT ts_ms, balance
        FROM wallet_snapshots
        WHERE coin = 'USDC' AND source = 'hl_account_total'
        ORDER BY ts_ms
        """
    ).fetchall()

    if not rows:
        return []

    # Build sorted list of (ts_ms, spot_fill_value) for efficient range queries
    spot_fills = con.execute(
        """
        SELECT f.ts_ms, ABS(f.qty * f.price) AS fill_abs_value
        FROM fills f
        JOIN positions p ON f.position_id = p.id
        WHERE p.instrument = 'SPOT'
        ORDER BY f.ts_ms
        """
    ).fetchall()

    def nearby_spot_fill_sum(center_ts: int, window_ms: int = 3_600_000) -> float:
        lo = center_ts - window_ms
        hi = center_ts + window_ms
        total = 0.0
        for fts, fval in spot_fills:
            if fts < lo:
                continue
            if fts > hi:
                break
            total += fval
        return total

    cash_flows = []
    for i in range(1, len(rows)):
        prev_ts, prev_bal = rows[i - 1]
        ts, bal = rows[i]
        delta = bal - prev_bal
        if abs(delta) < CASH_FLOW_THRESHOLD_USD:
            continue

        nearby = nearby_spot_fill_sum(ts)
        if nearby >= abs(delta) * 0.5:
            # Spot fills in the window can account for this jump — position activity
            continue

        note = "deposit" if delta > 0 else "withdrawal"
        cash_flows.append({"ts_ms": ts, "delta_usd": delta, "note": note})

    return cash_flows


def _time_weighted_avg_occupied(
    snaps: list[tuple[int, float, float]],
    t_start_ms: int,
    t_end_ms: int,
) -> float:
    """Compute time-weighted average of occupied_capital over [t_start_ms, t_end_ms].

    snaps: list of (ts_ms, total_equity, spot_value) sorted by ts_ms.
    occupied_capital is approximated as spot_value (the deployed spot capital).
    """
    filtered = [(ts, sp) for ts, _te, sp in snaps if t_start_ms <= ts <= t_end_ms]
    if not filtered:
        return 0.0

    total_weight = 0.0
    weighted_sum = 0.0
    for j in range(len(filtered)):
        ts_j, occ_j = filtered[j]
        if j + 1 < len(filtered):
            ts_next, _ = filtered[j + 1]
            dt = ts_next - ts_j
        elif j > 0:
            # Use last interval length for the final point
            dt = ts_j - filtered[j - 1][0]
        else:
            dt = 1  # single point fallback
        weighted_sum += occ_j * dt
        total_weight += dt

    if total_weight == 0:
        return filtered[0][1] if filtered else 0.0
    return weighted_sum / total_weight


def _interpolate_equity(
    snaps: list[tuple[int, float, float]],
    target_ts: int,
) -> float:
    """Return equity at target_ts, interpolating between adjacent snapshots."""
    if not snaps:
        return 0.0
    if target_ts <= snaps[0][0]:
        return snaps[0][1]
    if target_ts >= snaps[-1][0]:
        return snaps[-1][1]

    for i in range(1, len(snaps)):
        ts_a, eq_a, _ = snaps[i - 1]
        ts_b, eq_b, _ = snaps[i]
        if ts_a <= target_ts <= ts_b:
            if ts_b == ts_a:
                return eq_a
            frac = (target_ts - ts_a) / (ts_b - ts_a)
            return eq_a + frac * (eq_b - eq_a)
    return snaps[-1][1]


def compute_strategy_metrics(
    db_path: str,
    strategy_id: int,
    as_of_ms: int | None = None,
) -> dict:
    """
    Returns a JSON-serializable dict with all strategy performance metrics.

    as_of_ms defaults to the latest equity_snapshot timestamp.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # --- Load strategy metadata ---
    row = con.execute(
        "SELECT id, name, version, status, started_at_ms FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Strategy id={strategy_id} not found in {db_path}")

    strategy_name = row["name"]
    started_at_ms = row["started_at_ms"]
    if started_at_ms is None:
        raise ValueError(f"Strategy id={strategy_id} has no started_at_ms")

    # --- Load equity snapshots ---
    snap_rows = con.execute(
        """
        SELECT ts_ms, total_equity, cash, spot_value, perp_unrealized,
               perp_realized_cum, funding_cum, fees_cum
        FROM equity_snapshots
        WHERE strategy_id = ?
        ORDER BY ts_ms
        """,
        (strategy_id,),
    ).fetchall()

    if not snap_rows:
        raise ValueError(f"No equity snapshots found for strategy id={strategy_id}")

    snaps_full = [
        (r["ts_ms"], r["total_equity"], r["spot_value"]) for r in snap_rows
    ]
    last_snap = snap_rows[-1]

    if as_of_ms is None:
        as_of_ms = last_snap["ts_ms"]

    # Filter snapshots up to as_of_ms
    snaps = [(ts, te, sv) for ts, te, sv in snaps_full if ts <= as_of_ms]
    if not snaps:
        raise ValueError("No equity snapshots before as_of_ms")

    # --- Detect external cash flows ---
    cash_flows = _detect_external_cash_flows(con)

    # Also include an implicit initial deposit: the first wallet balance at or
    # near strategy start, if not already detected.
    initial_wallet = con.execute(
        """
        SELECT ts_ms, balance FROM wallet_snapshots
        WHERE coin = 'USDC' AND source = 'hl_account_total'
        ORDER BY ts_ms ASC LIMIT 1
        """
    ).fetchone()

    if initial_wallet:
        first_ts = initial_wallet["ts_ms"]
        first_bal = initial_wallet["balance"]
        # If there's no detected cash flow within 60s of initial_wallet, add it
        existing_ts = {cf["ts_ms"] for cf in cash_flows}
        if not any(abs(cf["ts_ms"] - first_ts) < 60_000 for cf in cash_flows):
            cash_flows.insert(0, {
                "ts_ms": first_ts,
                "delta_usd": first_bal,
                "note": "initial deposit",
            })

    cash_flows.sort(key=lambda x: x["ts_ms"])

    # Sum of external cash flows within a time window
    def sum_cash_flows(t_start_ms: int, t_end_ms: int) -> float:
        return sum(
            cf["delta_usd"]
            for cf in cash_flows
            if t_start_ms <= cf["ts_ms"] <= t_end_ms
        )

    # --- Compute window metrics ---
    tenure_seconds = (as_of_ms - started_at_ms) // 1000

    def compute_window(window_seconds: int) -> dict:
        t_start_ms = as_of_ms - window_seconds * 1000
        available = (as_of_ms - started_at_ms) >= window_seconds * 1000

        if not available:
            return _window_result(None, None, window_seconds, False)

        # Equity at window start and end (interpolated)
        eq_start = _interpolate_equity(snaps, t_start_ms)
        eq_end = _interpolate_equity(snaps, as_of_ms)

        external_flows = sum_cash_flows(t_start_ms, as_of_ms)
        profit = eq_end - eq_start - external_flows

        snaps_in_window = [(ts, te, sv) for ts, te, sv in snaps if t_start_ms <= ts <= as_of_ms]
        avg_occupied = _time_weighted_avg_occupied(snaps, t_start_ms, as_of_ms)

        return _window_result(profit, avg_occupied, window_seconds, True)

    ltd_seconds = (as_of_ms - started_at_ms) // 1000
    ltd_start_ms = started_at_ms

    # LTD: equity growth minus external flows since strategy start
    # Use the first equity snapshot (since strategy may not have had a snapshot
    # at the exact started_at_ms).
    eq_ltd_start = snaps[0][1]  # earliest available snapshot
    eq_ltd_end = _interpolate_equity(snaps, as_of_ms)
    ltd_start_ts = snaps[0][0]

    external_flows_ltd = sum_cash_flows(ltd_start_ts, as_of_ms)
    profit_ltd = eq_ltd_end - eq_ltd_start - external_flows_ltd
    avg_occupied_ltd = _time_weighted_avg_occupied(snaps, ltd_start_ts, as_of_ms)

    ltd_window_seconds = (as_of_ms - ltd_start_ts) // 1000
    ltd = _window_result(profit_ltd, avg_occupied_ltd, ltd_window_seconds, True)

    d1 = compute_window(86400)
    d7 = compute_window(7 * 86400)

    # --- Current state ---
    # Funding and fees for OPEN farb positions (mirror of ledger._compute_*)
    funding_open = con.execute(
        """
        SELECT COALESCE(SUM(fa.amount), 0.0)
        FROM funding_accruals fa
        JOIN positions p ON fa.position_id = p.id
        JOIN farb_positions fp ON p.farb_position_id = fp.id
        WHERE fp.strategy_id = ? AND fp.state = 'OPEN'
        """,
        (strategy_id,),
    ).fetchone()[0]

    fees_open = con.execute(
        """
        SELECT COALESCE(SUM(f.fee), 0.0)
        FROM fills f
        JOIN positions p ON f.position_id = p.id
        JOIN farb_positions fp ON p.farb_position_id = fp.id
        WHERE fp.strategy_id = ? AND fp.state = 'OPEN'
        """,
        (strategy_id,),
    ).fetchone()[0]

    # Realized PnL from closed farb positions (perp_realized_cum in last snapshot
    # minus fees and including funding that settled).
    # The most reliable source is the latest equity snapshot's perp_realized_cum
    # which is the cumulative realized PnL across ALL positions.
    perp_realized_cum = last_snap["perp_realized_cum"]

    # For realized_pnl_closed: use perp_realized_cum from last snapshot.
    # This includes closed positions' realized P&L net of fees per ledger code.
    realized_pnl_closed = float(perp_realized_cum)

    # Perp unrealized from last snapshot
    perp_unrealized = float(last_snap["perp_unrealized"])

    # Net open PnL: funding accrued on open positions − fees on open positions + perp unrealized
    net_open_pnl = float(funding_open) - float(fees_open) + perp_unrealized

    total_equity = float(last_snap["total_equity"])
    wallet_cash = float(last_snap["cash"])
    spot_value_now = float(last_snap["spot_value"])

    # Occupied capital = spot_value (deployed spot leg) + collateral (USDC locked as perp margin)
    collateral_locked = con.execute(
        """
        SELECT COALESCE(SUM(p.qty * p.entry_price), 0.0)
        FROM positions p
        JOIN farb_positions fp ON p.farb_position_id = fp.id
        WHERE fp.strategy_id = ? AND fp.state = 'OPEN' AND p.instrument = 'COLLATERAL'
        """,
        (strategy_id,),
    ).fetchone()[0]

    occupied_capital_now = spot_value_now + float(collateral_locked)

    # --- Equity curve (downsampled to max 500 points) ---
    all_snap_tuples = [(r["ts_ms"], r["total_equity"], r["spot_value"]) for r in snap_rows]
    n = len(all_snap_tuples)
    if n <= EQUITY_CURVE_MAX_POINTS:
        curve_snaps = all_snap_tuples
    else:
        step = n / EQUITY_CURVE_MAX_POINTS
        curve_snaps = [all_snap_tuples[int(i * step)] for i in range(EQUITY_CURVE_MAX_POINTS)]
        if curve_snaps[-1] != all_snap_tuples[-1]:
            curve_snaps.append(all_snap_tuples[-1])

    equity_curve = [
        {
            "ts_ms": ts,
            "total_equity": te,
            "occupied_capital": sv,
        }
        for ts, te, sv in curve_snaps
    ]

    con.close()

    return {
        "strategy": {
            "id": strategy_id,
            "name": strategy_name,
            "started_at_ms": started_at_ms,
            "started_at_iso": _ts_to_iso(started_at_ms),
        },
        "as_of_ms": as_of_ms,
        "as_of_iso": _ts_to_iso(as_of_ms),
        "tenure_seconds": tenure_seconds,
        "windows": {
            "ltd": ltd,
            "d1": d1,
            "d7": d7,
        },
        "current": {
            "realized_pnl_closed_usd": realized_pnl_closed,
            "open_positions": {
                "funding_accrued_usd": float(funding_open),
                "fees_paid_usd": float(fees_open),
                "perp_unrealized_usd": perp_unrealized,
                "net_open_pnl_usd": net_open_pnl,
            },
            "total_equity_usd": total_equity,
            "wallet_cash_usd": wallet_cash,
            "occupied_capital_now_usd": occupied_capital_now,
            "occupied_capital_ltd_avg_usd": avg_occupied_ltd,
        },
        "cash_flows": cash_flows,
        "equity_curve": equity_curve,
    }


def _fmt_duration(seconds: int) -> str:
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def _pretty_print(metrics: dict) -> None:
    s = metrics["strategy"]
    started_msk = _ts_to_msk_str(s["started_at_ms"])
    as_of_msk = _ts_to_msk_str(metrics["as_of_ms"])
    tenure_str = _fmt_duration(metrics["tenure_seconds"])

    print(f"Strategy: {s['name']}  (id={s['id']})")
    print(f"Started:  {started_msk}")
    print(f"As of:    {as_of_msk}   (tenure: {tenure_str})")
    print()
    print(f"{'':16s}{'Profit $':>18s}{'Return %':>18s}{'APR (ann.) %':>22s}")
    print("─" * 74)

    w = metrics["windows"]
    tenure_days = metrics["tenure_seconds"] / 86400

    def _fmt_apr(window: dict) -> str:
        if not window["apr_meaningful"]:
            return f"{'n/a (short window)':>20s}"
        return f"{window['apr_pct']:>18.4f} %"

    def _print_row(label: str, window: dict) -> None:
        if window["available"]:
            print(
                f"{label:<16s}"
                f"  ${window['profit_usd']:>14.6f}"
                f"  {window['return_pct']:>14.4f} %"
                f"  {_fmt_apr(window)}"
            )
        else:
            print(f"{label:<16s}  n/a (started {tenure_days:.1f}d ago)")

    _print_row("LTD", w["ltd"])
    _print_row("24h", w["d1"])
    _print_row("7d", w["d7"])

    print()
    cur = metrics["current"]
    op = cur["open_positions"]
    print("Snapshot of current state:")
    print(f"  Realized (closed positions):   ${cur['realized_pnl_closed_usd']:>14.6f}")
    print(f"  Open positions net:            ${op['net_open_pnl_usd']:>+14.6f}")
    print(f"    Funding accrued:             ${op['funding_accrued_usd']:>14.6f}")
    print(f"    Fees paid (entry only):     ${-op['fees_paid_usd']:>+14.6f}")
    print(f"    Perp unrealized MTM:         ${op['perp_unrealized_usd']:>+14.6f}")
    print(f"  Wallet cash:                   ${cur['wallet_cash_usd']:>14.6f}")
    print(f"  Total equity:                  ${cur['total_equity_usd']:>14.6f}")
    print(f"  Occupied capital (now):        ${cur['occupied_capital_now_usd']:>14.6f}")
    print(f"  Occupied capital (LTD avg):    ${cur['occupied_capital_ltd_avg_usd']:>14.6f}")

    print()
    cfs = metrics["cash_flows"]
    if cfs:
        print("External cash flows detected:")
        for cf in cfs:
            ts_str = _ts_to_msk_str(cf["ts_ms"])
            sign = "+" if cf["delta_usd"] >= 0 else ""
            print(f"  {ts_str}   {sign}${cf['delta_usd']:.6f}   ({cf['note']})")
    else:
        print("External cash flows detected: none")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute live strategy performance metrics from a frab SQLite snapshot."
    )
    parser.add_argument(
        "--db",
        default="/tmp/frab_snapshot.db",
        help="Path to SQLite snapshot (default: /tmp/frab_snapshot.db)",
    )
    parser.add_argument(
        "--strategy-id",
        type=int,
        default=None,
        help="Strategy ID to analyse (default: auto-pick if only one active)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Emit JSON to stdout instead of pretty-print",
    )
    args = parser.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    if args.strategy_id is not None:
        strategy_id = args.strategy_id
    else:
        rows = con.execute(
            "SELECT id FROM strategies WHERE status = 'active'"
        ).fetchall()
        if len(rows) == 0:
            parser.error("No active strategies found. Use --strategy-id to specify one.")
        if len(rows) > 1:
            ids = ", ".join(str(r["id"]) for r in rows)
            parser.error(
                f"Multiple active strategies found (ids: {ids}). "
                "Use --strategy-id to specify one."
            )
        strategy_id = rows[0]["id"]
    con.close()

    metrics = compute_strategy_metrics(args.db, strategy_id)

    if args.emit_json:
        print(json.dumps(metrics, indent=2))
    else:
        _pretty_print(metrics)


if __name__ == "__main__":
    main()
