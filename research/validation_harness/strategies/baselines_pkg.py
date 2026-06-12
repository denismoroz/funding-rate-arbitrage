"""
Ф6 — пакеты-эталоны для валидации САМОГО стенда («кто проверяет проверяющего»).

Известные ответы (если стенд их не воспроизводит — баг в стенде, не в стратегии):
  • NOISE-меню (только случайные long/flat сигналы): выбор лучшего IS-конфига НЕ
    переносится → PBO высокий; Sharpe выбранного — артефакт перебора → DSR низкий.
  • CHEAT-пакет (меню = noise + look-ahead, подглядывающий на бар вперёд): чит
    реально предсказывает → он же IS- и OOS-лучший → PBO≈0, DSR≈1.
  • BUY&HOLD: total-return симулятора совпадает с прямым кумулятивом price_return.

Симулятор — векторный long/flat: pnl[i] = pos[i-1]·ret[i]·NOTIONAL − turnover·fee.
Сигналы считаются на ПОЛНОМ df (seam-safe), срез — по сегменту CPCV.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from costs import Costs, TAKER
from engine import load_data, TOTAL_CAPITAL

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
N_NOISE = 8


def sim_longflat(df: pd.DataFrame, pos: np.ndarray, costs: Costs,
                 notional: float = TOTAL_CAPITAL) -> np.ndarray:
    """Векторный long/flat pnl. pos∈[0,1] по барам; торгуем со сдвигом (лаг 1)."""
    ret = df["price_return"].values
    pos = np.asarray(pos, dtype=float)
    held = np.empty_like(pos)
    held[0] = 0.0
    held[1:] = pos[:-1]                       # позиция предыдущего бара
    gross = held * ret * notional
    dpos = np.abs(np.diff(np.concatenate([[0.0], pos])))
    fees = dpos * notional * costs.spot_cost
    return gross - fees


# ── сигналы (pos на полном df) ────────────────────────────────────────────────
def sig_buyhold(df: pd.DataFrame, seed: int = 0) -> np.ndarray:
    return np.ones(len(df))


def sig_noise(df: pd.DataFrame, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.random(len(df)) > 0.5).astype(float)


def sig_lookahead(df: pd.DataFrame, seed: int = 0) -> np.ndarray:
    """ЧИТ: держим лонг, если СЛЕДУЮЩИЙ бар вырастет (заглядывание вперёд)."""
    ret = df["price_return"].values
    pos = np.zeros(len(df))
    pos[:-1] = (ret[1:] > 0).astype(float)
    return pos


# ── адаптер сигнала под контракт стенда (для CPCV OOS) ────────────────────────
class SignalStrategy:
    def __init__(self, name: str, signal_fn, seed: int, costs: Costs = TAKER):
        self.name = name
        self._fn = signal_fn
        self._seed = seed
        self._costs = costs
        self._pos_cache: tuple[int, np.ndarray] | None = None

    def _pos(self, df: pd.DataFrame) -> np.ndarray:
        key = id(df)
        if self._pos_cache is None or self._pos_cache[0] != key:
            self._pos_cache = (key, self._fn(df, self._seed))
        return self._pos_cache[1]

    def fit(self, df, train_idx, costs):     # фиксированные правила — выбора нет
        return None

    def simulate(self, df: pd.DataFrame, seg: slice, config, costs: Costs) -> np.ndarray:
        pos = self._pos(df)[seg]
        return sim_longflat(df.iloc[seg], pos, costs)


# ── пакеты ────────────────────────────────────────────────────────────────────
class _BasePackage:
    coins = COINS

    def __init__(self, costs: Costs = TAKER):
        self.costs = costs

    def load(self, coin: str) -> pd.DataFrame:
        return load_data(coin)

    def _menu_signals(self) -> dict:
        raise NotImplementedError

    def selected(self, coin, df):
        nm = self.selected_name
        fn, seed = self._menu_signals()[nm]
        return SignalStrategy(nm, fn, seed, self.costs)

    def menu(self, coin, df) -> dict[str, pd.Series]:
        out = {}
        for nm, (fn, seed) in self._menu_signals().items():
            pnl = sim_longflat(df, fn(df, seed), self.costs)
            out[nm] = pd.Series(pnl, index=df.index)
        return out


class NoisePackage(_BasePackage):
    name = "ЭТАЛОН: noise-меню"
    selected_name = "noise0"

    def _menu_signals(self):
        return {f"noise{i}": (sig_noise, 100 + i) for i in range(N_NOISE)}


class CheatPackage(_BasePackage):
    name = "ЭТАЛОН: look-ahead cheat"
    selected_name = "lookahead"

    def _menu_signals(self):
        m = {f"noise{i}": (sig_noise, 100 + i) for i in range(N_NOISE)}
        m["lookahead"] = (sig_lookahead, 0)
        m["buyhold"] = (sig_buyhold, 0)
        return m
