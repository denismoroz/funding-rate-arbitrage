"""
Ф2 — контракт стратегии + утилиты для несмежных test-сегментов.

Контракт (mask-based, seam-safe):
  Стратегия знает свои внутренности (сигналы, симулятор); стенд знает только две
  операции:
    fit(df, train_idx, costs) -> config   # выбрать конфиг/сигнал ТОЛЬКО по train
    simulate(df, seg, config, costs) -> pnl  # прогон на СМЕЖНОМ test-сегменте

Почему маски, а не df_train/df_test:
  CPCV-train — это несмежные куски (комплемент k групп минус purge). Если склеить
  строки и считать pct_change — сигнал протечёт через шов. Поэтому стратегия
  считает сигналы на ПОЛНОМ df один раз (lookback цел), а train_idx/seg лишь
  отбирают строки. purge≥lookback гарантирует: окно выжившего train-бара не
  дотягивается до test (нет утечки в fit); сигнал test-бара может смотреть в
  прошлое-train — это легитимно (так торгуют live), параметры-то выбраны на train.
"""
from __future__ import annotations

from typing import Any, Protocol

import numpy as np
import pandas as pd

from costs import Costs


class Strategy(Protocol):
    name: str

    def fit(self, df: pd.DataFrame, train_idx: np.ndarray, costs: Costs) -> Any:
        """Выбрать конфиг, используя ТОЛЬКО строки train_idx. Без fit вернуть None."""
        ...

    def simulate(self, df: pd.DataFrame, seg: slice, config: Any, costs: Costs) -> np.ndarray:
        """Почасовой pnl на смежном сегменте df.iloc[seg] при выбранном config."""
        ...


def contiguous_slices(idx: np.ndarray) -> list[slice]:
    """Разбить отсортированный массив индексов на смежные пробеги → список slice.

    CPCV-test из k групп даёт до k смежных кусков; каждый симулируем отдельно,
    чтобы не клеить разорванные во времени отрезки в один equity-путь.
    """
    if idx.size == 0:
        return []
    idx = np.sort(idx)
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [idx.size - 1]))
    return [slice(int(idx[s]), int(idx[e]) + 1) for s, e in zip(starts, ends)]
