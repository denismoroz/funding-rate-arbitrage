"""The trend paper book: one HL cross-margin account holding signed perp positions.

Stepped one closed hourly bar at a time so a live engine can advance it bar by bar and resume after a
restart: every hour pays or receives funding and marks equity; at the rebalance hour the whole book is
resized to the day's target weights (signals.target_weights) computed from daily closes that were already
closed at that moment.

PAPER: fills happen at the bar close, taker fee + slippage charged as cost. No exchange call lives here.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from frab.constants import PERP_TAKER
from frab.strategy.trend.params import TrendParams

HOUR_MS = 3_600_000


@dataclass
class TrendBook:
    capital: float
    cash: float = 0.0
    units: dict[str, float] = field(default_factory=dict)      # signed perp units per coin
    entry: dict[str, float] = field(default_factory=dict)      # average entry price per open position
    weights: dict[str, float] = field(default_factory=dict)    # last target weights (reporting)
    signals: dict[str, float] = field(default_factory=dict)    # last ensemble signals (reporting)
    prices: dict[str, float] = field(default_factory=dict)     # last seen price per coin (reporting)
    started: bool = False
    last_bar_ms: int | None = None
    last_rebalance_ms: int | None = None
    fees: float = 0.0
    funding_total: float = 0.0
    realized: float = 0.0
    trades: int = 0
    rebalances: int = 0
    liquidations: int = 0
    skipped_min_order: int = 0
    size_scale: float = 1.0                                    # last book-volatility scaler (reporting)

    @classmethod
    def new(cls, params: TrendParams) -> "TrendBook":
        return cls(capital=params.capital_usd)

    # ── marks ────────────────────────────────────────────────────────────
    def unrealized(self, prices: dict[str, float]) -> float:
        return sum(u * (prices.get(c, self.entry.get(c, 0.0)) - self.entry.get(c, 0.0))
                   for c, u in self.units.items() if u)

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.unrealized(prices)

    def gross_notional(self, prices: dict[str, float]) -> float:
        return sum(abs(u) * prices.get(c, 0.0) for c, u in self.units.items())

    def net_notional(self, prices: dict[str, float]) -> float:
        return sum(u * prices.get(c, 0.0) for c, u in self.units.items())

    def maintenance_margin(self, prices: dict[str, float], params: TrendParams) -> float:
        return sum(abs(u) * prices.get(c, 0.0) * params.mmr(c) for c, u in self.units.items())

    def legs(self) -> int:
        return sum(1 for u in self.units.values() if u)

    # ── persistence ──────────────────────────────────────────────────────
    def to_state(self) -> dict:
        return asdict(self)

    @classmethod
    def from_state(cls, state: dict) -> "TrendBook":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in state.items() if k in known})


def _event(kind: str, coin: str, qty: float, price: float, fee: float, *, bar_ms: int, **extra) -> dict:
    return dict(kind=kind, coin=coin, qty=qty, price=price, notional=abs(qty) * price, fee=fee,
                bar_ms=bar_ms, **extra)


def start_book(book: TrendBook, *, bar_ms: int, params: TrendParams) -> list[dict]:
    """Fund the paper account; the engine sizes the book on the same bar."""
    book.cash = book.capital
    book.started = True
    book.last_bar_ms = bar_ms
    return [_event("fund", "-", 0.0, 0.0, 0.0, bar_ms=bar_ms, capital=book.capital)]


def apply_funding(book: TrendBook, prices: dict[str, float], rates: dict[str, float], bar_ms: int) -> None:
    """One hour of funding: a long pays a positive rate, a short receives it."""
    for coin, u in book.units.items():
        if not u:
            continue
        p, r = prices.get(coin), rates.get(coin, 0.0)
        if p is None or not r:
            continue
        flow = -u * p * r
        book.cash += flow
        book.funding_total += flow


def _trade(book: TrendBook, coin: str, delta: float, price: float, params: TrendParams, bar_ms: int,
           kind: str | None = None) -> dict | None:
    """Move the position by `delta` units at `price`, realising pnl on whatever it closes."""
    if not delta:
        return None
    cur = book.units.get(coin, 0.0)
    entry = book.entry.get(coin, price)
    new = cur + delta
    fee = abs(delta) * price * (PERP_TAKER + params.slippage)
    realized = 0.0
    if cur and (delta > 0) != (cur > 0):                       # reduce, close or flip
        closed = min(abs(delta), abs(cur)) * (1.0 if cur > 0 else -1.0)
        realized = closed * (price - entry)
        book.realized += realized
        book.cash += realized
    book.cash -= fee
    book.fees += fee
    book.trades += 1
    if abs(new) < 1e-12:
        book.units.pop(coin, None)
        book.entry.pop(coin, None)
        new = 0.0
    else:
        if cur and (new > 0) == (cur > 0) and abs(new) > abs(cur):
            book.entry[coin] = (cur * entry + delta * price) / new     # added to the position
        elif not cur or (new > 0) != (cur > 0):
            book.entry[coin] = price                                    # opened or flipped
        book.units[coin] = new
    if kind is None:
        kind = ("close" if new == 0 else "flip" if cur and (new > 0) != (cur > 0)
                else "open" if not cur else "increase" if abs(new) > abs(cur) else "reduce")
    return _event(kind, coin, delta, price, fee, bar_ms=bar_ms, realized=realized, units_after=new)


def rebalance(book: TrendBook, *, prices: dict[str, float], weights: dict[str, float],
              signals: dict[str, float], params: TrendParams, bar_ms: int,
              size_scale: float | None = None) -> list[dict]:
    """Resize every position to weight * equity. Orders below the exchange minimum are skipped,
    except a full close, which HL allows at any size."""
    equity = book.equity(prices)
    if size_scale is not None:
        book.size_scale = size_scale
    book.weights = {c: w for c, w in weights.items() if w}
    book.signals = dict(signals)
    ev: list[dict] = []
    if equity <= 0:
        return ev
    for coin in sorted(set(weights) | set(book.units)):
        price = prices.get(coin)
        if not price or price <= 0 or math.isnan(price):
            continue
        cur = book.units.get(coin, 0.0)
        target = weights.get(coin, 0.0) * equity / price
        delta = target - cur
        if not delta:
            continue
        closing = target == 0.0 and cur != 0.0
        if abs(delta) * price < params.min_order_usd and not closing:
            book.skipped_min_order += 1
            continue
        if abs(target) * price < params.min_order_usd and not closing:
            # the target leg itself is below the minimum: hold what we have rather than open dust
            if cur == 0.0:
                book.skipped_min_order += 1
                continue
        e = _trade(book, coin, delta, price, params, bar_ms)
        if e:
            ev.append(e)
    book.rebalances += 1
    book.last_rebalance_ms = bar_ms
    return ev


def liquidate_if_breached(book: TrendBook, prices: dict[str, float], params: TrendParams,
                          bar_ms: int) -> list[dict]:
    """HL closes the whole cross-margin account once equity falls under maintenance margin."""
    if not book.units:
        return []
    if book.equity(prices) > book.maintenance_margin(prices, params):
        return []
    ev = []
    for coin in list(book.units):
        price = prices.get(coin)
        if not price:
            continue
        e = _trade(book, coin, -book.units[coin], price, params, bar_ms, kind="liquidation")
        if e:
            ev.append(e)
    book.liquidations += 1
    book.weights = {}
    return ev


def step(book: TrendBook, *, bar_ms: int, prices: dict[str, float], funding: dict[str, float],
         params: TrendParams, weights: dict[str, float] | None = None,
         signals: dict[str, float] | None = None, size_scale: float | None = None) -> list[dict]:
    """Advance one closed hourly bar: funding, liquidation check, then a rebalance if one is due."""
    ev: list[dict] = []
    apply_funding(book, prices, funding, bar_ms)
    ev += liquidate_if_breached(book, prices, params, bar_ms)
    if weights is not None:
        ev += rebalance(book, prices=prices, weights=weights, signals=signals or {}, params=params,
                        bar_ms=bar_ms, size_scale=size_scale)
    for coin, p in prices.items():
        if p:
            book.prices[coin] = p
    book.last_bar_ms = bar_ms
    return ev
