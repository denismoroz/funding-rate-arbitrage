"""How does margin buffer / leverage affect APR on occupied capital?

Lower buffer (= effectively higher leverage) shrinks the locked USDC per position,
so occupied capital drops and APR on occupied rises — BUT thinner margin means more
liquidations / forced-closes. This sweep quantifies the tradeoff on the live universe.

Ceiling: occupied can never drop below the spot leg (delta-neutral hedge), so APR on
occupied is capped at ~the funding-rate-on-notional minus fees, no matter the leverage.

Research only.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import two_phase_margin as _tpm  # noqa: E402
from two_phase_margin import (  # noqa: E402
    TwoPhaseParams, load_prod_params, load_coin_df, common_timeline, simulate,
    RESEARCH_LEVERAGE, FALLBACK_LEVERAGE,
)

_tpm.RESEARCH_LEVERAGE.update({"BTC": 20, "ETH": 20})  # prod-actual per-coin leverage

POSITION_SIZE = 100.0
BUDGET = 1000.0


def occupied(r, mbuf):
    n = r["n_hours"] or 1
    occ = 0.0
    for c, pc in r["per_coin"].items():
        lev = RESEARCH_LEVERAGE.get(c, FALLBACK_LEVERAGE)
        occ += pc["hours_in_position"] * (POSITION_SIZE + POSITION_SIZE * mbuf / lev)
    return occ / n


def avg_notional(r):
    n = r["n_hours"] or 1
    return sum(pc["hours_in_position"] for pc in r["per_coin"].values()) * POSITION_SIZE / n


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


def main():
    base, _ = load_prod_params()
    coins = base.coins
    dfs = {c: load_coin_df(c) for c in coins}
    tl = common_timeline(dfs)
    ws, we, years = tl[0], tl[-1], len(tl) / 8760.0
    print(f"universe {coins}  window {ws.date()}→{we.date()} ({years:.2f}y)\n")
    print(f"{'buffer':>7}{'occupied$':>11}{'fundAPR/notional':>18}{'APR/occupied':>14}"
          f"{'maxDD%':>9}{'liq':>5}{'forced':>8}")
    for mbuf in [1.0, 2.0, 3.0, 5.0]:
        r = simulate(coins, make_params(base, coins, mbuf), margin_buffer_x=mbuf,
                     position_size=POSITION_SIZE, restrict_start=ws, restrict_end=we)
        net = r["final_equity"] - BUDGET
        occ = occupied(r, mbuf)
        notl = avg_notional(r)
        fund_apr_notl = r["total_funding"] / notl / years * 100 if notl else 0.0
        apr_occ = net / occ / years * 100 if occ else 0.0
        print(f"{mbuf:>7.1f}{occ:>11.0f}{fund_apr_notl:>+17.2f}%{apr_occ:>+13.2f}%"
              f"{r['max_dd_pct']:>9.2f}{r['n_liquidations']:>5}{r['n_forced_closes']:>8}")

    print("\nNote: funding APR on notional is ~constant (funding doesn't depend on buffer);")
    print("APR on occupied rises as buffer falls, ceiling ≈ funding-on-notional − fees.")
    print("Lower buffer → watch liq/forced columns (segregated-margin model overstates this vs HL unified).")


if __name__ == "__main__":
    main()
