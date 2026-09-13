"""The live coin book must reproduce the validated research simulator exactly.

Runs research/b_sim_ext.simulate_ext + research/strategy_b_v2/harness.carry_pnl on real
BTC hourly data and steps CoinBook through the same bars; equity and carry cash must match
bar by bar. Skipped when the research tree or its data is not present.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from frab.strategy.b2.book import CoinBook, start_book, step
from frab.strategy.b2.params import B2Params

ROOT = Path(__file__).resolve().parents[5]
RESEARCH = ROOT / "research"
pytestmark = pytest.mark.skipif(not (RESEARCH / "data" / "BTC_1h.csv").exists(),
                                reason="research data not available")


def _research():
    for p in (RESEARCH, RESEARCH / "strategy_b_v2"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    import harness as H  # noqa: E402
    from backtest_b_constdollar import build_trend_up  # noqa: E402
    from b_improve_ideas import sticky  # noqa: E402
    from b_sim_ext import simulate_ext  # noqa: E402
    from engine import TOTAL_CAPITAL, load_data  # noqa: E402
    return H, build_trend_up, sticky, simulate_ext, TOTAL_CAPITAL, load_data


@pytest.mark.parametrize("n_bars", [3000])
def test_book_matches_research_simulator(n_bars):
    H, build_trend_up, sticky, simulate_ext, TOTAL, load_data = _research()
    df = load_data("BTC").iloc[:n_bars]
    close = df["close"].values
    fund = df["fundingRate"].astype(float).fillna(0.0).values

    sig = sticky(H.hedge_signal(close, 0.0), 12)
    pnl, _ = simulate_ext(df, 0.0, sig, rebal_threshold=0.50, risk_free_apr=0.0,
                          refill_confirm=build_trend_up(close), signal_lag=1, slippage=0.0005)
    ref_equity = TOTAL + np.cumsum(pnl)
    ref_carry = np.cumsum(H.carry_pnl(df, 0.0005))

    params = B2Params(coins=("BTC",), capital_usd=TOTAL, spot_share=0.5, sticky_exit_hours=12,
                      ratchet_threshold=0.50, carry_enabled=True, min_order_usd=0.0)
    book = CoinBook.new("BTC", params)
    start_book(book, bar_ms=0, price=float(close[0]), params=params)
    worst_eq = worst_carry = 0.0
    trades = 0
    for i in range(n_bars - 1):          # research books closing fees on the very last bar
        ev = step(book, bar_ms=i, price=float(close[i]), funding_rate=float(fund[i]),
                  closes=list(close[max(0, i - 730):i + 1]), funding_hist=list(fund[max(0, i - 8):i + 1]),
                  params=params)
        trades += sum(e["kind"] == "hedge_open" for e in ev)
        worst_eq = max(worst_eq, abs(book.book_equity(float(close[i])) - ref_equity[i]))
        worst_carry = max(worst_carry, abs(book.carry_cash - ref_carry[i]))
    assert trades > 0, "test window must exercise the hedge"
    assert book.carry_trades > 0, "test window must exercise the carry"
    assert worst_eq < 1e-6, f"book equity diverges from research by {worst_eq}"
    assert worst_carry < 1e-6, f"carry diverges from research by {worst_carry}"


def test_min_order_blocks_tiny_actions():
    params = B2Params(coins=("BTC",), capital_usd=15.0, min_order_usd=10.0, carry_enabled=True)
    book = CoinBook.new("BTC", params)            # spot $7.5, reserve $7.5, carry notional $4.5
    start_book(book, bar_ms=0, price=100.0, params=params)
    book.hedge_prev = True
    book.carry_apr_prev = 1.0
    ev = step(book, bar_ms=1, price=100.0, funding_rate=0.0001, closes=[100.0], funding_hist=[0.0001], params=params)
    kinds = {e["kind"] for e in ev}
    assert "hedge_open" not in kinds and not book.in_pos, "a $7.5 hedge is below HL's minimum order"
    assert "carry_open" not in kinds and not book.carry_on, "a $4.5 carry leg is below HL's minimum order"


def test_state_roundtrip():
    params = B2Params()
    book = CoinBook.new("ETH", params)
    start_book(book, bar_ms=5, price=2000.0, params=params)
    again = CoinBook.from_state(book.to_state())
    assert again == book
