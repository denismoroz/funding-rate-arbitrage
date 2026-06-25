"""
Ф4 — Стратегия для одной пары: спред/z-score/входы/time-stop/pnl + funding accrual.

Логика:
  1. Spread S_t = resid_a_t - β_t * resid_b_t   (на BTC-нейтральных остатках)
  2. z-score = (S_t - roll_mean) / roll_std       (rolling окно Z_WINDOW)
  3. Вход long(A)/short(B) при z < -ENTRY_Z;
     вход short(A)/long(B) при z > +ENTRY_Z
  4. Выход при z пересекает 0 (mean-reversion)
  5. Time-stop: если позиция не закрылась за TIME_STOP_BARS баров → принудительный выход
  6. PnL dollar-neutral: notional/2 в каждую ногу
  7. Косты: perp TAKER ~4.4 bps/нога (PLAN §4) — при открытии И закрытии
  8. Held-funding accrual: long-нога получает +funding, short-нога платит -funding

Seam-safe: kalman_beta и spread считаются на ПОЛНОМ df один раз;
simulate() берёт уже готовые значения через срез df.iloc[seg].
fit() выбирает z-окно / entry-z / half-life только по train_idx.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from kalman import kalman_beta, fit_kalman_noise, KalmanConfig, DEFAULT_Q, DEFAULT_R

# ── Defaults (PLAN §4) ────────────────────────────────────────────────────────
ENTRY_Z    = 2.0      # пороги ±2σ для входа
EXIT_Z     = 0.0      # выход при пересечении нуля
Z_WINDOW   = 60       # дней для rolling z-score (default)
# half-life OU определяет time-stop в fit(); default = 30 дней
HALF_LIFE_DEFAULT = 30
TIME_STOP_MULT = 2.5  # time-stop = 2.5 × half-life (PLAN: 2–3)

# Perp TAKER cost: 3.5 bps + ~0.9 bps slippage = 4.4 bps/нога (PLAN §4)
PERP_COST_PER_LEG = 0.00044   # доля от notional

# Notional для каждой ноги (dollar-neutral пара)
PAIR_NOTIONAL = 1000.0   # USD на ногу


@dataclass(frozen=True)
class PairConfig:
    z_window: int
    entry_z: float
    time_stop_bars: int
    kalman: KalmanConfig


# Menu конфигов: разные z_window (дефолт + вариации)
Z_WINDOWS   = (30, 45, 60, 90)
ENTRY_ZS    = (1.5, 2.0, 2.5)

def _all_menu_configs() -> dict[str, PairConfig]:
    """Все конфиги меню для PBO/DSR. Kalman q/R — apriorные (не фитить по меню)."""
    cfgs: dict[str, PairConfig] = {}
    for zw in Z_WINDOWS:
        for ez in ENTRY_ZS:
            name = f"z{zw}_e{ez:.1f}"
            cfgs[name] = PairConfig(
                z_window=zw,
                entry_z=ez,
                time_stop_bars=int(HALF_LIFE_DEFAULT * TIME_STOP_MULT),
                kalman=KalmanConfig(q=DEFAULT_Q, R=DEFAULT_R),
            )
    return cfgs


MENU_CONFIGS = _all_menu_configs()
SELECTED_CONFIG_NAME = f"z{Z_WINDOW}_e{ENTRY_Z:.1f}"   # default


def compute_spread_and_z(
    df: pd.DataFrame,
    z_window: int,
    kalman_cfg: KalmanConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Полный препроцессинг пары: β, спред, z-score.

    Seam-safe: считается на ПОЛНОМ df, не на срезе.

    Returns
    -------
    beta   : (n,) time-varying β (Kalman)
    spread : (n,) S_t = resid_a - β_t * resid_b
    z      : (n,) rolling z-score of spread (NaN в первых z_window-1 барах)
    alpha  : (n,) Kalman intercept
    """
    ra = df["resid_a"].values
    rb = df["resid_b"].values

    beta, alpha = kalman_beta(ra, rb, q=kalman_cfg.q, R=kalman_cfg.R)
    spread = ra - beta * rb - alpha

    # Rolling z-score
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


def simulate_pair(
    df: pd.DataFrame,
    seg: slice,
    config: PairConfig,
    notional: float = PAIR_NOTIONAL,
) -> np.ndarray:
    """Симулировать pnl пары на смежном сегменте df.iloc[seg].

    Seam-safe:
      - beta/spread/z уже посчитаны на ПОЛНОМ df (передаём через колонки),
        если они ещё не в df — считаем на полном df сначала.
      - seg — смежный срез (start:stop), берём только эти строки.

    PnL на каждом баре (дневной):
      position: +1 = long_a/short_b, -1 = short_a/long_b, 0 = flat
      gross_pnl = pos * (ret_a - β * ret_b) * notional   (price-return нейтральный к BTC)
      funding_accrual = -pos * (funding_a - funding_b) * notional
        (long pays positive funding: long_a ПЛАТИТ fund_a, short_b ПОЛУЧАЕТ fund_b →
         net = -(fund_a - fund_b) for pos=+1; знак -pos*(...) — канонично, как
         accrual = -funding в survivorship.py / cross_sectional)
      cost_pnl = -|Δpos| * notional * 2 * PERP_COST_PER_LEG   (2 ноги при входе/выходе)
    """
    # Препроцессинг на полном df (seam-safe)
    if "_z" not in df.columns:
        raise ValueError("Нужно предварительно вызвать precompute_signals(df, config)")

    sub = df.iloc[seg]
    n_sub = len(sub)
    if n_sub == 0:
        return np.array([])

    z_full = df["_z"].values
    beta_full = df["_beta"].values
    alpha_full = df["_alpha"].values

    # Данные подсегмента
    z = sub["_z"].values
    beta = sub["_beta"].values

    # log-price returns (дневные)
    lpa = df["log_price_a"].values
    lpb = df["log_price_b"].values
    # dlog = log(P_t/P_{t-1}) ≈ ret
    ret_a = np.diff(lpa, prepend=lpa[0])
    ret_b = np.diff(lpb, prepend=lpb[0])

    funding_a = df["funding_a"].values
    funding_b = df["funding_b"].values

    # slice to segment
    sl = slice(seg.start, seg.stop)
    ret_a_seg  = ret_a[sl]
    ret_b_seg  = ret_b[sl]
    fund_a_seg = funding_a[sl]
    fund_b_seg = funding_b[sl]

    pnl = np.zeros(n_sub)
    pos = 0          # +1 long_a/short_b, -1 short_a/long_b, 0 flat
    bars_held = 0
    entry_z = config.entry_z
    time_stop = config.time_stop_bars

    for i in range(n_sub):
        zi = z[i]
        bi = beta[i]

        if np.isnan(zi):
            # warm-up — нет сигнала
            pnl[i] = 0.0
            continue

        # ── PnL бара i зарабатывается позицией, ВОШЕДШЕЙ в бар i (решённой на i-1),
        #    на движении за (i-1, i]. Позиция предшествует return'у (seam-safe,
        #    как held[t]·fwd_ret[t] в fx_pkg). Считаем ДО апдейта pos по z[i].
        if pos != 0:
            # Gross: dollar-neutral spread return
            # pos=+1: long_a, short_b → notional*(ret_a - bi*ret_b)
            # pos=-1: short_a, long_b → notional*(-ret_a + bi*ret_b)
            gross = float(pos) * (ret_a_seg[i] - bi * ret_b_seg[i]) * notional
            # Funding accrual за удержание через бар (long PAYS positive funding):
            # pos=+1: long_a ПЛАТИТ fund_a, short_b ПОЛУЧАЕТ fund_b → net = -(fund_a - fund_b)
            # pos=-1: знак инвертируется → +pos*(fund_a - fund_b), т.е. всегда -pos*(...)
            net_fund = -float(pos) * (fund_a_seg[i] - fund_b_seg[i]) * notional
        else:
            gross = 0.0
            net_fund = 0.0

        prev_pos = pos

        # ── Апдейт позиции по z[i] (известно на закрытии i; зарабатывает с i+1) ──
        # Выход по сигналу (z пересёк 0 в сторону ноля)
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

        # Вход (только если flat)
        if pos == 0:
            if zi < -entry_z:
                pos = 1   # long_a / short_b
                bars_held = 0
            elif zi > entry_z:
                pos = -1  # short_a / long_b
                bars_held = 0

        # Transaction costs (при смене позиции на этом баре)
        turnover = abs(pos - prev_pos)   # 0, 1, или 2 (флип через flat)
        cost = turnover * notional * PERP_COST_PER_LEG * 2   # 2 ноги

        pnl[i] = gross + net_fund - cost

    return pnl


def precompute_signals(df: pd.DataFrame, config: PairConfig) -> pd.DataFrame:
    """Добавить в df колонки _beta, _alpha, _spread, _z на ПОЛНОМ ряду.

    Вызывать один раз перед всеми simulate() на разных сегментах.
    Изменяет df in-place и возвращает его.
    """
    beta, spread, z, alpha = compute_spread_and_z(df, config.z_window, config.kalman)
    df["_beta"] = beta
    df["_alpha"] = alpha
    df["_spread"] = spread
    df["_z"] = z
    return df


def estimate_half_life(spread: np.ndarray) -> float:
    """OU half-life по OLS авторегрессии: ΔS_t = θ*(μ - S_{t-1}) + ε.

    Возвращает half-life = -ln(2)/ln(1-θ) в барах.
    """
    s = spread[~np.isnan(spread)]
    if len(s) < 10:
        return HALF_LIFE_DEFAULT
    ds = np.diff(s)
    s_lag = s[:-1]
    # OLS: ds = a + b*s_lag
    xm = np.column_stack([np.ones(len(s_lag)), s_lag])
    try:
        coef, _, _, _ = np.linalg.lstsq(xm, ds, rcond=None)
    except np.linalg.LinAlgError:
        return HALF_LIFE_DEFAULT
    b = coef[1]
    if b >= 0 or b <= -1:
        return HALF_LIFE_DEFAULT
    hl = -np.log(2) / np.log(1 + b)
    # Clamp to reasonable range [5, 180]
    return float(np.clip(hl, 5, 180))


class PairsStrategy:
    """Адаптер под contract.Strategy для одной пары.

    Seam-safe:
    - fit() выбирает лучший config_name ТОЛЬКО по train_idx
    - simulate() использует precomputed _beta/_z на ПОЛНОМ df (переданном)
    """

    name: str

    def __init__(
        self,
        pair_id: str,
        config_name: str = SELECTED_CONFIG_NAME,
        menu_configs: dict[str, PairConfig] | None = None,
    ):
        self.name = f"pairs_{config_name}_{pair_id}"
        self._config_name = config_name
        self._menu_configs = menu_configs or MENU_CONFIGS
        self._df_id: int | None = None   # for caching precomputed signals

    def fit(
        self,
        df: pd.DataFrame,
        train_idx: np.ndarray,
        costs,
    ) -> str:
        """Выбрать лучший config по train_idx (по Sharpe на train).

        Returns config_name.
        """
        from engine import compute_metrics  # импорт внутри, чтобы не циклить sys.path

        best_name = self._config_name
        best_sr = -np.inf

        for name, cfg in self._menu_configs.items():
            # Precompute signals на ПОЛНОМ df
            df_work = df.copy()
            precompute_signals(df_work, cfg)

            # PnL только на train_idx
            train_pnl = []
            # run simulate on train contiguous slices
            from contract import contiguous_slices
            for seg in contiguous_slices(train_idx):
                p = simulate_pair(df_work, seg, cfg)
                train_pnl.extend(p)

            arr = np.array(train_pnl, dtype=float)
            if arr.size < 20:
                continue
            m = compute_metrics(arr)
            sr = m.get("sharpe", -np.inf)
            if sr > best_sr:
                best_sr = sr
                best_name = name

        return best_name

    def simulate(
        self,
        df: pd.DataFrame,
        seg: slice,
        config: str,
        costs,
    ) -> np.ndarray:
        """PnL на смежном сегменте, используя config_name.

        Сигналы пересчитываются на ПОЛНОМ df при каждом вызове с новым df
        (кэшируем по id(df)).
        """
        cfg = self._menu_configs.get(config or self._config_name, self._menu_configs[self._config_name])

        # Кэш precomputed signals (один df — один прогон)
        df_id = id(df)
        if self._df_id != df_id or "_z" not in df.columns:
            precompute_signals(df, cfg)
            self._df_id = df_id
        elif df.get("_cfg_name") != config:
            # Конфиг изменился — пересчитать
            precompute_signals(df, cfg)
            df["_cfg_name"] = config
            self._df_id = df_id

        return simulate_pair(df, seg, cfg)


# ── self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "validation_harness"))
    sys.path.insert(0, str(Path(__file__).parent.parent))
    sys.path.insert(0, str(Path(__file__).parent.parent / "cross_sectional" / "crypto"))

    from pairs_data import load_pair_df

    pair = ("SOL", "AVAX")
    df = load_pair_df(pair)
    print(f"pair {pair}: {len(df)} days")

    cfg = MENU_CONFIGS[SELECTED_CONFIG_NAME]
    precompute_signals(df, cfg)

    # half-life
    hl = estimate_half_life(df["_spread"].values)
    print(f"half-life: {hl:.1f} days")

    # Simulate on full df
    seg = slice(0, len(df))
    pnl = simulate_pair(df, seg, cfg)
    print(f"pnl shape: {pnl.shape}, total: {pnl.sum():.2f}, mean/day: {pnl.mean():.4f}")

    n_trades = (np.abs(np.diff(np.sign(df['_z'].fillna(0).values))) > 0).sum()
    print(f"z-crossings (proxy trades): {n_trades}")
    print("self-test passed.")
