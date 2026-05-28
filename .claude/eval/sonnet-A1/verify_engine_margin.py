"""
Verification script for margin-aware simulate() in engine.py.

Runs assertions to confirm:
  1. Backwards compatibility (byte-identical pnl_arr when margin not activated)
  2. Margin model: reserved capital, sanity, liquidation, top-up, forced-close

Run with: uv run python research/verify_engine_margin.py
"""

import sys
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engine import (
    load_data, simulate,
    POSITION_SIZE, TOTAL_CAPITAL, PERP_TAKER, SPOT_TAKER, HOURS_PER_YEAR,
    DEFAULT_MAINT_RATIO,
)

PASS = "PASS"
FAIL = "FAIL"

failures = []


def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {name}{suffix}")
    if not condition:
        failures.append(name)


# ---------------------------------------------------------------------------
# Test 1: Backwards compat — A_cycle
# ---------------------------------------------------------------------------
print("\n=== Test 1: Backwards compat — A_cycle ===")
df_btc = load_data("BTC")
pnl_no_margin, _   = simulate(df_btc, strategy="A_cycle")
pnl_none_explicit, _ = simulate(df_btc, strategy="A_cycle", per_coin_leverage=None)
check(
    "A_cycle: None vs default byte-identical",
    np.array_equal(pnl_no_margin, pnl_none_explicit),
)

# ---------------------------------------------------------------------------
# Test 2: Backwards compat — A_spot_keep, B, B_hedge
# ---------------------------------------------------------------------------
print("\n=== Test 2: Backwards compat — other strategies ===")

from engine import regime_below_ma

# A_spot_keep
pnl_ask_base, _ = simulate(df_btc, strategy="A_spot_keep")
pnl_ask_none, _ = simulate(df_btc, strategy="A_spot_keep", per_coin_leverage=None)
check("A_spot_keep: byte-identical", np.array_equal(pnl_ask_base, pnl_ask_none))

# B with regime_filter
pnl_b_base, _ = simulate(df_btc, strategy="B", regime_filter=regime_below_ma)
pnl_b_none, _ = simulate(df_btc, strategy="B", regime_filter=regime_below_ma, per_coin_leverage=None)
check("B (regime_below_ma): byte-identical", np.array_equal(pnl_b_base, pnl_b_none))

# B_hedge with synthetic hedge_signal
n_btc = len(df_btc)
rng = np.random.default_rng(42)
hedge_sig = rng.random(n_btc) > 0.85   # ~15% in hedge
pnl_bh_base, _ = simulate(df_btc, strategy="B_hedge", hedge_signal=hedge_sig)
pnl_bh_none, _ = simulate(df_btc, strategy="B_hedge", hedge_signal=hedge_sig, per_coin_leverage=None)
check("B_hedge (synthetic signal): byte-identical", np.array_equal(pnl_bh_base, pnl_bh_none))

# ---------------------------------------------------------------------------
# Test 3: Margin reserved — peak_committed_capital
# ---------------------------------------------------------------------------
print("\n=== Test 3: Margin reserved — peak_committed_capital ===")
pnl_m, info_m = simulate(
    df_btc,
    strategy="A_cycle",
    coin="BTC",
    per_coin_leverage={"BTC": 10},
    margin_buffer_x=3.0,
)
expected_min_committed = POSITION_SIZE + POSITION_SIZE / 10 * 3.0
check(
    "peak_committed_capital >= POSITION_SIZE + margin",
    info_m["peak_committed_capital"] >= expected_min_committed,
    f"peak={info_m['peak_committed_capital']:.2f}, min_expected={expected_min_committed:.2f}",
)
print(f"    n_liquidations={info_m['n_liquidations']}, n_top_ups={info_m['n_top_ups']}, "
      f"n_forced_closes={info_m['n_forced_closes']}, "
      f"min_margin_ratio={info_m['min_margin_ratio']:.4f}")

# ---------------------------------------------------------------------------
# Test 4: Sanity — equity close to old model on benign 2024 BTC window
# ---------------------------------------------------------------------------
print("\n=== Test 4: Sanity — equity close to old on benign window ===")

# Use 2024 Jan–Jun BTC (relatively stable bull market)
df_2024 = df_btc[
    (df_btc.index >= pd.Timestamp("2024-01-01", tz="UTC")) &
    (df_btc.index <= pd.Timestamp("2024-06-30", tz="UTC"))
].copy()

if len(df_2024) < 100:
    print("  [SKIP] Insufficient data for 2024 window")
else:
    pnl_old_2024, _ = simulate(df_2024, strategy="A_cycle")
    pnl_new_2024, info_new_2024 = simulate(
        df_2024,
        strategy="A_cycle",
        coin="BTC",
        per_coin_leverage={"BTC": 10},
        margin_buffer_x=3.0,
    )
    old_total = pnl_old_2024.sum()
    new_total = pnl_new_2024.sum()
    if abs(old_total) > 1e-9:
        diff_pct = abs(new_total - old_total) / abs(old_total) * 100
    else:
        diff_pct = 0.0
    check(
        "Sanity: equity within 1% of old model (no liquidations expected)",
        diff_pct < 1.0 and info_new_2024["n_liquidations"] == 0,
        f"old={old_total:.2f}, new={new_total:.2f}, diff={diff_pct:.4f}%, "
        f"n_liq={info_new_2024['n_liquidations']}",
    )

# ---------------------------------------------------------------------------
# Test 5: Liquidation triggers — sharp adverse price move
# ---------------------------------------------------------------------------
print("\n=== Test 5: Liquidation triggers ===")

# Build a synthetic df where price triples over 24h after entry
# Entry signal: high positive funding so position opens at hour 0
# Then price rises sharply → short underwater → liquidated

N_LIQ = 200
prices = np.full(N_LIQ, 100.0)
# Keep price flat for first 5 hours so entry opens, then sharp rise
prices[5:] = 300.0   # 3x price rise — short goes massively underwater

rates_liq = np.full(N_LIQ, 0.001)   # strongly positive funding → entry signal fires

idx = pd.date_range("2024-01-01", periods=N_LIQ, freq="h", tz="UTC")
df_liq = pd.DataFrame({
    "close":       prices,
    "fundingRate": rates_liq,
    "ma200":       np.full(N_LIQ, np.nan),
    "mom_72h":     np.zeros(N_LIQ),
    "mom_168h":    np.zeros(N_LIQ),
}, index=idx)

# Low leverage + low buffer → easy liquidation
pnl_liq, info_liq = simulate(
    df_liq,
    strategy="A_cycle",
    coin="BTC",
    per_coin_leverage={"BTC": 2},
    per_coin_maint_ratio={"BTC": 0.10},   # high maint ratio to force liq
    margin_buffer_x=1.5,
    top_up_trigger=2.0,
    healthy_ratio=3.0,
)
check(
    "Liquidation triggers: n_liquidations >= 1",
    info_liq["n_liquidations"] >= 1,
    f"n_liquidations={info_liq['n_liquidations']}, "
    f"n_top_ups={info_liq['n_top_ups']}, "
    f"n_forced_closes={info_liq['n_forced_closes']}, "
    f"min_margin_ratio={info_liq['min_margin_ratio']:.4f}",
)

# ---------------------------------------------------------------------------
# Test 6: Top-up triggers — moderate adverse move, no liquidation
# ---------------------------------------------------------------------------
print("\n=== Test 6: Top-up triggers ===")

# Price rises moderately (not enough to liquidate with plenty of spot_cash)
N_TOPUP = 500
prices_topup = np.linspace(100.0, 140.0, N_TOPUP)  # 40% rise
rates_topup = np.full(N_TOPUP, 0.0008)  # positive funding to stay open

idx_topup = pd.date_range("2024-01-01", periods=N_TOPUP, freq="h", tz="UTC")
df_topup = pd.DataFrame({
    "close":       prices_topup,
    "fundingRate": rates_topup,
    "ma200":       np.full(N_TOPUP, np.nan),
    "mom_72h":     np.zeros(N_TOPUP),
    "mom_168h":    np.zeros(N_TOPUP),
}, index=idx_topup)

pnl_topup, info_topup = simulate(
    df_topup,
    strategy="A_cycle",
    coin="BTC",
    per_coin_leverage={"BTC": 5},
    per_coin_maint_ratio={"BTC": 0.05},
    margin_buffer_x=2.0,
    top_up_trigger=3.0,   # aggressive top-up trigger
    healthy_ratio=4.0,
    min_hold=10,
    exit_threshold=-1.0,  # never exit on funding (never negative enough)
)
check(
    "Top-up triggers: n_top_ups >= 1 and n_liquidations == 0",
    info_topup["n_top_ups"] >= 1 and info_topup["n_liquidations"] == 0,
    f"n_top_ups={info_topup['n_top_ups']}, "
    f"n_liquidations={info_topup['n_liquidations']}, "
    f"n_forced_closes={info_topup['n_forced_closes']}, "
    f"min_margin_ratio={info_topup['min_margin_ratio']:.4f}",
)

# ---------------------------------------------------------------------------
# Test 7: Forced-close on insufficient spot
# ---------------------------------------------------------------------------
print("\n=== Test 7: Forced-close on insufficient spot ===")

# Strategy: drain spot_cash by buying a large spot already, then open a position
# with a price that rises — spot is thin, can't top up enough.
# We achieve "thin spot" by using A_spot_keep (spot already bought at start)
# combined with a large price rise and high top_up_trigger.

N_FC = 300
prices_fc = np.concatenate([
    np.full(5, 100.0),     # flat start
    np.linspace(100.0, 180.0, N_FC - 5),  # price rises 80%
])
rates_fc = np.full(N_FC, 0.0008)

idx_fc = pd.date_range("2024-01-01", periods=N_FC, freq="h", tz="UTC")
df_fc = pd.DataFrame({
    "close":       prices_fc,
    "fundingRate": rates_fc,
    "ma200":       np.full(N_FC, np.nan),
    "mom_72h":     np.zeros(N_FC),
    "mom_168h":    np.zeros(N_FC),
}, index=idx_fc)

# A_spot_keep: spot bought at start (uses ~POSITION_SIZE of spot_cash)
# Then required_margin is large relative to remaining spot_cash
# Use high maint ratio + high buffer so top-up demand exceeds available spot_cash
pnl_fc, info_fc = simulate(
    df_fc,
    strategy="A_spot_keep",
    coin="BTC",
    per_coin_leverage={"BTC": 3},
    per_coin_maint_ratio={"BTC": 0.10},
    margin_buffer_x=2.0,
    top_up_trigger=5.0,    # aggressive: almost always wants to top up
    healthy_ratio=8.0,     # enormous top-up target → drains spot_cash fast
    min_hold=5,
    exit_threshold=-1.0,
)
check(
    "Forced-close: n_forced_closes >= 1",
    info_fc["n_forced_closes"] >= 1,
    f"n_forced_closes={info_fc['n_forced_closes']}, "
    f"n_top_ups={info_fc['n_top_ups']}, "
    f"n_liquidations={info_fc['n_liquidations']}, "
    f"min_margin_ratio={info_fc['min_margin_ratio']:.4f}",
)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
if failures:
    print(f"RESULT: {len(failures)} assertion(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("RESULT: ALL assertions passed.")
