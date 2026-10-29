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

Two research mechanisms sit behind params that default to today's behaviour (no production book sets
them; tests/test_book_pool_parity.py replays stored histories and compares every bar with a digest
recorded before they existed): `shared_margin` makes the coin books one HL cross account (`MarginPool`:
free margin, top-up trigger and liquidation are asked of the sum), and `topup_delay_hours` makes a top-up
take effect that many hours after it fell due, with the shorts exposed meanwhile.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from frab.constants import PERP_TAKER, SPOT_TAKER
from frab.strategy.b2.params import B2Params

HOURS_PER_YEAR = 8760
HOUR_MS = 3_600_000
CARRY_WINDOW = 8


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
    # share of the spot the wanted hedge covers (params.partial_hedge_share; 1 = in full)
    hedge_share_prev: float = 1.0
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
    # open ms of the bar at which a delayed top-up takes effect (params.topup_delay_hours > 0)
    topup_due_ms: int | None = None

    @classmethod
    def new(cls, coin: str, params: B2Params) -> "CoinBook":
        spot, reserve, carry = params.book_sizes(coin)
        return cls(coin=coin, capital=params.book_capital, position_size=spot, reserve=reserve,
                   carry_target=carry, leverage=params.leverage(coin), mmr=params.mmr(coin))

    @classmethod
    def from_state(cls, state: dict) -> "CoinBook":
        return cls(**state)

    def to_state(self) -> dict:
        state = asdict(self)
        if state["topup_due_ms"] is None:
            # Written only while a delayed top-up is pending: a book that never uses the delay
            # stores exactly the state it stored before the field existed.
            del state["topup_due_ms"]
        if state["hedge_share_prev"] == 1.0:
            del state["hedge_share_prev"]  # the same for a book that hedges in full
        return state

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
    book.topup_due_ms = None                      # nothing left to top up
    loss = penalty + (qh + qc) * max(liq - price, 0.0)
    book.liquidations += 1
    book.liquidation_loss += loss
    return [_event("liquidation", qh + qc, liq, (qh + qc) * liq, penalty, bar_ms=bar_ms,
                   hedge_units=qh, carry_units=qc, loss=loss)]


def _hedge_room(book: CoinBook, price: float, perp_cost: float, pool: "MarginPool | None" = None) -> float:
    """Short units the pool's free margin can open, fee included.

    With a shared pool the free margin is the whole pool's, not this book's own."""
    free = book.free_margin(price) if pool is None else pool.free_margin()
    return max(free, 0.0) / (price * (1.0 / book.leverage + perp_cost))


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


# ── one margin pool for several coin books (params.shared_margin) ─────────────────────────
#
# On Hyperliquid the margin of a cross account is shared: every short is backed by all the
# USDC on the account. The books keep their own ledgers (cash, spot, equity), but the margin
# questions -- may another short open, is a top-up due, is the account liquidated -- are asked
# of the SUM over the books. The caller (an engine or the q-lab stand) owns the pool:
#
#   1. set `pool.marks[coin]` to this bar's close for every book that has a price;
#   2. `pool_liquidate_if_breached` on the bar's highs;
#   3. `step(book, ..., pool=pool)` for each book that has data this bar;
#   4. `pool_top_up` once.
#
# Not modelled in a pool: the carry sleeve (B2Params refuses carry_enabled with shared_margin).

@dataclass
class MarginPool:
    books: dict[str, CoinBook] = field(default_factory=dict)
    marks: dict[str, float] = field(default_factory=dict)
    topup_due_ms: int | None = None

    def add(self, book: CoinBook, price: float) -> None:
        self.books[book.coin] = book
        self.marks[book.coin] = price

    def remove(self, coin: str) -> None:
        self.books.pop(coin, None)
        self.marks.pop(coin, None)

    def account_value(self) -> float:
        return sum(b.account_value(self.marks[c]) for c, b in self.books.items())

    def initial_margin(self) -> float:
        return sum(b.initial_margin(self.marks[c]) for c, b in self.books.items())

    def free_margin(self) -> float:
        return self.account_value() - self.initial_margin()


def pool_liquidate_if_breached(pool: MarginPool, *, highs: dict[str, float], bar_ms: int) -> list[dict]:
    """The pool is liquidated when the shorts' losses take its account value down to the sum of
    their maintenance margins; every short of the pool is then closed.

    `highs[coin]` is the bar's high for each coin with a short (the close of a book without data
    this bar). The bar's path is unknown, so all the highs are taken at once -- the worst case
    the bar's data allows, and exact for a pool of one coin. Each price moves from the book's
    last close to its high in proportion; the liquidation happens where account value meets
    maintenance margin, which is linear in that proportion. A pool already below maintenance at
    the last closes is liquidated at those closes.
    """
    hedged = [(c, b) for c, b in pool.books.items() if b.in_pos]
    if not hedged:
        return []
    usdc = sum(b.hl_usdc() for b in pool.books.values())
    base = slope = 0.0                      # account value - maintenance margin = base + slope * t
    refs: dict[str, float] = {}
    for coin, b in hedged:
        ref = b.last_price if b.last_price > 0 else b.entry_price
        move = max(highs[coin], ref) - ref
        refs[coin] = ref
        q = b.short_size
        base += q * (b.entry_price - ref) - q * b.mmr * ref
        slope -= q * move * (1.0 + b.mmr)
    base += usdc
    if base + slope > 0.0:
        return []
    t = 0.0 if base <= 0.0 or slope == 0.0 else base / -slope
    ev: list[dict] = []
    for coin, b in hedged:
        ref = refs[coin]
        liq = ref + t * (max(highs[coin], ref) - ref)
        q = b.short_size
        realized = q * (b.entry_price - liq)
        penalty = q * liq * b.mmr
        b.cash += realized
        b.short_realized += realized
        b.short_size, b.entry_price, b.in_pos = 0.0, 0.0, False
        if b.units_spot * pool.marks[coin] < b.position_size:
            b.refill_pending = True
        b.cash -= penalty
        b.topup_due_ms = None
        loss = penalty + q * max(liq - pool.marks[coin], 0.0)
        b.liquidations += 1
        b.liquidation_loss += loss
        ev.append(_event("liquidation", q, liq, q * liq, penalty, bar_ms=bar_ms, hedge_units=q,
                         carry_units=0.0, loss=loss, coin=coin, pool=True))
    pool.topup_due_ms = None
    return ev


def pool_top_up(pool: MarginPool, *, bar_ms: int, params: B2Params) -> list[dict]:
    """Step 6 of `step`, asked of the pool: top up once the shorts' losses have eaten into the
    pool's initial margin. Every losing short is topped up as for a single book (spot sold for
    its loss, the short cut by the same units); with `params.topup_delay_hours` the top-up takes
    effect that many hours after it fell due, whether or not it is still needed then."""
    if not any(b.in_pos for b in pool.books.values()):
        pool.topup_due_ms = None
        return []
    delay_ms = params.topup_delay_hours * HOUR_MS
    if delay_ms and pool.topup_due_ms is not None and bar_ms >= pool.topup_due_ms:
        pool.topup_due_ms = None
        return _pool_apply_top_up(pool, params, bar_ms)
    if pool.account_value() < params.rebalance_at_im_share * pool.initial_margin():
        if not delay_ms:
            return _pool_apply_top_up(pool, params, bar_ms)
        if pool.topup_due_ms is None:
            pool.topup_due_ms = bar_ms + delay_ms
    return []


def _pool_apply_top_up(pool: MarginPool, params: B2Params, bar_ms: int) -> list[dict]:
    cost = SPOT_TAKER + params.slippage + PERP_TAKER + params.slippage
    ev: list[dict] = []
    for coin, b in pool.books.items():
        if b.in_pos:
            for e in _top_up(b, pool.marks[coin], cost, bar_ms):
                ev.append({**e, "coin": coin})
    return ev


def _resize_hedge(book: CoinBook, P: float, perp_cost: float, min_ord: float, bar_ms: int,
                  pool: "MarginPool | None", margin: bool) -> list[dict]:
    """A partial hedge (params.partial_hedge_share < 1) follows the wanted share: grown when the
    signal turns from sideways to down, cut when it turns back. A change below the minimum order
    waits; growth is capped by the free margin as an entry is."""
    target = book.units_spot * book.hedge_share_prev
    delta = target - book.short_size
    if abs(delta) * P < min_ord:
        return []
    if delta > 0:
        if margin:
            delta = min(delta, max(_hedge_room(book, P, perp_cost, pool), 0.0))
            if delta * P < min_ord:
                return []
        fee = delta * P * perp_cost
        book.entry_price = (book.entry_price * book.short_size + P * delta) / (book.short_size + delta)
        book.short_size += delta
        book.cash -= fee
        book.perp_fees += fee
        book.trades += 1
        return [_event("hedge_grow", delta, P, delta * P, fee, bar_ms=bar_ms)]
    cut = -delta
    realized = cut * (book.entry_price - P)
    fee = cut * P * perp_cost
    book.cash += realized - fee
    book.short_realized += realized
    book.perp_fees += fee
    book.short_size -= cut
    return [_event("hedge_cut", cut, P, cut * P, fee, bar_ms=bar_ms, realized=realized)]


def signals_at(closes: list[float], funding: list[float], params: B2Params) -> tuple[bool, bool, float | None]:
    """Raw hedge wish, refill trend confirmation and carry APR at the LAST element.

    closes/funding are the hourly histories ending at the bar being closed.
    Mirrors harness.hedge_signal / build_trend_up / carry_pnl's rolling mean.
    """
    raw_hedge, _, trend_up, carry_apr = _signals(closes, funding, params)
    return raw_hedge, trend_up, carry_apr


def _signals(closes: list[float], funding: list[float], params: B2Params
             ) -> tuple[bool, bool, bool, float | None]:
    """`signals_at` plus whether the wish is MIXED: one window up, the other not."""
    n = len(closes)
    p = closes[-1]
    h_short, h_long = params.short_window_days * 24, params.long_window_days * 24
    mom_short = p / closes[-1 - h_short] - 1.0 if n > h_short else None
    mom_long = p / closes[-1 - h_long] - 1.0 if n > h_long else None
    mixed = False
    if mom_long is None:
        raw_hedge = False                                   # not enough history -> no hedge
    else:
        short_up = mom_short is not None and mom_short > params.hedge_threshold
        long_up = mom_long > params.hedge_threshold
        raw_hedge = not (short_up and long_up)
        mixed = short_up != long_up
    trend_up = mom_short is not None and mom_short > 0.0
    carry_apr = (sum(funding[-CARRY_WINDOW:]) / CARRY_WINDOW * HOURS_PER_YEAR
                 if len(funding) >= CARRY_WINDOW else None)
    return raw_hedge, mixed, trend_up, carry_apr


def _graded_share(closes: list[float], params: B2Params) -> float:
    """Share of the spot to hedge by the signal's strength (params.graded_hedge_depth): the mean,
    over the two windows, of how far the return is below the threshold as a share of the depth."""
    n = len(closes)
    h_short, h_long = params.short_window_days * 24, params.long_window_days * 24
    if n <= h_long:
        return 0.0                                          # not enough history -> no hedge
    total = 0.0
    for h in (h_short, h_long):
        mom = closes[-1] / closes[-1 - h] - 1.0
        total += min(max((params.hedge_threshold - mom) / params.graded_hedge_depth, 0.0), 1.0)
    return total / 2.0


def advance_signals(book: CoinBook, closes: list[float], funding_hist: list[float], params: B2Params) -> None:
    """Compute this bar's signals and store them for the next bar (sticky exit applied)."""
    raw, mixed, trend_up, carry_apr = _signals(closes, funding_hist, params)
    graded = _graded_share(closes, params) if params.graded_hedge_depth > 0.0 else 0.0
    if params.graded_hedge_depth > 0.0:
        raw = graded > 0.0      # one window below the threshold is enough; at it there is no strength
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
    if params.partial_hedge_share < 1.0 and raw:
        # A mixed signal hedges a part; while the sticky exit holds the hedge without a wish,
        # the last share is kept.
        book.hedge_share_prev = params.partial_hedge_share if mixed else 1.0
    elif params.graded_hedge_depth > 0.0 and raw:
        book.hedge_share_prev = graded
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
         funding_hist: list[float], params: B2Params, high: float | None = None,
         pool: MarginPool | None = None) -> list[dict]:
    """Advance one closed hourly bar. Returns the paper fills generated in this bar.

    `closes` / `funding_hist` must END at this bar (they feed the signals for the NEXT bar).
    `high` is the bar's high, used for the liquidation check (defaults to the close).
    `pool` is the shared margin pool this book belongs to (params.shared_margin): its liquidation
    (step 0) and top-up (step 6) are then the caller's (`pool_liquidate_if_breached`,
    `pool_top_up`), and a hedge may use the whole pool's free margin. Without it -- every
    production book -- the book is its own pool, as ever.
    """
    P = float(price)
    rate = float(funding_rate)
    perp_cost = PERP_TAKER + params.slippage
    spot_cost = SPOT_TAKER + params.slippage
    min_ord = params.min_order_usd
    margin = params.margin_enabled
    ev: list[dict] = []

    # 0) liquidation on the shorts carried into this bar
    if margin and pool is None:
        ev += _liquidate_if_breached(book, max(P, float(high if high is not None else P)), P, spot_cost, bar_ms)

    # 1) funding on an open hedge (short receives positive funding)
    if book.in_pos:
        f = book.short_size * P * rate
        book.cash += f
        book.funding_total += f

    want_hedge = book.hedge_prev

    # 2) hedge entry / exit
    if not book.in_pos and want_hedge:
        units = book.units_spot * book.hedge_share_prev
        limited = False
        if margin and _hedge_room(book, P, perp_cost, pool) < units:
            ev += _top_up(book, P, spot_cost + perp_cost, bar_ms)      # realise a pumped carry's loss
            ev += _shrink_carry_for_hedge(book, P, units, spot_cost, perp_cost, bar_ms)
            room = _hedge_room(book, P, perp_cost, pool)
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
        book.topup_due_ms = None                  # no short left to top up
        if book.units_spot * P < book.position_size:
            book.refill_pending = True
    elif book.in_pos and (params.partial_hedge_share < 1.0 or params.graded_hedge_depth > 0.0):
        ev += _resize_hedge(book, P, perp_cost, min_ord, bar_ms, pool if margin else None,
                            margin)

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
    #    (a shared pool does this once for all its books: `pool_top_up`)
    if margin and pool is None:
        if book.in_pos or book.carry_units:
            delay_ms = params.topup_delay_hours * HOUR_MS
            if delay_ms and book.topup_due_ms is not None and bar_ms >= book.topup_due_ms:
                # a delayed top-up arrives: sized to the losses as they stand now
                book.topup_due_ms = None
                ev += _top_up(book, P, spot_cost + perp_cost, bar_ms)
            elif book.account_value(P) < params.rebalance_at_im_share * book.initial_margin(P):
                if not delay_ms:
                    ev += _top_up(book, P, spot_cost + perp_cost, bar_ms)
                elif book.topup_due_ms is None:
                    book.topup_due_ms = bar_ms + delay_ms
        else:
            book.topup_due_ms = None

    # 7) signals of THIS bar, used at the next bar
    advance_signals(book, closes, funding_hist, params)
    book.last_bar_ms = bar_ms
    book.last_price = P
    return ev
