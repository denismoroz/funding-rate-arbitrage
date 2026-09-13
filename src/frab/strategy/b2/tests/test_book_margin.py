"""Margin model of a B2 coin book: sizing, top-up, liquidation, margin-limited opens."""
from __future__ import annotations

import pytest

from frab.strategy.b2.book import CoinBook, start_book, step
from frab.strategy.b2.params import B2Params


def _book(coin="BTC", capital=1000.0, **kw) -> tuple[CoinBook, B2Params]:
    params = B2Params(coins=(coin,), capital_usd=capital, min_order_usd=0.0, **kw)
    book = CoinBook.new(coin, params)
    start_book(book, bar_ms=0, price=100.0, params=params)
    return book, params


def _bar(book, params, i, price, high=None, rate=0.0, hedge=None, carry_apr=None):
    if hedge is not None:
        book.hedge_prev = hedge
    if carry_apr is not None:
        book.carry_apr_prev = carry_apr
    ev = step(book, bar_ms=i, price=price, funding_rate=rate, closes=[price], funding_hist=[rate],
              params=params, high=high)
    # the one-bar history keeps the raw signal off; pin the wish we are testing
    if hedge is not None:
        book.hedge_prev = hedge
    if carry_apr is not None:
        book.carry_apr_prev = carry_apr
    return ev


@pytest.mark.parametrize("coin,lev", [("BTC", 3.0), ("ETH", 2.0), ("SOL", 1.5), ("AVAX", 1.5), ("XYZ", 1.0)])
def test_sizing_fits_spot_carry_and_both_margins(coin, lev):
    params = B2Params(coins=(coin,), capital_usd=400.0)
    spot, reserve, carry = params.book_sizes(coin)
    need = spot + carry + carry / lev + spot / lev + params.margin_buffer * spot
    assert params.leverage(coin) == lev
    assert abs(spot + reserve - 400.0) < 1e-9 and abs(need - 400.0) < 1e-9
    assert abs(carry - params.carry_fraction * spot) < 1e-12


def test_research_sizing_when_margin_disabled():
    spot, reserve, carry = B2Params(coins=("BTC",), capital_usd=400.0, margin_enabled=False).book_sizes("BTC")
    assert (spot, reserve, carry) == (200.0, 200.0, 0.6 * 200.0)


def test_hedge_and_carry_both_open_on_a_fresh_book():
    book, params = _book()
    ev = _bar(book, params, 1, 100.0, hedge=True, carry_apr=0.5)
    kinds = [e["kind"] for e in ev]
    assert "hedge_open" in kinds and "carry_open" in kinds
    assert book.hedge_limited == 0 and book.carry_blocked_hours == 0
    assert book.free_margin(100.0) > 0


def test_top_up_keeps_delta_neutral_and_equity_minus_fees():
    book, params = _book(carry_enabled=False)
    _bar(book, params, 1, 100.0, hedge=True)
    assert book.in_pos and book.short_size == pytest.approx(book.units_spot)
    p = 100.0
    ev = []
    for i in range(2, 60):                      # grind up until the pool needs a top-up
        p *= 1.01
        before, usdc = book.equity(p), book.hl_usdc()
        ev = _bar(book, params, i, p, hedge=True)
        if any(e["kind"] == "margin_rebalance" for e in ev):
            break
    assert any(e["kind"] == "margin_rebalance" for e in ev), "a 1%/h rally must trigger a top-up"
    fee = sum(e["fee"] for e in ev)
    assert book.equity(p) == pytest.approx(before - fee, abs=1e-9)
    assert book.short_size == pytest.approx(book.units_spot), "spot and short cut by the same units"
    assert book.entry_price == p and book.short_pnl(p) == 0.0
    assert book.hl_usdc() == pytest.approx(usdc - fee, abs=1e-9), "the spot sale pays the short's loss"
    assert book.account_value(p) >= params.rebalance_at_im_share * book.initial_margin(p)
    assert book.liquidations == 0


def test_spike_through_liquidation_price_liquidates_the_shorts():
    book, params = _book(carry_enabled=False)
    _bar(book, params, 1, 100.0, hedge=True)
    liq = book.liquidation_price()
    assert liq is not None and liq > 100.0
    eq_before = book.equity(100.0)
    ev = _bar(book, params, 2, 100.0, high=liq * 1.001, hedge=False)
    assert [e["kind"] for e in ev][0] == "liquidation"
    assert not book.in_pos and book.short_size == 0.0 and book.liquidations == 1
    assert book.equity(100.0) < eq_before, "liquidation costs the maintenance margin and the gap"


def test_no_liquidation_when_high_stays_below():
    book, params = _book(carry_enabled=False)
    _bar(book, params, 1, 100.0, hedge=True)
    ev = _bar(book, params, 2, 100.0, high=book.liquidation_price() * 0.999, hedge=True)
    assert all(e["kind"] != "liquidation" for e in ev) and book.in_pos


def test_depleted_pool_limits_the_hedge_and_blocks_carry():
    book, params = _book()
    book.cash = book.position_size * 0.05          # losses drained the USDC pool
    ev = _bar(book, params, 1, 100.0, hedge=True, carry_apr=0.5)
    opened = [e for e in ev if e["kind"] == "hedge_open"]
    assert opened and opened[0].get("limited_by_margin") and book.short_size < book.units_spot
    assert book.hedge_limited == 1
    assert not book.carry_on and book.carry_blocked_hours == 1
    assert book.free_margin(100.0) == pytest.approx(0.0, abs=1e-9)


def test_margin_state_roundtrip():
    book, params = _book(coin="SOL")
    _bar(book, params, 1, 100.0, hedge=True, carry_apr=0.5)
    again = CoinBook.from_state(book.to_state())
    assert again == book and again.leverage == 1.5 and again.carry_units > 0
