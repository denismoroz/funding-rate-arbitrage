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
) -> pd.Series:
    """Нетто-доходность long-short книги по периодам.

    weights: dollar-neutral веса (см. rank_to_weights), известны в t.
    fwd_ret: доходность с t на t+1 (выравнена вызывающим, NO look-ahead).
    rebal_every: держим веса rebal_every периодов; ребалансим на индексах
                 0, rebal_every, 2*rebal_every, ... (carry-forward, без дрейфа).
    costs_bps: спред в б.п.; на ребалансе вычитаем turnover×costs_bps/1e4.

    Возврат: pd.Series нетто-доходности, индексирована датами. Это «один актив»
             (вся книга) для скармливания в validation_harness.
    """
    w = weights.reindex_like(fwd_ret).fillna(0.0)
    r = fwd_ret.fillna(0.0)
    idx = w.index
    cost_rate = costs_bps / 1e4

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
        out[i] = gross - cost
    return pd.Series(out, index=idx, name="xsec_net")


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

    print("=== xsec self-test ===")
    print("\nweights:")
    print(w.to_string())
    print("\nnet pnl (rebal_every=1):")
    print(pnl.round(6).to_string())
    print("\nnet pnl (rebal_every=2):")
    print(pnl2.round(6).to_string())
    print(f"\nperiod0 net = {pnl.iloc[0]:.6f} (expected {exp_p0:.6f})")
    print(f"period1 net = {pnl.iloc[1]:.6f} (expected {exp_p1:.6f})")
    print("\nALL ASSERTS PASSED")
