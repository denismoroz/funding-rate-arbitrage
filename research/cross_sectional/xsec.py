"""
Cross-sectional long-short engine (data-independent, crypto + FX).

Каждый ребаланс: ранжируем инструменты по cross-sectional скору, лонг верхняя
треть / шорт нижняя → **dollar-neutral** (Σлонг = +1, Σшорт = −1, gross = 2,
net = 0). Equal-weight внутри ноги.

Выравнивание (NO look-ahead):
  - scores[t]   — известны в момент решения t (используют инфу ≤ t).
  - fwd_ret[t]  — доходность, РЕАЛИЗОВАННАЯ ПОСЛЕ решения t, т.е. с t на t+1.
                  Вес w[t] зарабатывает fwd_ret[t]. Выравнивание fwd_ret —
                  ответственность вызывающего (shift делается снаружи).

Допущения:
  - rebal_every: веса держатся постоянными rebal_every периодов между
    ребалансами (carry-forward). Дрейф весов внутри окна удержания
    ИГНОРИРУЕТСЯ (упрощение): на каждом шаге применяем зафиксированный на
    последнем ребалансе вектор весов, без ре-нормировки под накопленный pnl.
  - Costs: на ребалансе cost = turnover × costs_bps/1e4, turnover =
    Σ|w_new − w_old|. Первый период — turnover = gross свежей книги (от нуля).

Только numpy/pandas.
"""

import numpy as np
import pandas as pd


def rank_to_weights(scores: pd.DataFrame, tercile_frac: float = 1 / 3) -> pd.DataFrame:
    """Ранг → dollar-neutral веса по строкам.

    scores: index=date, columns=instrument, value=cross-sectional скор
            (выше = привлекательнее в лонг). NaN = инструмент невалиден этот
            период → исключён из ранжирования.
    Возврат: DataFrame той же формы; +вес в верхней доле tercile_frac (Σ=+1),
             −вес в нижней (Σ=−1), 0 в середине/исключённых. Equal-weight в ноге.
    <2 валидных инструмента в строке → нулевая строка (нет сделки).
    """
    w = pd.DataFrame(0.0, index=scores.index, columns=scores.columns)
    for dt, row in scores.iterrows():
        valid = row.dropna()
        n = len(valid)
        if n < 2:
            continue
        k = max(1, int(np.floor(n * tercile_frac)))  # размер каждой ноги
        order = valid.sort_values(ascending=False)    # лучшие сверху
        longs = order.index[:k]
        shorts = order.index[n - k:]
        w.loc[dt, longs] = 1.0 / k
        w.loc[dt, shorts] = -1.0 / k
    return w


def portfolio_returns(
    weights: pd.DataFrame,
    fwd_ret: pd.DataFrame,
    costs_bps: float = 5.0,
    rebal_every: int = 1,
    accrual: pd.DataFrame | None = None,
) -> pd.Series:
    """Нетто-доходность long-short книги по периодам.

    weights: dollar-neutral веса (см. rank_to_weights), известны в t.
    fwd_ret: доходность с t на t+1 (выравнена вызывающим, NO look-ahead).
    rebal_every: держим веса rebal_every периодов; ребалансим на индексах
                 0, rebal_every, 2*rebal_every, ... (carry-forward, без дрейфа).
    costs_bps: спред в б.п.; на ребалансе вычитаем turnover×costs_bps/1e4.

    accrual: ОПЦИОНАЛЬНЫЙ held-position carry/funding cash-flow panel, ту же форму
             что и fwd_ret, в уже-периодных ДРОБНЫХ единицах (НЕ % p.a., НЕ б.п.):
             accrual[t,c] — доход (или расход, если <0), который удерживаемая
             позиция c зарабатывает ЗА ОДИН период удержания t→t+1. Начисляется
             КАЖДЫЙ удерживаемый период (не только в дни ребаланса), на тот же
             вектор held-весов, что и gross. Выравнивание — как у fwd_ret (NO
             look-ahead): accrual[t] известен в начале периода t и зарабатывается
             на интервале t→t+1, поэтому пара (held[t], accrual[t]) корректна и не
             заглядывает вперёд. Знак следует за позицией: лонг (held>0) с
             положительным accrual зарабатывает; шорт (held<0) с отрицательным
             accrual ТОЖЕ зарабатывает (held*accrual>0) — это by-construction
             корректно для rate-differential / funding flow.

             accrual=None (DEFAULT) → начисление ВЫКЛЮЧЕНО, out[i]=gross-cost, т.е.
             поведение В ТОЧНОСТИ как раньше (это инвариант, на котором держится
             неизменность crypto-книги — crypto никогда не передаёт accrual).

    Возврат: pd.Series нетто-доходности, индексирована датами. Это «один актив»
             (вся книга) для скармливания в validation_harness.
    """
    w = weights.reindex_like(fwd_ret).fillna(0.0)
    r = fwd_ret.fillna(0.0)
    idx = w.index
    cost_rate = costs_bps / 1e4

    accr_aligned = None
    if accrual is not None:
        accr_aligned = accrual.reindex_like(fwd_ret).fillna(0.0)

    held = pd.Series(0.0, index=w.columns)  # текущие удерживаемые веса
    prev = pd.Series(0.0, index=w.columns)  # веса до последнего ребаланса
    out = np.zeros(len(idx))
    for i in range(len(idx)):
        if i % rebal_every == 0:
            held = w.iloc[i]
            turnover = (held - prev).abs().sum()
            prev = held
            cost = turnover * cost_rate
        else:
            cost = 0.0
        gross = float((held * r.iloc[i]).sum())
        if accr_aligned is not None:
            accr = float((held * accr_aligned.iloc[i]).sum())
            out[i] = gross + accr - cost
        else:
            out[i] = gross - cost
    return pd.Series(out, index=idx, name="xsec_net")


def zscore_cross_section(scores: pd.DataFrame) -> pd.DataFrame:
    """Standardize each ROW (date) across instruments: (x - row_mean) / row_std.

    Mean/std are taken over the non-NaN instruments of that date (ddof=0,
    population). NaNs are preserved. Rows with <2 valid instruments or zero
    cross-sectional spread (std==0) yield NaN (undefined standardization → no
    information that date). Output is unit-scaled so different factors are
    comparable for blending.
    """
    mean = scores.mean(axis=1)
    std = scores.std(axis=1, ddof=0)
    z = scores.sub(mean, axis=0).div(std.replace(0.0, np.nan), axis=0)
    return z


def blend(panels_or_scores, weights=None) -> pd.DataFrame:
    """Weighted average of per-factor z-scored panels → one blended score.

    panels_or_scores: list of pd.DataFrame factor panels (ALREADY z-scored, so the
                      legs are comparable; pass them through zscore_cross_section).
    weights: per-factor weights (any positive scale; normalized to sum 1). Default
             equal weights.

    Frames are aligned on the UNION of index/columns. At each cell the blend is the
    weight-renormalized average over the factors that are non-NaN there (a missing
    factor is dropped from that cell's average, not treated as zero); a cell with
    NO valid factor stays NaN. Higher = more attractive long, by construction.
    """
    panels = list(panels_or_scores)
    if not panels:
        raise ValueError("blend: need at least one factor panel")
    if weights is None:
        weights = [1.0] * len(panels)
    if len(weights) != len(panels):
        raise ValueError("blend: weights length must match number of panels")
    w = np.asarray(weights, dtype=float)
    if (w < 0).any() or w.sum() == 0:
        raise ValueError("blend: weights must be non-negative and not all zero")

    idx = panels[0].index
    cols = panels[0].columns
    for p in panels[1:]:
        idx = idx.union(p.index)
        cols = cols.union(p.columns)
    panels = [p.reindex(index=idx, columns=cols) for p in panels]

    num = pd.DataFrame(0.0, index=idx, columns=cols)
    den = pd.DataFrame(0.0, index=idx, columns=cols)
    for wi, p in zip(w, panels):
        valid = p.notna()
        num = num.add((p.where(valid) * wi).fillna(0.0))
        den = den.add(valid.astype(float) * wi)
    return num.div(den.replace(0.0, np.nan))


if __name__ == "__main__":
    # --- Hand-checkable toy: 4 instruments, 6 dates -----------------------
    dates = pd.date_range("2026-01-01", periods=6, freq="D")
    cols = ["A", "B", "C", "D"]
    # Скоры: ранжирование очевидно. n=4, tercile_frac=1/3 → k=floor(4/3)=1.
    # Каждый период: лонг лучший (вес +1), шорт худший (вес −1), B/C флэт.
    scores = pd.DataFrame(
        [
            [4, 3, 2, 1],   # long A, short D
            [1, 2, 3, 4],   # long D, short A
            [4, 3, 2, 1],   # long A, short D
            [4, 3, 2, 1],   # long A, short D
            [10, 1, 2, 3],  # long A, short B
            [np.nan, np.nan, np.nan, 5],  # n=1 < 2 → нет сделки
        ],
        index=dates, columns=cols, dtype=float,
    )

    w = rank_to_weights(scores)

    # Ручная проверка весов
    exp_w = pd.DataFrame(
        [
            [1, 0, 0, -1],
            [-1, 0, 0, 1],
            [1, 0, 0, -1],
            [1, 0, 0, -1],
            [1, -1, 0, 0],
            [0, 0, 0, 0],
        ],
        index=dates, columns=cols, dtype=float,
    )
    assert np.allclose(w.values, exp_w.values), f"weights mismatch:\n{w}"

    # Dollar-neutrality: каждая не-флэт строка → Σлонг=+1, Σшорт=−1
    for dt, row in w.iterrows():
        if row.abs().sum() == 0:
            continue
        assert np.isclose(row[row > 0].sum(), 1.0), f"long leg != +1 @ {dt}"
        assert np.isclose(row[row < 0].sum(), -1.0), f"short leg != -1 @ {dt}"

    # --- Forward returns (с t на t+1) ------------------------------------
    fwd = pd.DataFrame(
        [
            [0.02, 0.00, 0.00, -0.01],  # A +2%, D −1%
            [0.00, 0.00, 0.00, 0.00],
            [0.01, 0.00, 0.00, 0.00],
            [0.00, 0.00, 0.00, 0.00],
            [0.05, -0.03, 0.00, 0.00],
            [0.00, 0.00, 0.00, 0.00],
        ],
        index=dates, columns=cols, dtype=float,
    )

    costs_bps = 5.0
    pnl = portfolio_returns(w, fwd, costs_bps=costs_bps, rebal_every=1)

    # Ручной расчёт period 0:
    #   gross = 1*0.02 + (-1)*(-0.01) = 0.03
    #   turnover (от нуля) = |1|+|-1| = 2  → cost = 2 * 5/1e4 = 0.001
    #   net = 0.03 - 0.001 = 0.029
    exp_p0 = (1 * 0.02 + (-1) * (-0.01)) - 2 * (costs_bps / 1e4)
    assert np.isclose(pnl.iloc[0], exp_p0), f"period0 {pnl.iloc[0]} != {exp_p0}"

    # Ручной расчёт period 1: веса флипнулись A:1→-1, D:-1→1
    #   gross = (-1)*0 + 1*0 = 0
    #   turnover = |-1-1| + |1-(-1)| = 2+2 = 4 → cost = 4*5/1e4 = 0.002
    #   net = -0.002
    exp_p1 = 0.0 - 4 * (costs_bps / 1e4)
    assert np.isclose(pnl.iloc[1], exp_p1), f"period1 {pnl.iloc[1]} != {exp_p1}"

    # --- rebal_every: держим веса period0 два периода ---------------------
    # При rebal_every=2 на i=1 ребаланса нет: held остаётся [1,0,0,-1].
    #   gross_1 = 1*0 + (-1)*0 = 0, cost=0 → net_1 = 0
    pnl2 = portfolio_returns(w, fwd, costs_bps=costs_bps, rebal_every=2)
    assert np.isclose(pnl2.iloc[1], 0.0), f"rebal_every=2 period1 {pnl2.iloc[1]} != 0"
    assert np.isclose(pnl2.iloc[0], exp_p0), "rebal_every=2 period0 mismatch"

    # --- accrual=None regression guard: identical to the no-accrual pnl ----
    pnl_none = portfolio_returns(w, fwd, costs_bps=costs_bps, rebal_every=1,
                                 accrual=None)
    assert np.allclose(pnl_none.values, pnl.values), \
        "accrual=None must reproduce the original pnl EXACTLY"
    pnl2_none = portfolio_returns(w, fwd, costs_bps=costs_bps, rebal_every=2,
                                  accrual=None)
    assert np.allclose(pnl2_none.values, pnl2.values), \
        "accrual=None (rebal_every=2) must reproduce the original pnl EXACTLY"

    # --- hand-checkable accrual case: a single held long with a constant rate -
    # One instrument 'X', held long (weight +1) every period, zero spot return,
    # zero turnover after period 0; a constant per-period accrual rate adds
    # exactly held*rate = +1*rate to EACH period's net (minus only the period-0
    # turnover cost of establishing the position from zero).
    adates = pd.date_range("2026-04-01", periods=4, freq="D")
    aw = pd.DataFrame(1.0, index=adates, columns=["X"])     # always long 1 unit
    afwd = pd.DataFrame(0.0, index=adates, columns=["X"])   # no spot move
    rate = 0.0003                                            # const per-period accrual
    aacc = pd.DataFrame(rate, index=adates, columns=["X"])
    apnl = portfolio_returns(aw, afwd, costs_bps=costs_bps, rebal_every=1,
                             accrual=aacc)
    # period 0: gross 0 + accr (1*rate) - turnover cost (|1-0|*costs_bps/1e4)
    exp_a0 = 0.0 + 1.0 * rate - 1.0 * (costs_bps / 1e4)
    assert np.isclose(apnl.iloc[0], exp_a0), f"accrual period0 {apnl.iloc[0]} != {exp_a0}"
    # periods 1..3: gross 0, no turnover (weights unchanged) → net == 1*rate.
    assert np.allclose(apnl.iloc[1:].values, rate), \
        f"each held period must add exactly held*rate={rate}:\n{apnl}"
    # short symmetry: held=-1 with a NEGATIVE rate earns +|rate| (held*accr>0).
    aws = pd.DataFrame(-1.0, index=adates, columns=["X"])
    aacc_neg = pd.DataFrame(-rate, index=adates, columns=["X"])
    apnl_s = portfolio_returns(aws, afwd, costs_bps=0.0, rebal_every=1,
                               accrual=aacc_neg)
    assert np.allclose(apnl_s.values, rate), "short*(-rate) must earn +rate each period"

    print("=== xsec self-test ===")
    print("\nweights:")
    print(w.to_string())
    print("\nnet pnl (rebal_every=1):")
    print(pnl.round(6).to_string())
    print("\nnet pnl (rebal_every=2):")
    print(pnl2.round(6).to_string())
    print(f"\nperiod0 net = {pnl.iloc[0]:.6f} (expected {exp_p0:.6f})")
    print(f"period1 net = {pnl.iloc[1]:.6f} (expected {exp_p1:.6f})")
    print("\n=== xsec accrual ===  accrual=None reproduces original pnl exactly; "
          f"\n  held-long const rate {rate} adds exactly held*rate each period "
          "(long & short symmetric)  OK")
    # --- zscore_cross_section: each valid row mean≈0, std≈1 ---------------
    zsrc = pd.DataFrame(
        [
            [1.0, 2.0, 3.0, 4.0],          # full row
            [10.0, np.nan, 20.0, 30.0],    # one NaN, 3 valid
            [5.0, np.nan, np.nan, np.nan], # 1 valid → NaN row
            [7.0, 7.0, 7.0, 7.0],          # zero spread → NaN row
        ],
        index=pd.date_range("2026-02-01", periods=4, freq="D"),
        columns=cols, dtype=float,
    )
    z = zscore_cross_section(zsrc)
    valid = zsrc.notna().sum(axis=1) >= 2
    rmean = z.mean(axis=1)
    rstd = z.std(axis=1, ddof=0)
    # rows 0,1 are valid with non-zero spread → mean 0, std 1.
    assert np.allclose(rmean.iloc[[0, 1]], 0.0, atol=1e-12), "z row mean != 0"
    assert np.allclose(rstd.iloc[[0, 1]], 1.0, atol=1e-12), "z row std != 1"
    assert z.iloc[2].isna().all(), "z: <2 valid → NaN row"
    assert z.iloc[3].isna().all(), "z: zero spread → NaN row"
    # NaN cell in row 1 is preserved (not standardized into a number).
    assert np.isnan(z.iloc[1, 1]), "z must preserve NaN cells"
    print("\n=== xsec zscore_cross_section ===  valid rows mean≈0 std≈1  OK")

    # --- blend: equal-weight average of two known panels == hand average --
    pa = pd.DataFrame(
        [[1.0, 3.0, np.nan], [2.0, np.nan, 6.0]],
        index=pd.date_range("2026-03-01", periods=2, freq="D"),
        columns=["A", "B", "C"], dtype=float,
    )
    pb = pd.DataFrame(
        [[5.0, 1.0, 4.0], [np.nan, np.nan, 2.0]],
        index=pd.date_range("2026-03-01", periods=2, freq="D"),
        columns=["A", "B", "C"], dtype=float,
    )
    bl = blend([pa, pb])
    # cell [0,A] = mean(1,5)=3 ; [0,B] = mean(3,1)=2 ; [0,C] only pb=4 ;
    # [1,A] only pa=2 ; [1,B] both NaN → NaN ; [1,C] = mean(6,2)=4.
    exp = pd.DataFrame(
        [[3.0, 2.0, 4.0], [2.0, np.nan, 4.0]],
        index=pa.index, columns=["A", "B", "C"], dtype=float,
    )
    assert np.allclose(bl.fillna(-9).values, exp.fillna(-9).values), \
        f"blend mismatch:\n{bl}\nexpected:\n{exp}"
    # weighted blend [3,1] on a full-overlap cell renormalizes: [0,A]=(3*1+5*3)/4
    blw = blend([pa, pb], weights=[1.0, 3.0])
    assert np.isclose(blw.iloc[0, 0], (1.0 * 1 + 5.0 * 3) / 4.0), "blend weights"
    print("=== xsec blend ===  equal-weight & weighted per-cell average  OK")

    print("\nALL ASSERTS PASSED")
