"""Trend: can the drawdown be cut without giving up more return than the drawdown gains?

PRE-REGISTERED 2026-09-14, before any result of this script was seen.

The committed book at deployable size makes ~23%/yr with a 35% drawdown on HL-listed coins. The question
from the user: they would take ~15%/yr if the drawdown came down with it. Halving the size halves both,
so the honest question is about SHAPE, not size: does any risk knob lower the drawdown MORE than it
lowers the return? Everything below is therefore also reported rescaled to a common 15% annual return,
where the only thing left to compare is the drawdown.

Variants (all on the committed signal — 30/60/90/120 TSMOM ensemble, HL-listed coins, 4.4 bps):
  BASE       what runs in the paper test: per-asset vol target 2%/day, gross cap 3x, size 0.2
  SCALE65    the same book at 0.65 of the size — the reference point for "make it 15%"
  BOOKVOL    on top of BASE, scale the whole book daily so its own trailing 60-day volatility targets
             20%/yr (scale clipped to 0.25..2, computed on days strictly before t)
  NETCAP     cap the book's net exposure at 25% of gross: the dominant side is scaled down until the
             net fits. Removes most of the crypto-beta the ensemble takes in a one-way market
  DDTHROTTLE halve the book once it is 15% below its equity peak, restore when back within 5% of it
  MAJORS     BASE on the eight largest HL coins only (BTC ETH SOL BNB XRP DOGE LINK AVAX)
  SLOW       BASE with lookbacks 60/90/120/180 instead of 30/60/90/120

Windows: NEW 2020-01 .. 2023-06 (never seen by the committed config), OLD 2023-06 .. 2026-06, FULL.
No variant is "selected": the output is a table of return/drawdown pairs to choose a point from, and the
selection question ("is this knob real or fitted?") is answered by whether a variant helps on BOTH
windows, not just one.

AMENDMENT 2026-09-14, after the first run: BOOKVOL won, and the user asked whether picking weakly
correlated coins would calm the book further. Three more variants, all on top of BOOKVOL, judged the
same way (both windows, drawdown at a matched 15%/yr):
  LOWCORR12 / LOWCORR8  every day keep only the 12 (8) coins whose average 60-day correlation to the
                        rest of the eligible set is lowest, among those the signal wants to trade
  DECORR                keep everything, but weight each position by 1 / (1 + average correlation to
                        the rest), renormalised to the same gross
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
for p in ("cross_sectional", "xsmom_v2", "validation_harness"):
    sys.path.insert(0, str(HERE.parent / p))

import engine as X                                    # noqa: E402
import long_history as L                              # noqa: E402
from trend import realized_vol, tsmom_ensemble, portfolio_returns_directional  # noqa: E402

OUT = HERE / "risk_shaping.json"
MAJORS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "LINK", "AVAX"]
TARGET_RETURN = 0.15


def scaled_positions(panel, elig, lookbacks=L.TSMOM_LOOKBACKS):
    """Positions after the per-asset vol target and the gross cap — what BASE actually holds."""
    vol = realized_vol(panel["price"], L.VOL_WINDOW)
    sig = tsmom_ensemble(panel, lookbacks=lookbacks, vol_window=L.VOL_WINDOW).where(elig, 0.0)
    pos = (sig * (L.VOL_TARGET / vol)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    gross = pos.abs().sum(axis=1)
    return pos.div(np.maximum(gross / L.LEVERAGE_CAP, 1.0), axis=0)


def net_capped(pos: pd.DataFrame, cap: float = 0.25) -> pd.DataFrame:
    """Scale the dominant side down until |net| <= cap * gross."""
    out = pos.copy()
    longs, shorts = pos.clip(lower=0), pos.clip(upper=0)
    lg, sh = longs.sum(axis=1), -shorts.sum(axis=1)
    gross = lg + sh
    allowed = cap * gross
    over_long = (lg - sh) > allowed
    over_short = (sh - lg) > allowed
    # solve for the factor f on the dominant side that makes the net exactly the cap
    f_long = ((allowed + sh) / lg).where(lg > 0, 1.0).clip(upper=1.0)
    f_short = ((allowed + lg) / sh).where(sh > 0, 1.0).clip(upper=1.0)
    out[pos > 0] = (longs.mul(f_long.where(over_long, 1.0), axis=0))[pos > 0]
    out[pos < 0] = (shorts.mul(f_short.where(over_short, 1.0), axis=0))[pos < 0]
    return out


def corr_matrix_scaler(panel, window: int = 60):
    """Per-coin average correlation to the rest of the book, causal (uses returns strictly before t)."""
    rets = panel["price"].pct_change()
    return rets.rolling(window).corr().groupby(level=0).mean().shift(1)


def low_corr_subset(pos: pd.DataFrame, avg_corr: pd.DataFrame, k: int) -> pd.DataFrame:
    """Keep the k least-correlated coins among those the signal wants."""
    out = pd.DataFrame(0.0, index=pos.index, columns=pos.columns)
    for t in pos.index:
        row, c = pos.loc[t], avg_corr.loc[t] if t in avg_corr.index else None
        wanted = row[row != 0]
        if c is None or wanted.empty:
            out.loc[t] = row
            continue
        ranked = c.reindex(wanted.index).dropna().nsmallest(k).index
        keep = ranked if len(ranked) else wanted.index
        out.loc[t, keep] = row[keep]
    gross = out.abs().sum(axis=1)
    return out.div(np.maximum(gross / L.LEVERAGE_CAP, 1.0), axis=0)


def decorr_weighted(pos: pd.DataFrame, avg_corr: pd.DataFrame) -> pd.DataFrame:
    """Weight each position by 1 / (1 + its average correlation to the rest), same gross."""
    w = 1.0 / (1.0 + avg_corr.reindex_like(pos).clip(lower=-0.9))
    out = pos * w.fillna(1.0)
    gross_before, gross_after = pos.abs().sum(axis=1), out.abs().sum(axis=1)
    return out.mul((gross_before / gross_after.replace(0, np.nan)).fillna(1.0), axis=0)


def book_vol_scaled(pnl: pd.Series, target_ann: float = 0.20, window: int = 60) -> pd.Series:
    """Scale the book by its own trailing volatility (causal: the scale for day t uses days < t)."""
    vol = pnl.rolling(window).std().shift(1) * np.sqrt(365)
    scale = (target_ann / vol).clip(0.25, 2.0).fillna(1.0)
    return pnl * scale


def dd_throttle(pnl: pd.Series, cut_at: float = 0.15, restore_at: float = 0.05, factor: float = 0.5) -> pd.Series:
    """Halve the book below a 15% drawdown, restore within 5% of the peak."""
    out = np.empty(len(pnl))
    eq, peak, on = 1.0, 1.0, False
    for i, r in enumerate(pnl.to_numpy()):
        out[i] = r * (factor if on else 1.0)
        eq *= 1 + out[i]
        peak = max(peak, eq)
        dd = 1 - eq / peak
        on = True if dd >= cut_at else (False if dd <= restore_at else on)
    return pd.Series(out, index=pnl.index)


def stats_at(pnl: pd.Series, target_return: float | None = None) -> dict:
    """Stats as-is, or after linearly rescaling the series to `target_return` a year."""
    s = pnl
    if target_return is not None:
        years = len(s) / 365
        ann = (1 + s).prod() ** (1 / years) - 1
        if ann <= 0:
            return dict(sharpe=float("nan"), ann=float("nan"), maxdd=float("nan"), calmar=float("nan"), scale=float("nan"))
        k = 1.0
        for _ in range(60):                       # compounding is not linear in the scale: solve for it
            trial = (1 + s * k).prod() ** (1 / years) - 1
            k *= (target_return / trial) ** 0.5 if trial > 0 else 0.5
        s = pnl * k
    st = L.stats(s)
    st["calmar"] = st["ann"] / st["maxdd"] if st["maxdd"] > 0 else float("nan")
    st["scale"] = float((s.std() / pnl.std()) if pnl.std() else 1.0)
    return st


def main():
    p = X.build_panel()
    panel, accr, elig = L.to_trend_panel(p, hl_only=True)
    keep = [c for c in panel["coins"] if c in MAJORS]
    pos = scaled_positions(panel, elig)
    fwd = panel["fwd_ret"]

    def book(positions, **kw):
        return portfolio_returns_directional(positions, fwd, L.BPS_LOW, accrual=accr, **kw)

    base = L.rescale(book(pos))                                    # deployable size (30% vol)
    variants = {
        "BASE": base,
        "SCALE65": base * 0.65,
        "BOOKVOL": book_vol_scaled(base),
        "NETCAP": L.rescale(book(net_capped(pos))),
        "DDTHROTTLE": dd_throttle(base),
        "MAJORS": L.rescale(book(pos[keep])),
        "SLOW": L.rescale(book(scaled_positions(panel, elig, lookbacks=(60, 90, 120, 180)))),
    }
    avg_corr = corr_matrix_scaler(panel)
    variants["LOWCORR12"] = book_vol_scaled(L.rescale(book(low_corr_subset(pos, avg_corr, 12))))
    variants["LOWCORR8"] = book_vol_scaled(L.rescale(book(low_corr_subset(pos, avg_corr, 8))))
    variants["DECORR"] = book_vol_scaled(L.rescale(book(decorr_weighted(pos, avg_corr))))

    res = {"windows": {}, "at_15pct": {}}
    for wname, (lo, hi) in {"NEW": L.NEW, "OLD": L.OLD, "FULL": (L.NEW[0], L.FRESH[1])}.items():
        res["windows"][wname] = {}
        print(f"\n== {wname} {lo} .. {hi}")
        for name, s in variants.items():
            w = L.window(s, lo, hi)
            st = stats_at(w)
            res["windows"][wname][name] = st
            print(f"  {name:<11} {st['ann']:+6.1f}%/yr  vol {w.std() * np.sqrt(365) * 100:5.1f}%  "
                  f"DD {st['maxdd']:5.1f}  Calmar {st['calmar']:5.2f}  Sharpe {st['sharpe']:+.2f}")

    print(f"\n== every variant rescaled to {TARGET_RETURN:.0%}/yr — what is left to compare is the drawdown")
    for wname, (lo, hi) in {"NEW": L.NEW, "OLD": L.OLD, "FULL": (L.NEW[0], L.FRESH[1])}.items():
        res["at_15pct"][wname] = {}
        print(f"  {wname}:")
        for name, s in variants.items():
            st = stats_at(L.window(s, lo, hi), TARGET_RETURN)
            res["at_15pct"][wname][name] = st
            print(f"    {name:<11} DD {st['maxdd']:5.1f}%  (size {st['scale']:.2f}x of the 30%-vol book)")
    OUT.write_text(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main()
