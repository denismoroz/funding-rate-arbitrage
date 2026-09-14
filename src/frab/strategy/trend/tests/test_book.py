"""The trend paper book: sizing, fees, funding, flips, the exchange minimum and liquidation."""
from __future__ import annotations

import math

from frab.constants import PERP_TAKER
from frab.strategy.trend.book import TrendBook, apply_funding, liquidate_if_breached, rebalance, start_book, step
from frab.strategy.trend.params import TrendParams

P = TrendParams(coins=("BTC", "ETH"), capital_usd=1000.0, slippage=0.0005, min_order_usd=10.0)
COST = PERP_TAKER + P.slippage


def fresh() -> TrendBook:
    b = TrendBook.new(P)
    start_book(b, bar_ms=0, params=P)
    return b


def test_funding_is_paid_by_longs_and_received_by_shorts():
    b = fresh()
    b.units = {"BTC": 0.01, "ETH": -1.0}
    b.entry = {"BTC": 50_000.0, "ETH": 3_000.0}
    apply_funding(b, {"BTC": 50_000.0, "ETH": 3_000.0}, {"BTC": 0.0001, "ETH": 0.0001}, 0)
    assert b.funding_total == -0.01 * 50_000 * 0.0001 + 1.0 * 3_000 * 0.0001
    assert b.cash == b.capital + b.funding_total


def test_rebalance_sizes_legs_to_weight_times_equity_and_charges_the_taker_fee():
    b = fresh()
    ev = rebalance(b, prices={"BTC": 50_000.0, "ETH": 2_000.0}, weights={"BTC": 0.3, "ETH": -0.2},
                   signals={"BTC": 1.0, "ETH": -1.0}, params=P, bar_ms=0)
    assert {e["kind"] for e in ev} == {"open"}
    assert b.units["BTC"] == 0.3 * 1000 / 50_000
    assert b.units["ETH"] == -0.2 * 1000 / 2_000
    fee = (0.3 + 0.2) * 1000 * COST
    assert math.isclose(b.fees, fee, rel_tol=1e-12)
    assert math.isclose(b.cash, 1000.0 - fee, rel_tol=1e-12)
    assert math.isclose(b.equity({"BTC": 50_000.0, "ETH": 2_000.0}), 1000.0 - fee, rel_tol=1e-12)


def test_price_move_shows_up_as_unrealized_and_is_realised_on_close():
    b = fresh()
    rebalance(b, prices={"BTC": 50_000.0}, weights={"BTC": 0.5}, signals={}, params=P, bar_ms=0)
    units = b.units["BTC"]
    assert math.isclose(b.unrealized({"BTC": 55_000.0}), units * 5_000.0, rel_tol=1e-12)
    rebalance(b, prices={"BTC": 55_000.0}, weights={"BTC": 0.0}, signals={}, params=P, bar_ms=1)
    assert b.units == {}
    assert math.isclose(b.realized, units * 5_000.0, rel_tol=1e-12)
    assert math.isclose(b.equity({"BTC": 55_000.0}), b.cash, rel_tol=1e-12)


def test_flip_realises_the_old_leg_and_reopens_at_the_new_price():
    b = fresh()
    rebalance(b, prices={"BTC": 50_000.0}, weights={"BTC": 0.4}, signals={}, params=P, bar_ms=0)
    long_units = b.units["BTC"]
    ev = rebalance(b, prices={"BTC": 45_000.0}, weights={"BTC": -0.4}, signals={}, params=P, bar_ms=1)
    assert [e["kind"] for e in ev] == ["flip"]
    assert b.units["BTC"] < 0
    assert b.entry["BTC"] == 45_000.0
    assert math.isclose(b.realized, long_units * (45_000.0 - 50_000.0), rel_tol=1e-12)


def test_adding_to_a_position_averages_the_entry():
    b = fresh()
    rebalance(b, prices={"BTC": 50_000.0}, weights={"BTC": 0.2}, signals={}, params=P, bar_ms=0)
    u1, e1 = b.units["BTC"], b.entry["BTC"]
    rebalance(b, prices={"BTC": 60_000.0}, weights={"BTC": 0.6}, signals={}, params=P, bar_ms=1)
    u2 = b.units["BTC"]
    assert u2 > u1 and b.realized == 0.0
    assert math.isclose(b.entry["BTC"], (u1 * e1 + (u2 - u1) * 60_000.0) / u2, rel_tol=1e-12)


def test_orders_below_the_exchange_minimum_are_skipped_but_a_full_close_is_not():
    b = fresh()
    rebalance(b, prices={"BTC": 50_000.0}, weights={"BTC": 0.004}, signals={}, params=P, bar_ms=0)
    assert b.units == {} and b.skipped_min_order == 1          # a $4 leg is below the $10 minimum
    rebalance(b, prices={"BTC": 50_000.0}, weights={"BTC": 0.05}, signals={}, params=P, bar_ms=1)
    assert math.isclose(b.units["BTC"] * 50_000.0, 0.05 * b.equity({"BTC": 50_000.0}), rel_tol=0.02)
    b.units["BTC"] = 0.0001                                     # $5 left after a price move
    b.entry["BTC"] = 50_000.0
    ev = rebalance(b, prices={"BTC": 50_000.0}, weights={}, signals={}, params=P, bar_ms=2)
    assert [e["kind"] for e in ev] == ["close"] and b.units == {}


def test_liquidation_closes_everything_once_equity_falls_under_maintenance():
    b = fresh()
    rebalance(b, prices={"BTC": 50_000.0}, weights={"BTC": 3.0}, signals={}, params=P, bar_ms=0)
    assert b.units["BTC"] > 0
    crash = {"BTC": 50_000.0 * (1 - 0.34)}                       # 3x long, -34% -> account wiped out
    ev = liquidate_if_breached(b, crash, P, bar_ms=1)
    assert [e["kind"] for e in ev] == ["liquidation"]
    assert b.units == {} and b.liquidations == 1


def test_step_runs_funding_then_the_rebalance_and_remembers_prices():
    b = fresh()
    ev = step(b, bar_ms=0, prices={"BTC": 50_000.0}, funding={"BTC": 0.0001}, params=P,
              weights={"BTC": 0.5}, signals={"BTC": 1.0})
    assert [e["kind"] for e in ev] == ["open"]
    assert b.funding_total == 0.0                                # no position when funding was applied
    step(b, bar_ms=1, prices={"BTC": 51_000.0}, funding={"BTC": 0.0001}, params=P)
    assert b.funding_total < 0                                   # now long, pays a positive rate
    assert b.prices["BTC"] == 51_000.0 and b.last_bar_ms == 1
    assert b.rebalances == 1                                     # the second hour was not a rebalance hour


def test_state_survives_a_round_trip():
    b = fresh()
    rebalance(b, prices={"BTC": 50_000.0, "ETH": 2_000.0}, weights={"BTC": 0.3, "ETH": -0.2},
              signals={"BTC": 1.0, "ETH": -1.0}, params=P, bar_ms=0)
    back = TrendBook.from_state(b.to_state())
    assert back.to_state() == b.to_state()
    assert back.equity({"BTC": 50_000.0, "ETH": 2_000.0}) == b.equity({"BTC": 50_000.0, "ETH": 2_000.0})
