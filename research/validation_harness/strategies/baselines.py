"""
Baseline-стратегии — известный ответ для проверки стенда (Ф6) и дымовых прогонов.

  BuyHold     — купить спот, держать (+стейкинг). Метрики должны совпадать с
                прямым engine.buy_and_hold (тривиальная верификация симулятора).
  AlwaysFlat  — ничего не делает: pnl≡0. Sanity для runner/metrics.

Полный набор эталонов Ф6 (noise / look-ahead cheat) добавим в Ф6.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from costs import Costs
from engine import buy_and_hold


class BuyHold:
    name = "buy_and_hold"

    def __init__(self, staking: float = 0.0):
        self.staking = staking

    def fit(self, df, train_idx, costs: Costs):
        return None

    def simulate(self, df: pd.DataFrame, seg: slice, config, costs: Costs) -> np.ndarray:
        return buy_and_hold(df.iloc[seg], self.staking)


class AlwaysFlat:
    name = "always_flat"

    def fit(self, df, train_idx, costs: Costs):
        return None

    def simulate(self, df: pd.DataFrame, seg: slice, config, costs: Costs) -> np.ndarray:
        return np.zeros(seg.stop - seg.start)
