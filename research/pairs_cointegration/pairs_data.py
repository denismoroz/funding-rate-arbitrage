"""
Ф2 — Пул кандидатных пар (freeze по секторам) + BTC-нейтральные остатки.

Пул ФИКСИРОВАН по экономической логике (PLAN.md §3).
Никакого all-pairs = p-hacking.

BTC-нейтрализация (PLAN §2a):
  Для каждой монеты регрессируем log-price на log(BTC) — rolling OLS на окне
  BTC_RESID_WINDOW дней. Остаток = log_price - β_btc * log_btc - α.
  Seam-safe: считаем на ПОЛНОМ ряду (lookback цел), маски CPCV лишь отбирают строки.
  Rolling-регрессия по своей природе не подглядывает вперёд (использует данные <=t).

Переиспользует cryptodata.load_panel для загрузки данных.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd
import sys

# ── путь к cryptodata ────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_CRYPTO_PATH = _HERE.parent / "cross_sectional" / "crypto"
if str(_CRYPTO_PATH) not in sys.path:
    sys.path.insert(0, str(_CRYPTO_PATH))

from cryptodata import load_panel  # noqa: E402

# ── Rolling BTC-residual window ──────────────────────────────────────────────
BTC_RESID_WINDOW = 90   # дней: достаточно для β-оценки, не слишком много

# ── Фиксированный пул пар (секторы) ─────────────────────────────────────────
# Каждый сектор — самостоятельная группа (общий драйвер сверх BTC-beta).
# Пары — неупорядоченные (a, b) внутри сектора.

_SECTORS: dict[str, list[str]] = {
    "L1": ["SOL", "AVAX", "NEAR", "ADA", "DOT", "ATOM", "APT", "SUI"],
    "L2": ["ARB"],          # только один L2 с данными — пары внутри невозможны
    "DeFi": ["AAVE", "UNI", "CRV"],
    "LST_restaking": ["ETHFI", "EIGEN"],
}

# Все пары из секторов с >=2 монетами
def _sector_pairs() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for coins in _SECTORS.values():
        for i in range(len(coins)):
            for j in range(i + 1, len(coins)):
                pairs.append((coins[i], coins[j]))
    return pairs


CANDIDATE_PAIRS: list[tuple[str, str]] = _sector_pairs()

# Все уникальные монеты в пуле (+ BTC для нейтрализации)
def _all_coins() -> list[str]:
    coins_set: set[str] = {"BTC"}
    for a, b in CANDIDATE_PAIRS:
        coins_set.add(a)
        coins_set.add(b)
    return sorted(coins_set)


ALL_UNIVERSE_COINS: list[str] = _all_coins()


class PairData(NamedTuple):
    """Данные одной пары: df с колонками для обеих ног + BTC + funding + residuals."""
    pair: tuple[str, str]
    df: pd.DataFrame   # index: DatetimeIndex (daily), колонки ниже


def load_pair_df(
    pair: tuple[str, str],
    panel: dict | None = None,
) -> pd.DataFrame | None:
    """Загрузить DF для пары: оба coin + BTC + residuals + funding.

    Колонки результата:
      log_price_a, log_price_b, log_price_btc — сырые log-цены
      resid_a, resid_b                         — BTC-нейтральные остатки
      funding_a, funding_b                     — дневной сум. funding rate
      btc_ret                                  — дневной return BTC (forward proxy)

    Seam-safe: rolling OLS на окне BTC_RESID_WINDOW не смотрит вперёд.
    Purge должен быть >= BTC_RESID_WINDOW (делается в run_pairs.py).
    """
    a, b = pair
    coins_needed = sorted({a, b, "BTC"})

    if panel is None:
        panel = load_panel(coins_needed)

    price = panel["price"]
    funding = panel["funding"]

    # Проверка наличия данных
    for c in coins_needed:
        if c not in price.columns:
            return None
    # Убрать строки где BTC или любая нога = NaN
    mask = price[a].notna() & price[b].notna() & price["BTC"].notna()
    df = price.loc[mask, [a, b, "BTC"]].copy()
    if len(df) < BTC_RESID_WINDOW * 2:
        return None

    # Log-prices
    df["log_price_a"] = np.log(df[a])
    df["log_price_b"] = np.log(df[b])
    df["log_price_btc"] = np.log(df["BTC"])

    # Funding (дневной суммарный)
    fa = funding[a].reindex(df.index).fillna(0.0)
    fb = funding[b].reindex(df.index).fillna(0.0)
    df["funding_a"] = fa
    df["funding_b"] = fb

    # BTC daily return (lag 0 = today's return)
    df["btc_ret"] = df["BTC"].pct_change()

    # BTC-нейтральные остатки через rolling OLS
    df["resid_a"] = _btc_residual(df["log_price_a"].values, df["log_price_btc"].values)
    df["resid_b"] = _btc_residual(df["log_price_b"].values, df["log_price_btc"].values)

    # Убрать начальные строки где residuals = NaN (warm-up rolling OLS)
    df = df.dropna(subset=["resid_a", "resid_b"])

    # Clean up raw price columns (не нужны снаружи)
    df = df.drop(columns=[a, b, "BTC"])
    return df


def _btc_residual(log_price: np.ndarray, log_btc: np.ndarray) -> np.ndarray:
    """Rolling OLS: log_price_t ~ α + β * log_btc_t (на окне BTC_RESID_WINDOW).

    Возвращает остатки той же длины что и input (NaN в первых W-1 строках).
    Seam-safe: каждый остаток использует только данные <=t (rolling-окно).
    """
    n = len(log_price)
    W = BTC_RESID_WINDOW
    resid = np.full(n, np.nan)
    for t in range(W - 1, n):
        y = log_price[t - W + 1: t + 1]
        x = log_btc[t - W + 1: t + 1]
        # OLS: [α, β] = (X'X)^{-1} X'y
        xm = np.column_stack([np.ones(W), x])
        try:
            coef, _, _, _ = np.linalg.lstsq(xm, y, rcond=None)
        except np.linalg.LinAlgError:
            continue
        alpha, beta = coef
        resid[t] = log_price[t] - alpha - beta * log_btc[t]
    return resid


def load_all_pairs(
    pairs: list[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], pd.DataFrame]:
    """Загрузить все пары пула. Возвращает {pair: df} только для работающих пар."""
    if pairs is None:
        pairs = CANDIDATE_PAIRS

    # Загрузить весь panel один раз
    all_c = sorted({c for p in pairs for c in p} | {"BTC"})
    try:
        panel = load_panel(all_c)
    except Exception as e:
        print(f"Warning: load_panel failed ({e}), will skip missing coins")
        # fallback: загружать по одной
        panel = None

    result: dict[tuple[str, str], pd.DataFrame] = {}
    for pair in pairs:
        try:
            df = load_pair_df(pair, panel=panel)
            if df is not None and len(df) >= 200:
                result[pair] = df
        except Exception as e:
            print(f"  skip {pair}: {e}")
    return result


# ── self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Candidate pairs ({len(CANDIDATE_PAIRS)}):")
    for p in CANDIDATE_PAIRS:
        print(f"  {p[0]}/{p[1]}")

    print(f"\nAll universe coins ({len(ALL_UNIVERSE_COINS)}): {ALL_UNIVERSE_COINS}")

    print("\nLoading SOL/AVAX pair...")
    df = load_pair_df(("SOL", "AVAX"))
    if df is None:
        print("FAILED")
    else:
        print(f"  rows: {len(df)}, cols: {list(df.columns)}")
        print(f"  dates: {df.index[0].date()} -> {df.index[-1].date()}")
        print(f"  resid_a[:5]: {df['resid_a'].values[:5]}")
        print(f"  resid_b[:5]: {df['resid_b'].values[:5]}")
        print(f"  NaN resid_a: {df['resid_a'].isna().sum()}")
        # sanity: residuals should have low correlation with BTC log-price
        corr = np.corrcoef(df["resid_a"].values, df["log_price_btc"].values)[0, 1]
        print(f"  corr(resid_a, log_btc) = {corr:.3f} (expect |corr| << 1 after residualize)")
        assert abs(corr) < 0.5, f"BTC neutralization failed: corr={corr}"
        print("self-test passed.")
