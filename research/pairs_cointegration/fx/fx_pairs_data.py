"""
FX pairs data layer — пул = ВСЕ C(9,2)=36 пар среди 9 G10 валют.

USD-нейтрализация (аналог BTC-residual из крипто-версии):
  1. Загрузим log_spot для всех 9 валют (XXXUSD ориентация, уже в fxdata.py).
  2. Построим equal-weight USD-фактор: usd_factor = mean(log_spot_1..9).
  3. Для каждой валюты rolling-OLS: log_spot_c ~ α + β·usd_factor → остаток resid_c.
     Окно: USD_RESID_WINDOW (дней), seam-safe: считается на ПОЛНОМ ряду, маски
     только отбирают строки.
  4. Пара торгуется/тестируется на остатках resid_c_a, resid_c_b.

df на пару содержит:
  log_spot_a, log_spot_b   — исходные логарифмы цен
  resid_a, resid_b         — USD-нейтральные остатки
  carry_a, carry_b         — дневная ставка-дифф к USD (per-business-day fraction)
                             = (rate_c - rate_USD) / 100 / 252

Загрузка spot/rate по образцу fxdata.py; REER нам не нужен.

Пул ЗАМОРОЖЕН — 36 пар, никаких изменений по ходу.
"""
from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

# ── sys.path ──────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_FX_XSEC = _HERE.parent.parent / "cross_sectional" / "fx"
_RESEARCH = _HERE.parent.parent

for _p in (_FX_XSEC, _RESEARCH):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fxdata

# ── Конфиги ──────────────────────────────────────────────────────────────────
# Валюты (из fxdata/fetch)
CURRENCIES = ["AUD", "CAD", "CHF", "EUR", "GBP", "JPY", "NOK", "NZD", "SEK"]

# Все C(9,2) = 36 пар (freeze)
ALL_PAIRS: list[tuple[str, str]] = list(combinations(CURRENCIES, 2))

# Окно rolling-OLS для USD-нейтрализации
USD_RESID_WINDOW = 90   # дней (как BTC_RESID_WINDOW в крипте)

# Cache панели (загружается один раз)
_PANEL_CACHE: dict | None = None


def _get_panel() -> dict:
    global _PANEL_CACHE
    if _PANEL_CACHE is None:
        _PANEL_CACHE = fxdata.load_panel()
    return _PANEL_CACHE


def _compute_usd_residuals(price: pd.DataFrame) -> pd.DataFrame:
    """Rolling-OLS USD-нейтрализация для всех валют.

    Для каждой валюты c:
        usd_factor_t = mean(log_spot_1..9)_t
        rolling window=[t-W+1..t]: log_spot_c ~ alpha + beta * usd_factor
        resid_c[t] = log_spot_c[t] - (alpha_hat + beta_hat * usd_factor[t])

    Seam-safe: считается на ПОЛНОМ price-DataFrame, маски лишь отбирают строки.

    Returns
    -------
    pd.DataFrame с теми же индексом/столбцами что price, значения = остатки.
    Первые USD_RESID_WINDOW-1 строк = NaN (тепловая разгонка OLS).
    """
    log_price = np.log(price)   # shape (T, 9)
    usd_factor = log_price.mean(axis=1)  # equal-weight mean — (T,)

    n = len(log_price)
    W = USD_RESID_WINDOW

    resid = pd.DataFrame(
        np.full_like(log_price.values, np.nan),
        index=log_price.index,
        columns=log_price.columns,
    )

    for c in log_price.columns:
        y = log_price[c].values
        x = usd_factor.values
        for t in range(W - 1, n):
            y_w = y[t - W + 1: t + 1]
            x_w = x[t - W + 1: t + 1]
            # OLS: y ~ alpha + beta * x
            X = np.column_stack([np.ones(W), x_w])
            try:
                coef, _, _, _ = np.linalg.lstsq(X, y_w, rcond=None)
            except np.linalg.LinAlgError:
                continue
            alpha_hat, beta_hat = coef[0], coef[1]
            resid.loc[resid.index[t], c] = y[t] - (alpha_hat + beta_hat * x[t])

    return resid


# ── Глобальный cache USD-остатков (один прогон на всю сессию) ────────────────
_RESID_CACHE: pd.DataFrame | None = None
_CARRY_RATE_CACHE: pd.DataFrame | None = None
_LOG_PRICE_CACHE: pd.DataFrame | None = None


def _get_residuals_and_carry() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Вернуть (log_price, resid, carry_rate) — вычислить один раз и закэшировать."""
    global _RESID_CACHE, _CARRY_RATE_CACHE, _LOG_PRICE_CACHE

    if _RESID_CACHE is None:
        P = _get_panel()
        price = P["price"]     # XXXUSD ориентация, уже ffill'd
        short_rate = P["short_rate"]   # % p.a., per currency
        usd_rate = P["usd_rate"]["USD"]   # % p.a.

        _LOG_PRICE_CACHE = np.log(price)
        _RESID_CACHE = _compute_usd_residuals(price)

        # carry_rate[t,c] = (rate_c[t] - rate_USD[t]) / 100 / 252 (per-bday fraction)
        diff = short_rate.sub(usd_rate, axis=0)
        _CARRY_RATE_CACHE = (diff / 100.0 / 252.0).reindex_like(_RESID_CACHE)

    return _LOG_PRICE_CACHE, _RESID_CACHE, _CARRY_RATE_CACHE


def load_pair_df(pair: tuple[str, str]) -> pd.DataFrame | None:
    """Загрузить df для одной пары.

    Returns
    -------
    DataFrame с колонками:
      log_spot_a, log_spot_b   — log(XXXUSD) обеих валют
      resid_a, resid_b         — USD-нейтральные остатки (NaN в warm-up)
      carry_a, carry_b         — per-bday carry rate vs USD (fraction)
    Строки отброшены, где любой из resid NaN (warm-up + первые USD_RESID_WINDOW-1).
    """
    ccy_a, ccy_b = pair
    if ccy_a not in CURRENCIES or ccy_b not in CURRENCIES:
        raise ValueError(f"Unknown currencies: {pair}")

    log_price, resid, carry = _get_residuals_and_carry()

    df = pd.DataFrame({
        "log_spot_a": log_price[ccy_a],
        "log_spot_b": log_price[ccy_b],
        "resid_a": resid[ccy_a],
        "resid_b": resid[ccy_b],
        "carry_a": carry[ccy_a],
        "carry_b": carry[ccy_b],
    })

    # Убрать строки с NaN в residuals (warm-up)
    mask = df[["resid_a", "resid_b"]].notna().all(axis=1)
    df = df[mask].copy()

    if len(df) < 100:
        return None

    return df


# ── self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Всего пар: {len(ALL_PAIRS)}  (ожидается 36)")
    assert len(ALL_PAIRS) == 36, f"Неожиданное число пар: {len(ALL_PAIRS)}"

    pair = ("AUD", "NZD")
    df = load_pair_df(pair)
    assert df is not None, "AUD/NZD не загрузилась"
    print(f"\npair {pair}: {len(df)} days  {df.index.min().date()} .. {df.index.max().date()}")
    print(f"columns: {list(df.columns)}")
    print(f"resid_a NaN: {df['resid_a'].isna().sum()}")
    print(f"carry_a mean: {df['carry_a'].mean():.6f}  (ожидается малая дробь ~±2e-5)")

    # Проверим, что resid имеет меньшую дисперсию USD-фактора
    P = _get_panel()
    log_p = np.log(P["price"])
    usd_fac = log_p.mean(axis=1).reindex(df.index)
    corr_raw_a = df["log_spot_a"].corr(usd_fac)
    corr_resid_a = df["resid_a"].corr(usd_fac)
    print(f"\ncorr(log_spot_AUD, usd_factor) = {corr_raw_a:.3f}  (ожидается высокая)")
    print(f"corr(resid_AUD,    usd_factor) = {corr_resid_a:.3f}  (ожидается ≈0 после нейтрализации)")
    assert abs(corr_resid_a) < abs(corr_raw_a), "USD-нейтрализация не снизила корреляцию!"
    print("\nself-test passed.")
