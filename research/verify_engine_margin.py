"""
Verification scenarios for the margin-model refactor of research/engine.py.

Scenarios 1-4: backwards compatibility — pnl_arr byte-identical when per_coin_leverage=None.
Scenario 5: margin reservation — peak_committed_capital populated.
Scenario 6: liquidation triggers — n_liquidations >= 1 on synthetic adverse-move df.
"""

import sys
import numpy as np
import pandas as pd

# Add research dir to path if running from repo root
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from engine import (
    simulate, load_data, compute_metrics,
    STAKING_YIELD, TOTAL_CAPITAL, POSITION_SIZE,
)


def make_synthetic_df(n_hours=500, start_price=100.0, price_fn=None, funding_rate=0.0001):
    """Create a minimal synthetic DataFrame with required columns."""
    times = pd.date_range("2023-06-08", periods=n_hours, freq="h", tz="UTC")
    if price_fn is not None:
        prices = np.array([price_fn(i) for i in range(n_hours)], dtype=float)
    else:
        prices = np.full(n_hours, start_price)
    rates = np.full(n_hours, funding_rate)
    df = pd.DataFrame({
        "close": prices,
        "fundingRate": rates,
    }, index=times)
    df["price_return"] = df["close"].pct_change().fillna(0)
    df["ma200"] = df["close"].rolling(200, min_periods=200).mean()
    df["mom_72h"] = df["close"].pct_change(72)
    df["mom_168h"] = df["close"].pct_change(168)
    return df


# ─── Scenarios 1-4: backwards compatibility ───────────────────────────────────

def scenario_1_compat_A_cycle():
    """A_cycle strategy: pnl_arr must be identical with/without margin params."""
    try:
        df = load_data("BTC")
    except Exception:
        df = make_synthetic_df(n_hours=1000, funding_rate=0.0003)

    pnl_base, _ = simulate(df, staking_yield=0.0, strategy="A_cycle")
    pnl_margin, _ = simulate(df, staking_yield=0.0, strategy="A_cycle",
                             per_coin_leverage=None)
    ok = np.array_equal(pnl_base, pnl_margin)
    print(f"[1] A_cycle backwards compat: {'PASS' if ok else 'FAIL'}")
    if not ok:
        diff_idx = np.where(pnl_base != pnl_margin)[0]
        print(f"    First diff at index {diff_idx[0]}: base={pnl_base[diff_idx[0]]:.6f} margin={pnl_margin[diff_idx[0]]:.6f}")
    assert ok, "Scenario 1 FAILED: A_cycle pnl_arr not identical with per_coin_leverage=None"
    return True


def scenario_2_compat_A_spot_keep():
    """A_spot_keep strategy: pnl_arr must be identical."""
    try:
        df = load_data("ETH")
    except Exception:
        df = make_synthetic_df(n_hours=1000, funding_rate=0.0003)

    pnl_base, _ = simulate(df, staking_yield=STAKING_YIELD.get("ETH", 0.0), strategy="A_spot_keep")
    pnl_margin, _ = simulate(df, staking_yield=STAKING_YIELD.get("ETH", 0.0), strategy="A_spot_keep",
                             per_coin_leverage=None)
    ok = np.array_equal(pnl_base, pnl_margin)
    print(f"[2] A_spot_keep backwards compat: {'PASS' if ok else 'FAIL'}")
    assert ok, "Scenario 2 FAILED: A_spot_keep pnl_arr not identical with per_coin_leverage=None"
    return True


def scenario_3_compat_B_regime():
    """B strategy with regime_below_ma: pnl_arr must be identical."""
    from engine import regime_below_ma
    try:
        df = load_data("SOL")
    except Exception:
        df = make_synthetic_df(n_hours=1000, funding_rate=0.0003)

    pnl_base, _ = simulate(df, staking_yield=0.0, strategy="B",
                           regime_filter=regime_below_ma)
    pnl_margin, _ = simulate(df, staking_yield=0.0, strategy="B",
                             regime_filter=regime_below_ma, per_coin_leverage=None)
    ok = np.array_equal(pnl_base, pnl_margin)
    print(f"[3] B (regime_below_ma) backwards compat: {'PASS' if ok else 'FAIL'}")
    assert ok, "Scenario 3 FAILED: B (regime_below_ma) pnl_arr not identical"
    return True


def scenario_4_compat_B_hedge():
    """B_hedge strategy with synthetic signal: pnl_arr must be identical."""
    df = make_synthetic_df(n_hours=800, funding_rate=0.0002)
    rng = np.random.default_rng(42)
    hedge_signal = rng.integers(0, 2, size=len(df)).astype(bool)

    pnl_base, _ = simulate(df, staking_yield=0.0, strategy="B_hedge",
                           hedge_signal=hedge_signal)
    pnl_margin, _ = simulate(df, staking_yield=0.0, strategy="B_hedge",
                             hedge_signal=hedge_signal, per_coin_leverage=None)
    ok = np.array_equal(pnl_base, pnl_margin)
    print(f"[4] B_hedge (synthetic signal) backwards compat: {'PASS' if ok else 'FAIL'}")
    assert ok, "Scenario 4 FAILED: B_hedge pnl_arr not identical with per_coin_leverage=None"
    return True


# ─── Scenario 5: margin reservation ──────────────────────────────────────────

def scenario_5_margin_reservation():
    """With per_coin_leverage={"BTC": 10}, peak_committed_capital >= POSITION_SIZE/10.
    Uses high funding rate (0.00003/h = ~26% annual) to trigger entry threshold of 20%.
    """
    # 0.00003 * 8760 = 26.28% annual — above entry_threshold=20%
    df = make_synthetic_df(n_hours=500, funding_rate=0.00003)
    _, info = simulate(df, staking_yield=0.0, strategy="A_cycle",
                       coin="BTC",
                       per_coin_leverage={"BTC": 10},
                       margin_buffer_x=3.0)
    pcc = info.get("peak_committed_capital", 0.0)
    expected_min = POSITION_SIZE + POSITION_SIZE / 10 * 3.0  # committed = 1000 + (1000/10)*3 = 1300.0
    ok = pcc >= expected_min
    print(f"[5] Margin reservation: peak_committed_capital={pcc:.2f} >= {expected_min:.2f}: {'PASS' if ok else 'FAIL'}")
    assert ok, f"Scenario 5 FAILED: peak_committed_capital={pcc} < {expected_min}"
    # Also verify margin keys exist
    for key in ("n_liquidations", "n_top_ups", "n_forced_closes",
                "n_skipped_opens_capital", "min_margin_ratio",
                "peak_committed_capital", "final_spot_cash", "final_perp_cash"):
        assert key in info, f"Missing key {key!r} in info"
    print(f"    All margin info keys present.")
    return True


# ─── Scenario 6: liquidation triggers ────────────────────────────────────────

def scenario_6_liquidation():
    """
    Adverse-move scenario: price rises sharply while holding a short.
    High funding rate (>20% annual) triggers entry at i=0. Then price spikes 10x,
    wiping out the leveraged short margin => liquidation or forced close.
    """
    n = 600
    # 0.00003/h * 8760 = 26.28% annual — above entry_threshold=20%
    n_hours = n
    times = pd.date_range("2023-06-08", periods=n_hours, freq="h", tz="UTC")
    # Price starts at 100, then rises 10x — kills leveraged short
    prices = np.where(
        np.arange(n_hours) < 5,
        100.0,
        np.minimum(100.0 + (np.arange(n_hours) - 5) * 3.0, 1000.0)
    ).astype(float)
    rates = np.full(n_hours, 0.00003)
    df = pd.DataFrame({"close": prices, "fundingRate": rates}, index=times)
    df["price_return"] = df["close"].pct_change().fillna(0)
    df["ma200"] = df["close"].rolling(200, min_periods=200).mean()
    df["mom_72h"] = df["close"].pct_change(72)
    df["mom_168h"] = df["close"].pct_change(168)

    # High leverage = small initial margin => gets liquidated quickly on adverse move
    _, info = simulate(df, staking_yield=0.0, strategy="A_cycle",
                       coin="BTC",
                       per_coin_leverage={"BTC": 20},
                       per_coin_maint_ratio={"BTC": 0.01},
                       margin_buffer_x=2.0,
                       top_up_trigger=1.5,
                       healthy_ratio=2.0)

    n_liq = info.get("n_liquidations", 0)
    n_forced = info.get("n_forced_closes", 0)
    total_risk_events = n_liq + n_forced
    ok = total_risk_events >= 1
    print(f"[6] Liquidation/forced-close: n_liq={n_liq}, n_forced={n_forced}, total={total_risk_events}: {'PASS' if ok else 'FAIL'}")
    assert ok, f"Scenario 6 FAILED: no liquidations or forced closes triggered (n_liq={n_liq}, n_forced={n_forced})"
    return True


if __name__ == "__main__":
    results = []
    scenarios = [
        scenario_1_compat_A_cycle,
        scenario_2_compat_A_spot_keep,
        scenario_3_compat_B_regime,
        scenario_4_compat_B_hedge,
        scenario_5_margin_reservation,
        scenario_6_liquidation,
    ]

    for fn in scenarios:
        try:
            fn()
            results.append(True)
        except AssertionError as e:
            print(f"  ASSERTION ERROR: {e}")
            results.append(False)
        except Exception as e:
            import traceback
            print(f"  EXCEPTION: {e}")
            traceback.print_exc()
            results.append(False)

    passed = sum(results)
    total = len(results)
    print(f"\nResult: {passed}/{total} scenarios passed.")

    if passed < total:
        sys.exit(1)
    else:
        print("All scenarios PASSED.")
        sys.exit(0)
