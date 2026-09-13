"""One coin book of Strategy B v2, stepped one closed hourly bar at a time.

A line-for-line port of research/b_sim_ext.simulate_ext (hedge_ratio 1, own-coin hedge,
no staking, 0% cash yield) plus research/strategy_b_v2/harness.carry_pnl, turned from a
batch loop into an incremental state machine so a live engine can advance it bar by bar
and resume after a restart. Decisions at bar i use signals computed at bar i-1 (the
research signal_lag=1), executed at bar i's close.

Execution here is PAPER: fills happen at the bar close, with taker fee + slippage charged
as cost (the same cost model the research used). No exchange call exists in this module.

With params.margin_enabled the book also carries what the research treated as free: every
coin book is one HL cross-margin pool holding USDC only (spot is not collateral, so the hedge
spot may live in a cold wallet). The shorts need initial margin to open, a pump drains the
pool, the book tops up by selling spot and cutting the short by the same units, and HL
liquidates the shorts if the bar's high takes the pool below maintenance margin.
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
    carry_target: float = 0.0
    # margin (used only with params.margin_enabled)
    leverage: float = 1.0
    mmr: float = 0.0
    carry_units: float = 0.0
    carry_entry: float = 0.0
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
    margin_rebals: int = 0
    margin_fees: float = 0.0
    liquidations: int = 0
    liquidation_loss: float = 0.0
    hedge_limited: int = 0
    carry_blocked_hours: int = 0
    last_price: float = 0.0

    @classmethod
    def new(cls, coin: str, params: B2Params) -> "CoinBook":
        spot, reserve, carry = params.book_sizes(coin)
        return cls(coin=coin, capital=params.book_capital, position_size=spot, reserve=reserve,
                   carry_target=carry, leverage=params.leverage(coin), mmr=params.mmr(coin))

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

    # ── margin (HL cross pool of this book, USDC only) ────────────────────
    def hl_usdc(self) -> float:
        """USDC on HL: all book cash and carry P&L, less what the carry spot leg cost."""
        return self.cash + self.carry_cash - self.carry_units * self.carry_entry

    def hedge_units(self) -> float:
        return self.short_size if self.in_pos else 0.0

    def account_value(self, price: float) -> float:
        return self.hl_usdc() + self.short_pnl(price) + self.carry_units * (self.carry_entry - price)

    def initial_margin(self, price: float) -> float:
        return (self.hedge_units() + self.carry_units) * price / self.leverage

    def free_margin(self, price: float) -> float:
        return self.account_value(price) - self.initial_margin(price)

    def liquidation_price(self) -> float | None:
        """Price at which account value falls to maintenance margin (shorts lose as price rises)."""
        q = self.hedge_units() + self.carry_units
        if q <= 0:
            return None
        basis = self.hedge_units() * self.entry_price + self.carry_units * self.carry_entry
        return (self.hl_usdc() + basis) / (q * (1.0 + self.mmr))


def _event(kind: str, qty: float, price: float, notional: float, fee: float, **extra) -> dict:
    return {"kind": kind, "qty": qty, "price": price, "notional": notional, "fee": fee, **extra}


def _liquidate_if_breached(book: CoinBook, high: float, price: float, spot_cost: float, bar_ms: int) -> list[dict]:
    """HL closes every short of the pool at the liquidation price and keeps the maintenance margin.

    The carry spot leg is left naked by that, so it is sold at the bar close.
    """
    liq = book.liquidation_price()
    if liq is None or high < liq:
        return []
    qh, qc = book.hedge_units(), book.carry_units
    penalty = (qh + qc) * liq * book.mmr
    if qh:
        realized = qh * (book.entry_price - liq)
        book.cash += realized
        book.short_realized += realized
        book.short_size, book.entry_price, book.in_pos = 0.0, 0.0, False
        if book.units_spot * price < book.position_size:
            book.refill_pending = True
    if qc:
        fee = qc * price * spot_cost
        book.carry_cash += qc * (price - liq) - fee          # short closed at liq, spot sold at close
        book.carry_fees += fee
        book.carry_units, book.carry_entry, book.carry_on, book.carry_below = 0.0, 0.0, False, 0
    book.cash -= penalty
    loss = penalty + (qh + qc) * max(liq - price, 0.0)
    book.liquidations += 1
    book.liquidation_loss += loss
    return [_event("liquidation", qh + qc, liq, (qh + qc) * liq, penalty, bar_ms=bar_ms,
                   hedge_units=qh, carry_units=qc, loss=loss)]


def _hedge_room(book: CoinBook, price: float, perp_cost: float) -> float:
    """Short units the pool's free margin can open, fee included."""
    return max(book.free_margin(price), 0.0) / (price * (1.0 / book.leverage + perp_cost))


def _shrink_carry_for_hedge(book: CoinBook, price: float, hedge_units: float, spot_cost: float,
                            perp_cost: float, bar_ms: int) -> list[dict]:
    """The hedge protects the capital, the carry is extra income: when the pool cannot fund the
    full hedge, close as much of the carry (spot sold, short bought back) as the hedge needs."""
    if not book.carry_units:
        return []
    need = hedge_units * price * (1.0 / book.leverage + perp_cost) - max(book.free_margin(price), 0.0)
    if need <= 0:
        return []
    leg_cost = spot_cost + perp_cost
    cut = min(book.carry_units, need / (price * (1.0 + 1.0 / book.leverage - leg_cost)))
    fee = cut * price * leg_cost
    book.carry_units -= cut                    # both legs of the cut part net to zero P&L
    book.carry_cash -= fee
    book.carry_fees += fee
    closed = book.carry_units <= 1e-12
    if closed:
        book.carry_units, book.carry_entry, book.carry_on, book.carry_below = 0.0, 0.0, False, 0
    return [_event("carry_reduce", cut, price, cut * price, fee, bar_ms=bar_ms, reason="hedge_margin",
                   closed=closed)]


def _top_up(book: CoinBook, price: float, cost: float, bar_ms: int) -> list[dict]:
    """Restore margin after a pump: for every losing short, sell spot worth its loss and cut the
    short by the same units. Both legs keep equal size; the entry resets to the current price;
    the pool's USDC is unchanged (the loss is paid by the spot sale); only fees are lost."""
    ev = []
    if book.in_pos and price > book.entry_price:
        q = book.short_size
        keep = q * book.entry_price / price
        cut = q - keep
        realized = q * (book.entry_price - price)
        fee = cut * price * cost
        book.cash += realized + cut * price - fee
        book.short_realized += realized
        book.units_spot -= cut
        book.short_size, book.entry_price = keep, price
        book.margin_fees += fee
        ev.append(_event("margin_rebalance", cut, price, cut * price, fee, bar_ms=bar_ms, leg="hedge"))
    if book.carry_units and price > book.carry_entry:
        c = book.carry_units
        keep = c * book.carry_entry / price
        cut = c - keep
        fee = cut * price * cost
        book.carry_cash -= fee
        book.carry_units, book.carry_entry = keep, price
        book.margin_fees += fee
        ev.append(_event("margin_rebalance", cut, price, cut * price, fee, bar_ms=bar_ms, leg="carry"))
    if ev:
        book.margin_rebals += 1
    return ev


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
         funding_hist: list[float], params: B2Params, high: float | None = None) -> list[dict]:
    """Advance one closed hourly bar. Returns the paper fills generated in this bar.

    `closes` / `funding_hist` must END at this bar (they feed the signals for the NEXT bar).
    `high` is the bar's high, used for the liquidation check (defaults to the close).
    """
    P = float(price)
    rate = float(funding_rate)
    perp_cost = PERP_TAKER + params.slippage
    spot_cost = SPOT_TAKER + params.slippage
    min_ord = params.min_order_usd
    margin = params.margin_enabled
    ev: list[dict] = []

    # 0) liquidation on the shorts carried into this bar
    if margin:
        ev += _liquidate_if_breached(book, max(P, float(high if high is not None else P)), P, spot_cost, bar_ms)

    # 1) funding on an open hedge (short receives positive funding)
    if book.in_pos:
        f = book.short_size * P * rate
        book.cash += f
        book.funding_total += f

    want_hedge = book.hedge_prev

    # 2) hedge entry / exit
    if not book.in_pos and want_hedge:
        units = book.units_spot
        limited = False
        if margin and _hedge_room(book, P, perp_cost) < units:
            ev += _top_up(book, P, spot_cost + perp_cost, bar_ms)      # realise a pumped carry's loss
            ev += _shrink_carry_for_hedge(book, P, units, spot_cost, perp_cost, bar_ms)
            room = _hedge_room(book, P, perp_cost)
            if room < units:
                units, limited = room, True
        notional = units * P
        if notional >= min_ord and units > 0:
            book.short_size = units
            book.entry_price = P
            fee = book.short_size * P * perp_cost
            book.cash -= fee
            book.perp_fees += fee
            book.in_pos = True
            book.trades += 1
            book.hedge_limited += limited
            ev.append(_event("hedge_open", book.short_size, P, notional, fee, bar_ms=bar_ms,
                             **({"limited_by_margin": True} if limited else {})))
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
        leg_cost = SPOT_TAKER + PERP_TAKER + 2 * params.slippage
        sig = book.carry_apr_prev
        if not book.carry_on and sig is not None and sig > params.carry_entry_apr and book.carry_target >= min_ord:
            notional = book.carry_target
            cost = notional * leg_cost
            # margin: the pool must pay for the spot leg plus the short's initial margin
            if margin and book.free_margin(P) < notional * (1.0 + 1.0 / book.leverage) + cost:
                book.carry_blocked_hours += 1
            else:
                book.carry_on, book.carry_below = True, 0
                book.carry_cash -= cost
                book.carry_fees += cost
                book.carry_trades += 1
                if margin:
                    book.carry_units, book.carry_entry = notional / P, P
                ev.append(_event("carry_open", notional / P, P, notional, cost, bar_ms=bar_ms))
        elif book.carry_on:
            book.carry_below = book.carry_below + 1 if (sig is not None and sig < 0) else 0
            if book.carry_below >= params.carry_exit_hours:
                notional = book.carry_units * P if margin else book.carry_target
                cost = notional * leg_cost
                book.carry_on = False
                book.carry_cash -= cost
                book.carry_fees += cost
                ev.append(_event("carry_close", notional / P, P, notional, cost, bar_ms=bar_ms))
                book.carry_units, book.carry_entry = 0.0, 0.0
        if book.carry_on:
            earned = (book.carry_units * P if margin else book.carry_target) * rate
            book.carry_cash += earned
            book.carry_funding += earned

    # 6) margin top-up once the shorts' losses have eaten into initial margin
    if margin and (book.in_pos or book.carry_units):
        if book.account_value(P) < params.rebalance_at_im_share * book.initial_margin(P):
            ev += _top_up(book, P, spot_cost + perp_cost, bar_ms)

    # 7) signals of THIS bar, used at the next bar
    advance_signals(book, closes, funding_hist, params)
    book.last_bar_ms = bar_ms
    book.last_price = P
    return ev
