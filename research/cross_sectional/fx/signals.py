"""
Cross-sectional factor signals for the G10 FX long-short book (9 currencies vs
USD, XXXUSD orientation). Sister module to crypto/signals.py, same contract.

Each function consumes the aligned daily panel from fxdata.load_panel() (or the
relevant frame) and returns a SCORE panel pd.DataFrame[date x currency] where
HIGHER = more attractive to LONG, NaN where history is insufficient. Scores feed
xsec.rank_to_weights, which longs the top tercile / shorts the bottom tercile.

NO look-ahead: every signal at row t uses only data with index <= t. The signals
are STRUCTURALLY incapable of reading fwd_ret — they take only price / short_rate
/ usd_rate / reer, never the fwd_ret frame (which is the realised t->t+1 return,
the engine's job to align). Seam-safe: signals are built on the FULL panel so
lookback windows stay intact; where a currency lacks enough history the cell
stays NaN (never fabricated).

Horizon conventions (documented once, used throughout):
  - 1 month  ~= 21 business days   (MONTH = 21)
  - 1 year   ~= 252 business days  (YEAR  = 252)
Months/years passed to the momentum/value lookbacks are converted with these.
The panel is a regular business-day grid (fxdata FREQ="B"), so `.shift(n)` is an
exact n-business-day lag; pre-history NaN in the lagged frame propagates, so a
cell stays NaN until a full window of real data exists.

Only numpy/pandas.
"""

import numpy as np
import pandas as pd

from xsec import blend, zscore_cross_section

MONTH = 21   # business days per month
YEAR = 252   # business days per year


def carry(panel: dict, smooth_days: int = 1) -> pd.DataFrame:
    """Interest-rate-differential carry, score = higher → more attractive to LONG.

    score[t,c] = short_rate[t,c] - usd_rate[t]  (foreign minus USD 3M short rate,
    in % p.a.), using rates known at t (data <= t only → NO look-ahead).

    Sign convention (classic FX carry, the TEXTBOOK sign — DIFFERENT from crypto,
    where funding was NEGATED): a HIGHER foreign-minus-USD rate differential is
    DIRECTLY more attractive to be long. The book goes long the high-yielders and
    shorts the low-yielders, harvesting the rate carry (the empirical FX carry
    premium of Lustig/Verdelhan, "Value and Momentum Everywhere" carry leg).

    Rates are already ffilled to daily by fxdata; no smoothing is needed. The
    optional `smooth_days` (default 1 = no smoothing) applies a trailing mean of
    the differential over the last `smooth_days` days (min_periods=smooth_days) if
    one wants to damp month-end rate steps; the differential is computed first so
    the smoothing is on the same quantity that is scored.

    NaN where the foreign rate or the USD rate is NaN (pre-start), and (for
    smooth_days>1) the first `smooth_days-1` rows of each series.
    """
    short_rate = panel["short_rate"] if isinstance(panel, dict) else panel
    usd = panel["usd_rate"]["USD"]
    diff = short_rate.sub(usd, axis=0)
    if smooth_days and smooth_days > 1:
        diff = diff.rolling(smooth_days, min_periods=smooth_days).mean()
    return diff


def momentum(panel: dict, lookback_months: int = 12,
             skip_months: int = 1) -> pd.DataFrame:
    """Cross-sectional "12-1" price momentum, score = higher → more attractive.

    score[t,c] = price[t - skip] / price[t - lookback] - 1, where
        skip     = skip_months * MONTH      business days, and
        lookback = lookback_months * MONTH  business days,
    i.e. the trailing total return over the window that ENDS one `skip` (default
    one month) before t and starts `lookback` before t. Using price <= t only →
    NO look-ahead. The skip of the most recent month is the canonical 12-1 of
    cross-asset momentum (Asness/Moskowitz/Pedersen, "Value and Momentum
    Everywhere"; AQR), dropping the last month to avoid short-term reversal.

    HIGHER trailing return = LONG the winners / short the losers. Because the
    index is a regular business-day grid, the two shifts are exact business-day
    lags; the first `lookback_months*MONTH` listed rows of each currency are NaN
    (the deep lagged price is missing), and the binding NaN window is `lookback`
    (it is the older of the two bars).

    lookback_months / skip_months are params so F3 can probe a small set of
    trials; the default is the textbook 12-1.
    """
    price = panel["price"] if isinstance(panel, dict) else panel
    skip = skip_months * MONTH
    lookback = lookback_months * MONTH
    return price.shift(skip) / price.shift(lookback) - 1.0


def value(panel: dict, lookback_years: int = 5) -> pd.DataFrame:
    """Real-exchange-rate (REER) reversion value, score = higher → attractive.

    score[t,c] = -( log(reer[t,c]) - log(reer[t - lookback, c]) ),  with
        lookback = lookback_years * YEAR business days,
    i.e. the NEGATIVE of the ~5-year change in log REER, using reer <= t only →
    NO look-ahead.

    Sign / economic logic (AQR "Value Everywhere" currency definition): a currency
    whose REAL exchange rate FELL over the lookback (it got CHEAPER in real terms)
    gets a HIGH score → LONG, betting on mean reversion toward purchasing-power
    parity; one that APPRECIATED in real terms gets a low score → SHORT. The 5y
    change is the AMP/AQR currency-value measure, so this is the exact factor the
    F2 AQR cross-check compares against.

    Alternative formulation (NOT used): "deviation from a trailing long-run mean",
    score = -(log reer[t] - mean_{w}(log reer)), i.e. how far above its own rolling
    average a currency's real rate sits. We chose the fixed 5y-CHANGE version
    because (a) it is the published AMP "Value Everywhere" definition the F2 cross-
    check is built around, and (b) it needs only two REER observations (t and
    t-5y), making the no-look-ahead bar exact and hand-checkable, whereas the
    rolling-mean version mixes a whole window and its window length is a second
    free parameter. The two are highly correlated in practice.

    NaN for the first `lookback_years*YEAR` listed rows of each currency (the
    deep lagged REER is missing) and wherever either REER bar is NaN.
    """
    reer = panel["reer"] if isinstance(panel, dict) else panel
    lookback = lookback_years * YEAR
    log_reer = np.log(reer)
    return -(log_reer - log_reer.shift(lookback))


def blend_fx(panel: dict, weights=None) -> pd.DataFrame:
    """Equal-weight (default) blended multi-factor FX score, higher = LONG.

    Blends the cross-sectionally z-scored carry, 12-1 momentum, and 5y-REER value
    panels via xsec.blend (each leg z-scored FIRST so the factors are on a common
    unit scale before averaging; a missing factor is dropped from that cell's
    weighted average, not treated as zero). `weights` (default equal) lets F3 tilt.

    This is the multi-factor book whose thesis — that carry, momentum and value
    DIVERSIFY in FX (where they did not in crypto) — F3 will test.
    """
    legs = [
        zscore_cross_section(carry(panel)),
        zscore_cross_section(momentum(panel)),
        zscore_cross_section(value(panel)),
    ]
    return blend(legs, weights=weights)


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import fxdata

    P = fxdata.load_panel()
    price, reer = P["price"], P["reer"]
    short_rate, usd_rate = P["short_rate"], P["usd_rate"]
    usd_s = usd_rate["USD"]
    ccys = P["currencies"]
    print(f"\n=== PANEL (G10 vs USD) ===  {price.shape[0]} days x {price.shape[1]} "
          f"ccy  ({price.index.min().date()} -> {price.index.max().date()})")

    car = carry(P)
    mom = momentum(P)                 # 12-1 default
    val = value(P)                    # 5y default
    LB_MOM = 12 * MONTH               # 252
    SKIP = 1 * MONTH                  # 21
    LB_VAL = 5 * YEAR                 # 1260

    # ── No-look-ahead hand-checks ─────────────────────────────────────────────
    c = "EUR"
    # (a) momentum: hand-recompute one cell == price[t-skip]/price[t-lookback]-1.
    t = price[c].dropna().index[-3]
    i = price.index.get_loc(t)
    t_skip = price.index[i - SKIP]
    t_lb = price.index[i - LB_MOM]
    man_mom = price.loc[t_skip, c] / price.loc[t_lb, c] - 1.0
    assert np.isclose(mom.loc[t, c], man_mom), \
        f"momentum mismatch {mom.loc[t, c]} != {man_mom}"
    # lag bars are exactly SKIP / LB_MOM business days back.
    assert i - price.index.get_loc(t_skip) == SKIP, "skip bar not SKIP bdays back"
    assert i - price.index.get_loc(t_lb) == LB_MOM, "lookback bar not LB_MOM back"
    print(f"\n[no-look-ahead] {c} momentum(12-1) @ {t.date()} = {mom.loc[t, c]:+.4f}"
          f"  == price[t-{SKIP}]/price[t-{LB_MOM}]-1 ({man_mom:+.4f})  OK")

    # (b) value: hand-recompute one cell == -(log reer[t] - log reer[t-LB_VAL]).
    tv = reer[c].dropna().index[-3]
    iv = reer.index.get_loc(tv)
    tv_lb = reer.index[iv - LB_VAL]
    man_val = -(np.log(reer.loc[tv, c]) - np.log(reer.loc[tv_lb, c]))
    assert np.isclose(val.loc[tv, c], man_val), \
        f"value mismatch {val.loc[tv, c]} != {man_val}"
    assert iv - reer.index.get_loc(tv_lb) == LB_VAL, "value bar not LB_VAL bdays back"
    print(f"[no-look-ahead] {c} value(5y) @ {tv.date()} = {val.loc[tv, c]:+.4f}"
          f"  == -(log reer[t] - log reer[t-{LB_VAL}]) ({man_val:+.4f})  OK")

    # (c) carry: hand-recompute one cell == short_rate[t] - usd_rate[t].
    tc = short_rate[c].dropna().index[-3]
    man_car = short_rate.loc[tc, c] - usd_s.loc[tc]
    assert np.isclose(car.loc[tc, c], man_car), "carry == foreign - USD rate"
    print(f"[no-look-ahead] {c} carry @ {tc.date()} = {car.loc[tc, c]:+.4f}"
          f"  == short_rate - usd_rate ({man_car:+.4f})  OK")

    # (d) first `lookback` listed rows are NaN; the row AT lookback is defined.
    listed = price[c].dropna().index
    assert mom.loc[listed[:LB_MOM], c].isna().all(), "first LB_MOM mom rows must be NaN"
    assert not np.isnan(mom.loc[listed[LB_MOM], c]), "mom row LB_MOM should be defined"
    reer_listed = reer[c].dropna().index
    assert val.loc[reer_listed[:LB_VAL], c].isna().all(), "first LB_VAL val rows NaN"
    assert not np.isnan(val.loc[reer_listed[LB_VAL], c]), "val row LB_VAL defined"
    print(f"[no-look-ahead] first {LB_MOM} mom rows & first {LB_VAL} val rows NaN, "
          f"row==lookback defined  OK")

    # (e) structurally cannot read fwd_ret: functions take price/short_rate/
    #     usd_rate/reer only; assert fwd_ret is still in the panel untouched.
    assert "fwd_ret" in P and mom.shape == price.shape == val.shape == car.shape
    print("[no-look-ahead] signals read price/short_rate/usd_rate/reer only; "
          "fwd_ret untouched  OK")

    # ── Sign sanity: top-5 / bottom-5 on the latest fully-defined date ────────
    # use the latest date where ALL three factors are defined for all ccys.
    defined = car.notna() & mom.notna() & val.notna()
    d = defined.index[defined.all(axis=1)][-1]
    print(f"\n=== SIGN SANITY @ {d.date()} (latest all-factor-defined date) ===")

    # carry: top = high-yielders (AUD/NZD/NOK), bottom = low-yielders (JPY/CHF).
    crow = car.loc[d].dropna().sort_values(ascending=False)
    diff_chk = (short_rate.loc[d] - usd_s.loc[d])
    print(f"\ncarry  (foreign - USD 3M rate, % p.a.)  HIGH=LONG:")
    print(f"  top-5 (LONG, high-yield):  "
          + "  ".join(f"{cc}{crow[cc]:+.2f}" for cc in crow.index[:5]))
    print(f"  bot-5 (SHORT, low-yield):  "
          + "  ".join(f"{cc}{crow[cc]:+.2f}" for cc in crow.index[-5:]))
    assert diff_chk[crow.idxmax()] > diff_chk[crow.idxmin()], \
        "carry argmax must have a higher (foreign-USD) rate than argmin"
    print(f"  [assert] argmax {crow.idxmax()} diff {diff_chk[crow.idxmax()]:+.2f} > "
          f"argmin {crow.idxmin()} diff {diff_chk[crow.idxmin()]:+.2f}  OK")

    # momentum: top = rose most vs USD over the 12-1 window; hand-verify the top.
    mrow = mom.loc[d].dropna().sort_values(ascending=False)
    i_d = price.index.get_loc(d)
    d_skip = price.index[i_d - SKIP]
    d_lb = price.index[i_d - LB_MOM]
    print(f"\nmomentum(12-1)  (price[t-1m]/price[t-12m]-1)  HIGH=LONG:")
    print(f"  {'ccy':<5}{'score':>9}{'p[t-12m]':>11}{'p[t-1m]':>11}{'chk':>9}")
    for cc in mrow.index:
        chk = price.loc[d_skip, cc] / price.loc[d_lb, cc] - 1.0
        mark = " <-top" if cc == mrow.index[0] else (" <-bot" if cc == mrow.index[-1] else "")
        print(f"  {cc:<5}{mrow[cc]:>+9.4f}{price.loc[d_lb, cc]:>11.5f}"
              f"{price.loc[d_skip, cc]:>11.5f}{chk:>+9.4f}{mark}")
        assert np.isclose(mrow[cc], chk), f"momentum hand-check {cc}"
    print(f"  [verify] top {mrow.index[0]} rose most over 12-1, bot "
          f"{mrow.index[-1]} fell most  OK")

    # value: top = REER fell most over ~5y; hand-verify the top.
    vrow = val.loc[d].dropna().sort_values(ascending=False)
    iv_d = reer.index.get_loc(d)
    dv_lb = reer.index[iv_d - LB_VAL]
    print(f"\nvalue(5y)  (-Δlog REER over 5y; cheaper-in-real-terms=LONG)  HIGH=LONG:")
    print(f"  {'ccy':<5}{'score':>9}{'reer[t-5y]':>12}{'reer[t]':>11}{'chk':>9}")
    for cc in vrow.index:
        chk = -(np.log(reer.loc[d, cc]) - np.log(reer.loc[dv_lb, cc]))
        mark = " <-top" if cc == vrow.index[0] else (" <-bot" if cc == vrow.index[-1] else "")
        print(f"  {cc:<5}{vrow[cc]:>+9.4f}{reer.loc[dv_lb, cc]:>12.2f}"
              f"{reer.loc[d, cc]:>11.2f}{chk:>+9.4f}{mark}")
        assert np.isclose(vrow[cc], chk), f"value hand-check {cc}"
    top_v = vrow.index[0]
    assert reer.loc[d, top_v] < reer.loc[dv_lb, top_v], \
        f"value top {top_v}: REER should have FALLEN over 5y"
    print(f"  [verify] top {top_v} REER fell {reer.loc[dv_lb, top_v]:.1f}->"
          f"{reer.loc[d, top_v]:.1f} over 5y (cheaper → LONG)  OK")

    # ── Z-score check: each valid row of z(carry) has mean≈0, std≈1 ───────────
    zc = zscore_cross_section(car)
    valid_rows = car.notna().sum(axis=1) >= 2
    rmean = zc.mean(axis=1)[valid_rows].dropna()
    rstd = zc.std(axis=1, ddof=0)[valid_rows].dropna()
    assert np.allclose(rmean, 0.0, atol=1e-9), "z(carry) row mean != 0"
    assert (rstd - 1.0).abs().max() < 1e-9, "z(carry) row std != 1"
    print(f"\n=== Z-SCORE (carry) ===  rows {len(rmean)}  max|mean|="
          f"{rmean.abs().max():.2e}  max|std-1|={(rstd - 1.0).abs().max():.2e}  OK")

    # ── Blend + shapes & NaN% ─────────────────────────────────────────────────
    bl = blend_fx(P)
    brow = bl.loc[d].dropna().sort_values(ascending=False)
    print(f"\n=== BLEND_FX (equal-weight z-carry + z-mom + z-value) @ {d.date()} ===")
    print(f"  top-5 (LONG):  " + "  ".join(f"{cc}{brow[cc]:+.2f}" for cc in brow.index[:5]))
    print(f"  bot-5 (SHORT): " + "  ".join(f"{cc}{brow[cc]:+.2f}" for cc in brow.index[-5:]))

    def nanpct(df):
        return 100.0 * df.isna().mean().mean()
    print(f"\n=== SHAPES & NaN% ===")
    for name, df in [("carry", car), ("momentum(12-1)", mom), ("value(5y)", val),
                     ("z(carry)", zc), ("blend_fx", bl)]:
        print(f"  {name:<16} shape={str(df.shape):<13} NaN%={nanpct(df):5.1f}")

    print("\nALL ASSERTS PASSED")
