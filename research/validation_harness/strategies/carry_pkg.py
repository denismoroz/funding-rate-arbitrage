"""
Carry-пакет (FRAB-семейство) для КАЛИБРОВКИ стенда.

Зачем: стенд (CPCV+DSR+PBO) ни разу не калибровался против заведомо-рабочей
стратегии. FRAB carry живёт на проде, но через ЭТОТ стенд не прогонялся (его
симулятор baselines — price-return long/flat, funding он не умеет). Этот пакет
добавляет funding-aware симулятор и прогоняет carry как ground-truth «GO»:

  • carry проходит DSR>0.95 → порог честный, price-return NO-GO (pairs/trend/spread)
    реальны;
  • carry проваливает, хотя прибылен live → порог стенда слишком жёсткий, NO-GO
    надо перечитывать.

Это ПРОКСИ эджа FRAB, не точная two-phase логика:
  delta-neutral carry: перп шортим при funding>0 (хеджим спотом → price-return
  сокращается), собираем funding. PnL = собранный funding − косты входа/выхода
  (perp-нога + spot-нога). Сигнал = сглаженный annualized funding; в рынке когда
  |signal| >= threshold; направление = sign(signal).

Seam-safe: signal на ПОЛНОМ df (smooth_funding — только прошлое), срез по сегменту.
PnL без off-by-one: held = позиция предыдущего бара (lag 1), как в baselines.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from costs import Costs, TAKER
from engine import load_data, smooth_funding, TOTAL_CAPITAL, HOURS_PER_YEAR

# Монеты-кандидаты (фильтруются по наличию данных в load()).
COINS = ["BTC", "ETH", "AVAX", "HYPE", "INJ", "LINK", "AAVE", "ARB", "DOGE"]

# Меню конфигов: пороги входа (annualized funding) × окно сглаживания.
THRESHOLDS = (0.0, 0.05, 0.10, 0.20)   # 0/5/10/20% годовых
SMOOTH_WINDOWS = (24, 72, 168)         # часы
SELECTED = ("carry_t0.10_w72")         # FRAB-подобный дефолт


def _signal_ann(df: pd.DataFrame, window: int) -> np.ndarray:
    """Сглаженный annualized funding (та же конвенция, что engine.run_strategy)."""
    rates = df["fundingRate"].values
    return smooth_funding(rates, window) * HOURS_PER_YEAR


def sim_carry(df: pd.DataFrame, signal_ann: np.ndarray, threshold: float,
              costs: Costs, notional: float = TOTAL_CAPITAL) -> np.ndarray:
    """Почасовой PnL delta-neutral carry-харвестера.

    pos[i] = sign(signal)·1[|signal|>=threshold]  — направление сбора funding.
    Зарабатывает позиция ПРЕДЫДУЩЕГО бара (lag 1): held[i]=pos[i-1].
    funding_pnl[i] = held[i]·fundingRate[i]·notional
       (held=+1, т.к. сглаж. funding>0 → шорт перпа → +rate, когда rate реально >0;
        если funding флипнул, пока держим → убыток. Честно, без look-ahead.)
    Косты смены delta-neutral позиции = perp-нога + spot-нога.
    Price-return сокращается хеджем — в PnL не входит.
    """
    rate = df["fundingRate"].values
    s = np.sign(signal_ann)
    inmkt = (np.abs(signal_ann) >= threshold).astype(float)
    pos = s * inmkt

    held = np.empty_like(pos)
    held[0] = 0.0
    held[1:] = pos[:-1]

    funding_pnl = held * rate * notional

    dpos = np.abs(np.diff(np.concatenate([[0.0], pos])))
    fees = dpos * notional * (costs.perp_cost + costs.spot_cost)
    return funding_pnl - fees


class CarryStrategy:
    """Адаптер под contract.Strategy. fit() выбирает (threshold,window) по train Sharpe."""

    def __init__(self, config: tuple[float, int] | None = None, costs: Costs = TAKER):
        self.name = "carry"
        self._cfg = config or (0.10, 72)
        self._costs = costs
        self._sig_cache: dict[tuple[int, int], np.ndarray] = {}

    def _sig(self, df: pd.DataFrame, window: int) -> np.ndarray:
        key = (id(df), window)
        if key not in self._sig_cache:
            self._sig_cache[key] = _signal_ann(df, window)
        return self._sig_cache[key]

    def fit(self, df, train_idx, costs):
        from engine import compute_metrics
        from contract import contiguous_slices
        best, best_sr = self._cfg, -np.inf
        for thr in THRESHOLDS:
            for w in SMOOTH_WINDOWS:
                sig = self._sig(df, w)
                pnl = []
                for seg in contiguous_slices(train_idx):
                    pnl.extend(sim_carry(df.iloc[seg], sig[seg], thr, costs))
                arr = np.asarray(pnl, float)
                if arr.size < 50:
                    continue
                sr = compute_metrics(arr).get("sharpe", -np.inf) or -np.inf
                if np.isfinite(sr) and sr > best_sr:
                    best_sr, best = sr, (thr, w)
        return best

    def simulate(self, df, seg: slice, config, costs):
        thr, w = config or self._cfg
        sig = self._sig(df, w)
        return sim_carry(df.iloc[seg], sig[seg], thr, costs)


class CarryPackage:
    name = "Carry (FRAB-семейство) — КАЛИБРОВКА"
    selected_name = SELECTED
    coins = COINS

    def __init__(self, costs: Costs = TAKER):
        self.costs = costs

    def load(self, coin: str):
        try:
            df = load_data(coin)
            return df if df is not None and len(df) > 2000 else None
        except Exception:
            return None

    def selected(self, coin, df):
        return CarryStrategy((0.10, 72), self.costs)

    def menu(self, coin, df) -> dict[str, pd.Series]:
        out = {}
        for thr in THRESHOLDS:
            for w in SMOOTH_WINDOWS:
                sig = _signal_ann(df, w)
                pnl = sim_carry(df, sig, thr, self.costs)
                out[f"carry_t{thr:.2f}_w{w}"] = pd.Series(pnl, index=df.index)
        return out
