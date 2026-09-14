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


def book_vol_scale(daily_equity: Sequence[float], params: TrendParams) -> float:
    """How much to scale the whole book so its own volatility meets the target.

    `daily_equity` is the book's equity sampled once a day, oldest first, ending at the last closed day
    (causal). Before the book has `book_vol_min_days` of its own history the prior is used instead of a
    measurement. The result is clipped so a quiet stretch cannot lever the book up without bound.
    """
    if params.book_vol_target_ann is None:
        return 1.0
    eq = [e for e in daily_equity if e and e > 0][-(params.book_vol_window_days + 1):]
    rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq))]
    if len(rets) < params.book_vol_min_days:
        vol = params.book_vol_prior_ann
    else:
        mean = sum(rets) / len(rets)
        vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) * math.sqrt(365)
    if vol <= 0:
        vol = params.book_vol_prior_ann
    return min(max(params.book_vol_target_ann / vol, params.book_vol_scale_min), params.book_vol_scale_max)


def target_weights(closes_by_coin: dict[str, Sequence[float]], params: TrendParams,
                   size_scale: float = 1.0) -> dict[str, float]:
    """Signed book weights (share of equity per coin) for the next day.

    weight = signal * vol_target / realised daily vol; if the book's gross exceeds leverage_cap the whole
    book is scaled down to the cap; then everything is scaled by risk_scale and by `size_scale` (the
    book-volatility scaler, see book_vol_scale). A coin without enough history gets weight 0 (the engine
    then closes any position it holds).
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
    return {c: w * cap * params.risk_scale * size_scale for c, w in raw.items()}
