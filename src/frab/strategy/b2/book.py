"""One coin book of Strategy B v2, stepped one closed hourly bar at a time.

A line-for-line port of research/b_sim_ext.simulate_ext (hedge_ratio 1, own-coin hedge,
no staking, 0% cash yield) plus research/strategy_b_v2/harness.carry_pnl, turned from a
batch loop into an incremental state machine so a live engine can advance it bar by bar
and resume after a restart. Decisions at bar i use signals computed at bar i-1 (the
research signal_lag=1), executed at bar i's close.

Execution here is PAPER: fills happen at the bar close, with taker fee + slippage charged
as cost (the same cost model the research used). No exchange call exists in this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from frab.constants import PERP_TAKER, SPOT_TAKER
from frab.strategy.b2.params import B2Params

HOURS_PER_YEAR = 8760
H14, H30, CARRY_WINDOW = 14 * 24, 30 * 24, 8


@dataclass
class CoinBook:
    coin: str
    capital: float
    position_size: float
    reserve: float
    started: bool = False
    last_bar_ms: int | None = None
    cash: float = 0.0
    units_spot: float = 0.0
    short_size: float = 0.0
    entry_price: float = 0.0
    in_pos: bool = False
    refill_pending: bool = False
    # signal state carried from the previous bar (research signal_lag = 1)
    sticky_on: bool = False
    sticky_off: int = 0
    hedge_prev: bool = False
    trend_up_prev: bool = False
    carry_apr_prev: float | None = None
    carry_on: bool = False
    carry_below: int = 0
    carry_cash: float = 0.0
    # accounting
    funding_total: float = 0.0
    short_realized: float = 0.0
    perp_fees: float = 0.0
    spot_fees: float = 0.0
    carry_funding: float = 0.0
    carry_fees: float = 0.0
    trades: int = 0
    rebals: int = 0
    carry_trades: int = 0
    last_price: float = 0.0

    @classmethod
    def new(cls, coin: str, params: B2Params) -> "CoinBook":
        cap = params.book_capital
        pos = cap * params.spot_share
        return cls(coin=coin, capital=cap, position_size=pos, reserve=cap - pos)

    @classmethod
    def from_state(cls, state: dict) -> "CoinBook":
        return cls(**state)

    def to_state(self) -> dict:
        return asdict(self)

    # ── valuation ────────────────────────────────────────────────────────
    def short_pnl(self, price: float) -> float:
        return self.short_size * (self.entry_price - price) if self.in_pos else 0.0

    def book_equity(self, price: float) -> float:
        return self.cash + self.units_spot * price + self.short_pnl(price)

    def equity(self, price: float) -> float:
        return self.book_equity(price) + self.carry_cash

    def carry_notional(self, params: B2Params) -> float:
        return params.carry_fraction * self.reserve


def _event(kind: str, qty: float, price: float, notional: float, fee: float, **extra) -> dict:
    return {"kind": kind, "qty": qty, "price": price, "notional": notional, "fee": fee, **extra}


def signals_at(closes: list[float], funding: list[float], params: B2Params) -> tuple[bool, bool, float | None]:
    """Raw hedge wish, refill trend confirmation and carry APR at the LAST element.

    closes/funding are the hourly histories ending at the bar being closed.
    Mirrors harness.hedge_signal / build_trend_up / carry_pnl's rolling mean.
    """
    n = len(closes)
    p = closes[-1]
    mom14 = p / closes[-1 - H14] - 1.0 if n > H14 else None
    mom30 = p / closes[-1 - H30] - 1.0 if n > H30 else None
    if mom30 is None:
        raw_hedge = False                                   # not enough history -> no hedge
    else:
        up = (mom14 is not None and mom14 > params.hedge_threshold) and mom30 > params.hedge_threshold
        raw_hedge = not up
    trend_up = mom14 is not None and mom14 > 0.0
    carry_apr = (sum(funding[-CARRY_WINDOW:]) / CARRY_WINDOW * HOURS_PER_YEAR
                 if len(funding) >= CARRY_WINDOW else None)
    return raw_hedge, trend_up, carry_apr


def advance_signals(book: CoinBook, closes: list[float], funding_hist: list[float], params: B2Params) -> None:
    """Compute this bar's signals and store them for the next bar (sticky exit applied)."""
    raw, trend_up, carry_apr = signals_at(closes, funding_hist, params)
    if params.sticky_exit_hours:
        if raw:
            book.sticky_on, book.sticky_off = True, 0
        elif book.sticky_on:
            book.sticky_off += 1
            if book.sticky_off >= params.sticky_exit_hours:
                book.sticky_on = False
        book.hedge_prev = book.sticky_on
    else:
        book.hedge_prev = raw
    book.trend_up_prev = trend_up
    book.carry_apr_prev = carry_apr


def start_book(book: CoinBook, *, bar_ms: int, price: float, params: B2Params) -> list[dict]:
    """Initial spot purchase at the first bar's close (research: before bar 0 is stepped)."""
    spot_cost = SPOT_TAKER + params.slippage
    book.cash = book.capital - book.position_size
    book.units_spot = book.position_size / price
    fee = book.position_size * spot_cost
    book.cash -= fee
    book.spot_fees += fee
    book.started = True
    return [_event("init_spot_buy", book.units_spot, price, book.position_size, fee, bar_ms=bar_ms)]


def step(book: CoinBook, *, bar_ms: int, price: float, funding_rate: float, closes: list[float],
         funding_hist: list[float], params: B2Params) -> list[dict]:
    """Advance one closed hourly bar. Returns the paper fills generated in this bar.

    `closes` / `funding_hist` must END at this bar (they feed the signals for the NEXT bar).
    """
    P = float(price)
    rate = float(funding_rate)
    perp_cost = PERP_TAKER + params.slippage
    spot_cost = SPOT_TAKER + params.slippage
    min_ord = params.min_order_usd
    ev: list[dict] = []

    # 1) funding on an open hedge (short receives positive funding)
    if book.in_pos:
        f = book.short_size * P * rate
        book.cash += f
        book.funding_total += f

    want_hedge = book.hedge_prev

    # 2) hedge entry / exit
    if not book.in_pos and want_hedge:
        notional = book.units_spot * P
        if notional >= min_ord:
            book.short_size = book.units_spot
            book.entry_price = P
            fee = book.short_size * P * perp_cost
            book.cash -= fee
            book.perp_fees += fee
            book.in_pos = True
            book.trades += 1
            ev.append(_event("hedge_open", book.short_size, P, notional, fee, bar_ms=bar_ms))
    elif book.in_pos and not want_hedge:
        realized = book.short_size * (book.entry_price - P)
        book.cash += realized
        book.short_realized += realized
        fee = book.short_size * P * perp_cost
        book.cash -= fee
        book.perp_fees += fee
        ev.append(_event("hedge_close", book.short_size, P, book.short_size * P, fee,
                         bar_ms=bar_ms, realized=realized))
        book.short_size = 0.0
        book.entry_price = 0.0
        book.in_pos = False
        if book.units_spot * P < book.position_size:
            book.refill_pending = True

    # 3) refill spot once the trend has turned up
    if book.refill_pending and not book.in_pos and book.trend_up_prev:
        spot_value = book.units_spot * P
        if spot_value < book.position_size:
            need = book.position_size - spot_value
            buy = min(need, max(book.cash - book.reserve, 0.0))
            if buy > 0 and buy >= min_ord:
                fee = buy * spot_cost
                book.cash -= buy + fee
                book.spot_fees += fee
                book.units_spot += buy / P
                book.rebals += 1
                ev.append(_event("refill_buy", buy / P, P, buy, fee, bar_ms=bar_ms))
        book.refill_pending = False

    # 4) ratchet: sell spot growth above target (only while unhedged)
    if not book.in_pos:
        spot_value = book.units_spot * P
        if spot_value > book.position_size * (1 + params.ratchet_threshold):
            excess = spot_value - book.position_size
            if excess >= min_ord:
                fee = excess * spot_cost
                book.cash += excess - fee
                book.spot_fees += fee
                book.units_spot -= excess / P
                book.rebals += 1
                ev.append(_event("ratchet_sell", excess / P, P, excess, fee, bar_ms=bar_ms))

    # 5) funding carry on the idle reserve (decision on the previous bar's 8h APR)
    if params.carry_enabled:
        notional = book.carry_notional(params)
        cost = notional * (SPOT_TAKER + PERP_TAKER + 2 * params.slippage)
        sig = book.carry_apr_prev
        if not book.carry_on and sig is not None and sig > params.carry_entry_apr and notional >= min_ord:
            book.carry_on, book.carry_below = True, 0
            book.carry_cash -= cost
            book.carry_fees += cost
            book.carry_trades += 1
            ev.append(_event("carry_open", notional / P, P, notional, cost, bar_ms=bar_ms))
        elif book.carry_on:
            book.carry_below = book.carry_below + 1 if (sig is not None and sig < 0) else 0
            if book.carry_below >= params.carry_exit_hours:
                book.carry_on = False
                book.carry_cash -= cost
                book.carry_fees += cost
                ev.append(_event("carry_close", notional / P, P, notional, cost, bar_ms=bar_ms))
        if book.carry_on:
            earned = notional * rate
            book.carry_cash += earned
            book.carry_funding += earned

    # 6) signals of THIS bar, used at the next bar
    advance_signals(book, closes, funding_hist, params)
    book.last_bar_ms = bar_ms
    book.last_price = P
    return ev
