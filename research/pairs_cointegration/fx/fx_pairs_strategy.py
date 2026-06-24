"""
FX-вариант стратегии pairs/cointegration mean-reversion.

Переиспользуем логику pairs_strategy.py (seam-safe PnL, timing-фикс) с двумя
FX-специфичными отличиями:
  1. Косты: 2.0 bps/нога spot (не 4.4 bps perp как в крипте)
  2. Held-accrual: carry rate-diff обеих ног (не funding)
       pos=+1: long_a, short_b → accrual = carry_a − carry_b
       pos=-1: short_a, long_b → accrual = carry_b − carry_a
     Т.е. accrual = pos * (carry_a − carry_b)

Seam-safe: kalman_beta и spread считаются на ПОЛНОМ df;
simulate() берёт уже готовые значения через срез.
PnL бара t = позиция, ВОШЕДШАЯ в бар (решённая на t-1),
на forward-движении — без off-by-one бага из README.

Импортируем kalman из родительского пакета (не копируем).
Меню конфигов: те же (z_window × entry_z) что в крипто-версии.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ── sys.path: добавить родительский pairs_cointegration/ для kalman.py ────────
_HERE = Path(__file__).parent
_PAIRS_ROOT = _HERE.parent   # research/pairs_cointegration/
_HARNESS = _PAIRS_ROOT.parent / "validation_harness"
_RESEARCH = _PAIRS_ROOT.parent

for _p in (_PAIRS_ROOT, _HARNESS, _RESEARCH, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from kalman import kalman_beta, KalmanConfig, DEFAULT_Q, DEFAULT_R

# ── Defaults ──────────────────────────────────────────────────────────────────
ENTRY_Z    = 2.0
EXIT_Z     = 0.0
Z_WINDOW   = 60
HALF_LIFE_DEFAULT = 30
TIME_STOP_MULT = 2.5

# FX SPOT bid-ask spread cost: 2.0 bps/нога (PLAN Приложение A)
FX_COST_PER_LEG = 0.00020    # 2.0 bps = 0.00020

# Notional на ногу (dollar-neutral)
PAIR_NOTIONAL = 1000.0


@dataclass(frozen=True)
class FXPairConfig:
    z_window: int
    entry_z: float
    time_stop_bars: int
    kalman: KalmanConfig


# Меню конфигов: те же z_window × entry_z, что в крипте
Z_WINDOWS  = (30, 45, 60, 90)
ENTRY_ZS   = (1.5, 2.0, 2.5)


def _all_menu_configs() -> dict[str, FXPairConfig]:
    cfgs: dict[str, FXPairConfig] = {}
    for zw in Z_WINDOWS:
        for ez in ENTRY_ZS:
            name = f"z{zw}_e{ez:.1f}"
            cfgs[name] = FXPairConfig(
                z_window=zw,
                entry_z=ez,
                time_stop_bars=int(HALF_LIFE_DEFAULT * TIME_STOP_MULT),
                kalman=KalmanConfig(q=DEFAULT_Q, R=DEFAULT_R),
            )
    return cfgs


MENU_CONFIGS = _all_menu_configs()
SELECTED_CONFIG_NAME = f"z{Z_WINDOW}_e{ENTRY_Z:.1f}"


def compute_spread_and_z(
    df: pd.DataFrame,
    z_window: int,
    kalman_cfg: KalmanConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Полный препроцессинг пары: β, спред, z-score.

    Seam-safe: считается на ПОЛНОМ df, не на срезе.

    Returns (beta, spread, z, alpha).
    Spread S_t = resid_a_t − β_t * resid_b_t − α_t (USD-нейтральные остатки).
    """
    ra = df["resid_a"].values
    rb = df["resid_b"].values

    beta, alpha = kalman_beta(ra, rb, q=kalman_cfg.q, R=kalman_cfg.R)
    spread = ra - beta * rb - alpha

    n = len(spread)
    z = np.full(n, np.nan)
    for t in range(z_window - 1, n):
        s = spread[t - z_window + 1: t + 1]
        mu = np.mean(s)
        sigma = np.std(s, ddof=1)
        if sigma > 1e-12:
            z[t] = (spread[t] - mu) / sigma
        else:
            z[t] = 0.0

    return beta, spread, z, alpha


def precompute_signals(df: pd.DataFrame, config: FXPairConfig) -> pd.DataFrame:
    """Добавить _beta, _alpha, _spread, _z в df на ПОЛНОМ ряду (in-place)."""
    beta, spread, z, alpha = compute_spread_and_z(df, config.z_window, config.kalman)
    df["_beta"] = beta
    df["_alpha"] = alpha
    df["_spread"] = spread
    df["_z"] = z
    return df


def simulate_pair(
    df: pd.DataFrame,
    seg: slice,
    config: FXPairConfig,
    notional: float = PAIR_NOTIONAL,
) -> np.ndarray:
    """Симулировать PnL FX-пары на смежном сегменте df.iloc[seg].

    Seam-safe:
      - _beta/_z/_spread уже посчитаны на ПОЛНОМ df (precompute_signals).
      - seg — смежный срез, берём только эти строки.

    PnL alignment (timing-фикс, без off-by-one):
      pos[i] решается по z[i] (известно на закрытии бара i, т.е. «конец дня i»)
      PnL бара i зарабатывает позиция, ВОШЕДШАЯ в бар (решённая на i-1),
      на движении (i-1 → i]: это fwd_ret[i] = log_spot[i] - log_spot[i-1].

    Gross PnL:
      pos=+1: long_a, short_b → notional * (ret_a[i] - β[i] * ret_b[i])
      (dollar-neutral spread return)

    Carry accrual:
      carry_a[i], carry_b[i] = per-bday rate diff vs USD
      accrual = pos * (carry_a - carry_b) * notional
      (pos=+1: long_a получает carry_a, short_b «платит» carry_b → net = carry_a − carry_b)

    Cosты: 2.0 bps/нога при открытии И закрытии (2 ноги каждый раз).
    """
    if "_z" not in df.columns:
        raise ValueError("Нужно предварительно вызвать precompute_signals(df, config)")

    sub = df.iloc[seg]
    n_sub = len(sub)
    if n_sub == 0:
        return np.array([])

    # Данные всего df (для log-returns из полного ряда)
    lsa = df["log_spot_a"].values
    lsb = df["log_spot_b"].values
    # dlog return: ret[i] = log_spot[i] - log_spot[i-1]
    # prepend[0] → ret[0] = 0 (нет движения на первом баре — нет проблем т.к. pos=0 там)
    ret_a = np.diff(lsa, prepend=lsa[0])
    ret_b = np.diff(lsb, prepend=lsb[0])

    carry_a_full = df["carry_a"].values
    carry_b_full = df["carry_b"].values
    beta_full    = df["_beta"].values
    z_full       = df["_z"].values

    sl = slice(seg.start, seg.stop)
    ret_a_seg   = ret_a[sl]
    ret_b_seg   = ret_b[sl]
    carry_a_seg = carry_a_full[sl]
    carry_b_seg = carry_b_full[sl]
    beta_seg    = beta_full[sl]
    z_seg       = z_full[sl]

    pnl = np.zeros(n_sub)
    pos = 0          # +1 long_a/short_b, -1 short_a/long_b, 0 flat
    bars_held = 0
    entry_z = config.entry_z
    time_stop = config.time_stop_bars

    for i in range(n_sub):
        zi = z_seg[i]
        bi = beta_seg[i]

        if np.isnan(zi):
            pnl[i] = 0.0
            continue

        # ── PnL бара i зарабатывает позиция, вошедшая в бар (решённая на i-1).
        # Считаем ДО апдейта pos по z[i]. (timing-фикс: без off-by-one)
        if pos != 0:
            # Gross: dollar-neutral spread return
            gross = float(pos) * (ret_a_seg[i] - bi * ret_b_seg[i]) * notional
            # Carry accrual: pos=+1 → carry_a − carry_b; pos=-1 → инверсия
            accrual = float(pos) * (carry_a_seg[i] - carry_b_seg[i]) * notional
        else:
            gross = 0.0
            accrual = 0.0

        prev_pos = pos

        # ── Апдейт позиции по z[i] (зарабатывает начиная с i+1) ──────────
        if pos != 0:
            bars_held += 1
            exit_cond = (
                (pos == 1 and zi >= EXIT_Z) or
                (pos == -1 and zi <= EXIT_Z) or
                (bars_held >= time_stop)
            )
            if exit_cond:
                pos = 0
                bars_held = 0

        if pos == 0:
            if zi < -entry_z:
                pos = 1    # long_a / short_b
                bars_held = 0
            elif zi > entry_z:
                pos = -1   # short_a / long_b
                bars_held = 0

        # Транзакционные косты (при смене позиции)
        turnover = abs(pos - prev_pos)   # 0, 1, или 2 (флип)
        cost = turnover * notional * FX_COST_PER_LEG * 2   # 2 ноги

        pnl[i] = gross + accrual - cost

    return pnl


def estimate_half_life(spread: np.ndarray) -> float:
    """OU half-life через OLS авторегрессию ΔS = a + b·S_{t-1}."""
    s = spread[~np.isnan(spread)]
    if len(s) < 10:
        return HALF_LIFE_DEFAULT
    ds = np.diff(s)
    s_lag = s[:-1]
    xm = np.column_stack([np.ones(len(s_lag)), s_lag])
    try:
        coef, _, _, _ = np.linalg.lstsq(xm, ds, rcond=None)
    except np.linalg.LinAlgError:
        return HALF_LIFE_DEFAULT
    b = coef[1]
    if b >= 0 or b <= -1:
        return HALF_LIFE_DEFAULT
    hl = -np.log(2) / np.log(1 + b)
    return float(np.clip(hl, 5, 180))


class FXPairsStrategy:
    """Адаптер под contract.Strategy для одной FX-пары."""

    def __init__(
        self,
        pair_id: str,
        config_name: str = SELECTED_CONFIG_NAME,
        menu_configs: dict[str, FXPairConfig] | None = None,
    ):
        self.name = f"fx_pairs_{config_name}_{pair_id}"
        self._config_name = config_name
        self._menu_configs = menu_configs or MENU_CONFIGS
        self._cache_key: tuple | None = None

    def fit(
        self,
        df: pd.DataFrame,
        train_idx: np.ndarray,
        costs: Any,
    ) -> str:
        """Выбрать лучший config по Sharpe на train_idx."""
        from engine import compute_metrics
        from contract import contiguous_slices

        best_name = self._config_name
        best_sr = -np.inf

        for name, cfg in self._menu_configs.items():
            df_work = df.copy()
            precompute_signals(df_work, cfg)
            train_pnl: list[float] = []
            for seg in contiguous_slices(train_idx):
                if seg.stop - seg.start < 5:
                    continue
                p = simulate_pair(df_work, seg, cfg)
                if len(p) > 0:
                    train_pnl.extend(p.tolist())
            arr = np.array(train_pnl, dtype=float)
            if arr.size < 20:
                continue
            m = compute_metrics(arr)
            sr = m.get("sharpe", -np.inf) or -np.inf
            if np.isfinite(sr) and sr > best_sr:
                best_sr = sr
                best_name = name

        return best_name

    def simulate(
        self,
        df: pd.DataFrame,
        seg: slice,
        config: str,
        costs: Any,
    ) -> np.ndarray:
        cfg_name = config or self._config_name
        cfg = self._menu_configs.get(cfg_name, self._menu_configs[self._config_name])

        cache_key = (id(df), cfg_name)
        if self._cache_key != cache_key:
            precompute_signals(df, cfg)
            self._cache_key = cache_key

        return simulate_pair(df, seg, cfg)


# ── self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from fx_pairs_data import load_pair_df

    pair = ("AUD", "NZD")
    df = load_pair_df(pair)
    assert df is not None, "AUD/NZD не загрузилась"
    print(f"pair {pair}: {len(df)} days")

    cfg = MENU_CONFIGS[SELECTED_CONFIG_NAME]
    precompute_signals(df, cfg)

    hl = estimate_half_life(df["_spread"].values)
    print(f"half-life: {hl:.1f} days")

    seg = slice(0, len(df))
    pnl = simulate_pair(df, seg, cfg)
    print(f"pnl shape: {pnl.shape}, total: {pnl.sum():.2f}, mean/day: {pnl.mean():.4f}")
    print("self-test passed.")
