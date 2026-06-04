"""Rally stress test: how low can margin buffer go before the short leg breaks?

A delta-neutral book is stressed by a RALLY (spot up = perp short loses → margin
eaten → top-up from idle budget / forced-close / liquidation). The calm/down
window showed 0 liquidations even at buffer=1; this finds the strongest rally
sub-window and sweeps buffer there, at prod-realistic budget tightness (where
top-up room is small — that's where the buffer actually matters).

CAVEAT: two_phase_margin.py models SEGREGATED margin (perp sub-account can
liquidate even though spot offsets it). Real HL is UNIFIED — spot collateralizes
perp directly, so a delta-neutral book is far harder to liquidate. So whatever
buffer this says is "safe" is a CONSERVATIVE lower bound; real HL is more forgiving.

Research only.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import two_phase_margin as _tpm  # noqa: E402
from two_phase_margin import (  # noqa: E402
    TwoPhaseParams, load_prod_params, load_coin_df, common_timeline, simulate,
    RESEARCH_LEVERAGE, FALLBACK_LEVERAGE,
)

_tpm.RESEARCH_LEVERAGE.update({"BTC": 20, "ETH": 20})  # prod-actual per-coin leverage

# Prod-realistic tightness: prod runs budget $90 / position $12 = 7.5x ratio.
# Keep the ratio, scale up for readable $: position $100, budget $750.
POSITION_SIZE = 100.0
BUDGET = 750.0
RALLY_DAYS = 21


def occupied(r, mbuf):
    n = r["n_hours"] or 1
    occ = 0.0
    for c, pc in r["per_coin"].items():
        lev = RESEARCH_LEVERAGE.get(c, FALLBACK_LEVERAGE)
        occ += pc["hours_in_position"] * (POSITION_SIZE + POSITION_SIZE * mbuf / lev)
    return occ / n


def make_params(base, coins, mbuf):
    return TwoPhaseParams(
        coins=coins, entry_threshold_apr=base.entry_threshold_apr,
        phase2_exit_threshold=base.phase2_exit_threshold,
        base_min_hold_hours=base.base_min_hold_hours, cap_min_hold_hours=base.cap_min_hold_hours,
        safety_mult=base.safety_mult, signal_window_hours=base.signal_window_hours,
        concurrency_cap=base.concurrency_cap, position_size_usdc=POSITION_SIZE,
        budget_cap_usdc=BUDGET, margin_buffer_factor=mbuf,
        phase1_negative_patience=base.phase1_negative_patience,
        phase1_breakeven_cap_hours=base.phase1_breakeven_cap_hours,
    )


def find_rally(dfs, win_h):
    """Equal-weight price index; return (start, end) of the win_h window with max return."""
    idx = common_timeline(dfs)
    norm = []
    for c, df in dfs.items():
        s = df["close"].reindex(idx).ffill()
        norm.append(s / s.iloc[0])
    index = pd.concat(norm, axis=1).mean(axis=1)
    best_ret, best_i = -1e9, 0
    arr = index.values
    for i in range(len(arr) - win_h):
        ret = arr[i + win_h] / arr[i] - 1
        if ret > best_ret:
            best_ret, best_i = ret, i
    return idx[best_i], idx[best_i + win_h], best_ret


def main():
    base, _ = load_prod_params()
    coins = base.coins
    dfs = {c: load_coin_df(c) for c in coins}
    ws, we, ret = find_rally(dfs, RALLY_DAYS * 24)
    years = (we - ws).total_seconds() / 3600 / 8760
    print(f"universe {coins}")
    print(f"strongest {RALLY_DAYS}d rally: {ws.date()}→{we.date()}  "
          f"portfolio +{ret*100:.1f}%  (budget ${BUDGET:.0f} / pos ${POSITION_SIZE:.0f}, prod-tight)\n")
    print(f"{'buffer':>7}{'net$':>9}{'APR/occ':>10}{'maxDD%':>9}{'liq':>5}{'forced':>8}{'topups':>8}")
    for mbuf in [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]:
        r = simulate(coins, make_params(base, coins, mbuf), margin_buffer_x=mbuf,
                     position_size=POSITION_SIZE, restrict_start=ws, restrict_end=we)
        net = r["final_equity"] - BUDGET
        occ = occupied(r, mbuf)
        apr = net / occ / years * 100 if (occ and years) else 0.0
        print(f"{mbuf:>7.1f}{net:>+9.2f}{apr:>+9.2f}%{r['max_dd_pct']:>9.2f}"
              f"{r['n_liquidations']:>5}{r['n_forced_closes']:>8}{r['n_top_ups']:>8}")
    print("\nliq/forced > 0 → buffer too thin for this rally (SEGREGATED model = conservative;")
    print("real HL unified margin would tolerate a thinner buffer than shown here).")


if __name__ == "__main__":
    main()
