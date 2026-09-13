"""XSMOM v2 engine: point-in-time Binance perp panel + vectorised cross-sectional books.

Conventions (identical to research/cross_sectional/xsec.py, verified bit-exact in selftest()):
  * weights decided at day t earn fwd[t] = close[t+1]/close[t] - 1;
  * held weights stay constant between rebalances (no intra-period drift);
  * cost at a rebalance = sum|w_new - w_held| * bps/1e4;
  * funding accrual: held[t] * accr[t], accr[t] = -funding summed over day t+1 (a long pays positive funding).
Book scale: long leg +0.5, short leg -0.5 of capital (prod XSMOM: each side = half the book, leverage 1).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RAW = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/xsmom_binance")
CACHE = RAW / "panel.npz"          # default universe; other filters get their own file
START, END = pd.Timestamp("2020-01-01", tz="UTC"), pd.Timestamp("2026-09-12", tz="UTC")
MIN_DAYS, TOP_N = 90, 40


# ── panel ────────────────────────────────────────────────────────────────────
@dataclass
class Panel:
    dates: pd.DatetimeIndex
    syms: list[str]
    close: np.ndarray      # T x N, NaN where not tradable
    fwd: np.ndarray        # T x N, NaN -> 0 when used
    accr: np.ndarray       # T x N, per-unit long cash-flow for holding t -> t+1
    fund: np.ndarray       # T x N, daily summed funding (known at end of day t)
    elig: np.ndarray       # T x N bool, point-in-time universe
    ret1: np.ndarray       # T x N daily return close[t]/close[t-1]-1
    vol30: np.ndarray      # T x N trailing 30d std of daily returns
    btc: int


def build_panel(top_n: int = TOP_N, min_days: int = MIN_DAYS, refresh: bool = False) -> Panel:
    cache = CACHE if (top_n, min_days) == (TOP_N, MIN_DAYS) else RAW / f"panel_top{top_n}_min{min_days}.npz"
    if cache.exists() and not refresh:
        z = np.load(cache, allow_pickle=True)
        return Panel(pd.DatetimeIndex(z["dates"]).tz_localize("UTC") if pd.DatetimeIndex(z["dates"]).tz is None
                     else pd.DatetimeIndex(z["dates"]), list(z["syms"]), z["close"], z["fwd"], z["accr"], z["fund"],
                     z["elig"], z["ret1"], z["vol30"], int(z["btc"]))
    dates = pd.date_range(START, END, freq="D")
    closes, vols, funds = {}, {}, {}
    for f in sorted((RAW / "klines").glob("*.csv")):
        if f.stat().st_size < 40:
            continue
        k = pd.read_csv(f)
        idx = pd.to_datetime(k["open_ms"], unit="ms", utc=True).dt.floor("D")
        c = pd.Series(k["close"].values, index=idx)
        v = pd.Series(k["quote_volume"].values, index=idx)
        c = c[~c.index.duplicated()]; v = v[~v.index.duplicated()]
        c = c.where(v > 0)                                   # no trading that day -> not tradable
        closes[f.stem] = c.reindex(dates); vols[f.stem] = v.reindex(dates)
        ff = RAW / "funding" / f"{f.stem}.csv"
        if ff.exists() and ff.stat().st_size > 30:
            fr = pd.read_csv(ff)
            t = pd.to_datetime(fr["fundingTime"], unit="ms", utc=True).dt.floor("D")
            funds[f.stem] = fr.groupby(t)["fundingRate"].sum().reindex(dates)
    syms = sorted(closes)
    C = pd.DataFrame(closes)[syms]; V = pd.DataFrame(vols)[syms]
    F = pd.DataFrame({s: funds.get(s, pd.Series(np.nan, index=dates)) for s in syms})[syms]
    listed_days = C.notna().cumsum()
    medvol = V.rolling(30, min_periods=20).median().where(listed_days >= min_days)
    rank = medvol.rank(axis=1, ascending=False)
    elig = (rank <= top_n) & C.notna()
    fwd = C.shift(-1) / C - 1.0
    F0 = F.fillna(0.0)
    accr = -F0.shift(-1).fillna(0.0)
    ret1 = C / C.shift(1) - 1.0
    vol30 = ret1.rolling(30, min_periods=20).std()
    p = Panel(dates, syms, C.values, fwd.values, accr.values, F0.values, elig.values, ret1.values, vol30.values,
              syms.index("BTCUSDT"))
    np.savez_compressed(cache, dates=dates.tz_convert(None).values, syms=np.array(syms), close=p.close, fwd=p.fwd,
                        accr=p.accr, fund=p.fund, elig=p.elig, ret1=p.ret1, vol30=p.vol30, btc=p.btc)
    return p


# ── signals (all use data <= t) ──────────────────────────────────────────────
def zscore_rows(x: np.ndarray, mask: np.ndarray) -> np.ndarray:
    x = np.where(mask & np.isfinite(x), x, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        m = np.nanmean(x, axis=1, keepdims=True)
        s = np.nanstd(x, axis=1, keepdims=True)
        z = (x - m) / np.where(s > 0, s, np.nan)
    return z


def momentum(p: Panel, L: int, skip: int) -> np.ndarray:
    c = p.close
    out = np.full_like(c, np.nan)
    out[L:] = c[L - skip: c.shape[0] - skip] / c[:-L] - 1.0 if skip else c[L:] / c[:-L] - 1.0
    return out


_resid_cache: dict = {}


def residual_returns(p: Panel, beta_window: int = 60) -> np.ndarray:
    """Daily return minus beta * BTC return; beta from the trailing window ending the day BEFORE."""
    if "e" in _resid_cache:
        return _resid_cache["e"]
    r = pd.DataFrame(p.ret1)
    rb = r[p.btc]
    cov = r.rolling(beta_window, min_periods=40).cov(rb)
    var = rb.rolling(beta_window, min_periods=40).var()
    beta = cov.div(var, axis=0).shift(1)
    e = (r - beta.mul(rb, axis=0)).values
    _resid_cache["e"] = e
    return e


def residual_momentum(p: Panel, L: int, skip: int) -> np.ndarray:
    e = pd.DataFrame(np.nan_to_num(residual_returns(p), nan=0.0))
    valid = pd.DataFrame(np.isfinite(residual_returns(p)).astype(float))
    s = e.rolling(L - skip, min_periods=1).sum().shift(skip)
    n = valid.rolling(L - skip, min_periods=1).sum().shift(skip)
    out = s.where(n >= 0.8 * (L - skip)).values
    return out


LOOKBACKS = {"S": (7, 14, 21), "M": (14, 21, 30, 45, 60), "L": (30, 60, 90), "XL": (60, 90, 120, 180)}


def score(p: Panel, lb: str, skip: int, resid: bool, funding_penalty: bool) -> np.ndarray:
    legs = []
    for L in LOOKBACKS[lb]:
        if L <= skip:                                     # skipping the whole window leaves nothing to measure
            continue
        raw = residual_momentum(p, L, skip) if resid else momentum(p, L, skip)
        legs.append(zscore_rows(raw, p.elig))
    arr = np.stack(legs)
    ens = np.where(np.isfinite(arr).all(axis=0), np.nanmean(arr, axis=0), np.nan)
    if funding_penalty:
        f7 = pd.DataFrame(np.where(np.isfinite(p.close), p.fund, np.nan)).rolling(7, min_periods=5).mean().values
        ens = ens - zscore_rows(f7, p.elig)
    return np.where(p.elig, ens, np.nan)


# ── weights ──────────────────────────────────────────────────────────────────
def weights(p: Panel, sc: np.ndarray, k: int, invvol: bool, structure: str) -> np.ndarray:
    T, N = sc.shape
    W = np.zeros((T, N))
    filled = np.where(np.isfinite(sc), sc, -np.inf)
    order = np.argsort(-filled, axis=1, kind="stable")
    nvalid = np.isfinite(sc).sum(axis=1)
    iv = 1.0 / np.where(p.vol30 > 0, p.vol30, np.nan)
    for t in range(T):
        n = nvalid[t]
        if n < 2 * k:
            continue
        longs, shorts = order[t, :k], order[t, n - k:n]
        for leg, sign in ((longs, 1.0), (shorts, -1.0)):
            if structure == "long_btc" and sign < 0 or structure == "short_btc" and sign > 0:
                continue
            if invvol:
                w = iv[t, leg]
                w = np.where(np.isfinite(w), w, np.nanmean(w) if np.isfinite(w).any() else 1.0)
                w = w / w.sum()
            else:
                w = np.full(len(leg), 1.0 / len(leg))
            W[t, leg] += sign * 0.5 * w
        if structure == "long_btc":
            W[t, p.btc] -= 0.5
        elif structure == "short_btc":
            W[t, p.btc] += 0.5
    return W


# ── book ─────────────────────────────────────────────────────────────────────
def book(p: Panel, W: np.ndarray, rebal: np.ndarray, bps: float) -> np.ndarray:
    """Daily net pnl of a book rebalanced on the days where rebal is True."""
    T = W.shape[0]
    last = np.maximum.accumulate(np.where(rebal, np.arange(T), -1))
    held = np.where(last[:, None] >= 0, W[np.maximum(last, 0)], 0.0)
    prev = np.vstack([np.zeros((1, W.shape[1])), held[:-1]])
    cost = np.where(rebal, np.abs(held - prev).sum(axis=1), 0.0) * bps / 1e4
    fwd = np.nan_to_num(p.fwd, nan=0.0)
    return (held * fwd).sum(axis=1) + (held * p.accr).sum(axis=1) - cost


def weekday_mask(p: Panel, wd: int) -> np.ndarray:
    return (p.dates.weekday == wd)


def tranched(p: Panel, W: np.ndarray, bps: float) -> np.ndarray:
    return np.mean([book(p, W, weekday_mask(p, wd), bps) for wd in range(7)], axis=0)


def vol_target(pnl: np.ndarray, bps: float, target_ann: float = 0.20, window: int = 60, cap: float = 2.0) -> np.ndarray:
    s = pd.Series(pnl).rolling(window, min_periods=40).std().shift(1).values * np.sqrt(365)
    scale = np.clip(np.where(np.isfinite(s) & (s > 0), target_ann / s, 1.0), 0.0, cap)
    change = np.abs(np.diff(np.concatenate([[scale[0]], scale])))
    return scale * pnl - change * bps / 1e4


def dispersion_gate(p: Panel, pnl: np.ndarray, bps: float) -> np.ndarray:
    m30 = np.where(p.elig, momentum(p, 30, 0), np.nan)
    disp = pd.Series(np.nanstd(m30, axis=1))
    med = disp.rolling(365, min_periods=180).median()
    g = (disp > med).astype(float).where(med.notna(), 1.0).values
    g = np.concatenate([[1.0], g[:-1]])                   # decided at t-1, applied to t
    change = np.abs(np.diff(np.concatenate([[g[0]], g])))
    return g * pnl - change * bps / 1e4


# ── metrics ──────────────────────────────────────────────────────────────────
def stats(pnl: np.ndarray) -> dict:
    pnl = np.asarray(pnl, float)
    sd = pnl.std(ddof=1)
    eq = np.cumprod(1 + pnl)
    years = len(pnl) / 365
    return dict(sharpe=float(pnl.mean() / sd * np.sqrt(365)) if sd > 0 else 0.0,
                ann=float((eq[-1] ** (1 / years) - 1) * 100) if eq[-1] > 0 else -100.0,
                total=float((eq[-1] - 1) * 100),
                maxdd=float(-(eq / np.maximum.accumulate(eq) - 1).min() * 100))


# ── selftest ─────────────────────────────────────────────────────────────────
def selftest(p: Panel) -> None:
    sys.path.insert(0, str(ROOT / "research" / "cross_sectional"))
    import xsec
    sc = score(p, "M", 0, False, False)
    W = weights(p, sc, 8, False, "ls")
    wd0 = int(p.dates[0].weekday())
    mine = book(p, W, weekday_mask(p, wd0), 4.4)
    Wdf = pd.DataFrame(W, index=p.dates)
    ref = xsec.portfolio_returns(Wdf, pd.DataFrame(np.nan_to_num(p.fwd, nan=0.0), index=p.dates), costs_bps=4.4,
                                 rebal_every=7, accrual=pd.DataFrame(p.accr, index=p.dates)).values
    err = np.abs(mine - ref).max()
    assert err < 1e-12, f"engine != xsec.portfolio_returns (max err {err})"
    assert np.allclose(np.abs(W).sum(axis=1)[np.abs(W).sum(axis=1) > 0], 1.0), "book gross must be 1"
    print(f"selftest: engine == xsec.portfolio_returns (max err {err:.1e}); gross 1; OK")
