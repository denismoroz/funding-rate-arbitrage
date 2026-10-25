"""One margin pool for several coin books, and top-ups that arrive late (docs: book.py, "one margin
pool"; params.shared_margin, params.topup_delay_hours).

The pool's rules, each pinned here:

- a pool of one coin is that coin's book, bar for bar, on a stored history;
- the pool is liquidated on the SUM of its shorts' losses against the SUM of its USDC;
- a hedge may use the pool's free margin, not only its own book's;
- a top-up due at bar t takes effect at bar t + delay, and the shorts can be liquidated in between.
"""
from __future__ import annotations

import pytest

from frab.strategy.b2.book import (
    HOUR_MS,
    CoinBook,
    MarginPool,
    advance_signals,
    pool_liquidate_if_breached,
    pool_top_up,
    start_book,
    step,
)
from frab.strategy.b2.params import B2Params
from frab.strategy.b2.tests._replay import (
    HISTORY_BARS,
    WARMUP_BARS,
    load_history,
    variant_params,
)

PRICE0 = 100.0


def _params(capital=1000.0, coins=("A", "B"), **kw) -> B2Params:
    kw.setdefault("carry_enabled", False)
    kw.setdefault("min_order_usd", 0.0)
    kw.setdefault("short_leverage", {c: 2.0 for c in coins})
    kw.setdefault("maint_margin_rate", {c: 0.05 for c in coins})
    return B2Params(coins=tuple(coins), capital_usd=capital, **kw)


def _pool(params: B2Params) -> tuple[MarginPool, dict[str, CoinBook]]:
    pool = MarginPool()
    books = {}
    for coin in params.coins:
        one = B2Params(**{**params.__dict__, "coins": (coin,), "capital_usd": params.capital_usd / len(params.coins)})
        book = CoinBook.new(coin, one)
        start_book(book, bar_ms=0, price=PRICE0, params=one)
        pool.add(book, PRICE0)
        books[coin] = book
    return pool, books


def _bar(pool, params, i, prices, highs=None, hedge=None):
    """One bar of a pool, in the caller's order (book.py, "one margin pool")."""
    highs = highs or {}
    hedge = hedge or {}
    for coin, p in prices.items():
        pool.marks[coin] = p
    ev = pool_liquidate_if_breached(
        pool, highs={c: max(p, highs.get(c, p)) for c, p in prices.items()}, bar_ms=i * HOUR_MS)
    for coin, p in prices.items():
        book = pool.books.get(coin)
        if book is None:
            continue
        if coin in hedge:
            book.hedge_prev = hedge[coin]
        ev += step(book, bar_ms=i * HOUR_MS, price=p, funding_rate=0.0, closes=[p], funding_hist=[0.0],
                   params=params, high=highs.get(coin), pool=pool)
        if coin in hedge:
            book.hedge_prev = hedge[coin]
    ev += pool_top_up(pool, bar_ms=i * HOUR_MS, params=params)
    return ev


def _kinds(ev):
    return [e["kind"] for e in ev]


# ── a pool of one coin is the coin's own book ────────────────────────────────────────────────

def _run_alone(coin, params, data):
    closes, highs, rates = data
    first = HISTORY_BARS + WARMUP_BARS
    book = CoinBook.new(coin, params)
    for k in range(first - WARMUP_BARS, first):
        advance_signals(book, closes[max(0, k - HISTORY_BARS + 9):k + 1], rates[max(0, k - 8):k + 1], params)
    start_book(book, bar_ms=first * HOUR_MS, price=closes[first], params=params)
    out = []
    for i in range(first, len(closes)):
        ev = step(book, bar_ms=i * HOUR_MS, price=closes[i], funding_rate=rates[i],
                  closes=closes[max(0, i - HISTORY_BARS + 9):i + 1],
                  funding_hist=rates[max(0, i - 8):i + 1], params=params, high=highs[i])
        out.append((_kinds(ev), book.equity(closes[i])))
    return out


def _run_in_pool(coin, params, data):
    closes, highs, rates = data
    first = HISTORY_BARS + WARMUP_BARS
    book = CoinBook.new(coin, params)
    for k in range(first - WARMUP_BARS, first):
        advance_signals(book, closes[max(0, k - HISTORY_BARS + 9):k + 1], rates[max(0, k - 8):k + 1], params)
    start_book(book, bar_ms=first * HOUR_MS, price=closes[first], params=params)
    pool = MarginPool()
    pool.add(book, closes[first])
    out = []
    for i in range(first, len(closes)):
        pool.marks[coin] = closes[i]
        ev = pool_liquidate_if_breached(pool, highs={coin: max(closes[i], highs[i])}, bar_ms=i * HOUR_MS)
        ev += step(book, bar_ms=i * HOUR_MS, price=closes[i], funding_rate=rates[i],
                   closes=closes[max(0, i - HISTORY_BARS + 9):i + 1],
                   funding_hist=rates[max(0, i - 8):i + 1], params=params, high=highs[i], pool=pool)
        ev += pool_top_up(pool, bar_ms=i * HOUR_MS, params=params)
        out.append((_kinds(ev), book.equity(closes[i])))
    return out


@pytest.mark.parametrize("coin, variant, liquidations", [
    ("BTC", "stress_no_sticky", 0),      # top-ups, no liquidation: identical to the last bit
    ("SOL", "stress_no_sticky", 0),
    ("DOGE", "stress_no_sticky", 1),     # one liquidation: the pool solves the same price a line apart
    ("SOL", "prod_b2", 0),
])
def test_a_pool_of_one_coin_is_that_coins_own_book(coin, variant, liquidations):
    params = variant_params(coin, variant)
    pooled = B2Params(**{**params.__dict__, "shared_margin": True})
    data = load_history(coin)
    alone = _run_alone(coin, params, data)
    inside = _run_in_pool(coin, pooled, data)
    assert [k for k, _ in alone] == [k for k, _ in inside]
    assert sum(k.count("liquidation") for k, _ in inside) == liquidations
    worst = max(abs(a - b) for (_, a), (_, b) in zip(alone, inside))
    assert worst < 1e-9 if liquidations else worst == 0.0


# ── liquidation on the sum ──────────────────────────────────────────────────────────────────

def _hedged_pool(**kw):
    params = _params(shared_margin=True, **kw)
    pool, books = _pool(params)
    _bar(pool, params, 1, {"A": PRICE0, "B": PRICE0}, hedge={"A": True, "B": True})
    assert all(b.in_pos for b in books.values())
    return params, pool, books


def test_a_short_that_alone_would_be_liquidated_is_carried_by_the_pools_other_usdc():
    params, pool, books = _hedged_pool()
    alone = CoinBook.from_state(books["A"].to_state())
    liq_alone = alone.liquidation_price()
    spike = liq_alone * 1.02
    # A alone would be gone at this high ...
    assert spike >= liq_alone
    ev = _bar(pool, params, 2, {"A": PRICE0, "B": PRICE0}, highs={"A": spike, "B": PRICE0})
    # ... but B's USDC backs it in the pool.
    assert "liquidation" not in _kinds(ev) and all(b.in_pos for b in books.values())


def test_the_pool_is_liquidated_when_the_sum_of_losses_reaches_the_sum_of_maintenance_margin():
    params, pool, books = _hedged_pool()
    # Both coins spike together: the losses add, the cushion does not double.
    t = 1.0
    while True:
        t += 0.01
        high = {"A": PRICE0 * t, "B": PRICE0 * t}
        probe = MarginPool(books={c: CoinBook.from_state(b.to_state()) for c, b in pool.books.items()},
                           marks=dict(pool.marks))
        if pool_liquidate_if_breached(probe, highs=high, bar_ms=0):
            break
        assert t < 5.0
    ev = _bar(pool, params, 2, {"A": PRICE0, "B": PRICE0}, highs=high, hedge={"A": False, "B": False})
    assert _kinds(ev).count("liquidation") == 2, "a pool liquidation closes every short of the pool"
    assert all(e["pool"] for e in ev if e["kind"] == "liquidation")
    assert not any(b.in_pos for b in books.values())
    assert all(b.liquidations == 1 and b.refill_pending is False for b in books.values())
    # The liquidation price is where account value meets maintenance margin, so each book pays
    # the maintenance margin of its short at that price, not at the bar's high.
    liq_prices = {e["coin"]: e["price"] for e in ev if e["kind"] == "liquidation"}
    assert PRICE0 < liq_prices["A"] <= high["A"] and liq_prices["A"] == pytest.approx(liq_prices["B"])


def test_the_pool_is_liquidated_exactly_where_value_meets_maintenance_margin():
    """After the pool is closed at its liquidation prices, what is left is the penalty."""
    params, pool, books = _hedged_pool()
    before = sum(b.equity(PRICE0) for b in books.values())
    ev = _bar(pool, params, 2, {"A": PRICE0, "B": PRICE0}, highs={"A": 400.0, "B": 400.0},
              hedge={"A": False, "B": False})
    assert _kinds(ev).count("liquidation") == 2
    liq = ev[0]["price"]
    assert liq < 400.0, "the pool is gone before the high, at the price its margin ran out"
    usdc = sum(b.hl_usdc() for b in books.values())
    q = sum(e["qty"] for e in ev if e["kind"] == "liquidation")
    # At the liquidation price, account value was exactly the maintenance margin; the penalty
    # takes it, the rest stays as USDC.
    assert usdc == pytest.approx(before - sum(b.units_spot for b in books.values()) * PRICE0
                                 - (liq - PRICE0) * q - q * liq * 0.05, rel=1e-6)


def test_independent_books_lose_the_unlucky_coin_alone():
    """The same spike on the same two coins without a shared pool: A is liquidated, B is not."""
    params = _params()
    books = {}
    for coin in params.coins:
        one = B2Params(**{**params.__dict__, "coins": (coin,), "capital_usd": 500.0})
        books[coin] = CoinBook.new(coin, one)
        start_book(books[coin], bar_ms=0, price=PRICE0, params=one)
        books[coin].hedge_prev = True
        step(books[coin], bar_ms=HOUR_MS, price=PRICE0, funding_rate=0.0, closes=[PRICE0],
             funding_hist=[0.0], params=params)
    spike = books["A"].liquidation_price() * 1.02
    ev_a = step(books["A"], bar_ms=2 * HOUR_MS, price=PRICE0, funding_rate=0.0, closes=[PRICE0],
                funding_hist=[0.0], params=params, high=spike)
    ev_b = step(books["B"], bar_ms=2 * HOUR_MS, price=PRICE0, funding_rate=0.0, closes=[PRICE0],
                funding_hist=[0.0], params=params, high=PRICE0)
    assert "liquidation" in _kinds(ev_a) and "liquidation" not in _kinds(ev_b)


# ── a hedge uses the pool's free margin ─────────────────────────────────────────────────────

def test_a_hedge_may_use_the_free_margin_of_the_whole_pool():
    params = _params(shared_margin=True)
    pool, books = _pool(params)
    books["A"].cash = books["A"].position_size * 0.05      # A's own USDC is nearly gone
    ev = _bar(pool, params, 1, {"A": PRICE0, "B": PRICE0}, hedge={"A": True, "B": False})
    opened = [e for e in ev if e["kind"] == "hedge_open"]
    assert opened and not opened[0].get("limited_by_margin"), "B's idle USDC backs A's hedge"
    assert books["A"].short_size == pytest.approx(books["A"].units_spot)

    alone = CoinBook.from_state(books["A"].to_state())
    alone.in_pos, alone.short_size, alone.entry_price = False, 0.0, 0.0
    alone_params = _params(coins=("A",), capital=500.0)
    alone.hedge_prev = True
    ev_alone = step(alone, bar_ms=2 * HOUR_MS, price=PRICE0, funding_rate=0.0, closes=[PRICE0],
                    funding_hist=[0.0], params=alone_params)
    assert [e for e in ev_alone if e["kind"] == "hedge_open"][0].get("limited_by_margin")


def test_a_top_up_for_the_pool_cuts_every_losing_short_and_leaves_the_rest():
    params, pool, books = _hedged_pool()
    ev = []
    p = PRICE0
    for i in range(2, 80):
        p *= 1.01
        ev = _bar(pool, params, i, {"A": p, "B": PRICE0}, hedge={"A": True, "B": True})
        if "margin_rebalance" in _kinds(ev):
            break
    assert "margin_rebalance" in _kinds(ev)
    assert {e["coin"] for e in ev if e["kind"] == "margin_rebalance"} == {"A"}
    assert books["A"].entry_price == p and books["B"].entry_price == PRICE0
    assert pool.account_value() >= params.rebalance_at_im_share * pool.initial_margin()


# ── a top-up that arrives late ──────────────────────────────────────────────────────────────

def _single(delay, shared=False):
    params = _params(coins=("A",), capital=1000.0, topup_delay_hours=delay, shared_margin=shared)
    book = CoinBook.new("A", params)
    start_book(book, bar_ms=0, price=PRICE0, params=params)
    pool = None
    if shared:
        pool = MarginPool()
        pool.add(book, PRICE0)
    return params, book, pool


def _climb(params, book, pool, prices, highs=None, hedge_until=10**9):
    """Run `prices` from bar 1 with the hedge wished through bar `hedge_until`; returns {bar: kinds}."""
    out = {}
    for i, p in enumerate(prices, start=1):
        book.hedge_prev = i <= hedge_until
        ev = []
        if pool is not None:
            pool.marks["A"] = p
            ev += pool_liquidate_if_breached(pool, highs={"A": max(p, (highs or {}).get(i, p))}, bar_ms=i * HOUR_MS)
        ev += step(book, bar_ms=i * HOUR_MS, price=p, funding_rate=0.0, closes=[p], funding_hist=[0.0],
                   params=params, high=(highs or {}).get(i), pool=pool)
        book.hedge_prev = i + 1 <= hedge_until
        if pool is not None:
            ev += pool_top_up(pool, bar_ms=i * HOUR_MS, params=params)
        out[i] = _kinds(ev)
    return out


RALLY = [100.0, 104.0, 108.0, 112.0, 116.0, 120.0, 124.0, 128.0, 132.0, 136.0, 140.0, 144.0]


@pytest.mark.parametrize("shared", [False, True])
def test_a_top_up_with_no_delay_happens_the_bar_it_falls_due(shared):
    params, book, pool = _single(0, shared)
    seen = _climb(params, book, pool, RALLY)
    first = min(i for i, k in seen.items() if "margin_rebalance" in k)
    assert first < len(RALLY), "the rally must trigger a top-up"


@pytest.mark.parametrize("shared", [False, True])
def test_a_delayed_top_up_arrives_that_many_hours_after_it_fell_due(shared):
    now = min(i for i, k in _climb(*_single(0, shared), RALLY).items() if "margin_rebalance" in k)
    for delay in (1, 3):
        params, book, pool = _single(delay, shared)
        seen = _climb(params, book, pool, RALLY + [RALLY[-1]] * 6)
        later = min(i for i, k in seen.items() if "margin_rebalance" in k)
        assert later == now + delay, f"delay {delay}: due at bar {now}, arrived at bar {later}"
        assert book.topup_due_ms is None if not shared else pool.topup_due_ms is None


@pytest.mark.parametrize("shared", [False, True])
def test_a_liquidation_inside_the_delay_beats_the_top_up(shared):
    """The same rally: topped up at once the short survives; topped up four hours late it does not."""
    spike = {10: 160.0}
    survives = _single(0, shared)
    assert not any("liquidation" in k for k in _climb(*survives, RALLY, spike).values())
    assert survives[1].in_pos

    params, book, pool = _single(4, shared)
    seen = _climb(params, book, pool, RALLY, spike)
    assert any("liquidation" in k for k in seen.values()), "the short must be gone inside the delay"
    died = min(i for i, k in seen.items() if "liquidation" in k)
    due = min(i for i, k in _climb(*_single(0, shared), RALLY).items() if "margin_rebalance" in k)
    assert due < died < due + 4, "liquidated between the moment it fell due and the late top-up"
    assert book.liquidations == 1 and survives[1].liquidations == 0

    # Left flat after the liquidation, the book must not run the top-up that was on its way.
    params, book, pool = _single(4, shared)
    seen = _climb(params, book, pool, RALLY, spike, hedge_until=died - 1)
    assert not book.in_pos and book.topup_due_ms is None
    assert all("margin_rebalance" not in k for i, k in seen.items() if i >= died)


def test_a_pending_top_up_is_dropped_when_the_hedge_closes():
    params, book, _ = _single(5)
    seen = {}
    for i, p in enumerate(RALLY[:8], start=1):
        book.hedge_prev = True
        seen[i] = _kinds(step(book, bar_ms=i * HOUR_MS, price=p, funding_rate=0.0, closes=[p],
                              funding_hist=[0.0], params=params))
        book.hedge_prev = True
        if book.topup_due_ms is not None:
            break
    assert book.topup_due_ms is not None
    book.hedge_prev = False
    ev = step(book, bar_ms=(i + 1) * HOUR_MS, price=RALLY[i], funding_rate=0.0, closes=[RALLY[i]],
              funding_hist=[0.0], params=params)
    assert "hedge_close" in _kinds(ev) and book.topup_due_ms is None
