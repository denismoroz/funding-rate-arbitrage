"""Trend signals and sizing — a line-for-line port of research/trend_following/trend.py.

Everything is causal: a decision made on day D uses daily closes up to and including D-1's candle, the
last one closed when the book rebalances at 00:00 UTC of day D.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from frab.strategy.trend.params import TrendParams


def tsmom_sign(closes: Sequence[float], lookback: int) -> float:
    """sign(close[-1] / close[-1 - lookback] - 1); flat when the history is too short."""
    if len(closes) < lookback + 1:
        return 0.0
    past = closes[-1 - lookback]
    if past <= 0:
        return 0.0
    r = closes[-1] / past - 1.0
    return 1.0 if r > 0 else (-1.0 if r < 0 else 0.0)


def ensemble_signal(closes: Sequence[float], params: TrendParams) -> float:
    """Equal-weight mean of the per-lookback signs: +1 all agree long, -1 all agree short."""
    return sum(tsmom_sign(closes, lb) for lb in params.lookbacks) / len(params.lookbacks)


def realized_vol(closes: Sequence[float], window: int) -> float | None:
    """Population std of the last `window` daily returns; None when the history is too short."""
    if len(closes) < window + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(len(closes) - window, len(closes))
            if closes[i - 1] > 0]
    if len(rets) < window:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    sd = math.sqrt(var)
    return sd if sd > 0 else None


def target_weights(closes_by_coin: dict[str, Sequence[float]], params: TrendParams) -> dict[str, float]:
    """Signed book weights (share of equity per coin) for the next day.

    weight = signal * vol_target / realised daily vol; if the book's gross exceeds leverage_cap the whole
    book is scaled down to the cap; finally everything is scaled by risk_scale. A coin without enough
    history gets weight 0 (the engine then closes any position it holds).
    """
    raw: dict[str, float] = {}
    for coin in params.coins:
        closes = closes_by_coin.get(coin) or []
        if len(closes) < params.min_history_days:
            raw[coin] = 0.0
            continue
        vol = realized_vol(closes, params.vol_window)
        sig = ensemble_signal(closes, params)
        raw[coin] = 0.0 if vol is None else sig * params.vol_target_daily / vol
    gross = sum(abs(w) for w in raw.values())
    cap = params.leverage_cap / gross if gross > params.leverage_cap else 1.0
    return {c: w * cap * params.risk_scale for c, w in raw.items()}
