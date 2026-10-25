"""Monthly re-tuning of the Bv2 book: simulate two candidates continuously, trade the better one of the past.

The frab counterpart of q-lab's `qlab.strategies.retune.Retune` wrapped around `qlab.strategies.live.bv2.LiveBv2`
(spec `bv2-research-headroom-retune`). Pure functions and plain-dict state, no I/O: the engine feeds it one closed
hourly bar at a time (`advance`) and persists `RetuneState`. Everything a decision reads is dated before it, which
is what makes the choice a forward test by construction.

Semantics mirrored, with the q-lab line each one comes from (`retune.py` unless noted):

* Each candidate runs CONTINUOUSLY as its own full set of per-coin `CoinBook`s (this module's `Candidate`), from
  the same start as the stand starts them, stepped every closed hourly bar whichever candidate is chosen
  ("Each candidate runs as the inner strategy runs alone", docstring; `live/bv2.py` for when each coin's book
  starts and how bars are stepped -- the driver here is a streaming port of it: a coin's books starts with the
  warm-up of `engine._warm_signals`, its start bar buys the spot and is stepped too).
* Re-tune on the first bar of every UTC calendar month, never on the very first bar of the history (`refit_rows`).
* Score = mean / std(ddof=1) of the candidate's hourly net returns realised BEFORE the refit, over the expanding
  window that starts at the candidate's first active bar (`_score`, `past[...][past.index >= started]`). The
  stand's "row t" return is earned over the NEXT candle (decision at the close of candle t), so the rows before
  the refit row `tau` end with the move of candle `tau` itself; here that is `E_h / E_(h-1) - 1` for
  `first_active < h <= tau`. A refit therefore happens when the first candle of the month has closed, and the
  chosen candidate earns from the following candle on. Computed from running sums (Welford), not stored lists.
* A candidate is eligible only if it started before the PREVIOUS refit (traded through one whole period);
  a NaN score never wins; strictly greater wins, ties go to the earlier candidate.
* No eligible candidate -> the book holds cash until the next refit; never a favourite.

## What differs from the stand, and why

The stand turns each candidate's books into WEIGHTS and lets its harness account for them with the spec's costs
(5.25 bps fee + 0.9 bps slippage on weight changes); candidate scores are the harness's net returns. Frab's book
accounts in money: the candidate's hourly return here is its own equity return, with frab's own taker fees
(`SPOT_TAKER`, `PERP_TAKER`) and `params.slippage` already inside the fills. So scores can differ slightly, and
a month where two Sharpes are close can flip. Nothing here is tuned to agree.

## Composite accounting (the frab counterpart of "take the chosen candidate's weights")

The published book is not a third `CoinBook` that trades on its own. It is the chosen candidate's book seen at a
scale:

    B   book equity; starts at the configured capital at the first choice, from then on
    k   B / E_chosen   (E_chosen = the candidate's total equity, all coins, a coin that has not started counts
                        its capital as cash, exactly like the stand's `book_equity`)

Between two refits B moves by the chosen candidate's hourly equity return (`B = k * E_chosen`, k constant);
the positions shown (units of spot and perp, cash, margin) are the candidate's per coin multiplied by k, and the
candidate's fills are published multiplied by k as well. When the choice changes at a refit the book must trade
from candidate a's scaled positions to candidate b's. That trade is charged per coin and leg on the ABSOLUTE
difference of the two scaled positions, at the close of the refit bar:

    cost = |k_b * spot_b - k_a * spot_a| * P * (SPOT_TAKER + slippage)
         + |k_b * perp_b - k_a * perp_a| * P * (PERP_TAKER + slippage)

and recorded as `retune_switch` fills (one per coin and leg). B drops by the cost and k_b = (B - cost) / E_b;
the cost depends on k_b, so it is solved by iteration (converges to ~1e-12 in a few rounds).

* The first choice is a switch from cash: it opens the positions with B = capital before the cost. The published
  curve gets a row at the capital stamped with the open of the refit bar, so the opening cost is its first move.
* A month with no eligible candidate is a switch to cash (closing the positions), after which B stays constant.
  A "cash" month publishes zero positions and the book equity spread evenly over the coins.
* Per-coin equity jumps at a switch because the two candidates split their money between coins differently;
  the sum over coins is continuous apart from the cost.

## Backfill and launch

The whole history is replayed once when the book is created, in the same code path as live ticks; the engine
marks everything produced by that first run as backfilled (`launch_ms`, event `phase`). The simulation state
rides in one `b2_books` row (coin `__retune__`) saved in the same transaction as the published rows, so a
restart resumes at the next bar instead of replaying the history.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from frab.constants import PERP_TAKER, SPOT_TAKER
from frab.strategy.b2.book import CoinBook, advance_signals, start_book, step
from frab.strategy.b2.engine import HISTORY_BARS, WARMUP_BARS
from frab.strategy.b2.params import B2Params
from frab.strategy.b2.retune_history import CoinSeries
from frab.strategy.b2.retune_params import B2RetuneParams

HOUR_MS = 3_600_000
STATE_VERSION = 1
RETUNE_COIN = "__retune__"            # the b2_books row that carries `RetuneState`

# Scaled by k when a candidate's coin book is shown as the published book.
SCALED_FIELDS = ("cash", "units_spot", "short_size", "carry_cash", "carry_target", "carry_units",
                 "position_size", "reserve")
# Cumulative since the first choice: accumulated bar by bar at the k in force.
CUM_MONEY = ("funding_total", "short_realized", "perp_fees", "spot_fees", "carry_funding", "carry_fees",
             "margin_fees", "liquidation_loss")
CUM_COUNT = ("trades", "rebals", "carry_trades", "margin_rebals", "liquidations", "hedge_limited",
             "carry_blocked_hours")
# Money fields of a candidate fill that are multiplied by k when the fill is published.
EVENT_SCALED = ("qty", "notional", "fee", "loss", "realized", "hedge_units", "carry_units")


# ── calendar ────────────────────────────────────────────────────────────────

def _month(ms: int) -> int:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.year * 12 + dt.month - 1


def is_refit_bar(bar_ms: int) -> bool:
    """True for the first hourly bar of a UTC calendar month (q-lab `refit_rows`, refit_every = M)."""
    return _month(bar_ms) != _month(bar_ms - HOUR_MS)


def next_refit_ms(after_ms: int) -> int:
    """Open time of the first bar of the first month that starts strictly after `after_ms`."""
    dt = datetime.fromtimestamp(after_ms / 1000, tz=timezone.utc)
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000)


# ── scoring ─────────────────────────────────────────────────────────────────

def sharpe(n: int, mean: float, m2: float) -> float:
    """mean / std (ddof=1) from running sums; NaN when undefined (q-lab `_score`)."""
    if n < 2:
        return math.nan
    std = math.sqrt(m2 / (n - 1))
    return mean / std if std and not math.isnan(std) else math.nan


@dataclass
class Candidate:
    name: str
    params: B2Params
    books: dict[str, CoinBook]
    last_equity: float | None = None          # total equity after the previous bar
    first_active_ms: int | None = None        # first bar on which any coin book was running
    n: int = 0                                # hourly returns accumulated: (first_active, last bar]
    mean: float = 0.0
    m2: float = 0.0

    def add_return(self, r: float) -> None:
        self.n += 1
        delta = r - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (r - self.mean)

    @property
    def score(self) -> float:
        return sharpe(self.n, self.mean, self.m2)

    def to_dict(self) -> dict:
        return {"books": {c: b.to_state() for c, b in self.books.items()}, "last_equity": self.last_equity,
                "first_active_ms": self.first_active_ms, "n": self.n, "mean": self.mean, "m2": self.m2}


def choose(candidates: dict[str, Candidate], previous_refit_ms: int | None) -> tuple[str | None, dict]:
    """The q-lab selection rule: (winner or None, per-candidate scores for the log)."""
    best, best_score = None, -math.inf
    scores: dict[str, dict] = {}
    for name, cand in candidates.items():
        started = cand.first_active_ms
        eligible = started is not None and previous_refit_ms is not None and started < previous_refit_ms
        score = cand.score
        scores[name] = {"eligible": eligible, "score": None if math.isnan(score) else score, "n": cand.n,
                        "first_active_ms": started}
        if eligible and not math.isnan(score) and score > best_score:
            best, best_score = name, score
    return best, scores


# ── state ───────────────────────────────────────────────────────────────────

@dataclass
class RetuneState:
    candidates: dict[str, Candidate]
    fingerprint: dict
    last_bar_ms: int | None = None
    last_refit_ms: int | None = None
    refits: int = 0
    chosen: str | None = None
    chosen_score: float | None = None
    book_equity: float | None = None          # B; None until the first choice
    scale: float | None = None                # k; None while the book is in cash
    first_choice_ms: int | None = None        # when the first choice was made (the close of the refit bar)
    changes: int = 0                          # refits at which one candidate replaced the other (q-lab: switches)
    pub: dict[str, dict] = field(default_factory=dict)      # coin -> cumulative published accounting
    last_retune: dict | None = None
    launch_ms: int | None = None
    backfill: dict | None = None

    @classmethod
    def new(cls, params: B2RetuneParams) -> "RetuneState":
        cands = {}
        for name in params.candidates:
            p = params.candidate_params(name)
            cands[name] = Candidate(name, p, {c: CoinBook.new(c, p) for c in params.coins})
        pub = {c: {k: 0.0 for k in CUM_MONEY} | {k: 0 for k in CUM_COUNT} for c in params.coins}
        return cls(candidates=cands, fingerprint=params.fingerprint(), pub=pub)

    def to_dict(self) -> dict:
        return {"v": STATE_VERSION, "fingerprint": self.fingerprint, "last_bar_ms": self.last_bar_ms,
                "last_refit_ms": self.last_refit_ms, "refits": self.refits, "chosen": self.chosen,
                "chosen_score": self.chosen_score, "book_equity": self.book_equity, "scale": self.scale,
                "first_choice_ms": self.first_choice_ms, "changes": self.changes, "pub": self.pub,
                "last_retune": self.last_retune, "launch_ms": self.launch_ms, "backfill": self.backfill,
                "candidates": {n: c.to_dict() for n, c in self.candidates.items()}}

    @classmethod
    def from_dict(cls, d: dict, params: B2RetuneParams) -> "RetuneState":
        if d.get("v") != STATE_VERSION:
            raise ValueError(f"b2_retune state version {d.get('v')!r} is not {STATE_VERSION}")
        if d["fingerprint"] != params.fingerprint():
            raise ValueError("b2_retune: the strategy params changed since the state was saved; the saved "
                             "history no longer matches them (reset the book to start over)")
        cands = {}
        for name in params.candidates:
            raw = d["candidates"][name]
            cands[name] = Candidate(name, params.candidate_params(name),
                                    {c: CoinBook.from_state(dict(s)) for c, s in raw["books"].items()},
                                    raw["last_equity"], raw["first_active_ms"], raw["n"], raw["mean"], raw["m2"])
        return cls(candidates=cands, fingerprint=d["fingerprint"], last_bar_ms=d["last_bar_ms"],
                   last_refit_ms=d["last_refit_ms"], refits=d["refits"], chosen=d["chosen"],
                   chosen_score=d["chosen_score"], book_equity=d["book_equity"], scale=d["scale"],
                   first_choice_ms=d["first_choice_ms"], changes=d["changes"], pub=d["pub"],
                   last_retune=d["last_retune"], launch_ms=d["launch_ms"], backfill=d.get("backfill"))


@dataclass
class BarOutput:
    """What one bar adds to the published book."""
    bar_ms: int
    events: list[dict] = field(default_factory=list)          # fills + `retune` log, with a `coin` key
    equity_rows: list[dict] = field(default_factory=list)     # one per coin, once the book exists
    views: dict[str, CoinBook] = field(default_factory=dict)  # the published book per coin, after this bar


# ── one candidate on one bar ────────────────────────────────────────────────

def _step_coin(cand: Candidate, coin: str, series: CoinSeries, bar_ms: int, params: B2RetuneParams
               ) -> tuple[list[dict], float]:
    """Advance one coin book of a candidate through the bar; returns (fills, the coin's equity)."""
    book = cand.books[coin]
    k = series.index(bar_ms)
    price = series.close[k]
    if math.isnan(price) or math.isnan(series.high[k]):
        raise ValueError(f"{coin}: no candle for {bar_ms} (the engine must only step bars the history covers)")
    fresh: list[dict] = []
    if not book.started:
        if bar_ms < params.start_ms(coin):
            return [], book.capital                  # not running yet: its share is cash (stand: coin_equity)
        _warm(book, series, k, cand.params)
        fresh += start_book(book, bar_ms=bar_ms, price=price, params=cand.params)
        if cand.first_active_ms is None:
            cand.first_active_ms = bar_ms
    rate = series.funding[k]
    rate = 0.0 if math.isnan(rate) else rate
    fresh += step(book, bar_ms=bar_ms, price=price, funding_rate=rate,
                  closes=series.close[max(0, k - HISTORY_BARS + 9):k + 1],
                  funding_hist=_funding_window(series, k), params=cand.params, high=series.high[k])
    return fresh, book.equity(price)


def _funding_window(series: CoinSeries, k: int) -> list[float]:
    return [0.0 if math.isnan(f) else f for f in series.funding[max(0, k - 8):k + 1]]


def _warm(book: CoinBook, series: CoinSeries, k0: int, p: B2Params) -> None:
    """Rebuild the previous-bar signal state the book would carry (engine `_warm_signals`)."""
    if k0 < HISTORY_BARS + WARMUP_BARS:
        raise ValueError(f"{series.coin}: only {k0} hours of history before the book starts, "
                         f"{HISTORY_BARS + WARMUP_BARS} needed")
    for k in range(k0 - WARMUP_BARS, k0):
        advance_signals(book, series.close[max(0, k - HISTORY_BARS + 9):k + 1], _funding_window(series, k), p)


def _positions(book: CoinBook, price: float, margin_enabled: bool) -> tuple[float, float]:
    """(spot units, perp short units) the book holds, carry sleeve included (live/bv2.py exposure columns)."""
    if not book.started:
        return 0.0, 0.0
    carry = 0.0
    if book.carry_on:
        carry = book.carry_units if margin_enabled else book.carry_target / price
    return book.units_spot + carry, book.hedge_units() + carry


def _cum(book: CoinBook) -> dict:
    return {f: getattr(book, f) for f in CUM_MONEY + CUM_COUNT}


# ── published view ──────────────────────────────────────────────────────────

def _view(book: CoinBook | None, coin: str, k: float | None, pub: dict, params: B2RetuneParams,
          cash_share: float) -> CoinBook:
    """The coin book as the published book holds it: the candidate's, scaled by k (module docstring)."""
    capital = params.base.book_capital
    if book is None or k is None or not book.started:
        # in cash (no choice, or a coin whose book has not started): the share of B that sits idle
        share = cash_share if book is None or k is None else k * book.capital
        view = CoinBook(coin=coin, capital=capital, position_size=0.0, reserve=0.0, started=True, cash=share,
                        leverage=params.base.leverage(coin), mmr=params.base.mmr(coin))
    else:
        view = replace(book, capital=capital)
        for f in SCALED_FIELDS:
            setattr(view, f, getattr(book, f) * k)
    for f in CUM_MONEY + CUM_COUNT:
        setattr(view, f, pub[f])
    return view


def _row(view: CoinBook, ts_ms: int, price: float) -> dict:
    return dict(ts_ms=ts_ms, coin=view.coin, price=price, equity=view.equity(price),
                book_equity=view.book_equity(price), cash=view.cash, spot_value=view.units_spot * price,
                short_pnl=view.short_pnl(price), carry_cash=view.carry_cash, hedge_on=view.in_pos,
                carry_on=view.carry_on)


# ── the composite ───────────────────────────────────────────────────────────

def _switch(state: RetuneState, params: B2RetuneParams, series: dict[str, CoinSeries], bar_ms: int,
            old: str | None, new: str | None, equity_before: float) -> list[dict]:
    """Trade from the old choice's scaled positions to the new one's and charge it (module docstring)."""
    slip = params.base.slippage
    spot_rate, perp_rate = SPOT_TAKER + slip, PERP_TAKER + slip
    k_old = state.scale if old is not None else None
    e_new = sum(_coin_equity(state.candidates[new], c, series[c], bar_ms) for c in params.coins) if new else None

    prices = {c: series[c].close[series[c].index(bar_ms)] for c in params.coins}
    margin = params.base.margin_enabled
    old_pos = {c: _scaled_positions(state.candidates[old].books[c], prices[c], k_old, margin) if old else (0.0, 0.0)
               for c in params.coins}

    def legs(k_new: float | None) -> list[tuple[str, str, float, float]]:
        out = []
        for c in params.coins:
            ns, nq = _scaled_positions(state.candidates[new].books[c], prices[c], k_new, margin) if new else (0.0, 0.0)
            for leg, delta, rate in (("spot", ns - old_pos[c][0], spot_rate), ("perp", nq - old_pos[c][1], perp_rate)):
                out.append((c, leg, abs(delta), abs(delta) * prices[c] * rate))
        return out

    k_new = equity_before / e_new if new else None
    for _ in range(50):                               # B_after = B - cost(k_new), k_new = B_after / E_new
        cost = sum(fee for *_, fee in legs(k_new))
        if not new:
            break
        nxt = (equity_before - cost) / e_new
        converged = abs(nxt - k_new) <= 1e-13 * max(1.0, abs(k_new))
        k_new = nxt
        if converged:
            break
    fills = legs(k_new)
    total = sum(fee for *_, fee in fills)
    events = []
    for coin, leg, units, fee in fills:
        if units <= 1e-12:
            continue
        price = prices[coin]
        events.append(dict(kind="retune_switch", coin=coin, qty=units, price=price, notional=units * price,
                           fee=fee, bar_ms=bar_ms, leg=leg, previous_choice=old, choice=new))
        state.pub[coin]["spot_fees" if leg == "spot" else "perp_fees"] += fee
    state.book_equity = equity_before - total
    state.scale = state.book_equity / e_new if new else None
    return events


def _scaled_positions(book: CoinBook, price: float, k: float | None, margin_enabled: bool
                      ) -> tuple[float, float]:
    if k is None:
        return 0.0, 0.0
    spot, perp = _positions(book, price, margin_enabled)
    return k * spot, k * perp


def _coin_equity(cand: Candidate, coin: str, series: CoinSeries, bar_ms: int) -> float:
    book = cand.books[coin]
    return book.equity(series.close[series.index(bar_ms)]) if book.started else book.capital


def advance(state: RetuneState, params: B2RetuneParams, series: dict[str, CoinSeries], bar_ms: int) -> BarOutput:
    """Process the closed bar that OPENED at `bar_ms`: step every candidate, move the composite, re-tune if this
    is the first bar of a month, and describe the published book after the bar."""
    if state.last_bar_ms is not None and bar_ms != state.last_bar_ms + HOUR_MS:
        raise ValueError(f"bars must arrive in order: after {state.last_bar_ms} came {bar_ms}")
    out = BarOutput(bar_ms)
    held = state.chosen                               # the choice that earns this bar
    k = state.scale

    # 1) every candidate, every coin
    fills: dict[str, dict[str, list[dict]]] = {}
    cum_before = {c: _cum(state.candidates[held].books[c]) for c in params.coins} if held else {}
    equity: dict[str, float] = {}
    for name, cand in state.candidates.items():
        total = 0.0
        fills[name] = {}
        for coin in params.coins:
            ev, eq = _step_coin(cand, coin, series[coin], bar_ms, params)
            fills[name][coin] = ev
            total += eq
        equity[name] = total
        if cand.last_equity is not None and cand.first_active_ms is not None and bar_ms > cand.first_active_ms:
            cand.add_return(total / cand.last_equity - 1.0)
        cand.last_equity = total

    # 2) the composite follows the candidate that was chosen before this bar
    if held is not None:
        state.book_equity = k * equity[held]
        for coin in params.coins:
            after = _cum(state.candidates[held].books[coin])
            for f in CUM_MONEY:
                state.pub[coin][f] += k * (after[f] - cum_before[coin][f])
            for f in CUM_COUNT:
                state.pub[coin][f] += after[f] - cum_before[coin][f]
            for e in fills[held][coin]:
                out.events.append(_scaled_fill(e, coin, k, held))

    # 3) re-tune at the first bar of a month (not the first bar of the history)
    unborn = state.book_equity is None
    if state.last_bar_ms is not None and is_refit_bar(bar_ms):
        out.events += _refit(state, params, series, bar_ms)
    state.last_bar_ms = bar_ms
    if unborn and state.book_equity is not None:
        # the book was born at this refit with its capital: the row before the opening trade, so the
        # published curve starts at the capital and the opening cost is its first move
        n = len(params.coins)
        for coin in params.coins:
            share = _view(None, coin, None, state.pub[coin], params, cash_share=params.base.capital_usd / n)
            out.equity_rows.append(_row(share, bar_ms, series[coin].close[series[coin].index(bar_ms)]))

    # 4) the published book after the bar
    if state.book_equity is not None:
        n = len(params.coins)
        chosen = state.candidates.get(state.chosen) if state.chosen else None
        for coin in params.coins:
            price = series[coin].close[series[coin].index(bar_ms)]
            view = _view(chosen.books[coin] if chosen else None, coin, state.scale, state.pub[coin], params,
                         cash_share=state.book_equity / n)
            view.last_bar_ms, view.last_price = bar_ms, price
            out.views[coin] = view
            out.equity_rows.append(_row(view, bar_ms + HOUR_MS, price))
    return out


def _scaled_fill(e: dict, coin: str, k: float, candidate: str) -> dict:
    scaled = {key: (val * k if key in EVENT_SCALED else val) for key, val in e.items()}
    return scaled | {"coin": coin, "candidate": candidate, "scale": k}


def _refit(state: RetuneState, params: B2RetuneParams, series: dict[str, CoinSeries], bar_ms: int) -> list[dict]:
    previous = state.chosen
    previous_refit = state.last_refit_ms
    best, scores = choose(state.candidates, previous_refit)
    events: list[dict] = []
    if state.book_equity is None:
        if best is not None:                          # the first choice: the book starts with its capital
            events += _switch(state, params, series, bar_ms, None, best, params.base.capital_usd)
            state.first_choice_ms = bar_ms + HOUR_MS
    elif best != previous:
        events += _switch(state, params, series, bar_ms, previous, best, state.book_equity)
    if previous is not None and best is not None and best != previous:
        state.changes += 1                           # one candidate replaced by the other (not cash <-> candidate)
    state.chosen = best
    state.chosen_score = None if best is None else scores[best]["score"]
    state.last_refit_ms = bar_ms
    state.refits += 1
    state.last_retune = {"bar_ms": bar_ms, "decided_ms": bar_ms + HOUR_MS, "choice": best,
                         "previous_choice": previous, "scores": scores, "refit": state.refits}
    events.append(dict(kind="retune", coin="-", qty=0.0, price=0.0, notional=0.0, fee=0.0, bar_ms=bar_ms,
                       choice=best, previous_choice=previous, score=state.chosen_score, scores=scores,
                       refit=state.refits, previous_refit_ms=previous_refit,
                       first_choice=best is not None and state.first_choice_ms == bar_ms + HOUR_MS,
                       equity_after=state.book_equity))
    return events
