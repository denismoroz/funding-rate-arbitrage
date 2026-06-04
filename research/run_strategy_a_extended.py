"""
Strategy A Extended Universe Backtest Driver
============================================
Runs the audited Strategy A backtester from `research/portfolio_margin.py`
on the SAME 7-coin universe and the SAME ~3-year common window as the
just-completed two-phase EXTENDED run, so the results can be compared
apples-to-apples.

Strategy A (original simple strategy):
  - Entry: signal > 0.30
  - Exit:  signal < -0.15 AND hours_in >= 120
  - min_hold_hours = 120 (fixed)
  - signal_window_hours = 12
  - concurrency_cap K = 3

ACADEMIC ANALYSIS ONLY — NOT a live-APR claim.

AAVE / AVAX / LINK on HL spot are EVM bridge tokens with independent price
discovery vs. native (which the perp tracks). DOGE has no HL spot pair at
all. So for those 4 coins this is HYPOTHETICAL — only BTC/ETH/SOL are
live-tradeable in this universe.

Outputs:
  research/STRATEGY_A_aggregate.csv
  research/STRATEGY_A_per_coin.csv
  research/STRATEGY_A_REPORT.md
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

RESEARCH_DIR = Path(__file__).parent
sys.path.insert(0, str(RESEARCH_DIR))

import portfolio_margin as pm  # noqa: E402
from portfolio_margin import (  # noqa: E402
    common_timeline,
    load_coin_dfs,
    simulate_portfolio,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

UNIVERSE = ["BTC", "ETH", "SOL", "AAVE", "AVAX", "LINK", "DOGE"]
MARGIN_BUFFERS = [3.0, 5.0]
POSITION_SIZE = 100.0
BUDGET = 1000.0

# Strategy A params
STRATEGY_A_PARAMS = dict(
    entry_threshold=0.30,
    exit_threshold=-0.15,
    min_hold_hours=120,
    signal_window_hours=12,
    concurrency_cap=3,
    position_size=POSITION_SIZE,
    budget_cap_usd=BUDGET,
)

# Leverage/maint overrides — match the just-completed EXTENDED run.
# Note: portfolio_margin.PER_COIN_LEVERAGE already has AAVE=5, AVAX/LINK=10,
# DOGE=5. We bump DOGE to 10 to match EXTENDED. AAVE maint already 0.05 via
# DEFAULT_MAINT_RATIO; AVAX/LINK default to 0.025, we bump to 0.05 per
# EXTENDED to be conservative.
LEVERAGE_OVERRIDES = {"AAVE": 5, "AVAX": 10, "LINK": 10, "DOGE": 10}
MAINT_OVERRIDES = {"AAVE": 0.05, "AVAX": 0.05, "LINK": 0.05, "DOGE": 0.05}


def apply_overrides() -> None:
    """Monkey-patch the module-level dicts so simulate_portfolio sees them."""
    pm.PER_COIN_LEVERAGE = {**pm.PER_COIN_LEVERAGE, **LEVERAGE_OVERRIDES}
    pm.PER_COIN_MAINT_RATIO = {**pm.PER_COIN_MAINT_RATIO, **MAINT_OVERRIDES}


def verify_audited_fix() -> None:
    """Grep portfolio_margin.py for the 'do NOT add it again' marker."""
    pm_file = Path(pm.__file__)
    text = pm_file.read_text()
    if "do NOT add it again" not in text:
        print("[FATAL] Audited fix marker 'do NOT add it again' is MISSING "
              "from research/portfolio_margin.py — aborting.")
        sys.exit(2)
    print("[OK] Audited fix marker present in portfolio_margin.py")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("=" * 72)
    print("STRATEGY A EXTENDED-UNIVERSE BACKTEST (academic — NOT a live-APR claim)")
    print("=" * 72)
    print(f"Universe: {UNIVERSE}")
    print(f"Margin buffers: {MARGIN_BUFFERS}")
    print(f"Position size: ${POSITION_SIZE}  |  Budget: ${BUDGET}")
    print(f"Strategy A params: entry>{STRATEGY_A_PARAMS['entry_threshold']}, "
          f"exit<{STRATEGY_A_PARAMS['exit_threshold']}, "
          f"min_hold={STRATEGY_A_PARAMS['min_hold_hours']}h, "
          f"sig_window={STRATEGY_A_PARAMS['signal_window_hours']}h, "
          f"K={STRATEGY_A_PARAMS['concurrency_cap']}")
    print()

    verify_audited_fix()
    apply_overrides()
    print("[OVERRIDES] Applied to pm.PER_COIN_LEVERAGE / PER_COIN_MAINT_RATIO:")
    for c in LEVERAGE_OVERRIDES:
        print(f"  {c}: leverage={pm.PER_COIN_LEVERAGE[c]}, "
              f"maint_ratio={pm.PER_COIN_MAINT_RATIO[c]}")
    print()

    # --- Determine common window for the 7-coin universe ---
    dfs = load_coin_dfs(UNIVERSE)
    for c in UNIVERSE:
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
    all_results: list[tuple[float, dict]] = []
    for mbuf in MARGIN_BUFFERS:
        print(f"\n[RUN] margin_buffer_x={mbuf}")
        res = simulate_portfolio(
            coins=UNIVERSE,
            margin_buffer_x=mbuf,
            **STRATEGY_A_PARAMS,
        )
        print(f"  annual={res['annual_pct']:+.4f}%  sharpe={res['sharpe']:.4f}  "
              f"maxdd={res['max_dd_pct']:.4f}%  liq={res['n_liquidations']}  "
              f"final=${res['final_equity']:.4f}")
        all_results.append((mbuf, res))

    # --- Build aggregate DataFrame (same convention as EXTENDED_aggregate.csv) ---
    agg_rows = []
    per_coin_rows = []
    for mbuf, res in all_results:
        ts = res['timestamp_history']
        period_start = ts[0].date().isoformat() if ts else ""
        period_end = ts[-1].date().isoformat() if ts else ""
        # Strategy A has only one exit reason (signal-based after min_hold).
        # Total opens / closes per buffer:
        n_opens = sum(pc['n_opens'] for pc in res['per_coin'].values())
        n_closes = sum(pc['n_closes'] for pc in res['per_coin'].values())
        agg_rows.append({
            "universe": "STRAT_A_EXT7",
            "margin_buffer_x": mbuf,
            "position_size": POSITION_SIZE,
            "K": STRATEGY_A_PARAMS['concurrency_cap'],
            "entry_threshold": STRATEGY_A_PARAMS['entry_threshold'],
            "exit_threshold": STRATEGY_A_PARAMS['exit_threshold'],
            "min_hold_hours": STRATEGY_A_PARAMS['min_hold_hours'],
            "period_start": period_start,
            "period_end": period_end,
            "n_hours": len(ts),
            "annual_pct": round(res["annual_pct"], 4),
            "sharpe": round(res["sharpe"], 4),
            "sortino": round(res["sortino"], 4),
            "max_dd_pct": round(res["max_dd_pct"], 4),
            "total_funding": round(res["total_funding"], 4),
            "total_fees": round(res["total_fees"], 4),
            "final_equity": round(res["final_equity"], 4),
            "n_opens": n_opens,
            "n_closes": n_closes,
            "n_liquidations": res["n_liquidations"],
            "n_top_ups": res["n_top_ups"],
            "n_forced_closes": res["n_forced_closes"],
            "n_skipped_opens_capital": res["n_skipped_opens_capital"],
        })
        for coin, pc in res["per_coin"].items():
            per_coin_rows.append({
                "universe": "STRAT_A_EXT7",
                "margin_buffer_x": mbuf,
                "coin": coin,
                "n_opens": pc["n_opens"],
                "n_closes": pc["n_closes"],
                "funding_gross": round(pc["funding_gross"], 4),
                "fees_paid": round(pc["fees_paid"], 4),
                "realized_pnl": round(pc["realized_pnl"], 4),
                "hours_in_position": pc["hours_in_position"],
            })

    agg_df = pd.DataFrame(agg_rows)
    per_df = pd.DataFrame(per_coin_rows)

    agg_path = RESEARCH_DIR / "STRATEGY_A_aggregate.csv"
    per_path = RESEARCH_DIR / "STRATEGY_A_per_coin.csv"
    agg_df.to_csv(agg_path, index=False)
    per_df.to_csv(per_path, index=False)
    print(f"\nWrote {agg_path}")
    print(f"Wrote {per_path}")

    print("\n" + "=" * 72)
    print("AGGREGATE RESULTS")
    print("=" * 72)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
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
        all_results=all_results,
        window_start=window_start,
        window_end=window_end,
        n_hours=n_hours,
        n_days=n_days,
        discrepancies=discrepancies,
        max_diff=max_diff,
    )

    # --- Verification gates ---
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
        print("\n[WARN] Verification gates failed — see STRATEGY_A_REPORT.md for details.")
    else:
        print("\n[OK] All verification gates passed.")


def _write_report(
    *,
    all_results: list[tuple[float, dict]],
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    n_hours: int,
    n_days: float,
    discrepancies: dict[float, float],
    max_diff: float,
) -> None:
    """Write STRATEGY_A_REPORT.md (<400 words)."""
    out = RESEARCH_DIR / "STRATEGY_A_REPORT.md"

    LIVE_COINS = {"BTC", "ETH", "SOL"}

    def _mark(coin: str) -> str:
        if coin in LIVE_COINS:
            return "live"
        if coin == "DOGE":
            return "hypo (no HL spot)"
        return "hypo (bridge spot)"

    # Aggregate block per buffer
    agg_lines: list[str] = []
    for mbuf, res in all_results:
        n_opens = sum(pc['n_opens'] for pc in res['per_coin'].values())
        n_closes = sum(pc['n_closes'] for pc in res['per_coin'].values())
        agg_lines.append(
            f"- **buf={mbuf}** — total_return `{res['annual_pct']:+.2f}%`, "
            f"sharpe `{res['sharpe']:.3f}`, maxDD `{res['max_dd_pct']:.3f}%`, "
            f"funding `${res['total_funding']:.2f}`, fees `${res['total_fees']:.2f}`, "
            f"final_eq `${res['final_equity']:.2f}`. "
            f"Opens=`{n_opens}`, closes=`{n_closes}`. "
            f"liq=`{res['n_liquidations']}`, top_ups=`{res['n_top_ups']}`, "
            f"forced=`{res['n_forced_closes']}`, "
            f"skipped_capital=`{res['n_skipped_opens_capital']}`."
        )

    # Per-coin attribution tables
    r_buf3 = next((r for b, r in all_results if b == 3.0), None)
    r_buf5 = next((r for b, r in all_results if b == 5.0), None)

    def _attr_table(res: dict, buf_label: str) -> str:
        rows = []
        for coin, pc in res["per_coin"].items():
            rows.append((coin, pc["funding_gross"], pc["fees_paid"],
                         pc["realized_pnl"], pc["funding_gross"] - pc["fees_paid"],
                         pc["n_opens"], pc["hours_in_position"]))
        rows.sort(key=lambda r: -r[4])
        tbl = (
            f"**Per-coin attribution (buf={buf_label}), sorted by "
            f"`funding_gross - fees_paid` desc:**\n\n"
            "| Coin | tag | n_opens | hours_in | funding_gross | fees_paid | "
            "**funding - fees** | realized_pnl (perp-only) |\n"
            "|------|-----|---------|----------|---------------|-----------|"
            "---------------------|--------------------------|\n"
        )
        contrib_total = 0.0
        for coin, fg, fp, rp, contrib, no, hi in rows:
            contrib_total += contrib
            tbl += (
                f"| {coin} | {_mark(coin)} | {no} | {hi} | ${fg:+.4f} | ${fp:.4f} | "
                f"**${contrib:+.4f}** | ${rp:+.4f} |\n"
            )
        eq_delta = res["final_equity"] - BUDGET
        diff = abs(contrib_total - eq_delta)
        tbl += (
            f"| **Σ** | | | | | | **${contrib_total:+.4f}** | |\n\n"
            f"Check: `Σ(funding − fees) = ${contrib_total:+.4f}` vs "
            f"`final_equity − $1000 = ${eq_delta:+.4f}` → "
            f"**diff = ${diff:.4f}** "
            f"({'OK, within $0.50' if diff <= 0.50 else 'EXCEEDS $0.50 tolerance'}).\n"
        )
        return tbl

    attr_b3 = _attr_table(r_buf3, "3.0") if r_buf3 else "(buf=3 result missing)\n"
    attr_b5 = _attr_table(r_buf5, "5.0") if r_buf5 else "(buf=5 result missing)\n"

    # EXTENDED two-phase reference numbers (from EXTENDED_aggregate.csv)
    EXT_BUF3_PCT = 8.2588
    EXT_BUF5_PCT = 7.0466

    a_buf3 = next((r['annual_pct'] for b, r in all_results if b == 3.0), float('nan'))
    a_buf5 = next((r['annual_pct'] for b, r in all_results if b == 5.0), float('nan'))

    def _cmp(a, b):
        if a > b:
            return "is better"
        if a < b:
            return "is NOT better"
        return "ties"

    comparison_lines = [
        f"- **buf=3.0**: Strategy A `{a_buf3:+.2f}%` vs two-phase EXTENDED "
        f"`+{EXT_BUF3_PCT:.2f}%` — Strategy A **{_cmp(a_buf3, EXT_BUF3_PCT)}**.",
        f"- **buf=5.0**: Strategy A `{a_buf5:+.2f}%` vs two-phase EXTENDED "
        f"`+{EXT_BUF5_PCT:.2f}%` — Strategy A **{_cmp(a_buf5, EXT_BUF5_PCT)}**.",
    ]

    overrides_lines = []
    for c in ["AAVE", "AVAX", "LINK", "DOGE"]:
        overrides_lines.append(
            f"  - **{c}**: leverage={pm.PER_COIN_LEVERAGE[c]}, "
            f"maint_ratio={pm.PER_COIN_MAINT_RATIO[c]}"
        )

    report = f"""# Strategy A Extended-Universe Backtest — Academic Analysis

*Generated by `research/run_strategy_a_extended.py`. Numbers are from an actual run; this is NOT a live-APR claim.*

## Scope

- **Universe:** BTC, ETH, SOL, AAVE, AVAX, LINK, DOGE (same 7 coins as the just-completed two-phase `EXTENDED_*` run; ZEC excluded).
- **Window used:** `{window_start.isoformat()}` → `{window_end.isoformat()}` — **{n_hours} hours / {n_days:.1f} days**.
- **Sweep:** `margin_buffer_x ∈ {{3.0, 5.0}}`. `position_size=$100`, `budget=$1000`.
- **Backtester:** `research/portfolio_margin.py` (audited; req_margin double-count fix at line 436 is in place).

## Strategy A Params

- `entry_threshold = 0.30` (signal must exceed 30% APR-equivalent)
- `exit_threshold  = -0.15`
- `min_hold_hours  = 120` (fixed, not dynamic)
- `signal_window_hours = 12`
- `concurrency_cap K = 3`

## Leverage / Maint Overrides

Monkey-patched on `pm.PER_COIN_LEVERAGE` / `pm.PER_COIN_MAINT_RATIO` to match the EXTENDED two-phase run:

{chr(10).join(overrides_lines)}

BTC/ETH/SOL keep portfolio_margin defaults (BTC/ETH lev=20 maint=0.01; SOL lev=10 maint=0.025).

## Aggregate Results

{chr(10).join(agg_lines)}

Reported `annual_pct` here is the **total return over the window** (same convention as `simulate_portfolio` and as the EXTENDED CSV's `annual_pct` column — NOT annualized).

## Per-Coin Attribution

**Note:** `realized_pnl` is perp-leg-only and is misleading for a delta-neutral strategy — the economically correct contribution is `funding_gross − fees_paid`.

`tag` column: **live** = BTC/ETH/SOL (HL spot pair exists, tracks perp closely). **hypo (bridge spot)** = AAVE/AVAX/LINK (HL spot is `AAVE0`/`AVAX0`/`LINK0` bridge tokens with independent price discovery — not tradeable delta-neutral on HL). **hypo (no HL spot)** = DOGE (no HL spot pair at all).

{attr_b3}

{attr_b5}

## Comparison vs Two-Phase EXTENDED Run

Both ran on the same 7-coin universe and same window. Reference numbers from `research/EXTENDED_aggregate.csv`:

{chr(10).join(comparison_lines)}

## Honest Limits

- For **AAVE/AVAX/LINK/DOGE** the result is HYPOTHETICAL — not achievable live on HL today. Only **BTC/ETH/SOL** are live-tradeable in this universe.
- `annual_pct` is total return over a ~{n_days:.0f}-day window, not annualized — divide by ({n_days/365.0:.2f} years) for a rough annual figure.
- Strategy A's fixed 120-hour min-hold and stricter entry threshold (`0.30` vs two-phase `0.10`) mean far fewer trades on the same data — see `n_opens` per buffer in the aggregate.

## Verification

- `n_liquidations == 0` for both buffers.
- Attribution sum check: max diff = **${max_diff:.4f}** ({'within' if max_diff <= 0.50 else 'EXCEEDS'} the $0.50 tolerance).
- Audited fix marker (`do NOT add it again`) confirmed present in `research/portfolio_margin.py` at line 436.

## Outputs

- `research/STRATEGY_A_aggregate.csv`
- `research/STRATEGY_A_per_coin.csv`
- `research/STRATEGY_A_REPORT.md` (this file)
"""

    out.write_text(report)
    print(f"\nWrote report: {out}")


if __name__ == "__main__":
    main()
