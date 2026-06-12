"""
DECISIVE TEST: does the crypto cross-sectional momentum book's edge survive
charging the REALIZED HL perp funding paid/earned on held positions?

The "C6 winner" daily Sharpe of ~1.34 was computed on SPOT price moves only
(fwd_ret), NOT charging the ongoing HL perp funding we pay/earn while a position
is held. We long recently-strong coins (often high positive funding → a LONG
PAYS funding) and short weak ones → unmodelled funding is a likely HEADWIND.

This script reuses the FROZEN book wiring verbatim (crypto_pkg config: the same
frozen 34-coin universe, rebal_every=7, costs_bps=8.5 = HL perp taker+slippage)
and the `accrual` mechanism already in xsec.portfolio_returns. The ONLY change
between the two runs is whether the funding accrual panel is supplied. Nothing
in crypto/ is modified — read & imported only.

────────────────────────────────────────────────────────────────────────────
FUNDING SIGN (verified against cryptodata + the carry signal)
────────────────────────────────────────────────────────────────────────────
cryptodata._daily_funding stores the RAW HL `fundingRate`, summed over the day's
hourly prints (cryptodata docstring: "daily carry = SUM of the day's hourly
funding rates"). On Hyperliquid a POSITIVE funding rate means LONGS PAY SHORTS.
So `funding[t,c]` is the per-day FRACTION a unit LONG pays on day t.

The carry signal corroborates the sign: signals.carry = -funding.rolling().mean()
with the docstring "positive HL funding means longs PAY shorts ... we want HIGHER
score (more attractive long) for LOWER funding, hence the leading minus." So a
LONG dislikes positive funding — confirming `funding>0` ⇒ a long bleeds.

P&L of a held position over one funding day, per unit held weight:
    held>0 (long),  funding>0  → PAYS    → contributes -held*funding < 0
    held<0 (short), funding>0  → RECEIVES → contributes -held*funding > 0
i.e. the funding cash flow per unit held = held * (-funding). The accrual
contract in xsec.portfolio_returns sums `(held * accrual[t]).sum()` each held
period, so the accrual panel that encodes funding is:

    funding_accrual[t,c] = -funding[t,c]            (per-day FRACTIONAL units)

────────────────────────────────────────────────────────────────────────────
ALIGNMENT (matched to fwd_ret's t→t+1 hold)
────────────────────────────────────────────────────────────────────────────
fwd_ret[t] = price[t+1]/price[t] - 1  → the return REALIZED over the hold t→t+1.
The accrual contract says accrual[t] is earned over that SAME interval t→t+1.

cryptodata stores funding[t] as the funding accrued DURING day t (the day whose
close defines price[t]) — it is a TRAILING quantity at t. The funding actually
charged on the position held over the forward interval t→t+1 is the funding
accrued during day t+1, which sits at row t+1 of the funding panel. Aligned to
row t (like fwd_ret), that is funding.shift(-1) at t. Hence:

    funding_accrual = -funding.shift(-1)     (PRIMARY alignment)

This is a REALIZED cost, NOT look-ahead: it never enters any signal (carry uses
TRAILING funding ≤ t; momentum uses price only). It is charged on the SAME
forward hold as fwd_ret, exactly like fwd_ret itself.

SENSITIVITY: funding is highly autocorrelated, so the verdict must not hinge on
the ±1-day alignment choice. We therefore ALSO report the t-aligned variant
(funding_accrual = -funding, no shift) and the +1 variant (-funding.shift(-2))
and show the metrics barely move.

Run:
  cd research/cross_sectional/crypto && PYTHONPATH=... python funding_impact.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import cryptodata
import signals
import xsec
from crypto_pkg import (
    CryptoXSecPackage,
    MOM_LOOKBACKS,
    BLEND_MOM,
    CARRY_SMOOTH,
)
from metrics_daily import daily_metrics


# ── Config: pull the EXACT frozen-book numbers (only funding changes) ──────────
_PKG = CryptoXSecPackage()              # rebal_every=7, costs_bps=8.5, frozen 34
CB = _PKG.costs_bps                     # 8.5 bps/leg (HL perp taker + slippage)
REBAL = _PKG.rebal_every               # 7 (weekly)
FROZEN = _PKG._frozen                   # frozen 34-coin universe
P = cryptodata.load_panel(coins=FROZEN)
FUND = P["funding"]
FWD = P["fwd_ret"]


# ── Funding accrual panels (per-day fractional, sign = -funding) ───────────────
def funding_accrual(shift: int) -> pd.DataFrame:
    """-funding aligned to the t→t+1 hold.

    shift = -1 : PRIMARY — funding accrued during day t+1 (the forward hold),
                 aligned to row t exactly like fwd_ret[t]=price[t+1]/price[t]-1.
    shift =  0 : t-aligned (the funding stored at the decision day t).
    shift = -2 : +1-day variant (funding of day t+2), the far side of the bracket.
    """
    return -(FUND.shift(shift))


ACCR_PRIMARY = funding_accrual(-1)


# ── Per-book score panels (reuse signals.py verbatim) ──────────────────────────
def score_panels() -> dict[str, pd.DataFrame]:
    ens = signals.momentum_ensemble(P, lookbacks=(14, 21, 30, 45, 60))
    mom30 = signals.momentum(P, 30)
    mom60 = signals.momentum(P, BLEND_MOM)              # 60
    z_mom = signals.zscore_cross_section(signals.momentum(P, BLEND_MOM))
    z_carry = signals.zscore_cross_section(signals.carry(P, smooth_days=CARRY_SMOOTH))
    blend = signals.blend([z_mom, z_carry])             # equal-weight z-mom60 + z-carry
    return {
        "momentum_ensemble": ens,
        "mom30": mom30,
        "mom60": mom60,
        "blend": blend,
    }


def pnl_pair(score: pd.DataFrame, accrual: pd.DataFrame | None = ACCR_PRIMARY):
    """(pnl_nofund, pnl_fund) for a score panel, identical wiring bar accrual."""
    w = xsec.rank_to_weights(score)
    pnl_nofund = xsec.portfolio_returns(w, FWD, costs_bps=CB, rebal_every=REBAL)
    pnl_fund = xsec.portfolio_returns(w, FWD, costs_bps=CB, rebal_every=REBAL,
                                      accrual=accrual)
    return w, pnl_nofund, pnl_fund


# ── Formatting helpers ─────────────────────────────────────────────────────────
def _fmt(m: dict) -> str:
    if not m:
        return "  (too few days)"
    return (f"Sharpe {m['sharpe']:+.2f}  ann {100*m['ann']:+6.2f}%  "
            f"maxDD {100*m['maxdd']:5.2f}%  Calmar {m['calmar']:+5.2f}  "
            f"hit {100*m['hit']:.1f}%")


def _half_split(pnl: pd.Series, label: str):
    r = pnl.dropna()
    h = len(r) // 2
    m1, m2 = daily_metrics(r.iloc[:h]), daily_metrics(r.iloc[h:])
    print(f"  {label} 1st half ({m1.get('n','?')}d): Sharpe {m1.get('sharpe',float('nan')):+.2f}"
          f"  ann {100*m1.get('ann',float('nan')):+.2f}%")
    print(f"  {label} 2nd half ({m2.get('n','?')}d): Sharpe {m2.get('sharpe',float('nan')):+.2f}"
          f"  ann {100*m2.get('ann',float('nan')):+.2f}%")


# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    pd.set_option("display.width", 200)
    print("=" * 80)
    print("CRYPTO CROSS-SECTIONAL MOMENTUM — DOES THE EDGE SURVIVE PERP FUNDING?")
    print("=" * 80)
    print(f"frozen universe : {len(FROZEN)} coins")
    print(f"panel           : {P['price'].index.min().date()} -> "
          f"{P['price'].index.max().date()}  ({len(P['price'])} days)")
    print(f"config          : rebal_every={REBAL}  costs_bps={CB:.2f}/leg "
          f"(HL perp taker+slip)  [pulled from crypto_pkg, frozen]")
    print(f"funding accrual : -funding.shift(-1)  (per-day fractional, "
          f"matched to fwd_ret's t->t+1 hold)")

    # ── Funding sign / no-look-ahead HAND-CHECK on one recent cell ─────────────
    print("\n" + "-" * 80)
    print("HAND-CHECK: funding sign on one held coin / one recent date")
    print("-" * 80)
    scores = score_panels()
    w_ens = xsec.rank_to_weights(scores["momentum_ensemble"])
    # find a recent date where some LONG sits in a POSITIVE-funding coin
    held_dates = w_ens.index[(w_ens != 0).any(axis=1)]
    picked = None
    for t in reversed(held_dates):
        i = FWD.index.get_loc(t)
        if i + 1 >= len(FWD.index):
            continue
        fwd_day_fund = FUND.iloc[i + 1]          # funding charged over the t->t+1 hold
        longs = w_ens.loc[t][w_ens.loc[t] > 0]
        cand = [c for c in longs.index if fwd_day_fund.get(c, 0) > 0
                and not np.isnan(fwd_day_fund.get(c, np.nan))]
        if cand:
            picked = (t, cand[0])
            break
    if picked is not None:
        t, c = picked
        i = FWD.index.get_loc(t)
        held = w_ens.loc[t, c]
        f_fwd = FUND.iloc[i + 1][c]               # funding accrued during day t+1
        accr_cell = ACCR_PRIMARY.loc[t, c]        # = -funding.shift(-1)[t] = -f_fwd
        contrib = held * accr_cell
        print(f"date t            : {t.date()}  (hold t->t+1, funding from day "
              f"{FUND.index[i+1].date()})")
        print(f"coin              : {c}")
        print(f"held weight       : {held:+.4f}  (LONG, >0)")
        print(f"funding day t+1   : {f_fwd:+.6e}  (POSITIVE → a LONG PAYS)")
        print(f"accrual[t,c]      : {accr_cell:+.6e}  (= -funding.shift(-1) = "
              f"-{f_fwd:+.6e})")
        print(f"accrual P&L term  : held*accrual = {contrib:+.6e}  "
              f"→ {'NEGATIVE (long bleeds funding) ✓' if contrib < 0 else 'POSITIVE ✗'}")
        assert np.isclose(accr_cell, -f_fwd), "accrual must equal -funding.shift(-1)"
        assert contrib < 0, "a long in a high-funding coin must show NEGATIVE accrual"
        print("HAND-CHECK PASSED: long in high-funding coin → negative accrual.")
    else:
        print("(no long-in-positive-funding cell found — unexpected)")

    # ── Baseline anchor: no-funding ensemble Sharpe must reproduce ~1.34 ───────
    print("\n" + "-" * 80)
    print("BASELINE ANCHOR: no-funding ensemble Sharpe must reproduce the frozen ~1.34")
    print("-" * 80)
    _, ens_nofund, ens_fund = pnl_pair(scores["momentum_ensemble"])
    m_ens_nf = daily_metrics(ens_nofund)
    print(f"momentum_ensemble no-funding daily Sharpe = {m_ens_nf['sharpe']:.4f}  "
          f"(frozen book ≈ 1.34)")
    if abs(m_ens_nf["sharpe"] - 1.34) < 0.10:
        print("  → matches the frozen book within 0.10  ✓ (baseline wiring confirmed)")
    else:
        print(f"  ⚠ DEVIATION from 1.34 is {m_ens_nf['sharpe']-1.34:+.3f} — investigate wiring")

    # ── WITH vs WITHOUT funding, all books ─────────────────────────────────────
    print("\n" + "=" * 80)
    print("WITH vs WITHOUT FUNDING  (honest daily metrics, PRIMARY alignment)")
    print("=" * 80)
    drag_rows = []
    for name in ("momentum_ensemble", "mom30", "mom60", "blend"):
        _, p_nf, p_f = pnl_pair(scores[name])
        m_nf, m_f = daily_metrics(p_nf), daily_metrics(p_f)
        drag = m_nf["ann"] - m_f["ann"]
        drag_rows.append((name, m_nf, m_f, drag))
        print(f"\n{name}")
        print(f"  WITHOUT funding : {_fmt(m_nf)}")
        print(f"  WITH    funding : {_fmt(m_f)}")
        print(f"  funding drag    : {100*drag:+.2f}%/yr  "
              f"(Sharpe {m_nf['sharpe']:+.2f} → {m_f['sharpe']:+.2f})")

    # ── Ensemble deep-dive: half-split WITH funding ────────────────────────────
    print("\n" + "=" * 80)
    print("ENSEMBLE DEEP-DIVE (the C6 winner)")
    print("=" * 80)
    print("Half-split daily Sharpe (does fade + funding combine to kill it?):")
    _half_split(ens_nofund, "no-funding ")
    _half_split(ens_fund, "WITH-fund  ")

    # ── By-year funding drag (stable or regime-dependent?) ─────────────────────
    print("\nBy-year funding drag on the ensemble (ann% no-fund − ann% with-fund):")
    print(f"  {'year':<6}{'n':>5}{'ann_noF%':>11}{'ann_wF%':>11}{'drag%/yr':>11}"
          f"{'shrp_noF':>10}{'shrp_wF':>9}")
    ens_nf_d, ens_f_d = ens_nofund.dropna(), ens_fund.dropna()
    common = ens_nf_d.index.intersection(ens_f_d.index)
    for yr in sorted(set(common.year)):
        msk = common.year == yr
        idx_y = common[msk]
        m_nf_y = daily_metrics(ens_nf_d.loc[idx_y])
        m_f_y = daily_metrics(ens_f_d.loc[idx_y])
        if not m_nf_y or not m_f_y:
            print(f"  {yr:<6}{len(idx_y):>5}  (too few days)")
            continue
        dr = m_nf_y["ann"] - m_f_y["ann"]
        print(f"  {yr:<6}{m_nf_y['n']:>5}{100*m_nf_y['ann']:>11.2f}"
              f"{100*m_f_y['ann']:>11.2f}{100*dr:>11.2f}"
              f"{m_nf_y['sharpe']:>10.2f}{m_f_y['sharpe']:>9.2f}")

    # ── Alignment sensitivity (±1 day; must barely move) ───────────────────────
    print("\n" + "=" * 80)
    print("ALIGNMENT SENSITIVITY on the ensemble (funding is autocorrelated →")
    print("the verdict must NOT hinge on the ±1-day shift choice)")
    print("=" * 80)
    w_e = xsec.rank_to_weights(scores["momentum_ensemble"])
    print(f"  {'alignment':<28}{'Sharpe':>9}{'ann%':>9}{'drag%/yr':>11}")
    base_nf = daily_metrics(ens_nofund)
    for shift, lbl in [(-1, "-funding.shift(-1) PRIMARY"),
                       (0,  "-funding        (t-align)"),
                       (-2, "-funding.shift(-2) (+1day)")]:
        p_f = xsec.portfolio_returns(w_e, FWD, costs_bps=CB, rebal_every=REBAL,
                                     accrual=funding_accrual(shift))
        m_f = daily_metrics(p_f)
        dr = base_nf["ann"] - m_f["ann"]
        print(f"  {lbl:<28}{m_f['sharpe']:>9.3f}{100*m_f['ann']:>9.2f}{100*dr:>11.2f}")
    print("  → if the three rows are within ~0.05 Sharpe, the verdict is alignment-robust.")

    # ── VERDICT ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    m_nf, m_f = daily_metrics(ens_nofund), daily_metrics(ens_fund)
    s_nf, s_f = m_nf["sharpe"], m_f["sharpe"]
    drag = m_nf["ann"] - m_f["ann"]
    if s_f <= 0:
        tag = "COLLAPSE"
    elif s_f < 0.5 * s_nf:
        tag = "HALVED (or worse)"
    elif s_f < 0.85 * s_nf:
        tag = "DENTED but survives"
    else:
        tag = "SURVIVES"
    print(f"VERDICT [{tag}]: ensemble daily Sharpe {s_nf:.2f} (no funding) → "
          f"{s_f:.2f} (with funding); "
          f"funding drag {100*drag:+.2f}%/yr; "
          f"with-funding ann {100*m_f['ann']:+.2f}%, maxDD {100*m_f['maxdd']:.1f}%.")
    print("=" * 80)
