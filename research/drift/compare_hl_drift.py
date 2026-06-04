"""
Comparison of Hyperliquid vs Drift funding rates.

Loads funding_history/ (Drift) and funding_history_hl/ (HL),
computes per-month and per-regime averages, outputs tables to console and CSV.

Regimes:
  Hot:  2023-06-01 → 2024-12-31
  Cold: 2025-01-01 → 2026-04-01  (Drift data freeze)
"""

import csv
import datetime
from collections import defaultdict
from pathlib import Path

DRIFT_DIR = Path(__file__).parent / "funding_history"
HL_DIR = Path(__file__).parent / "funding_history_hl"

COINS = ["BTC", "ETH", "SOL", "HYPE", "AVAX", "LINK", "DOGE"]

HOT_START  = datetime.date(2023,  6,  1)
HOT_END    = datetime.date(2024, 12, 31)
COLD_START = datetime.date(2025,  1,  1)
COLD_END   = datetime.date(2026,  4,  1)


# ── helpers ──────────────────────────────────────────────────────────────────

def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def ts_to_date(ts_ms: int) -> datetime.date:
    return datetime.datetime.utcfromtimestamp(ts_ms / 1000).date()


def ym(d: datetime.date) -> str:
    return f"{d.year}-{d.month:02d}"


def mean(vals: list[float]):
    if not vals:
        return None
    return sum(vals) / len(vals)


def pct_edge(drift_vals: list[float], hl_vals: list[float]):
    """% of hours where Drift annualized > HL annualized (matched by ts_ms)."""
    if not drift_vals or not hl_vals:
        return None
    wins = sum(1 for d, h in zip(drift_vals, hl_vals) if d > h)
    return 100.0 * wins / len(drift_vals)


# ── per-row bucket builder ────────────────────────────────────────────────────

def build_buckets(rows: list[dict], regime: str) -> dict[str, list[float]]:
    """Return {month_str: [annualized_pct, ...]} for rows in the given regime."""
    start, end = (HOT_START, HOT_END) if regime == "hot" else (COLD_START, COLD_END)
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        d = ts_to_date(int(r["ts_ms"]))
        if start <= d <= end:
            buckets[ym(d)].append(float(r["annualized_pct"]))
    return dict(buckets)


def regime_vals(rows: list[dict], regime: str) -> list[float]:
    start, end = (HOT_START, HOT_END) if regime == "hot" else (COLD_START, COLD_END)
    return [
        float(r["annualized_pct"])
        for r in rows
        if start <= ts_to_date(int(r["ts_ms"])) <= end
    ]


def regime_vals_with_ts(rows: list[dict], regime: str) -> dict[int, float]:
    start, end = (HOT_START, HOT_END) if regime == "hot" else (COLD_START, COLD_END)
    return {
        int(r["ts_ms"]): float(r["annualized_pct"])
        for r in rows
        if start <= ts_to_date(int(r["ts_ms"])) <= end
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    # ── load all data ────────────────────────────────────────────────────────
    drift_data: dict[str, list[dict]] = {}
    hl_data:    dict[str, list[dict]] = {}
    for coin in COINS:
        drift_data[coin] = load_csv(DRIFT_DIR / f"{coin}.csv")
        hl_data[coin]    = load_csv(HL_DIR    / f"{coin}.csv")

    # ── coverage table ───────────────────────────────────────────────────────
    print("\n" + "="*80)
    print("COVERAGE TABLE")
    print("="*80)
    hdr = f"{'Coin':<6} {'HL rows':>8} {'HL start':>12} {'HL end':>12} {'Drift rows':>10} {'Drift start':>13} {'Drift end':>12}"
    print(hdr)
    print("-"*80)
    for coin in COINS:
        d_rows = drift_data[coin]
        h_rows = hl_data[coin]

        def span(rows):
            if not rows:
                return ("N/A", "N/A")
            dates = [ts_to_date(int(r["ts_ms"])) for r in rows]
            return (str(min(dates)), str(max(dates)))

        h_start, h_end = span(h_rows)
        d_start, d_end = span(d_rows)
        print(f"{coin:<6} {len(h_rows):>8} {h_start:>12} {h_end:>12} {len(d_rows):>10} {d_start:>13} {d_end:>12}")

    # ── per-regime table ─────────────────────────────────────────────────────
    print("\n" + "="*80)
    print("PER-REGIME ANNUALIZED FUNDING AVERAGES (%)")
    print("="*80)
    print(f"{'Coin':<6} {'HL hot':>8} {'Drft hot':>9} {'Δhot':>7} | {'HL cold':>8} {'Drft cld':>9} {'Δcold':>7} | {'Freq>HL hot':>11} {'Freq>HL cld':>12}")
    print("-"*95)

    regime_results = []
    for coin in COINS:
        d_rows = drift_data[coin]
        h_rows = hl_data[coin]

        hl_hot   = regime_vals(h_rows, "hot")
        hl_cold  = regime_vals(h_rows, "cold")
        dr_hot   = regime_vals(d_rows, "hot")
        dr_cold  = regime_vals(d_rows, "cold")

        mhl_hot  = mean(hl_hot)
        mhl_cold = mean(hl_cold)
        mdr_hot  = mean(dr_hot)
        mdr_cold = mean(dr_cold)

        def fmt(v): return f"{v:+.2f}" if v is not None else "  N/A"
        def fmtv(v): return f"{v:.2f}" if v is not None else " N/A"

        d_hot_ts  = regime_vals_with_ts(d_rows, "hot")
        d_cold_ts = regime_vals_with_ts(d_rows, "cold")
        h_hot_ts  = regime_vals_with_ts(h_rows, "hot")
        h_cold_ts = regime_vals_with_ts(h_rows, "cold")

        # Edge frequency: % hours where Drift > HL (only matched timestamps)
        common_hot  = sorted(set(d_hot_ts) & set(h_hot_ts))
        common_cold = sorted(set(d_cold_ts) & set(h_cold_ts))

        freq_hot  = None if not common_hot  else 100*sum(1 for t in common_hot  if d_hot_ts[t]  > h_hot_ts[t])  / len(common_hot)
        freq_cold = None if not common_cold else 100*sum(1 for t in common_cold if d_cold_ts[t] > h_cold_ts[t]) / len(common_cold)

        delta_hot  = (mdr_hot  - mhl_hot)  if mdr_hot  is not None and mhl_hot  is not None else None
        delta_cold = (mdr_cold - mhl_cold) if mdr_cold is not None and mhl_cold is not None else None

        def fp(v): return f"{v:.1f}%" if v is not None else "   N/A"

        print(f"{coin:<6} {fmtv(mhl_hot):>8} {fmtv(mdr_hot):>9} {fmt(delta_hot):>7} | "
              f"{fmtv(mhl_cold):>8} {fmtv(mdr_cold):>9} {fmt(delta_cold):>7} | "
              f"{fp(freq_hot):>11} {fp(freq_cold):>12}")

        regime_results.append({
            "coin": coin,
            "hl_hot": mhl_hot, "drift_hot": mdr_hot, "delta_hot": delta_hot,
            "hl_cold": mhl_cold, "drift_cold": mdr_cold, "delta_cold": delta_cold,
            "freq_hot": freq_hot, "freq_cold": freq_cold,
        })

    # Weighted avg (equal weight per coin, skip HYPE in hot since no hot data on HL)
    def avg_regime(field):
        vals = [r[field] for r in regime_results if r[field] is not None]
        return mean(vals)

    print("-"*95)
    def fav(f): return f"{avg_regime(f):+.2f}" if avg_regime(f) is not None else " N/A"
    print(f"{'AVG':<6} {avg_regime('hl_hot') or 0:>8.2f} {avg_regime('drift_hot') or 0:>9.2f} {fav('delta_hot'):>7} | "
          f"{avg_regime('hl_cold') or 0:>8.2f} {avg_regime('drift_cold') or 0:>9.2f} {fav('delta_cold'):>7}")

    # ── per-month cold regime (most relevant) ────────────────────────────────
    print("\n" + "="*80)
    print("COLD REGIME MONTHLY AVERAGES (2025-01 → 2026-04)")
    print("="*80)

    # Collect all cold months across all coins
    all_months: set[str] = set()
    monthly_hl:    dict[str, dict[str, list[float]]] = {}
    monthly_drift: dict[str, dict[str, list[float]]] = {}

    for coin in COINS:
        monthly_hl[coin]    = build_buckets(hl_data[coin],    "cold")
        monthly_drift[coin] = build_buckets(drift_data[coin], "cold")
        all_months.update(monthly_hl[coin].keys())
        all_months.update(monthly_drift[coin].keys())

    sorted_months = sorted(all_months)

    # Print a compact multi-coin table: columns = months, rows = coin × venue
    # Group by coin for readability
    col_w = 8
    hdr2 = f"{'Coin-Venue':<14}" + "".join(f"{m:>{col_w}}" for m in sorted_months)
    print(hdr2)
    print("-" * (14 + col_w * len(sorted_months)))

    for coin in COINS:
        hl_row    = [mean(monthly_hl[coin].get(m, []))    for m in sorted_months]
        drift_row = [mean(monthly_drift[coin].get(m, [])) for m in sorted_months]
        delta_row = [
            (d - h) if d is not None and h is not None else None
            for d, h in zip(drift_row, hl_row)
        ]

        def fv(v): return f"{v:.1f}" if v is not None else "  --"
        def fd(v): return f"{v:+.1f}" if v is not None else "  --"

        print(f"{coin+' HL':<14}" + "".join(f"{fv(v):>{col_w}}" for v in hl_row))
        print(f"{coin+' Drift':<14}" + "".join(f"{fv(v):>{col_w}}" for v in drift_row))
        print(f"{coin+' Δ':<14}" + "".join(f"{fd(v):>{col_w}}" for v in delta_row))
        print()

    # ── save regime results to CSV ───────────────────────────────────────────
    out_csv = Path(__file__).parent / "regime_comparison.csv"
    with open(out_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "coin", "hl_hot", "drift_hot", "delta_hot",
            "hl_cold", "drift_cold", "delta_cold",
            "freq_hot", "freq_cold",
        ])
        writer.writeheader()
        writer.writerows(regime_results)
    print(f"\nSaved regime results → {out_csv}")

    # ── save monthly cold data ────────────────────────────────────────────────
    monthly_rows = []
    for coin in COINS:
        for m in sorted_months:
            hl_val    = mean(monthly_hl[coin].get(m, []))
            drift_val = mean(monthly_drift[coin].get(m, []))
            delta = (drift_val - hl_val) if drift_val is not None and hl_val is not None else None
            monthly_rows.append({
                "coin": coin, "month": m,
                "hl_ann_pct": round(hl_val, 4) if hl_val is not None else "",
                "drift_ann_pct": round(drift_val, 4) if drift_val is not None else "",
                "delta": round(delta, 4) if delta is not None else "",
            })

    monthly_csv = Path(__file__).parent / "monthly_cold_comparison.csv"
    with open(monthly_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["coin", "month", "hl_ann_pct", "drift_ann_pct", "delta"])
        writer.writeheader()
        writer.writerows(monthly_rows)
    print(f"Saved monthly cold data → {monthly_csv}")


if __name__ == "__main__":
    main()
