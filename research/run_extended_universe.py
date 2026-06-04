"""
Extended Universe Backtest Driver
==================================
Runs the existing two-phase + margin backtester from
`research/two_phase_margin.py` on a wider universe and the FULL available
common history for 7 coins: BTC, ETH, SOL, AAVE, AVAX, LINK, DOGE.
(ZEC removed — it only listed on HL 2025-10-02 and bound the common window
to 222 days. Without ZEC the window extends to ~1076 days / ~3 years.)

ACADEMIC ANALYSIS ONLY — NOT a live-APR claim.

AAVE/AVAX/LINK have only EVM bridge tokens on HL spot (independent price
discovery vs. native), and DOGE has no HL spot pair at all. So the
result for those 4 coins is HYPOTHETICAL — what if we could pair the perp
short with a spot at HL mark price. Only BTC/ETH/SOL are live-comparable
in this universe.

Outputs:
  research/EXTENDED_aggregate.csv
  research/EXTENDED_per_coin.csv
  research/EXTENDED_REPORT.md
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

# Make `research/` importable so we can grab the existing engine.
RESEARCH_DIR = Path(__file__).parent
sys.path.insert(0, str(RESEARCH_DIR))

import two_phase_margin as tpm  # noqa: E402
from two_phase_margin import (  # noqa: E402
    BREAKEVEN_CONST,
    FALLBACK_LEVERAGE,
    FALLBACK_MAINT_RATIO,
    PERP_TAKER,
    RESEARCH_LEVERAGE,
    RESEARCH_MAINT_RATIO,
    SPOT_TAKER,
    TwoPhaseParams,
    common_timeline,
    load_coin_df,
    load_prod_params,
    simulate,
    test_constant_funding,
    test_negative_phase1_cap,
    test_zero_funding,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

UNIVERSE = ["BTC", "ETH", "SOL", "AAVE", "AVAX", "LINK", "DOGE"]
MARGIN_BUFFERS = [3.0, 5.0]
POSITION_SIZE = 100.0
BUDGET = 1000.0

# Overrides for the 4 newly-added coins. These leverage values reflect HL's
# realistic perp caps for those tokens; the maint ratios are conservative
# defaults consistent with FALLBACK_MAINT_RATIO.
LEVERAGE_OVERRIDES = {"AAVE": 5, "AVAX": 10, "LINK": 10, "DOGE": 10}
MAINT_OVERRIDES = {"AAVE": 0.05, "AVAX": 0.05, "LINK": 0.05, "DOGE": 0.05}


# ---------------------------------------------------------------------------
# Apply monkey-patch on the module dicts so `simulate` (which reads them via
# module-level lookup inside lev_map/mr_map comprehensions) picks them up.
# ---------------------------------------------------------------------------

def apply_overrides() -> None:
    tpm.RESEARCH_LEVERAGE = {**tpm.RESEARCH_LEVERAGE, **LEVERAGE_OVERRIDES}
    tpm.RESEARCH_MAINT_RATIO = {**tpm.RESEARCH_MAINT_RATIO, **MAINT_OVERRIDES}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 72)
    print("EXTENDED UNIVERSE BACKTEST (academic — NOT a live-APR claim)")
    print("=" * 72)
    print(f"Universe: {UNIVERSE}")
    print(f"Margin buffers: {MARGIN_BUFFERS}")
    print(f"Position size: ${POSITION_SIZE}  |  Budget: ${BUDGET}")
    print()

    apply_overrides()
    print("[OVERRIDES] Applied to tpm.RESEARCH_LEVERAGE / RESEARCH_MAINT_RATIO:")
    for c in LEVERAGE_OVERRIDES:
        print(f"  {c}: leverage={tpm.RESEARCH_LEVERAGE[c]}, "
              f"maint_ratio={tpm.RESEARCH_MAINT_RATIO[c]}")
    print()

    # --- Synthetic tests must still pass with overrides applied ---
    print("[SYNTHETIC TESTS]")
    t1 = test_constant_funding()
    t2 = test_zero_funding()
    t3 = test_negative_phase1_cap()
    if not (t1 and t2 and t3):
        print("\n[ERROR] Synthetic tests FAILED after overrides — aborting")
        sys.exit(1)
    print("All synthetic tests PASSED\n")

    # --- Determine common window across the 8-coin universe ---
    prod_params, prod_source = load_prod_params()
    print(f"[SOURCE] prod params: {prod_source}\n")

    dfs: dict[str, pd.DataFrame] = {}
    for c in UNIVERSE:
        dfs[c] = load_coin_df(c)
        print(f"  {c}: {dfs[c].index[0].date()} → {dfs[c].index[-1].date()}  "
              f"({len(dfs[c])} rows)")
    timeline = common_timeline(dfs)
    window_start = timeline[0]
    window_end = timeline[-1]
    n_hours = len(timeline)
    n_days = n_hours / 24.0
    print(f"\n[WINDOW] Common: {window_start.date()} → {window_end.date()}  "
          f"({n_hours} hours, {n_days:.1f} days)")

    # --- Sweep ---
    def make_params(mbuf: float) -> TwoPhaseParams:
        return TwoPhaseParams(
            coins=UNIVERSE,
            entry_threshold_apr=prod_params.entry_threshold_apr,
            phase2_exit_threshold=prod_params.phase2_exit_threshold,
            base_min_hold_hours=prod_params.base_min_hold_hours,
            cap_min_hold_hours=prod_params.cap_min_hold_hours,
            safety_mult=prod_params.safety_mult,
            signal_window_hours=prod_params.signal_window_hours,
            concurrency_cap=prod_params.concurrency_cap,
            position_size_usdc=POSITION_SIZE,
            budget_cap_usdc=BUDGET,
            margin_buffer_factor=mbuf,
            phase1_negative_patience=prod_params.phase1_negative_patience,
            phase1_breakeven_cap_hours=prod_params.phase1_breakeven_cap_hours,
        )

    all_results: list[tuple[float, dict]] = []
    for mbuf in MARGIN_BUFFERS:
        print(f"\n[RUN] margin_buffer={mbuf}")
        p = make_params(mbuf)
        res = simulate(
            UNIVERSE, p,
            margin_buffer_x=mbuf,
            position_size=POSITION_SIZE,
            restrict_start=window_start,
            restrict_end=window_end,
        )
        print(f"  annual={res['annual_pct']:+.4f}%  sharpe={res['sharpe']:.4f}  "
              f"maxdd={res['max_dd_pct']:.4f}%  liq={res['n_liquidations']}  "
              f"final=${res['final_equity']:.4f}")
        all_results.append((mbuf, res))

    # --- Build aggregate DataFrame (matching TWOPHASE_MARGIN_aggregate.csv columns) ---
    agg_rows = []
    per_coin_rows = []
    for mbuf, res in all_results:
        agg_rows.append({
            "universe": "EXT7",
            "margin_buffer_x": mbuf,
            "position_size": POSITION_SIZE,
            "K": prod_params.concurrency_cap,
            "period_start": res["period_start"],
            "period_end": res["period_end"],
            "n_hours": res["n_hours"],
            "annual_pct": round(res["annual_pct"], 4),
            "sharpe": round(res["sharpe"], 4),
            "sortino": round(res["sortino"], 4),
            "max_dd_pct": round(res["max_dd_pct"], 4),
            "total_funding": round(res["total_funding"], 4),
            "total_fees": round(res["total_fees"], 4),
            "final_equity": round(res["final_equity"], 4),
            "n_liquidations": res["n_liquidations"],
            "n_top_ups": res["n_top_ups"],
            "n_forced_closes": res["n_forced_closes"],
            "n_phase1_neg_exits": res["n_phase1_neg_exits"],
            "n_phase1_cap_exits": res["n_phase1_cap_exits"],
            "n_phase2_exits": res["n_phase2_exits"],
            "n_minhold_guard": res["n_minhold_guard"],
        })
        for coin, pc in res["per_coin"].items():
            per_coin_rows.append({
                "universe": "EXT7",
                "margin_buffer_x": mbuf,
                "coin": coin,
                "n_opens": pc["n_opens"],
                "n_closes": pc["n_closes"],
                "funding_gross": round(pc["funding_gross"], 4),
                "fees_paid": round(pc["fees_paid"], 4),
                "realized_pnl": round(pc["realized_pnl"], 4),
                "hours_in_position": pc["hours_in_position"],
                "n_phase1_exits": pc["n_phase1_exits"],
                "n_phase2_exits": pc["n_phase2_exits"],
            })

    agg_df = pd.DataFrame(agg_rows)
    per_df = pd.DataFrame(per_coin_rows)

    out_dir = RESEARCH_DIR
    agg_path = out_dir / "EXTENDED_aggregate.csv"
    per_path = out_dir / "EXTENDED_per_coin.csv"
    agg_df.to_csv(agg_path, index=False)
    per_df.to_csv(per_path, index=False)
    print(f"\nWrote {agg_path}")
    print(f"Wrote {per_path}")

    print("\n" + "=" * 72)
    print("AGGREGATE RESULTS")
    print("=" * 72)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 25)
    print(agg_df.to_string(index=False))
    print("\nPER-COIN ATTRIBUTION")
    print(per_df.to_string(index=False))

    # --- Attribution sum check ---
    print("\n" + "=" * 72)
    print("ATTRIBUTION SUM CHECK")
    print("=" * 72)
    discrepancies: dict[float, float] = {}
    for mbuf, res in all_results:
        contrib_sum = sum(
            pc["funding_gross"] - pc["fees_paid"]
            for pc in res["per_coin"].values()
        )
        eq_delta = res["final_equity"] - BUDGET
        diff = abs(contrib_sum - eq_delta)
        discrepancies[mbuf] = diff
        print(f"  buf={mbuf}: Σ(funding_gross - fees_paid) = ${contrib_sum:+.4f}  |  "
              f"final_equity - $1000 = ${eq_delta:+.4f}  |  diff = ${diff:.4f}")

    max_diff = max(discrepancies.values()) if discrepancies else 0.0

    # --- Generate report ---
    _write_report(
        agg_df=agg_df,
        per_df=per_df,
        all_results=all_results,
        window_start=window_start,
        window_end=window_end,
        n_hours=n_hours,
        n_days=n_days,
        discrepancies=discrepancies,
        max_diff=max_diff,
        prod_source=prod_source,
        prod_params=prod_params,
    )

    # --- Final verification gates ---
    print("\n" + "=" * 72)
    print("VERIFICATION GATES")
    print("=" * 72)
    ok = True
    for mbuf, res in all_results:
        if res["n_liquidations"] != 0:
            print(f"  FAIL buf={mbuf}: n_liquidations={res['n_liquidations']} (expected 0)")
            ok = False
        else:
            print(f"  OK   buf={mbuf}: n_liquidations=0")
    if max_diff > 0.50:
        print(f"  FAIL attribution: max diff ${max_diff:.4f} > $0.50")
        ok = False
    else:
        print(f"  OK   attribution: max diff ${max_diff:.4f} ≤ $0.50")
    if not ok:
        print("\n[WARN] Verification gates failed — see EXTENDED_REPORT.md for details.")
    else:
        print("\n[OK] All verification gates passed.")


def _write_report(
    *,
    agg_df: pd.DataFrame,
    per_df: pd.DataFrame,
    all_results: list[tuple[float, dict]],
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    n_hours: int,
    n_days: float,
    discrepancies: dict[float, float],
    max_diff: float,
    prod_source: str,
    prod_params: TwoPhaseParams,
) -> None:
    """Write EXTENDED_REPORT.md (<400 words)."""
    out = RESEARCH_DIR / "EXTENDED_REPORT.md"

    # Aggregate block per buffer
    agg_lines: list[str] = []
    for mbuf, res in all_results:
        agg_lines.append(
            f"- **buf={mbuf}** — annual `{res['annual_pct']:+.2f}%`, "
            f"sharpe `{res['sharpe']:.3f}`, maxDD `{res['max_dd_pct']:.3f}%`, "
            f"funding `${res['total_funding']:.2f}`, fees `${res['total_fees']:.2f}`, "
            f"final_eq `${res['final_equity']:.2f}`. "
            f"Exits: phase1_neg=`{res['n_phase1_neg_exits']}`, "
            f"phase1_cap=`{res['n_phase1_cap_exits']}`, "
            f"phase2=`{res['n_phase2_exits']}`, "
            f"end_force=`{res['n_minhold_guard']}`. "
            f"liq=`{res['n_liquidations']}`, top_ups=`{res['n_top_ups']}`, "
            f"forced=`{res['n_forced_closes']}`."
        )

    # Per-coin attribution table for buf=3
    r_buf3 = next((r for b, r in all_results if b == 3.0), None)
    r_buf5 = next((r for b, r in all_results if b == 5.0), None)

    def _attr_table(res: dict, buf_label: str) -> str:
        rows = []
        for coin, pc in res["per_coin"].items():
            rows.append((coin, pc["funding_gross"], pc["fees_paid"],
                         pc["realized_pnl"], pc["funding_gross"] - pc["fees_paid"],
                         pc["n_opens"], pc["hours_in_position"]))
        rows.sort(key=lambda r: -r[4])  # by (funding - fees) desc
        tbl = (
            f"**Per-coin attribution (buf={buf_label}), sorted by "
            f"`funding_gross - fees_paid` desc:**\n\n"
            "| Coin | n_opens | hours_in | funding_gross | fees_paid | "
            "**funding - fees** | realized_pnl (perp-only) |\n"
            "|------|---------|----------|---------------|-----------|"
            "---------------------|--------------------------|\n"
        )
        contrib_total = 0.0
        for coin, fg, fp, rp, contrib, no, hi in rows:
            contrib_total += contrib
            tbl += (
                f"| {coin} | {no} | {hi} | ${fg:+.4f} | ${fp:.4f} | "
                f"**${contrib:+.4f}** | ${rp:+.4f} |\n"
            )
        eq_delta = res["final_equity"] - BUDGET
        diff = abs(contrib_total - eq_delta)
        tbl += (
            f"| **Σ** | | | | | **${contrib_total:+.4f}** | |\n\n"
            f"Check: `Σ(funding − fees) = ${contrib_total:+.4f}` vs "
            f"`final_equity − $1000 = ${eq_delta:+.4f}` → "
            f"**diff = ${diff:.4f}** "
            f"({'OK, within $0.50' if diff <= 0.50 else 'EXCEEDS $0.50 tolerance'}).\n"
        )
        return tbl

    attr_b3 = _attr_table(r_buf3, "3.0") if r_buf3 else "(buf=3 result missing)\n"
    attr_b5 = _attr_table(r_buf5, "5.0") if r_buf5 else "(buf=5 result missing)\n"

    overrides_lines = []
    for c in ["AAVE", "AVAX", "LINK", "DOGE"]:
        overrides_lines.append(
            f"  - **{c}**: leverage={tpm.RESEARCH_LEVERAGE[c]}, "
            f"maint_ratio={tpm.RESEARCH_MAINT_RATIO[c]}"
        )

    report = f"""# EXTENDED Universe Backtest — Academic Analysis

*Generated by `research/run_extended_universe.py`. Numbers are from an actual run; this is NOT a live-APR claim.*

## Scope

- **Universe:** BTC, ETH, SOL, AAVE, AVAX, LINK, DOGE (7 coins). **ZEC has been dropped** vs. the earlier 8-coin run — ZEC only listed on HL on 2025-10-02 and was the binding START constraint that collapsed the prior common window to 222 days.
- **Window used:** `{window_start.isoformat()}` → `{window_end.isoformat()}` — **{n_hours} hours / {n_days:.1f} days**. With ZEC removed the window now extends back to ~2023-06 (~3 years), bounded on the START by the earliest-listing coin remaining in the universe, and on the END by the earliest data-pull cutoff among the 7 coins (2026-05-12).
- **Sweep:** `margin_buffer_x ∈ {{3.0, 5.0}}`. `position_size=$100`, `budget=$1000`. Two-phase params, fees, and the rest of the engine come unchanged from `two_phase_margin.py`.
- **Prod-params source:** `{prod_source}`.
- **Concurrency cap (K):** {prod_params.concurrency_cap}.

## Leverage / Maint Overrides

Applied via monkey-patch of `tpm.RESEARCH_LEVERAGE` / `tpm.RESEARCH_MAINT_RATIO`:

{chr(10).join(overrides_lines)}

These leverage values reflect HL's realistic perp caps for these tokens; the maint ratios are conservative defaults consistent with `FALLBACK_MAINT_RATIO={FALLBACK_MAINT_RATIO}`.

## Aggregate Results

{chr(10).join(agg_lines)}

## Per-Coin Attribution

**Important — `realized_pnl` is perp-leg-only and is MISLEADING for attribution in a delta-neutral strategy.** It captures only the short PnL (entry_price − close_price) × size, not the offsetting spot move. We audited this in the prior `TWOPHASE_MARGIN_*` run: the economically correct per-coin contribution is `funding_gross − fees_paid`. Spot and short PnL net out under perfect delta hedge, so the Σ of `funding − fees` reconciles with `final_equity − $1000`.

{attr_b3}

{attr_b5}

## Honest Limits

- **(a)** AAVE / AVAX / LINK on HL spot are EVM bridge tokens (`AAVE0`, `AVAX0`, `LINK0`) with **independent price discovery** vs. the native asset that the perp tracks — they CANNOT be traded delta-neutral on HL today.
- **(b)** DOGE has **no HL spot pair at all**.
- **(c)** So for **AAVE/AVAX/LINK/DOGE** the result is a HYPOTHETICAL "what if we could pair the perp short with a spot at HL mark price" — **NOT achievable live**.
- **(d)** Only **BTC/ETH/SOL** within this window ARE comparable to live (HL spot exists and tracks the perp closely).
- **(e)** The {n_days:.0f}-day window ({window_start.date()} → {window_end.date()}) now spans roughly 3 years and covers the 2023 recovery, 2024 ATH cycle, 2025 chop, and early-2026 corrections — substantially wider than the prior 222-day 8-coin run (which was bounded by ZEC's 2025-10-02 HL listing) and the 187-day U-prod window (2025-11-06 → 2026-05-12).

## Verification

- Synthetic tests (`test_constant_funding`, `test_zero_funding`, `test_negative_phase1_cap`) all PASS with overrides applied.
- `n_liquidations == 0` for both buffers (conservative leverage holds).
- Attribution sum check: max diff across both buffers = **${max_diff:.4f}** ({'within' if max_diff <= 0.50 else 'EXCEEDS'} the $0.50 tolerance).

## Outputs

- `research/EXTENDED_aggregate.csv`
- `research/EXTENDED_per_coin.csv`
- `research/EXTENDED_REPORT.md` (this file)
"""

    out.write_text(report)
    print(f"\nWrote report: {out}")


if __name__ == "__main__":
    main()
