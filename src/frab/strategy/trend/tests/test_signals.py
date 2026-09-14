"""Trend signals and sizing: the ensemble, the vol scale, the gross cap and the history gate."""
from __future__ import annotations

import math

from frab.strategy.trend.params import TrendParams
from frab.strategy.trend.signals import ensemble_signal, realized_vol, target_weights, tsmom_sign

P = TrendParams(coins=("BTC", "ETH"), lookbacks=(2, 4), vol_window=3, min_history_days=6)


def test_tsmom_sign_is_causal_and_flat_without_history():
    closes = [10.0, 11.0, 12.0]
    assert tsmom_sign(closes, 2) == 1.0                  # 12 > 10
    assert tsmom_sign(closes, 5) == 0.0                  # not enough history -> flat
    assert tsmom_sign([10.0, 9.0, 8.0], 2) == -1.0
    assert tsmom_sign([10.0, 9.0, 10.0], 2) == 0.0       # unchanged -> flat


def test_ensemble_is_the_mean_of_the_signs():
    up_then_down = [10.0, 20.0, 30.0, 25.0, 22.0]        # 2d: 22<30 -> -1; 4d: 22>10 -> +1
    assert ensemble_signal(up_then_down, P) == 0.0
    assert ensemble_signal([1.0, 2.0, 3.0, 4.0, 5.0], P) == 1.0
    assert ensemble_signal([5.0, 4.0, 3.0, 2.0, 1.0], P) == -1.0


def test_realized_vol_matches_population_std_of_daily_returns():
    closes = [100.0, 110.0, 99.0, 108.9]
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, 4)]
    mean = sum(rets) / 3
    expected = math.sqrt(sum((r - mean) ** 2 for r in rets) / 3)
    assert realized_vol(closes, 3) == expected
    assert realized_vol(closes, 10) is None


def test_weight_scales_with_signal_over_vol_and_respects_the_history_gate():
    rising = [100.0 * (1.02 ** i) * (1 + 0.03 * (i % 2)) for i in range(10)]   # trend plus enough noise
    w = target_weights({"BTC": rising, "ETH": rising[:3]}, P)
    vol = realized_vol(rising, P.vol_window)
    assert w["ETH"] == 0.0                                            # too little history
    assert P.vol_target_daily / vol < P.leverage_cap                  # the cap does not bind here
    assert w["BTC"] == 1.0 * P.vol_target_daily / vol * P.risk_scale
    assert w["BTC"] > 0


def test_gross_cap_and_risk_scale_bound_the_book():
    quiet = [100.0 + 0.01 * i for i in range(200)]                    # tiny vol -> huge raw weights
    p = TrendParams(coins=("BTC", "ETH"), leverage_cap=3.0, risk_scale=0.2)
    w = target_weights({"BTC": quiet, "ETH": quiet}, p)
    gross = sum(abs(x) for x in w.values())
    assert math.isclose(gross, p.leverage_cap * p.risk_scale, rel_tol=1e-9)
    assert all(x > 0 for x in w.values())                             # both legs long, both capped together


def test_no_scaling_when_the_book_is_inside_the_cap():
    noisy = [100.0 * (1 + 0.03 * math.sin(i / 3)) for i in range(200)]
    p = TrendParams(coins=("BTC",), risk_scale=1.0)
    w = target_weights({"BTC": noisy}, p)
    vol = realized_vol(noisy, p.vol_window)
    assert abs(w["BTC"]) <= p.leverage_cap
    assert math.isclose(abs(w["BTC"]), abs(ensemble_signal(noisy, p)) * p.vol_target_daily / vol, rel_tol=1e-12)
