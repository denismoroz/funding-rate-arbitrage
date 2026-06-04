"""A/B verification: does letting phase-1 negative-patience override the
dynamic min_hold lock improve the prod-faithful (leverage + margin) backtest?

Compares neg_overrides_min_hold = False (current prod) vs True on:
  - U-prod  : live universe from the prod DB, natural common window
  - U3-long : BTC/ETH/SOL over full available history (more data for robustness)

Reuses the leverage/margin simulation in two_phase_margin.py (the prod mirror:
it imports decide_two_phase as an exact copy of src/frab/engine/two_phase_signals.py,
models per-coin leverage, required margin, top-up/forced-close/liquidation).

Usage:
    python research/verify_neg_override.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import two_phase_margin as _tpm  # noqa: E402
from two_phase_margin import (  # noqa: E402
    TwoPhaseParams, load_prod_params, load_coin_df, common_timeline, simulate,
    RESEARCH_LEVERAGE, FALLBACK_LEVERAGE,
)

# Prod actually caps BTC/ETH at 20x (per state_data.leverage on live farb_positions),
# not the 40x/25x in src/frab/constants.RESEARCH_LEVERAGE. Override the leverage map
# IN MEMORY for this research run only (do NOT edit prod constants). PURR/SOL/HYPE
# already match prod (3/20/10). This makes the sim's req_margin + APR denominator
# faithful to prod's real per-coin leverage.
_tpm.RESEARCH_LEVERAGE.update({"BTC": 20, "ETH": 20})

POSITION_SIZE = 100.0
BUDGET = 1000.0
MBUF = 3.0  # prod margin_buffer_factor


def make_params(base: TwoPhaseParams, coins: list[str]) -> TwoPhaseParams:
    """Same two-phase params as prod, but fixed $100/pos & $1000 budget for comparability."""
    return TwoPhaseParams(
        coins=coins,
        entry_threshold_apr=base.entry_threshold_apr,
        phase2_exit_threshold=base.phase2_exit_threshold,
        base_min_hold_hours=base.base_min_hold_hours,
        cap_min_hold_hours=base.cap_min_hold_hours,
        safety_mult=base.safety_mult,
        signal_window_hours=base.signal_window_hours,
        concurrency_cap=base.concurrency_cap,
        position_size_usdc=POSITION_SIZE,
        budget_cap_usdc=BUDGET,
        margin_buffer_factor=MBUF,
        phase1_negative_patience=base.phase1_negative_patience,
        phase1_breakeven_cap_hours=base.phase1_breakeven_cap_hours,
    )


def run_pair(coins, params, rs=None, re=None) -> dict:
    out = {}
    for flag in (False, True):
        out[flag] = simulate(
            coins, params,
            margin_buffer_x=MBUF, position_size=POSITION_SIZE,
            restrict_start=rs, restrict_end=re,
            neg_overrides_min_hold=flag,
        )
    return out


def fmt(label: str, r: dict) -> str:
    return (
        f"{label:<26} annual={r['annual_pct']:+7.2f}%  sharpe={r['sharpe']:6.3f}  "
        f"sortino={r['sortino']:6.3f}  maxDD={r['max_dd_pct']:6.2f}%  "
        f"final=${r['final_equity']:8.2f}  "
        f"p1neg={r['n_phase1_neg_exits']:3d}  p1cap={r['n_phase1_cap_exits']:3d}  "
        f"p2={r['n_phase2_exits']:3d}  forced={r['n_forced_closes']:3d}  liq={r['n_liquidations']:2d}"
    )


def denom_breakdown(r: dict, k_cap: int, mbuf: float) -> str:
    """Make the headline % interpretable on the capital actually committed to the
    3-leg positions: occupied = spot long + locked USDC margin (per-coin leverage).

    locked_per_pos = position_size × mbuf / leverage   (e.g. $12 @ 10x, buf 3 → $3.6)
    occupied_per_pos = position_size + locked_per_pos   (e.g. $12 + $3.6 = $15.6)

    The headline annual % is on the full $1000 budget (oversized vs occupied), which
    is why it looks tiny; APR on occupied capital is the honest figure."""
    years = r["n_hours"] / 8760.0
    net = r["final_equity"] - BUDGET
    n_hours = r["n_hours"] or 1

    spot_pos_hours = 0.0
    occupied_pos_hours = 0.0   # spot + locked margin, weighted by hours each coin was open
    for c, pc in r["per_coin"].items():
        lev = RESEARCH_LEVERAGE.get(c, FALLBACK_LEVERAGE)
        locked = POSITION_SIZE * mbuf / lev
        occupied_per_pos = POSITION_SIZE + locked
        spot_pos_hours += pc["hours_in_position"] * POSITION_SIZE
        occupied_pos_hours += pc["hours_in_position"] * occupied_per_pos

    avg_n_open = sum(pc["hours_in_position"] for pc in r["per_coin"].values()) / n_hours
    avg_spot = spot_pos_hours / n_hours
    avg_occupied = occupied_pos_hours / n_hours
    apr_budget = (net / BUDGET / years * 100) if years else 0.0
    apr_occupied = (net / avg_occupied / years * 100) if (avg_occupied and years) else 0.0
    return (
        f"    net P&L=${net:+.2f} over {years:.2f}y  |  "
        f"funding=${r['total_funding']:+.2f}  fees=-${r['total_fees']:.2f}  |  "
        f"avg {avg_n_open:.2f}/{k_cap} open → spot ${avg_spot:.0f} + locked margin "
        f"${avg_occupied - avg_spot:.0f} = occupied ${avg_occupied:.0f}  |  "
        f"APR on $1000 budget={apr_budget:+.2f}%  →  APR on occupied capital={apr_occupied:+.2f}%"
    )


def main() -> None:
    base, src = load_prod_params()
    print(f"[params source] {src}")
    print(
        f"[params] p1_neg_patience={base.phase1_negative_patience}  "
        f"cap_min_hold={base.cap_min_hold_hours}  entry={base.entry_threshold_apr}  "
        f"safety_mult={base.safety_mult}  sig_window={base.signal_window_hours}"
    )
    print(f"[fixed]  position_size=${POSITION_SIZE:.0f}  budget=${BUDGET:.0f}  margin_buffer={MBUF}x\n")

    # ── U-prod: live coins, natural common window ──────────────────────────
    prod_coins = base.coins
    dfs = {}
    for c in prod_coins:
        try:
            dfs[c] = load_coin_df(c)
        except FileNotFoundError:
            print(f"[warn] no data for {c}")
    tl = common_timeline(dfs)
    print(f"=== U-prod {prod_coins}  window {tl[0].date()}→{tl[-1].date()} "
          f"({len(tl)}h, {len(tl)/8760:.2f}y) ===")
    res = run_pair(prod_coins, make_params(base, prod_coins))
    print(fmt("  baseline (override=OFF)", res[False]))
    print(fmt("  fix      (override=ON )", res[True]))
    print(denom_breakdown(res[False], base.concurrency_cap, MBUF))

    # ── U3-long: BTC/ETH/SOL full history ──────────────────────────────────
    u3 = ["BTC", "ETH", "SOL"]
    dfs3 = {c: load_coin_df(c) for c in u3}
    tl3 = common_timeline(dfs3)
    print(f"\n=== U3-long {u3}  window {tl3[0].date()}→{tl3[-1].date()} "
          f"({len(tl3)}h, {len(tl3)/8760:.2f}y) ===")
    res3 = run_pair(u3, make_params(base, u3))
    print(fmt("  baseline (override=OFF)", res3[False]))
    print(fmt("  fix      (override=ON )", res3[True]))
    print(denom_breakdown(res3[False], base.concurrency_cap, MBUF))


if __name__ == "__main__":
    main()
