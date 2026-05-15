"""
Wait-time statistics: how long (hours) there are zero open positions in the simulation.

Configs:
  A — prod COMBO: entry=0.30, exit=-0.15, min_hold=120, signal_window=12, K=3
  B — local-dev (low entry): entry=0.10, exit=-0.15, min_hold=1, signal_window=12, K=3
"""

import sys
import numpy as np
from concurrency_cap import simulate_multi_capped

COINS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]

CONFIGS = [
    dict(
        label="A",
        title="prod COMBO (entry=30%, exit=-15%, min_hold=120, K=3, 7 coins)",
        entry=0.30,
        exit=-0.15,
        min_hold=120,
        signal_window=12,
    ),
    dict(
        label="B",
        title="local-dev low-entry (entry=10%, exit=-15%, min_hold=1, K=3, 7 coins)",
        entry=0.10,
        exit=-0.15,
        min_hold=1,
        signal_window=12,
    ),
]


def compute_runs(cap_per_hour):
    """Return list of consecutive run lengths where cap_per_hour == 0."""
    empty = cap_per_hour == 0
    runs = []
    run = 0
    for x in empty:
        if x:
            run += 1
        else:
            if run > 0:
                runs.append(run)
            run = 0
    if run > 0:
        runs.append(run)
    return runs, empty


def bucket_stats(runs):
    """Return histogram by predefined hour buckets."""
    buckets = [
        ("1h",       lambda r: r == 1),
        ("2-6h",     lambda r: 2 <= r <= 6),
        ("7-24h",    lambda r: 7 <= r <= 24),
        ("25-72h",   lambda r: 25 <= r <= 72),
        ("73-168h",  lambda r: 73 <= r <= 168),
        (">168h",    lambda r: r > 168),
    ]
    result = []
    for name, pred in buckets:
        matching = [r for r in runs if pred(r)]
        result.append((name, len(matching), sum(matching)))
    return result


def print_stats(cap_per_hour, label_prefix=""):
    runs, empty = compute_runs(cap_per_hour)
    n = len(cap_per_hour)
    n_empty = int(empty.sum())
    pct_empty = n_empty / n * 100

    arr = np.array(runs) if runs else np.array([0])

    print(f"{label_prefix}Hours total:        {n}")
    print(f"{label_prefix}Hours empty:        {n_empty} ({pct_empty:.1f}%)")
    print(f"{label_prefix}Empty windows:      {len(runs)}")
    if runs:
        print(f"{label_prefix}Mean wait:          {arr.mean():.1f} hours")
        print(f"{label_prefix}Median:             {np.median(arr):.0f} hours")
        p75, p90, p95, p99 = np.percentile(arr, [75, 90, 95, 99])
        print(f"{label_prefix}p75 / p90 / p95 / p99: {p75:.0f} / {p90:.0f} / {p95:.0f} / {p99:.0f} hours")
        print(f"{label_prefix}Max:                {arr.max():.0f} hours ({arr.max()/24:.1f} days)")
    else:
        print(f"{label_prefix}  (no empty windows)")

    print(f"\n{label_prefix}Window length distribution:")
    bkts = bucket_stats(runs)
    for name, count, total_h in bkts:
        bar = "#" * min(count, 40)
        print(f"{label_prefix}  {name:<10} {count:>5} runs   {total_h:>7} hours   {bar}")


def main():
    for cfg in CONFIGS:
        print()
        print(f"=== CONFIG {cfg['label']}: {cfg['title']} ===")
        print()

        _, cap_per_hour, info = simulate_multi_capped(
            COINS,
            max_concurrent=3,
            entry_threshold=cfg["entry"],
            exit_threshold=cfg["exit"],
            min_hold=cfg["min_hold"],
            signal_window=cfg["signal_window"],
        )

        print_stats(cap_per_hour)
        print(f"\n  (total trades across all coins: {info['total_trades']})")

        # For config A only: last-90-days slice
        if cfg["label"] == "A":
            SLICE = 90 * 24  # 2160 hours
            n = len(cap_per_hour)
            if n >= SLICE:
                print()
                print(f"--- Last 90 days slice (last {SLICE} hours of {n} total) ---")
                print()
                print_stats(cap_per_hour[-SLICE:], label_prefix="  ")
            else:
                print(f"\n  [skip] Series too short ({n} hours < {SLICE}) for 90-day slice")

        print()


if __name__ == "__main__":
    main()
