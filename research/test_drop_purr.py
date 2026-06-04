"""Should PURR be dropped from the live universe?

Apples-to-apples: run the prod-faithful margin sim on the SAME window with
PURR in vs PURR out, compare APR on occupied capital, and print PURR's own
per-coin attribution (funding it earned, fees, realized PnL, hours, locked $).

Research only — prod constants untouched (BTC/ETH leverage overridden to prod-actual 20x).
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
MBUF = 3.0


def make_params(base, coins):
    return TwoPhaseParams(
        coins=coins, entry_threshold_apr=base.entry_threshold_apr,
        phase2_exit_threshold=base.phase2_exit_threshold,
        base_min_hold_hours=base.base_min_hold_hours,
        cap_min_hold_hours=base.cap_min_hold_hours, safety_mult=base.safety_mult,
        signal_window_hours=base.signal_window_hours, concurrency_cap=base.concurrency_cap,
        position_size_usdc=POSITION_SIZE, budget_cap_usdc=BUDGET, margin_buffer_factor=MBUF,
        phase1_negative_patience=base.phase1_negative_patience,
        phase1_breakeven_cap_hours=base.phase1_breakeven_cap_hours,
    )


def occupied(r):
    n = r["n_hours"] or 1
    occ = 0.0
    for c, pc in r["per_coin"].items():
        lev = RESEARCH_LEVERAGE.get(c, FALLBACK_LEVERAGE)
        occ += pc["hours_in_position"] * (POSITION_SIZE + POSITION_SIZE * MBUF / lev)
    return occ / n


def apr_occ(r, years):
    occ = occupied(r)
    net = r["final_equity"] - BUDGET
    return (net / occ / years * 100) if occ and years else 0.0


def main():
    base, _ = load_prod_params()
    full = base.coins                       # BTC ETH SOL HYPE PURR
    no_purr = [c for c in full if c != "PURR"]

    # Fix the SAME window (the 5-coin common window, gated by HYPE/PURR start)
    dfs = {c: load_coin_df(c) for c in full}
    tl = common_timeline(dfs)
    ws, we = tl[0], tl[-1]
    years = len(tl) / 8760.0
    print(f"window {ws.date()}→{we.date()}  ({len(tl)}h, {years:.2f}y)  [same for both]\n")

    for label, coins in [("WITH PURR ", full), ("NO PURR   ", no_purr)]:
        r = simulate(coins, make_params(base, coins), margin_buffer_x=MBUF,
                     position_size=POSITION_SIZE, restrict_start=ws, restrict_end=we)
        net = r["final_equity"] - BUDGET
        print(f"{label} {coins}")
        print(f"   net=${net:+.2f}  funding=${r['total_funding']:+.2f}  fees=-${r['total_fees']:.2f}  "
              f"maxDD={r['max_dd_pct']:.2f}%  occupied=${occupied(r):.0f}  "
              f"APR_on_occupied={apr_occ(r, years):+.2f}%")

    # PURR's own attribution (from the WITH-PURR run).
    # Real edge per coin = funding - fees. (realized perp PnL is offset by the spot
    # leg in a delta-neutral book, so it is NOT profit — shown only for reference.)
    r_full = simulate(full, make_params(base, full), margin_buffer_x=MBUF,
                      position_size=POSITION_SIZE, restrict_start=ws, restrict_end=we)
    print("\nPer-coin attribution (WITH PURR run) — real edge = funding - fees:")
    print(f"   {'coin':<6}{'opens':>6}{'hours':>8}{'funding':>10}{'fees':>9}{'EDGE$':>9}"
          f"{'(perp_realized)':>16}  locked/pos")
    for c, pc in sorted(r_full["per_coin"].items(),
                        key=lambda x: -(x[1]["funding_gross"] - x[1]["fees_paid"])):
        lev = RESEARCH_LEVERAGE.get(c, FALLBACK_LEVERAGE)
        locked = POSITION_SIZE * MBUF / lev
        edge = pc["funding_gross"] - pc["fees_paid"]
        print(f"   {c:<6}{pc['n_opens']:>6}{pc['hours_in_position']:>8}"
              f"{pc['funding_gross']:>+10.2f}{pc['fees_paid']:>9.2f}{edge:>+9.2f}"
              f"{pc['realized_pnl']:>+16.2f}   ${locked:.1f} ({lev}x)")


if __name__ == "__main__":
    main()
