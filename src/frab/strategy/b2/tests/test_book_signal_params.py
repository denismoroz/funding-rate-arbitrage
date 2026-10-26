"""The hedge signal's windows as params, and the partial hedge on a mixed signal (q-lab's
researcher, 2026-10-26: «включать/отключать хеджирование почаще, более мелкий таймфрейм»;
«страховать не целиком, а частью»). Defaults are today's behaviour: the parity tests pin that."""
from __future__ import annotations

import pytest

from frab.strategy.b2.book import CoinBook, advance_signals, signals_at, start_book, step
from frab.strategy.b2.params import B2Params

HOUR = 3_600_000


def _closes(days_up: int, days_down: int, total_days: int = 31) -> list[float]:
    """Hourly closes: flat, then falling for `days_down`, then rising for `days_up`."""
    flat = [100.0] * ((total_days - days_up - days_down) * 24)
    down = [100.0 - 20.0 * (i + 1) / (days_down * 24) for i in range(days_down * 24)]
    base = down[-1] if down else 100.0
    up = [base + 5.0 * (i + 1) / (days_up * 24) for i in range(days_up * 24)]
    return flat + down + up + [up[-1] if up else base]


def test_the_windows_are_params_and_14_30_are_the_defaults():
    closes = _closes(days_up=5, days_down=10)  # up over 5 days, down over 14 and 30
    hedge14, _, _ = signals_at(closes, [], B2Params())
    hedge3, _, _ = signals_at(closes, [], B2Params(short_window_days=3, long_window_days=3))
    assert hedge14 is True       # the 14-day return is still negative
    assert hedge3 is False       # over 3 days the price only rose


def test_windows_are_validated():
    with pytest.raises(ValueError):
        B2Params.from_dict({"short_window_days": 31})
    with pytest.raises(ValueError):
        B2Params.from_dict({"short_window_days": 14, "long_window_days": 7})
    with pytest.raises(ValueError):
        B2Params.from_dict({"partial_hedge_share": 0})


def test_new_params_are_stored_only_when_set():
    assert "short_window_days" not in B2Params().to_dict()
    p = B2Params.from_dict({"short_window_days": 7, "partial_hedge_share": 0.5})
    assert p.to_dict()["short_window_days"] == 7 and p.to_dict()["partial_hedge_share"] == 0.5
    assert B2Params.from_dict(p.to_dict()) == p


def _book(params: B2Params) -> CoinBook:
    book = CoinBook.new("BTC", params)
    start_book(book, bar_ms=0, price=100.0, params=params)
    return book


def test_a_mixed_signal_hedges_a_share_and_a_falling_market_the_rest():
    params = B2Params(coins=("BTC",), margin_enabled=False, carry_enabled=False,
                      partial_hedge_share=0.5, sticky_exit_hours=0)
    book = _book(params)
    # the long (30d) window up, the short (14d) one down: rise for 25 days, then fall for 10
    rising = [80.0 + 40.0 * i / (25 * 24) for i in range(25 * 24)]
    falling = [rising[-1] - 20.0 * (i + 1) / (10 * 24) for i in range(10 * 24)]
    mixed = [80.0] * 24 + rising + falling
    advance_signals(book, mixed, [], params)
    assert book.hedge_prev and book.hedge_share_prev == 0.5
    step(book, bar_ms=HOUR, price=mixed[-1], funding_rate=0.0, closes=mixed, funding_hist=[],
         params=params)
    assert book.in_pos and book.short_size == pytest.approx(book.units_spot * 0.5)
    # both windows down: the hedge grows to the whole spot
    down = mixed + [mixed[-1] - 30.0] * 24 * 15
    advance_signals(book, down, [], params)
    assert book.hedge_share_prev == 1.0
    ev = step(book, bar_ms=2 * HOUR, price=down[-1], funding_rate=0.0, closes=down,
              funding_hist=[], params=params)
    assert [e["kind"] for e in ev] == ["hedge_grow"]
    assert book.short_size == pytest.approx(book.units_spot)


def test_a_full_hedge_is_never_resized():
    params = B2Params(coins=("BTC",), margin_enabled=False, carry_enabled=False,
                      sticky_exit_hours=0)
    book = _book(params)
    closes = _closes(days_up=0, days_down=20)
    advance_signals(book, closes, [], params)
    step(book, bar_ms=HOUR, price=closes[-1], funding_rate=0.0, closes=closes, funding_hist=[],
         params=params)
    size = book.short_size
    book.units_spot *= 1.2  # whatever the spot does, a full hedge is left as it was opened
    ev = step(book, bar_ms=2 * HOUR, price=closes[-1], funding_rate=0.0, closes=closes,
              funding_hist=[], params=params)
    assert book.short_size == size and not [e for e in ev if e["kind"].startswith("hedge_")]
