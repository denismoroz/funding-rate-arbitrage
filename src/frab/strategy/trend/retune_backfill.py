"""Backfill of the re-tuned trend variant: replay the past through the same daily step the live engine runs.

Why this is honest (the same reasoning as q-lab's stand, `trend-retune-weekly`): every input of a bar is dated
BEFORE it --

* closes: the daily candles closed by the bar (`open + 1 day <= decision`), cut at the window start, so a
  coin's history counts from there exactly as in the stand's panel (q-lab live/trend.py: `running_closes`);
* volume: the candle BEFORE the last closed one (`retune.build_day`, q-lab `rolling(1 bar).sum().shift(1)`);
* market caps: the weekly CoinMarketCap snapshot dated before the bar (`cap_history.CapHistory.visible`);
* funding: records settled in (previous bar, this bar].

Nothing here looks at a candle, a funding record or a snapshot of a later bar; `tests/test_retune_backfill.py`
proves it by replacing everything after a bar with nonsense and replaying again.

`IndexedHistory.day` is `retune.build_day` made fast for a replay of ~650 bars: the same cut, found with
bisect on pre-sorted arrays instead of rescanning every record per bar. A test pins the two to identical
output. The replay itself IS `retune.advance_day` -- the code the live engine runs -- bar by bar; this module
only decides which bars to feed and what universes they get.

The live book is NOT touched: only the candidates' histories (returns, equity curves, first-active dates) and
the re-tune state are rebuilt. Positions start at launch.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Iterator

from frab.strategy.trend.cap_history import CapHistory
from frab.strategy.trend.coin_selector import DAY_MS, select_universes
from frab.strategy.trend.retune import (
    BarOutcome, DayData, RetuneState, advance_day, ceil_hour_of_minute)
from frab.strategy.trend.retune_params import RetuneParams

Series = dict[str, dict[int, tuple[float, float | None]]]
FundingRecords = dict[str, list[tuple[int, float]]]


def history_bars(first_label_ms: int, before_ms: int) -> list[int]:
    """Decision bars (00:00 UTC) of the replay: the first one closes the candle labelled `first_label_ms`,
    the last is the latest decision strictly before `before_ms` (the first live hour)."""
    if first_label_ms % DAY_MS:
        raise ValueError("backfill_start must be a UTC midnight")
    return list(range(first_label_ms + DAY_MS, before_ms, DAY_MS))


class IndexedHistory:
    """The fetched candles and funding, indexed for bar-by-bar cuts."""

    def __init__(self, series: Series, funding: FundingRecords, since_ms: int) -> None:
        self._by_open: dict[str, dict[int, tuple[float, float | None]]] = {}
        self._opens: dict[str, list[int]] = {}
        self._closes: dict[str, list[float]] = {}
        for coin, by_open in series.items():
            kept = {o: v for o, v in by_open.items() if o >= since_ms}
            if not kept:
                continue
            opens = sorted(kept)
            self._by_open[coin] = kept
            self._opens[coin] = opens
            self._closes[coin] = [kept[o][0] for o in opens]
        self._f_ceil: dict[str, list[int]] = {}
        self._f_rate: dict[str, list[float]] = {}
        for coin, recs in funding.items():
            ordered = sorted(recs, key=lambda r: r[0])
            self._f_ceil[coin] = [ceil_hour_of_minute(ts) for ts, _ in ordered]
            self._f_rate[coin] = [r for _, r in ordered]

    @property
    def coins(self) -> list[str]:
        return sorted(self._opens)

    def universe_inputs(self, decision_ms: int) -> tuple[dict[str, int], set[str], dict[str, float]]:
        """(daily closes held, coins with a candle closed at the bar, trailing USD volume) at one bar --
        what `select_universes` reads, without building the close lists."""
        last_open = decision_ms - DAY_MS
        counts: dict[str, int] = {}
        tradeable: set[str] = set()
        volume_usd: dict[str, float] = {}
        for coin, opens in self._opens.items():
            k = bisect_right(opens, last_open)
            if not k:
                continue
            counts[coin] = k
            by_open = self._by_open[coin]
            if last_open in by_open:
                tradeable.add(coin)
            before = by_open.get(last_open - DAY_MS)
            if before is not None and before[1] is not None:
                volume_usd[coin] = before[1] * before[0]
        return counts, tradeable, volume_usd

    def day(self, decision_ms: int, prev_ms: int | None) -> DayData:
        """Same output as `retune.build_day` on the same (cut) data."""
        last_open = decision_ms - DAY_MS
        closes: dict[str, list[float]] = {}
        price_now: dict[str, float] = {}
        price_prev: dict[str, float] = {}
        volume_usd: dict[str, float] = {}
        for coin, opens in self._opens.items():
            k = bisect_right(opens, last_open)
            if not k:
                continue
            by_open = self._by_open[coin]
            closes[coin] = self._closes[coin][:k]
            if last_open in by_open:
                price_now[coin] = by_open[last_open][0]
            before = by_open.get(last_open - DAY_MS)
            if before is not None and before[1] is not None:
                volume_usd[coin] = before[1] * before[0]
            if prev_ms is not None and (prev_ms - DAY_MS) in by_open:
                price_prev[coin] = by_open[prev_ms - DAY_MS][0]
        funding: dict[str, float] = {}
        if prev_ms is not None:
            for coin, ceils in self._f_ceil.items():
                lo, hi = bisect_right(ceils, prev_ms), bisect_right(ceils, decision_ms)
                if hi > lo:
                    funding[coin] = sum(self._f_rate[coin][lo:hi])
        return DayData(decision_ms, closes, price_now, price_prev, funding, volume_usd)


def universes_for_bar(hist: IndexedHistory, snapshot: dict, params: RetuneParams, bar: int
                      ) -> dict[str, list[str]]:
    counts, tradeable, volume = hist.universe_inputs(bar)
    trend = params.trend
    return select_universes(
        cap_ranked=[r[0] for r in snapshot["ranked"]], closes_count=counts, tradeable=tradeable,
        volume_usd=volume, k_cap=params.top_k_by_market_cap, k_volume=params.top_k_by_volume,
        min_history=trend.min_history_days if params.top_k_requires_history else None)


def _snapshot(cap: CapHistory, bar: int) -> dict:
    snap = cap.snapshot_for_bar(bar)
    if snap is None:
        raise ValueError(f"cap history starts at {cap.first_date}: no ranking is visible at bar {bar}; "
                         "export it from an earlier snapshot")
    return snap


def funding_needs(hist: IndexedHistory, cap: CapHistory, params: RetuneParams, bars: list[int]
                  ) -> dict[str, tuple[int, int]]:
    """{coin: (first bar, last bar) at which some candidate could hold it}. Funding is only read for held
    coins, and a candidate holds only coins of its universe; a weight decided at bar b earns funding over
    (b, b + 1 day], so the funding window of a coin is (first bar, last bar + 1 day]. A superset of what the
    replay reads, and far smaller than "everything since the window start"."""
    span: dict[str, tuple[int, int]] = {}
    for bar in bars:
        for coins in universes_for_bar(hist, _snapshot(cap, bar), params, bar).values():
            for c in coins:
                span[c] = (span[c][0] if c in span else bar, bar)
    return span


@dataclass
class ReplayResult:
    state: RetuneState
    last: BarOutcome | None
    events: list[dict] = field(default_factory=list)       # the weekly re-tunes of the history, oldest first
    universes: dict[str, list[str]] = field(default_factory=dict)
    bars: int = 0


def iter_replay(hist: IndexedHistory, cap: CapHistory, params: RetuneParams, bars: list[int],
                state: RetuneState | None = None) -> Iterator[tuple[int, BarOutcome, dict]]:
    """Feed the bars to `advance_day`; yields after each so a caller can give the event loop a turn."""
    state = state or RetuneState.new(params)
    for bar in bars:
        snap = _snapshot(cap, bar)
        universes = universes_for_bar(hist, snap, params, bar)
        state.cap_snapshot = snap
        yield bar, advance_day(state, hist.day(bar, state.last_bar_ms), params, universes,
                               phase="history"), universes


def replay(hist: IndexedHistory, cap: CapHistory, params: RetuneParams, bars: list[int]
           ) -> ReplayResult:
    state = RetuneState.new(params)
    result = ReplayResult(state=state, last=None)
    for _, outcome, universes in iter_replay(hist, cap, params, bars, state):
        absorb(result, outcome, universes)
    return result


def absorb(result: ReplayResult, outcome: BarOutcome, universes: dict[str, list[str]]) -> None:
    """Collect one replayed bar. Only the weekly re-tunes are kept as events: the 600-odd daily records of
    the history would bury the live ones."""
    result.last = outcome
    result.universes = universes
    result.bars += 1
    result.events += [e for e in outcome.events if e["kind"] == "retune"]


def summarize(result: ReplayResult, cap: CapHistory, params: RetuneParams, bars: list[int]) -> dict:
    """What the API shows about the rebuilt past; stored in the state."""
    state = result.state
    snap = cap.visible(bars[-1]) if bars else None
    age_days = None if snap is None else (bars[-1] - DAY_MS - snap.date_ms) / DAY_MS
    choices = [(e["bar_ms"], e["choice"]) for e in result.events]
    first_pick = next((ms for ms, c in choices if c), None)
    return dict(
        from_label=params.backfill_start, first_bar_ms=bars[0] if bars else None,
        last_bar_ms=bars[-1] if bars else None, bars=len(bars), refits=state.refits,
        first_historical_choice_ms=first_pick, chosen=state.chosen,
        switches=sum(1 for (_, a), (_, b) in zip(choices, choices[1:]) if a != b),
        cap_history=dict(path=cap.path, first=cap.first_date, last=cap.last_date,
                         last_used=None if snap is None else snap.date, age_days=age_days,
                         stale=age_days is not None and age_days > params.cap_history_max_age_days))
