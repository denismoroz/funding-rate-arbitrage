"""
Ф7 — пакет Strategy B для прогона через стенд.

B = constant-dollar ratchet спот + бинарный short-hedge по моментум-сигналу
(simulate_constdollar). Меню = моментум-семейство, ЕДИНООБРАЗНО по всем монетам.
Никакого per-coin выбора сигнала — это и был артефакт, раздувавший Calmar
(см. memory project-strategy-b-final). selected = mom14|mom30 (честный дефолт).

Конфиг костов/параметров — как в honest re-run: THR=0.20, cash=4%, lag=1,
slip=5bps (taker), без min-hold/cooldown (lockup убивает timing-эдж).

Ожидание (memory: walk-forward OOS Calmar 1.39→0.32, выбор сигнала нестабилен):
  PBO высокий (выбор лучшего сигнала НЕ переносится), DSR низкий (Sharpe — артефакт
  перебора ~10 сигналов). Численно закрываем B.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from costs import Costs, TAKER
from engine import load_data, STAKING_YIELD
from backtest_b_constdollar import simulate_constdollar

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
THR, CASH, LAG = 0.20, 0.04, 1
MAX_LOOKBACK_H = 60 * 24      # макс. окно в меню → нижняя граница purge

SELECTED = "mom14|mom30"


def _momc(close: np.ndarray, d: int) -> np.ndarray:
    return pd.Series(close).pct_change(d * 24).fillna(0).values < 0


def _donchian(close: np.ndarray, d: int) -> np.ndarray:
    lo = pd.Series(close).rolling(d * 24, min_periods=d * 24).min().shift(1).values
    return np.nan_to_num(close < lo).astype(bool)


def build_menu(close: np.ndarray) -> dict[str, np.ndarray]:
    """Меню hedge-сигналов (bool[n]). Все ≤ 60d lookback (под purge)."""
    m14, m30, m60 = _momc(close, 14), _momc(close, 30), _momc(close, 60)
    return {
        "mom7":          _momc(close, 7),
        "mom14":         m14,
        "mom21":         _momc(close, 21),
        "mom30":         m30,
        "mom45":         _momc(close, 45),
        "mom60":         m60,
        "mom14|mom30":   m14 | m30,
        "2of3_14_30_60": (m14.astype(int) + m30 + m60) >= 2,
        "donchian20":    _donchian(close, 20),
        "donchian55":    _donchian(close, 55),
    }


def _trend_up(close: np.ndarray) -> np.ndarray:
    return pd.Series(close).pct_change(14 * 24).fillna(0).values > 0


class _BStrategy:
    """Адаптер B под контракт стенда: фикс. сигнал, прогон на сегменте CPCV."""
    def __init__(self, name: str, coin: str, costs: Costs):
        self.name = name
        self.coin = coin
        self.costs = costs
        self._cache: tuple[int, np.ndarray, np.ndarray] | None = None

    def _signals(self, df):
        key = id(df)
        if self._cache is None or self._cache[0] != key:
            close = df["close"].values
            self._cache = (key, build_menu(close)[self.name], _trend_up(close))
        return self._cache[1], self._cache[2]

    def fit(self, df, train_idx, costs):     # selected фиксирован — выбора нет
        return None

    def simulate(self, df: pd.DataFrame, seg: slice, config, costs: Costs) -> np.ndarray:
        hedge, refill = self._signals(df)
        sub = df.iloc[seg]
        pnl, _ = simulate_constdollar(
            sub, STAKING_YIELD.get(self.coin, 0.0), hedge[seg],
            rebal_threshold=THR, risk_free_apr=CASH,
            refill_confirm=refill[seg], signal_lag=LAG, slippage=costs.slippage)
        return pnl


class BPackage:
    name = "Strategy B (constant-dollar ratchet + momentum hedge)"
    selected_name = SELECTED
    coins = COINS

    def __init__(self, costs: Costs = TAKER):
        self.costs = costs

    def load(self, coin: str) -> pd.DataFrame:
        return load_data(coin)

    def selected(self, coin, df):
        return _BStrategy(self.selected_name, coin, self.costs)

    def menu(self, coin, df) -> dict[str, pd.Series]:
        close = df["close"].values
        menu = build_menu(close)
        refill = _trend_up(close)
        stk = STAKING_YIELD.get(coin, 0.0)
        out = {}
        for nm, hedge in menu.items():
            pnl, _ = simulate_constdollar(
                df, stk, hedge, rebal_threshold=THR, risk_free_apr=CASH,
                refill_confirm=refill, signal_lag=LAG, slippage=self.costs.slippage)
            out[nm] = pd.Series(pnl, index=df.index)
        return out
