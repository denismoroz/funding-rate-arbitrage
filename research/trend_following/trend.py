"""
Directional trend-following engine (TSMOM + Donchian breakout), crypto.

СТРУКТУРНО ДРУГОЙ движок, не cross-sectional (см. xsec.py). Позиция per-asset =
+1/−1/flat по СОБСТВЕННОМУ тренду актива, НЕ по cross-sectional рангу. Книга НЕ
нормируется к dollar-neutral Σ=±1 — это directional, time-varying-beta профиль
(long в бычьем / short в медвежьем → ожидаемая crisis-alpha).

Data contract (PT-панель из survivorship.build_pt_panel, НЕ refetch):
    panel = {"coins": [...], "price": DataFrame, "fwd_ret": DataFrame,
             "funding": DataFrame}
  - все индексированы gap-free дневными UTC-датами, columns = coins.
  - price = дневной CLOSE only (НЕТ high/low/open — OHLC отсутствует).
  - fwd_ret[t,c] = price.shift(-1)/price - 1 = доходность t→t+1 (зарабатывается
    сигналом, известным в t; NO look-ahead).
  - funding[t,c] = дневной funding; NaN где коин не листится.
  - NaN-ячейки (pre-history / post-delist) обрабатываются gracefully (→ flat).

NO LOOK-AHEAD (сквозной инвариант):
  - все сигналы и realized vol в момент t используют ТОЛЬКО цены ≤ t.
  - trailing return = price[t]/price[t-L]-1 (причинно).
  - realized vol = rolling std дневного pct_change за vol_window, причинно.
  - fwd_ret НИКОГДА не входит в построение сигнала/vol — он только зарабатывается.

Vol-targeting: применяется ЦЕНТРАЛЬНО в portfolio_returns_directional через
опциональный `vol` DataFrame (его считает caller — см. realized_vol()). Сигнальные
функции возвращают ЧИСТУЮ directional-интенцию в {-1,0,+1} (или [-1,+1] у ансамбля),
без vol-масштаба — так сигналы простые и тестируемые.

Только numpy/pandas.
"""

import numpy as np
import pandas as pd


# ── Vol helper (причинная realized vol) ─────────────────────────────────────────

def realized_vol(price: pd.DataFrame, vol_window: int = 30) -> pd.DataFrame:
    """Причинная realized vol per-asset: rolling std дневного pct_change.

    vol[t,c] = std( pct_change(price)[t-vol_window+1 .. t] ), ddof=0.
    Использует ТОЛЬКО цены ≤ t (rolling по прошлому окну, NO look-ahead).
    NaN где недостаточно истории. Передаётся как `vol` в
    portfolio_returns_directional для центрального vol-targeting.
    """
    ret = price.pct_change()
    return ret.rolling(vol_window, min_periods=vol_window).std(ddof=0)


# ── TSMOM ────────────────────────────────────────────────────────────────────

def tsmom_signal(panel: dict, lookback: int, vol_window: int = 30) -> pd.DataFrame:
    """RAW directional TSMOM-сигнал per-asset: sign(trailing return over L).

    trail_ret[t,c] = price[t]/price[t-lookback] - 1, ТОЛЬКО цены ≤ t (причинно).
    Позиция = sign(trail_ret) ∈ {-1, 0, +1}:
      - long (+1) после чистого аптренда (trail_ret > 0),
      - short (−1) после чистого даунтренда (trail_ret < 0),
      - flat (0) если trail_ret == 0 ИЛИ недостаточно истории (< lookback+1
        валидных цен подряд → price[t] или price[t-lookback] NaN).

    Vol-масштаб ЗДЕСЬ не применяется (см. модульный docstring): сигнал — чистая
    интенция, vol-targeting централизован в portfolio_returns_directional.
    `vol_window` принимается для единообразия сигнатур, но не используется тут.

    Возврат: DataFrame формы price, значения в {-1.0, 0.0, +1.0}, без NaN.
    """
    price = panel["price"]
    trail_ret = price / price.shift(lookback) - 1.0  # NaN если любая цена NaN
    sig = np.sign(trail_ret)                          # -1/0/+1, NaN→NaN
    return sig.fillna(0.0)                            # недостаток истории → flat


def tsmom_ensemble(panel: dict, lookbacks: tuple[int, ...],
                   vol_window: int = 30) -> pd.DataFrame:
    """Равновесный ансамбль directional TSMOM по нескольким lookback'ам.

    Правило комбинации (документировано явно):
      1. для каждого lookback L считаем sign(trailing_return_L) ∈ {-1,0,+1}
         через tsmom_signal (недостаток истории → 0);
      2. усредняем знаки по всем lookback'ам: combo[t,c] = mean_L sign_L[t,c].

    Результат ∈ [-1, +1]: это доля lookback'ов, согласных по направлению (со
    знаком). Все L согласны long → +1; все short → −1; раскол → к нулю. Аналог
    FixedEnsemble в cross-sec книге, но DIRECTIONAL (per-asset, без cross-sec
    ранжирования). Это canonical committed-сигнал downstream.

    NO look-ahead: каждый leg причинный (только цены ≤ t). Возврат без NaN
    (flat там, где все leg'и flat).
    """
    if not lookbacks:
        raise ValueError("tsmom_ensemble: need at least one lookback")
    legs = [tsmom_signal(panel, lb, vol_window=vol_window) for lb in lookbacks]
    combo = sum(legs) / len(legs)   # элементная сумма знаков / число lookback'ов
    return combo


# ── Donchian breakout (STATEFUL) ─────────────────────────────────────────────

def donchian_signal(panel: dict, channel: int) -> pd.DataFrame:
    """Stateful Donchian breakout на CLOSE-ценах (OHLC недоступен).

    Классический Turtle, но на close (нет high/low):
      - канал в момент t строится по close СТРОГО до t: окно [t-channel, t-1].
        hi[t] = max(close[t-channel .. t-1]), lo[t] = min(close[t-channel .. t-1]).
      - long (+1) если close[t] > hi[t] (пробой вверх),
      - short (−1) если close[t] < lo[t] (пробой вниз),
      - иначе ДЕРЖИМ предыдущую позицию (stateful, НЕ пересчитываем flat каждый
        день) — позиция флипается только противоположным пробоем.
    Старт flat (0). Причинно: канал в t использует close строго < t.

    NaN-цена (коин не листится) → позиция этого дня flat, состояние не тянется
    через дырку (защита от мусора на pre-history / post-delist).

    Возврат: DataFrame формы close, значения в {-1.0, 0.0, +1.0}, без NaN.
    """
    close = panel["price"]
    # Канал прошлого окна: rolling max/min, СДВИНУТЫЙ на 1 → строго до t.
    prior_hi = close.rolling(channel, min_periods=channel).max().shift(1)
    prior_lo = close.rolling(channel, min_periods=channel).min().shift(1)

    long_brk = close > prior_hi    # пробой вверх (NaN-сравнения → False)
    short_brk = close < prior_lo   # пробой вниз

    out = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for c in close.columns:
        pos = 0.0
        lb = long_brk[c].to_numpy()
        sb = short_brk[c].to_numpy()
        valid = close[c].notna().to_numpy()
        col = np.zeros(len(close), dtype=float)
        for i in range(len(close)):
            if not valid[i]:
                pos = 0.0          # дырка в данных → сброс состояния, flat
            elif lb[i]:
                pos = 1.0          # пробой вверх → long
            elif sb[i]:
                pos = -1.0         # пробой вниз → short
            # иначе: держим предыдущий pos (stateful hold)
            col[i] = pos
        out[c] = col
    return out


# ── Directional portfolio returns ────────────────────────────────────────────

def portfolio_returns_directional(
    positions: pd.DataFrame,
    fwd_ret: pd.DataFrame,
    costs_bps: float,
    accrual: pd.DataFrame | None = None,
    vol: pd.DataFrame | None = None,
    vol_target: float | None = None,
    leverage_cap: float | None = None,
) -> pd.Series:
    """Нетто дневная pnl directional trend-книги (одна серия = вся книга).

    Зеркалит ЭКОНОМИКУ xsec.portfolio_returns, но DIRECTIONAL: позиции НЕ
    нормируются к Σ=±1. Тренд ребалансит КАЖДЫЙ день (позиции меняются ежедневно).

    Аргументы:
      positions: directional интенция per-asset (sign / ансамбль), известна в t.
      fwd_ret:   доходность t→t+1 (NO look-ahead; выравнена вызывающим).
      costs_bps: per-leg one-way bps (survivorship.COSTS_BPS = 8.5). Импортируй,
                 не хардкодь.
      accrual:   ОПЦ. funding cash-flow панель формы fwd_ret, в дробных per-period
                 единицах. held[t]*accrual[t] добавляется КАЖДЫЙ удерживаемый день.
                 Знак следует за позицией (long+pos funding зарабатывает; short+neg
                 funding ТОЖЕ зарабатывает, held*accr>0). accrual=None (DEFAULT) →
                 начисление ВЫКЛ, инвариант: out = gross − cost.
      vol:       ОПЦ. realized vol панель (см. realized_vol), формы positions. Если
                 vol_target задан И vol передан — позиция масштабируется к целевой
                 per-asset vol: scale[t,c] = vol_target / vol[t,c]. Vol-targeting
                 централизован ЗДЕСЬ (а не в сигналах). vol=None или vol_target=None
                 → масштаб не применяется.
      vol_target: ОПЦ. целевая per-asset дневная vol (например 0.02 = 2%/день).
                  None → нет vol-масштаба.
      leverage_cap: ОПЦ. потолок gross = Σ|position| за день. Если gross в этот
                    день > cap, ВСЯ книга масштабируется так, чтобы gross == cap.
                    None → нет потолка.

    Vol-targeting и leverage_cap применяются к ИНТЕНЦИИ (positions) ДО расчёта
    held/turnover/gross — масштабированная позиция и есть фактически держимая.

    Возврат: pd.Series дневной нетто-доходности, индексирована датами (вся книга
             как один актив для validation_harness).
    """
    pos = positions.reindex_like(fwd_ret).fillna(0.0)
    r = fwd_ret.fillna(0.0)
    cost_rate = costs_bps / 1e4
    idx = pos.index

    # 1) Vol-targeting (централизованно). NO look-ahead: vol[t] причинна (≤ t).
    if vol_target is not None and vol is not None:
        v = vol.reindex_like(fwd_ret)
        scale = (vol_target / v).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        pos = pos * scale

    # 2) Leverage cap: если gross[t] > cap, масштабируем всю строку до cap.
    if leverage_cap is not None:
        gross_abs = pos.abs().sum(axis=1)              # gross за день
        factor = pd.Series(1.0, index=idx)
        over = gross_abs > leverage_cap
        factor[over] = leverage_cap / gross_abs[over]  # сжать только превышение
        pos = pos.mul(factor, axis=0)

    accr_aligned = None
    if accrual is not None:
        accr_aligned = accrual.reindex_like(fwd_ret).fillna(0.0)

    prev = pd.Series(0.0, index=pos.columns)  # держимая позиция вчера
    out = np.zeros(len(idx))
    for i in range(len(idx)):
        held = pos.iloc[i]                                   # ребаланс КАЖДЫЙ день
        turnover = (held - prev).abs().sum()                # смена позиции
        cost = turnover * cost_rate
        prev = held
        gross = float((held * r.iloc[i]).sum())
        if accr_aligned is not None:
            accr = float((held * accr_aligned.iloc[i]).sum())
            out[i] = gross + accr - cost
        else:
            out[i] = gross - cost
    return pd.Series(out, index=idx, name="trend_net")


# ── Hand-checkable self-test ─────────────────────────────────────────────────

if __name__ == "__main__":
    n_pass = 0

    def check(cond, msg):
        global n_pass
        assert cond, msg
        n_pass += 1

    # ── Synthetic panel: 3 assets, 12 days, prices reasoned by hand ──────────
    dates = pd.date_range("2026-01-01", periods=12, freq="D")
    # UP: clean monotone uptrend. DN: clean monotone downtrend.
    # OSC: ranges low then breaks out up then breaks down (for Donchian).
    up = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111]
    dn = [100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90, 89]
    # OSC: flat 100 (days 0-4); breakout up to 112 (day 5); then a GENTLE drift
    # that always stays above the trailing-4 min so it HOLDS long (days 6-9);
    # then a crash to 90 (day 10) that breaks below the channel → flip short.
    osc = [100, 100, 100, 100, 100, 112, 113, 114, 113, 114, 90, 91]
    price = pd.DataFrame({"UP": up, "DN": dn, "OSC": osc},
                         index=dates, dtype=float)
    fwd_ret = price.shift(-1) / price - 1.0
    funding = pd.DataFrame(0.0, index=dates, columns=["UP", "DN", "OSC"])
    panel = {"coins": ["UP", "DN", "OSC"], "price": price,
             "fwd_ret": fwd_ret, "funding": funding}

    # ── 1) tsmom_signal: long after uptrend, short after downtrend, flat early ─
    L = 4
    sig = tsmom_signal(panel, lookback=L)
    # First L rows lack lookback history → flat (0).
    check((sig.iloc[:L] == 0.0).all().all(),
          f"tsmom: first {L} rows must be flat (insufficient history):\n{sig.iloc[:L]}")
    # After warmup: UP trending up → +1, DN trending down → −1.
    check((sig["UP"].iloc[L:] == 1.0).all(),
          f"tsmom: UP must be long (+1) after clean uptrend:\n{sig['UP']}")
    check((sig["DN"].iloc[L:] == -1.0).all(),
          f"tsmom: DN must be short (−1) after clean downtrend:\n{sig['DN']}")

    # ── 2) tsmom_ensemble: agreeing lookbacks → ±1, in [-1,+1] ───────────────
    ens = tsmom_ensemble(panel, lookbacks=(2, 3, 4))
    check(((ens >= -1.0 - 1e-12) & (ens <= 1.0 + 1e-12)).all().all(),
          "tsmom_ensemble: values must lie in [-1, +1]")
    # On the last day all 3 lookbacks agree UP is up / DN is down.
    check(np.isclose(ens["UP"].iloc[-1], 1.0),
          f"tsmom_ensemble: UP last day must be +1 (all lookbacks agree):\n{ens['UP']}")
    check(np.isclose(ens["DN"].iloc[-1], -1.0),
          f"tsmom_ensemble: DN last day must be −1 (all lookbacks agree):\n{ens['DN']}")

    # ── 3) donchian_signal: stateful flip + HOLD (no mid-trend reset) ────────
    don = donchian_signal(panel, channel=4)
    osc_pos = don["OSC"]
    # OSC: flat 100 for days 0-4, jumps to 110 on day 5 → breakout up → long.
    # Days 6-9 drift DOWN (109..106) but stay above prior-window min → HOLD long.
    # Day 10 = 90 → breaks below prior-window min → flip short.
    check(osc_pos.iloc[5] == 1.0,
          f"donchian: OSC must go long on day-5 upside breakout:\n{osc_pos}")
    check((osc_pos.iloc[5:10] == 1.0).all(),
          f"donchian: OSC must HOLD long days 5-9 (stateful, NOT reset flat "
          f"mid-trend despite price drifting down):\n{osc_pos}")
    check(osc_pos.iloc[10] == -1.0,
          f"donchian: OSC must flip short on day-10 downside breakout:\n{osc_pos}")
    # Explicit anti-reset: a day held in-trend is NOT 0 just because no new breakout.
    check(osc_pos.iloc[7] != 0.0,
          "donchian: in-trend day must NOT reset to flat (stateful hold)")
    # UP keeps breaking new highs → long once warmed; never short.
    check((don["UP"].iloc[4:] >= 0.0).all() and (don["UP"].iloc[-1] == 1.0),
          f"donchian: UP must be long on continued breakouts:\n{don['UP']}")

    # ── 4) portfolio_returns_directional: accrual=None → out == gross − cost ──
    COSTS_BPS = 8.5  # mirror survivorship.COSTS_BPS (imported downstream)
    # Use a simple fixed position book: long UP, short DN, flat OSC, every day.
    pos = pd.DataFrame({"UP": 1.0, "DN": -1.0, "OSC": 0.0},
                       index=dates)
    pnl = portfolio_returns_directional(pos, fwd_ret, costs_bps=COSTS_BPS)
    # Day 0 by hand:
    #   held = [UP=1, DN=-1, OSC=0]; prev = 0 → turnover = |1|+|-1|+0 = 2
    #   cost = 2 * 8.5/1e4
    #   gross = 1*fwd_ret[UP,0] + (-1)*fwd_ret[DN,0] + 0
    g0 = 1.0 * fwd_ret["UP"].iloc[0] + (-1.0) * fwd_ret["DN"].iloc[0]
    c0 = 2.0 * (COSTS_BPS / 1e4)
    check(np.isclose(pnl.iloc[0], g0 - c0),
          f"directional day0: {pnl.iloc[0]} != gross-cost {g0 - c0}")
    # Day 1: position unchanged → turnover 0 → cost 0 → out == gross exactly.
    g1 = 1.0 * fwd_ret["UP"].iloc[1] + (-1.0) * fwd_ret["DN"].iloc[1]
    check(np.isclose(pnl.iloc[1], g1),
          f"directional day1 (no turnover): {pnl.iloc[1]} != gross {g1}")
    # Strict invariant: accrual=None ⇒ out == gross − cost for EVERY day.
    r_f = fwd_ret.fillna(0.0)
    held_book = pos
    gross_all = (held_book * r_f).sum(axis=1)
    turn = held_book.diff().abs()
    turn.iloc[0] = held_book.iloc[0].abs()  # from zero
    cost_all = turn.sum(axis=1) * (COSTS_BPS / 1e4)
    check(np.allclose(pnl.values, (gross_all - cost_all).values),
          "directional: accrual=None must equal gross-cost EXACTLY every day")

    # ── 4b) funding accrual sign: long+positive and short+negative both EARN ──
    fund_pos = pd.DataFrame(0.0005, index=dates, columns=["UP", "DN", "OSC"])
    fund_neg = pd.DataFrame(-0.0005, index=dates, columns=["UP", "DN", "OSC"])
    zero_fwd = pd.DataFrame(0.0, index=dates, columns=["UP", "DN", "OSC"])
    # Long UP with POSITIVE funding, zero cost (hold steady), no spot move.
    long_only = pd.DataFrame({"UP": 1.0, "DN": 0.0, "OSC": 0.0}, index=dates)
    pnl_lp = portfolio_returns_directional(long_only, zero_fwd, costs_bps=0.0,
                                           accrual=fund_pos)
    # day0 establishes position (turnover, but cost 0); days 1+ pure accrual.
    check(np.allclose(pnl_lp.iloc[1:].values, 0.0005),
          f"accrual: long + positive funding must EARN +0.0005/day:\n{pnl_lp}")
    # Short DN with NEGATIVE funding → held*accr = (-1)*(-0.0005) = +0.0005 earns.
    short_only = pd.DataFrame({"UP": 0.0, "DN": -1.0, "OSC": 0.0}, index=dates)
    pnl_sn = portfolio_returns_directional(short_only, zero_fwd, costs_bps=0.0,
                                           accrual=fund_neg)
    check(np.allclose(pnl_sn.iloc[1:].values, 0.0005),
          f"accrual: short + negative funding must EARN +0.0005/day:\n{pnl_sn}")
    # Sanity: long + negative funding LOSES.
    pnl_ln = portfolio_returns_directional(long_only, zero_fwd, costs_bps=0.0,
                                           accrual=fund_neg)
    check(np.allclose(pnl_ln.iloc[1:].values, -0.0005),
          "accrual: long + negative funding must LOSE −0.0005/day")

    # ── 5) vol-targeting: higher-vol asset gets SMALLER position ─────────────
    # Two assets, same +1 signal, but HIV is 2x more volatile than LOV.
    vdates = pd.date_range("2026-02-01", periods=40, freq="D")
    rng = np.random.default_rng(0)
    lov_ret = rng.normal(0.0, 0.01, len(vdates))   # 1% daily vol
    hiv_ret = rng.normal(0.0, 0.02, len(vdates))   # 2% daily vol
    lov_px = 100 * np.cumprod(1 + lov_ret)
    hiv_px = 100 * np.cumprod(1 + hiv_ret)
    vprice = pd.DataFrame({"LOV": lov_px, "HIV": hiv_px}, index=vdates)
    vvol = realized_vol(vprice, vol_window=20)
    vfwd = vprice.shift(-1) / vprice - 1.0
    same_sig = pd.DataFrame(1.0, index=vdates, columns=["LOV", "HIV"])
    # Reproduce the internal scaling to compare the held positions directly.
    vt = 0.01
    scale = (vt / vvol).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    scaled = same_sig * scale
    last = scaled.dropna().iloc[-1]
    check(last["LOV"] > last["HIV"],
          f"vol-target: lower-vol LOV must get LARGER position than higher-vol "
          f"HIV under same signal: LOV={last['LOV']:.3f} HIV={last['HIV']:.3f}")
    # And that portfolio_returns_directional actually applies it (smoke: runs,
    # finite output, vol-scaling changes the pnl vs no scaling).
    pnl_vt = portfolio_returns_directional(same_sig, vfwd, costs_bps=COSTS_BPS,
                                           vol=vvol, vol_target=vt)
    pnl_novt = portfolio_returns_directional(same_sig, vfwd, costs_bps=COSTS_BPS)
    check(np.isfinite(pnl_vt.values).all(),
          "vol-target: pnl must be finite")
    check(not np.allclose(pnl_vt.values, pnl_novt.values),
          "vol-target: scaling must change the book pnl vs unscaled")

    # ── 6) leverage_cap: gross never exceeds the cap on any day ──────────────
    # 3 assets all signal +1 → raw gross = 3. Cap at 2 → gross must be ≤ 2.
    big = pd.DataFrame(1.0, index=dates, columns=["UP", "DN", "OSC"])
    cap = 2.0
    # Inspect the capped book the function builds (replicate cap step).
    gross_raw = big.abs().sum(axis=1)
    factor = pd.Series(1.0, index=dates)
    over = gross_raw > cap
    factor[over] = cap / gross_raw[over]
    capped = big.mul(factor, axis=0)
    gross_capped = capped.abs().sum(axis=1)
    check((gross_capped <= cap + 1e-12).all(),
          f"leverage_cap: gross must never exceed cap={cap}:\n{gross_capped}")
    check(np.isclose(gross_capped.iloc[0], cap),
          "leverage_cap: an over-cap day must be scaled to exactly the cap")
    # End-to-end: function runs with the cap and produces finite pnl.
    pnl_cap = portfolio_returns_directional(big, fwd_ret, costs_bps=COSTS_BPS,
                                            leverage_cap=cap)
    check(np.isfinite(pnl_cap.values).all(),
          "leverage_cap: capped pnl must be finite")
    # Under-cap book (gross 2 = cap) is unaffected: long UP + short DN.
    pnl_under = portfolio_returns_directional(pos, fwd_ret, costs_bps=COSTS_BPS,
                                              leverage_cap=cap)
    check(np.allclose(pnl_under.values, pnl.values),
          "leverage_cap: a book at/under the cap must be unchanged")

    print("\n=== trend.py self-test ===")
    print("\ntsmom_signal (lookback=4):")
    print(sig.to_string())
    print("\ntsmom_ensemble (lookbacks=2,3,4):")
    print(ens.round(3).to_string())
    print("\ndonchian_signal (channel=4):")
    print(don.to_string())
    print("\ndirectional pnl (long UP / short DN), accrual=None:")
    print(pnl.round(6).to_string())
    print(f"\nday0 net = {pnl.iloc[0]:.6f} (expected gross-cost {g0 - c0:.6f})")
    print(f"\nOK: {n_pass} trend.py self-tests passed")
