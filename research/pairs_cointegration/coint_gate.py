"""
Ф5 — Коинтеграционный гейт.

Engle-Granger / ADF на BTC-нейтральных спредах:
  - p-value стационарности по подпериодам (ADF на rolling-окнах)
  - стабильность β (дрейф Kalman-state)
  - half-life (OU)
  - число mean-crossings (proxy торгуемости)

Назначение — отключать развалившиеся пары, а не искать новые.
Тесты проводятся ТОЛЬКО на train_idx — seam-safe.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller, coint


@dataclass
class CointReport:
    pair: tuple[str, str]
    eg_pval: float         # Engle-Granger p-value на train-остатках
    adf_mean_pval: float   # среднее ADF p-value по sub-окнам
    adf_frac_sig: float    # доля sub-окон с ADF p < 0.10
    half_life: float       # OU half-life (баров)
    mean_crossings: int    # число пересечений нуля спреда (train)
    beta_drift: float      # std Kalman-β на train (мера дрейфа)
    is_cointegrated: bool  # вердикт (ADF p < 0.10 в > 50% окон)


def adf_pval(series: np.ndarray) -> float:
    """ADF p-value для ряда (одностороннее: тест на единичный корень)."""
    s = series[np.isfinite(series)]
    if len(s) < 20:
        return 1.0
    try:
        result = adfuller(s, autolag="AIC", regression="c")
        return float(result[1])
    except Exception:
        return 1.0


def eg_pval(resid_a: np.ndarray, resid_b: np.ndarray) -> float:
    """Engle-Granger cointegration p-value (через statsmodels.coint)."""
    mask = np.isfinite(resid_a) & np.isfinite(resid_b)
    a, b = resid_a[mask], resid_b[mask]
    if len(a) < 30:
        return 1.0
    try:
        _, pval, _ = coint(a, b, autolag="AIC")
        return float(pval)
    except Exception:
        return 1.0


def half_life_ou(spread: np.ndarray) -> float:
    """OU half-life через OLS авторегрессию ΔS = θ*(μ - S_{t-1}) + ε."""
    s = spread[np.isfinite(spread)]
    if len(s) < 10:
        return 9999.0
    ds = np.diff(s)
    s_lag = s[:-1]
    xm = np.column_stack([np.ones(len(s_lag)), s_lag])
    try:
        coef, _, _, _ = np.linalg.lstsq(xm, ds, rcond=None)
    except np.linalg.LinAlgError:
        return 9999.0
    b = coef[1]
    if b >= 0 or b <= -2:
        return 9999.0
    hl = -np.log(2) / np.log(1 + b)
    return float(np.clip(hl, 1, 9999))


def mean_crossings(spread: np.ndarray) -> int:
    """Число пересечений нуля (mean=0) спреда."""
    s = spread[np.isfinite(spread)]
    if len(s) < 2:
        return 0
    signs = np.sign(s - np.mean(s))
    return int(np.sum(np.abs(np.diff(signs)) > 0))


def check_pair(
    df: pd.DataFrame,
    train_idx: np.ndarray,
    pair: tuple[str, str],
    *,
    n_sub_windows: int = 4,
) -> CointReport:
    """Оценить коинтегрированность пары на train_idx.

    Параметры
    ----------
    df        : полный df пары (из pairs_data.load_pair_df)
    train_idx : индексы train (от CPCV splitter)
    pair      : (coin_a, coin_b) — для идентификации
    n_sub_windows : число подпериодов для rolling ADF
    """
    train_df = df.iloc[train_idx]

    ra = train_df["resid_a"].values
    rb = train_df["resid_b"].values

    # Engle-Granger
    ep = eg_pval(ra, rb)

    # Rolling ADF на спреде (простой спред = resid_a - resid_b, без Kalman для быстроты)
    simple_spread = ra - rb
    n = len(simple_spread)
    sub_w = max(30, n // n_sub_windows)
    adf_pvals = []
    for start in range(0, n - sub_w + 1, sub_w):
        piece = simple_spread[start: start + sub_w]
        adf_pvals.append(adf_pval(piece))

    adf_pvals_arr = np.array(adf_pvals)
    adf_mean = float(np.mean(adf_pvals_arr)) if len(adf_pvals_arr) else 1.0
    adf_frac = float(np.mean(adf_pvals_arr < 0.10)) if len(adf_pvals_arr) else 0.0

    # Half-life
    hl = half_life_ou(simple_spread)

    # Mean crossings
    mc = mean_crossings(simple_spread)

    # Beta drift (Kalman): если _beta в df, берём на train
    if "_beta" in df.columns:
        beta_train = df["_beta"].values[train_idx]
        beta_drift = float(np.nanstd(beta_train))
    else:
        beta_drift = float("nan")

    is_coint = adf_frac > 0.5

    return CointReport(
        pair=pair,
        eg_pval=ep,
        adf_mean_pval=adf_mean,
        adf_frac_sig=adf_frac,
        half_life=hl,
        mean_crossings=mc,
        beta_drift=beta_drift,
        is_cointegrated=is_coint,
    )


def filter_pairs(
    pairs_data: dict[tuple[str, str], pd.DataFrame],
    train_idx_map: dict[tuple[str, str], np.ndarray] | None = None,
) -> tuple[list[tuple[str, str]], list[CointReport]]:
    """Прогнать гейт на всех парах, вернуть отфильтрованный список + репорты.

    train_idx_map: если None, использует весь ряд (для предварительного скрининга).
    """
    passed = []
    reports = []
    for pair, df in pairs_data.items():
        if train_idx_map is not None:
            t_idx = train_idx_map.get(pair, np.arange(len(df)))
        else:
            t_idx = np.arange(len(df))
        rep = check_pair(df, t_idx, pair)
        reports.append(rep)
        if rep.is_cointegrated:
            passed.append(pair)
    return passed, reports


def print_coint_report(reports: list[CointReport]) -> None:
    print(f"\n{'pair':<15}{'EG_p':>8}{'ADF_mean':>10}{'ADF%sig':>9}{'HL':>8}{'Xings':>7}{'coint':>7}")
    for r in sorted(reports, key=lambda x: x.adf_mean_pval):
        flag = "YES" if r.is_cointegrated else "no"
        print(f"  {r.pair[0]+'/'+r.pair[1]:<13}{r.eg_pval:>8.3f}{r.adf_mean_pval:>10.3f}"
              f"{r.adf_frac_sig:>9.2f}{r.half_life:>8.1f}{r.mean_crossings:>7}{flag:>7}")


# ── self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "cross_sectional" / "crypto"))
    sys.path.insert(0, str(Path(__file__).parent))
    from pairs_data import load_pair_df

    rng = np.random.default_rng(0)

    # Test 1: stationary spread → should be cointegrated
    n = 500
    spread_stat = rng.standard_normal(n) * 0.05   # I(0)
    pval = adf_pval(spread_stat)
    print(f"Stationary ADF p={pval:.4f} (expect < 0.05)")
    assert pval < 0.05, pval

    # Test 2: random walk spread → should NOT be cointegrated
    spread_rw = rng.standard_normal(n).cumsum() * 0.1
    pval_rw = adf_pval(spread_rw)
    print(f"Random walk ADF p={pval_rw:.4f} (expect > 0.1)")
    assert pval_rw > 0.1, pval_rw

    # Test 3: on real pair
    pair = ("SOL", "AVAX")
    df = load_pair_df(pair)
    if df is not None:
        t_idx = np.arange(len(df) // 2)  # train = first half
        rep = check_pair(df, t_idx, pair)
        print(f"\nSOL/AVAX: EG_p={rep.eg_pval:.3f} ADF_mean={rep.adf_mean_pval:.3f} "
              f"HL={rep.half_life:.1f} crossings={rep.mean_crossings} coint={rep.is_cointegrated}")

    print("self-test passed.")
