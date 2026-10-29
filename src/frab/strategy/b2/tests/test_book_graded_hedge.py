"""The hedge graded by the strength of the signal (params.graded_hedge_depth): the hedged share of
the spot grows with how far the 14d and 30d returns are below the threshold. Off by default."""
from __future__ import annotations

import pytest

from frab.strategy.b2.book import CoinBook, advance_signals, start_book, step
from frab.strategy.b2.params import B2Params

HOUR = 3_600_000
LONG_BARS = 30 * 24


def _params(**kw) -> B2Params:
    return B2Params(coins=("BTC",), margin_enabled=False, carry_enabled=False,
                    sticky_exit_hours=0, **kw)


def _book(params: B2Params) -> CoinBook:
    book = CoinBook.new("BTC", params)
    start_book(book, bar_ms=0, price=100.0, params=params)
    return book


def _flat_then(last: float) -> list[float]:
    """Flat at 100 for the long window, then `last`: both windows return last / 100 - 1."""
    return [100.0] * LONG_BARS + [last]


def _share(closes: list[float], **kw) -> float:
    params = _params(**kw)
    book = _book(params)
    advance_signals(book, closes, [], params)
    return book.hedge_share_prev if book.hedge_prev else 0.0


def test_the_default_is_off_and_changes_nothing():
    params = _params()
    assert params.graded_hedge_depth == 0.0 and "graded_hedge_depth" not in params.to_dict()
    book = _book(params)
    advance_signals(book, _flat_then(95.0), [], params)
    assert book.hedge_prev and book.hedge_share_prev == 1.0
    assert "hedge_share_prev" not in book.to_state()


def test_the_share_follows_how_far_both_returns_are_down():
    assert _share(_flat_then(95.0), graded_hedge_depth=0.10) == pytest.approx(0.5)
    assert _share(_flat_then(97.0), graded_hedge_depth=0.10) == pytest.approx(0.3)
    assert _share(_flat_then(95.0), graded_hedge_depth=0.05) == pytest.approx(1.0)
    assert _share(_flat_then(80.0), graded_hedge_depth=0.05) == pytest.approx(1.0)   # capped


def test_no_hedge_while_both_returns_are_at_or_above_the_threshold():
    assert _share(_flat_then(105.0), graded_hedge_depth=0.10) == 0.0
    assert _share(_flat_then(100.0), graded_hedge_depth=0.10) == 0.0   # exactly at it


def test_a_mixed_signal_gets_the_mean_of_the_two_strengths():
    closes = [100.0] * (LONG_BARS + 1)
    closes[0] = 100.0 / 0.95                # the 30d return is -5%
    closes[-1 - 14 * 24] = 100.0 / 1.05     # the 14d return is +5%
    # only the 30d window is down, by half the depth: the hedge opens partially, at a quarter
    assert _share(closes, graded_hedge_depth=0.10) == pytest.approx(0.25)


def test_the_hedge_opens_at_the_share_and_is_resized_with_the_signal():
    params = _params(graded_hedge_depth=0.10)
    book = _book(params)
    closes = _flat_then(95.0)
    advance_signals(book, closes, [], params)
    ev = step(book, bar_ms=HOUR, price=95.0, funding_rate=0.0, closes=closes, funding_hist=[],
              params=params)
    assert [e["kind"] for e in ev] == ["hedge_open"]
    assert book.short_size == pytest.approx(book.units_spot * 0.5)
    deeper = _flat_then(90.0)               # both returns -10%: the whole spot
    advance_signals(book, deeper, [], params)
    ev = step(book, bar_ms=2 * HOUR, price=90.0, funding_rate=0.0, closes=deeper, funding_hist=[],
              params=params)
    assert [e["kind"] for e in ev] == ["hedge_grow"]
    assert book.short_size == pytest.approx(book.units_spot)
    shallower = _flat_then(98.0)            # -2%: a fifth of the spot
    advance_signals(book, shallower, [], params)
    ev = step(book, bar_ms=3 * HOUR, price=98.0, funding_rate=0.0, closes=shallower,
              funding_hist=[], params=params)
    assert [e["kind"] for e in ev] == ["hedge_cut"]
    assert book.short_size == pytest.approx(book.units_spot * 0.2)


def test_a_hedge_below_the_minimum_order_is_not_opened():
    params = _params(graded_hedge_depth=1.0)
    book = _book(params)
    closes = _flat_then(95.0)               # share 0.05 of 1.25 units = about 6 USD
    advance_signals(book, closes, [], params)
    assert book.hedge_prev and book.hedge_share_prev == pytest.approx(0.05)
    ev = step(book, bar_ms=HOUR, price=95.0, funding_rate=0.0, closes=closes, funding_hist=[],
              params=params)
    assert ev == [] and not book.in_pos


def test_the_param_is_stored_when_set_and_bad_values_are_refused():
    p = B2Params.from_dict({"graded_hedge_depth": 0.1})
    assert p.to_dict()["graded_hedge_depth"] == 0.1
    assert B2Params.from_dict(p.to_dict()) == p
    for bad in (-0.1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            B2Params.from_dict({"graded_hedge_depth": bad})
    with pytest.raises(ValueError):
        B2Params.from_dict({"graded_hedge_depth": 0.1, "partial_hedge_share": 0.5})
