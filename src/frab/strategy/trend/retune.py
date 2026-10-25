"""Weekly re-tuning of the trend variant: simulate every candidate daily, trade the best of the past.

A streaming port of q-lab's `qlab.strategies.retune.Retune` wrapped around
`qlab.strategies.live.trend.LiveTrendTSMOMEnsemble` (q-lab, validated as `trend-retune-weekly`). Pure
functions and plain-dict state, no I/O: the engine feeds it one `DayData` per daily rebalance bar and gets
the target weights back. Everything it uses is dated before the decision, which is what makes the choice a
forward test by construction (q-lab retune.py:15-22).

Semantics mirrored, with the q-lab line each one comes from:

* Each candidate is the inner trend strategy run alone, on its own equity curve (retune.py:34-36).
* Candidate returns are indexed by DECISION bar t and realised over (t, t+1]; the return of row t is
  appended at bar t+1 (qlab.harness.run: net = gross + accrual - turnover * cost; live/trend.py:250-276).
* `book_vol_scale` sees the candidate's equity up to the PREVIOUS bar only, and that equity excludes
  trading costs (live/trend.py:228-231, 63-77); the score DOES include them (retune.py:26-27).
* Re-tune on the first bar of every week, never on the first bar of all (retune.py:95-103, `refit_rows`).
* Score = mean / std(ddof=1) of the candidate's returns realised before the bar (expanding window)
  (retune.py:106-110, 180, 189), NaN when undefined.
* Eligible only if the candidate has traded through one whole re-tuning period before the previous
  re-tune (retune.py:185-188): `first_active < previous_refit`; the first re-tune never chooses.
* Strictly greater score wins, so ties go to the earlier candidate in grid order; NaN never wins
  (retune.py:183-191). No eligible candidate -> flat until the next re-tune, no favourite (retune.py:32-33).
* Between re-tunes the chosen candidate's weights are followed bar by bar (retune.py:193-195).
* The leverage cap of `signals.target_weights` normalises over EVERY coin of the Hyperliquid universe that
  has history, then the universe rule zeroes the coins outside the top-K (live/trend.py:234-247) -- this is
  how q-lab's runs behaved (`coins: null` = the whole panel), so the same is done here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from frab.strategy.trend.coin_selector import DAY_MS, week_key
from frab.strategy.trend.retune_params import RetuneParams
from frab.strategy.trend.signals import book_vol_scale, ensemble_signal, target_weights

HOUR_MS = 3_600_000
EQUITY_KEEP = 128           # equity points kept per candidate; book_vol_scale reads the last window + 1


# ── state ────────────────────────────────────────────────────────────────
@dataclass
class CandidateState:
    name: str
    universe: str
    lookbacks: list[int]
    equity_hist: list[float] = field(default_factory=lambda: [1.0])
    prev_weights: dict[str, float] = field(default_factory=dict)
    prev_cost: float = 0.0                       # cost of moving into prev_weights (charged to that row)
    returns: list[list[float]] = field(default_factory=list)   # [decision_bar_ms, net_return]
    first_active_ms: int | None = None
    weights: dict[str, float] = field(default_factory=dict)    # latest decided
    scale: float = 1.0

    def to_dict(self) -> dict:
        return dict(name=self.name, universe=self.universe, lookbacks=list(self.lookbacks),
                    equity_hist=list(self.equity_hist), prev_weights=dict(self.prev_weights),
                    prev_cost=self.prev_cost, returns=[list(r) for r in self.returns],
                    first_active_ms=self.first_active_ms, weights=dict(self.weights), scale=self.scale)

    @classmethod
    def from_dict(cls, d: dict) -> "CandidateState":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class RetuneState:
    last_bar_ms: int | None = None
    last_week: str | None = None
    last_refit_ms: int | None = None
    refits: int = 0
    chosen: str | None = None
    chosen_score: float | None = None
    cap_snapshot: dict | None = None
    candidates: dict[str, CandidateState] = field(default_factory=dict)

    @classmethod
    def new(cls, params: RetuneParams) -> "RetuneState":
        st = cls()
        for name, universe, lookbacks in params.candidates():
            st.candidates[name] = CandidateState(name, universe, list(lookbacks))
        return st

    def to_dict(self) -> dict:
        return dict(version=1, last_bar_ms=self.last_bar_ms, last_week=self.last_week,
                    last_refit_ms=self.last_refit_ms, refits=self.refits, chosen=self.chosen,
                    chosen_score=self.chosen_score, cap_snapshot=self.cap_snapshot,
                    candidates={k: c.to_dict() for k, c in self.candidates.items()})

    @classmethod
    def from_dict(cls, d: dict | None, params: RetuneParams) -> "RetuneState":
        if not d:
            return cls.new(params)
        st = cls(last_bar_ms=d.get("last_bar_ms"), last_week=d.get("last_week"),
                 last_refit_ms=d.get("last_refit_ms"), refits=int(d.get("refits", 0)),
                 chosen=d.get("chosen"), chosen_score=d.get("chosen_score"),
                 cap_snapshot=d.get("cap_snapshot"))
        saved = d.get("candidates") or {}
        for name, universe, lookbacks in params.candidates():
            st.candidates[name] = (CandidateState.from_dict(saved[name]) if name in saved
                                   else CandidateState(name, universe, list(lookbacks)))
        return st


# ── one day of input ─────────────────────────────────────────────────────
@dataclass
class DayData:
    decision_ms: int
    closes: dict[str, list[float]]               # daily closes through the candle closed at decision_ms
    price_now: dict[str, float]                  # close of that candle (coins with a candle at the bar)
    price_prev: dict[str, float]                 # close of the candle of the previous sim bar
    funding: dict[str, float]                    # summed funding rate over (previous bar, this bar]
    volume_usd: dict[str, float]                 # trailing 24h USD volume as of the candle BEFORE the last


def ceil_hour_of_minute(ts_ms: int) -> int:
    """q-lab align_funding_to_index: drop to the minute, then round up to the hour, so a settlement stamped a
    few ms after the mark belongs to the period that ends on it."""
    minute = ts_ms - ts_ms % 60_000
    return minute + (-minute) % HOUR_MS


def build_day(series: dict[str, dict[int, tuple[float, float | None]]],
              funding_records: dict[str, list[tuple[int, float]]], decision_ms: int,
              prev_ms: int | None) -> DayData:
    """Cut the fetched history at one decision bar. `series[coin][open_ms] = (close, base volume)`. A candle is
    closed at the bar when open_ms + 1 day <= decision_ms; nothing later is looked at."""
    last_open = decision_ms - DAY_MS
    closes: dict[str, list[float]] = {}
    price_now: dict[str, float] = {}
    price_prev: dict[str, float] = {}
    volume_usd: dict[str, float] = {}
    for coin, by_open in series.items():
        opens = sorted(o for o in by_open if o <= last_open)
        if not opens:
            continue
        closes[coin] = [by_open[o][0] for o in opens]
        if last_open in by_open:
            price_now[coin] = by_open[last_open][0]
        before = by_open.get(last_open - DAY_MS)
        if before is not None and before[1] is not None:
            volume_usd[coin] = before[1] * before[0]
        if prev_ms is not None and (prev_ms - DAY_MS) in by_open:
            price_prev[coin] = by_open[prev_ms - DAY_MS][0]
    funding: dict[str, float] = {}
    if prev_ms is not None:
        for coin, recs in funding_records.items():
            window = [r for ts, r in recs if prev_ms < ceil_hour_of_minute(ts) <= decision_ms]
            if window:
                funding[coin] = sum(window)
    return DayData(decision_ms, closes, price_now, price_prev, funding, volume_usd)


# ── scoring ──────────────────────────────────────────────────────────────
def sharpe(returns: list[float]) -> float:
    """mean / std(ddof=1); NaN when fewer than two points or a zero spread (q-lab retune._score)."""
    n = len(returns)
    if n < 2:
        return float("nan")
    mean = sum(returns) / n
    std = math.sqrt(sum((r - mean) ** 2 for r in returns) / (n - 1))
    return mean / std if std and not math.isnan(std) else float("nan")


def choose(state: RetuneState, decision_ms: int, previous_refit_ms: int | None
           ) -> tuple[str | None, float | None, dict[str, float | None]]:
    """The re-tune decision at `decision_ms`: (winner, its score, every candidate's score or None).
    Uses only returns appended before this bar (rows realised by it)."""
    scores: dict[str, float | None] = {}
    best: str | None = None
    best_score = -math.inf
    for name, cand in state.candidates.items():
        started = cand.first_active_ms
        if started is None or previous_refit_ms is None or started >= previous_refit_ms:
            scores[name] = None
            continue
        score = sharpe([r for t, r in cand.returns if started <= t < decision_ms])
        scores[name] = None if math.isnan(score) else score
        if not math.isnan(score) and score > best_score:
            best, best_score = name, score
    return best, (best_score if best is not None else None), scores


# ── one bar ──────────────────────────────────────────────────────────────
@dataclass
class BarOutcome:
    weights: dict[str, float]
    signals: dict[str, float]
    size_scale: float
    chosen: str | None
    refit: bool
    events: list[dict]


def advance_day(state: RetuneState, day: DayData, params: RetuneParams,
                universes: dict[str, list[str]]) -> BarOutcome:
    """Process one daily rebalance bar: complete the candidates' previous rows, decide their new weights,
    re-tune on the first bar of a week, return the weights the paper book should hold from this bar."""
    bar = day.decision_ms
    week = week_key(bar)
    refit = state.last_week is not None and week != state.last_week
    previous_refit = state.last_refit_ms
    coins = tuple(sorted(day.closes))
    universe_sets = {rule: set(v) for rule, v in universes.items()}
    day_returns: dict[str, float | None] = {}
    funding_missing: set[str] = set()

    for cand in state.candidates.values():
        tp = params.candidate_trend(tuple(cand.lookbacks), coins)
        # row of the previous decision, realised over (previous bar, this bar] -- computed first, appended
        # only after this bar's size scale and weights are fixed (live/trend.py "never include t's return")
        completed: tuple[float, float] | None = None
        if state.last_bar_ms is not None:
            gross = acc = 0.0
            for c, w in cand.prev_weights.items():
                if not w:
                    continue
                p0, p1 = day.price_prev.get(c), day.price_now.get(c)
                if p0 and p1:
                    gross += w * (p1 / p0 - 1.0)
                f = day.funding.get(c)
                if f is None:
                    funding_missing.add(c)
                else:
                    acc += -w * f             # a long pays a positive rate (qlab.harness.accrual)
            completed = (gross, acc)

        scale = book_vol_scale(cand.equity_hist, tp)
        raw = target_weights(day.closes, tp, size_scale=scale)
        members = universe_sets.get(cand.universe, set())
        weights = {c: w for c, w in raw.items() if w and c in day.price_now and c in members}

        if completed is not None:
            gross, acc = completed
            net = gross + acc - cand.prev_cost
            cand.returns.append([float(state.last_bar_ms), net])
            day_returns[cand.name] = net
            cand.equity_hist.append(cand.equity_hist[-1] * (1.0 + gross + acc))   # costs excluded, as q-lab
            cand.equity_hist = cand.equity_hist[-max(EQUITY_KEEP, tp.book_vol_window_days + 2):]
        turnover = sum(abs(weights.get(c, 0.0) - cand.prev_weights.get(c, 0.0))
                       for c in set(weights) | set(cand.prev_weights))
        cand.prev_cost = turnover * params.cost_rate
        cand.prev_weights = dict(weights)
        cand.weights = dict(weights)
        cand.scale = scale
        if cand.first_active_ms is None and sum(abs(w) for w in weights.values()) > 0:
            cand.first_active_ms = bar

    events: list[dict] = []
    if refit:
        winner, score, scores = choose(state, bar, previous_refit)
        events.append(dict(
            kind="retune", coin="-", qty=0.0, price=0.0, notional=0.0, fee=0.0, bar_ms=bar,
            choice=winner, previous_choice=state.chosen, score=score, scores=scores, week=week,
            observations={n: len([1 for t, _ in c.returns if c.first_active_ms is not None
                                  and c.first_active_ms <= t < bar]) for n, c in state.candidates.items()},
            previous_refit_ms=previous_refit))
        state.chosen, state.chosen_score = winner, score
        state.refits += 1
        state.last_refit_ms = bar

    chosen = state.candidates.get(state.chosen) if state.chosen else None
    weights = dict(chosen.weights) if chosen else {}
    signals: dict[str, float] = {}
    if chosen:
        tp = params.candidate_trend(tuple(chosen.lookbacks), coins)
        signals = {c: ensemble_signal(day.closes[c], tp) for c in weights if c in day.closes}

    events.append(dict(
        kind="retune_day", coin="-", qty=0.0, price=0.0, notional=0.0, fee=0.0, bar_ms=bar,
        week=week, chosen=state.chosen, universes={r: list(v) for r, v in universes.items()},
        candidate_returns=day_returns, scales={n: c.scale for n, c in state.candidates.items()},
        gross={n: sum(abs(w) for w in c.weights.values()) for n, c in state.candidates.items()},
        funding_missing=sorted(funding_missing), coins_with_history=len(coins),
        cap_snapshot_ms=(state.cap_snapshot or {}).get("taken_ms")))
    state.last_bar_ms = bar
    state.last_week = week
    return BarOutcome(weights=weights, signals=signals, size_scale=chosen.scale if chosen else 1.0,
                      chosen=state.chosen, refit=refit, events=events)
