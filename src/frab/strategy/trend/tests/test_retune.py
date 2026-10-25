"""Weekly re-tune: scoring, eligibility, causality, state round-trip, cost accounting."""
from __future__ import annotations

import copy
import json
import math

from frab.strategy.trend.coin_selector import DAY_MS, select_universes, week_key
from frab.strategy.trend.retune import (
    RetuneState, advance_day, build_day, choose, sharpe)
from frab.strategy.trend.retune_params import RetuneParams
from frab.strategy.trend.signals import target_weights

# 2026-01-05 is a Monday; a decision at 00:00 of day D uses the candle of D-1.
T0 = 1_767_571_200_000
assert T0 % DAY_MS == 0
COINS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
CAP_ORDER = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]

PARAMS = RetuneParams.from_dict({
    "capital_usd": 1000.0, "vol_window": 3, "min_history_days": 8, "risk_scale": 0.2,
    "lookback_sets": {"ens": [2, 4], "l2": [2], "l4": [4]},
    "top_k_by_market_cap": 3, "top_k_by_volume": 3, "daily_fetch_pause_s": 0.0,
})


def px(coin: str, day: int) -> float:
    i = COINS.index(coin)
    drift = (-1) ** i * 0.004 * (1 + i % 3)
    return 100.0 * (1 + drift) ** day * (1 + 0.03 * math.sin(day * (0.7 + 0.31 * i) + i))


def make_series(days: int, scramble_from: int | None = None) -> dict:
    """series[coin][open_ms] = (close, base volume); `scramble_from` replaces every candle opening on or after
    that day index with nonsense to prove nothing later is read."""
    out = {}
    for i, c in enumerate(COINS):
        by = {}
        for d in range(-40, days):
            close, vol = px(c, d), 1000.0 * (i + 1) * (1 + 0.2 * math.sin(d + i))
            if scramble_from is not None and d >= scramble_from:
                close, vol = close * 7.3 + 11 * d, vol * 50
            by[T0 + d * DAY_MS] = (close, vol)
        out[c] = by
    return out


def funding_for(days: int) -> dict:
    return {c: [(T0 + d * DAY_MS + h * 3_600_000 + 15, 0.00001 * (1 + i % 2)) for d in range(-2, days + 1)
                for h in range(24)] for i, c in enumerate(COINS)}


def run(series, funding, bars, params=PARAMS, state=None):
    state = state or RetuneState.new(params)
    outcomes = []
    for bar in bars:
        day0 = build_day(series, {}, bar, None)
        uni = select_universes(
            cap_ranked=CAP_ORDER, closes_count={c: len(v) for c, v in day0.closes.items()},
            tradeable=set(day0.price_now), volume_usd=day0.volume_usd, k_cap=3, k_volume=3,
            min_history=params.trend.min_history_days)
        day = build_day(series, funding, bar, state.last_bar_ms)
        outcomes.append(advance_day(state, day, params, uni))
    return state, outcomes


def bars(first_day: int, n: int) -> list[int]:
    """Decision bars at 00:00 of consecutive days (the candle of the day before has just closed)."""
    return [T0 + (first_day + k) * DAY_MS for k in range(n)]


def test_week_key_cuts_weeks_on_the_candle_open_date_like_qlab():
    mon = T0                                   # decision Monday 00:00 -> last closed candle is Sunday's
    assert week_key(mon) == week_key(mon - DAY_MS)          # Sunday and Monday-decision: same week
    assert week_key(mon + DAY_MS) != week_key(mon)          # Tuesday 00:00 decision includes Monday's candle


def test_sharpe_matches_the_qlab_definition_and_is_nan_when_undefined():
    r = [0.01, -0.02, 0.03, 0.0]
    mean = sum(r) / 4
    std = math.sqrt(sum((x - mean) ** 2 for x in r) / 3)
    assert sharpe(r) == mean / std
    assert math.isnan(sharpe([0.01])) and math.isnan(sharpe([])) and math.isnan(sharpe([0.01] * 5))


def _state_with(returns_by: dict[str, list[tuple[int, float]]], first_active: dict[str, int | None]) -> RetuneState:
    st = RetuneState.new(PARAMS)
    for name, c in st.candidates.items():
        c.returns = [[t, r] for t, r in returns_by.get(name, [])]
        c.first_active_ms = first_active.get(name)
    return st


def test_choose_picks_the_best_past_sharpe_and_only_among_the_eligible():
    rows = lambda vals: [(T0 + i * DAY_MS, v) for i, v in enumerate(vals)]      # noqa: E731
    st = _state_with(
        {"cap/ens": rows([0.01, 0.02, -0.01, 0.015]),                # decent
         "cap/l2": rows([0.02, 0.021, 0.019, 0.02]),                 # best: high mean, tiny spread
         "cap/l4": rows([0.05, -0.05, 0.05, -0.05]),                 # noisy
         "volume/ens": rows([0.5, 0.5, 0.5, 0.49])},                 # would win, but started too late
        {"cap/ens": T0, "cap/l2": T0, "cap/l4": T0, "volume/ens": T0 + 3 * DAY_MS})
    prev_refit = T0 + 2 * DAY_MS
    winner, score, scores = choose(st, T0 + 10 * DAY_MS, prev_refit)
    assert winner == "cap/l2"
    assert scores["volume/ens"] is None                              # first_active >= previous refit
    assert score == scores["cap/l2"] and score > scores["cap/ens"] > scores["cap/l4"]
    # the first re-tune (no previous one) never chooses
    assert choose(st, T0 + 10 * DAY_MS, None)[0] is None


def test_choose_ignores_returns_from_the_decision_bar_onwards_and_breaks_ties_by_grid_order():
    rows = [(T0 + i * DAY_MS, v) for i, v in enumerate([0.01, 0.03, 0.02, 0.04])]
    st = _state_with({"cap/ens": rows, "cap/l2": rows}, {"cap/ens": T0, "cap/l2": T0})
    winner, _, _ = choose(st, T0 + 4 * DAY_MS, T0 + DAY_MS)
    assert winner == "cap/ens"                                       # identical scores: earlier candidate
    # a huge return stamped at or after the decision bar must not change anything
    st.candidates["cap/l2"].returns.append([T0 + 4 * DAY_MS, 9.0])
    assert choose(st, T0 + 4 * DAY_MS, T0 + DAY_MS) == choose(
        _state_with({"cap/ens": rows, "cap/l2": rows}, {"cap/ens": T0, "cap/l2": T0}), T0 + 4 * DAY_MS, T0 + DAY_MS)


def test_no_choice_before_two_weekly_boundaries_then_a_candidate_trades():
    series, funding = make_series(70), funding_for(70)
    state, out = run(series, funding, bars(10, 40))
    refit_idx = [i for i, o in enumerate(out) if o.refit]
    assert len(refit_idx) >= 4
    first, second = refit_idx[0], refit_idx[1]
    assert all(o.chosen is None and not o.weights for o in out[: second])         # flat through the 1st re-tune
    assert out[second].chosen is not None and out[second].weights                  # 2nd re-tune picks someone
    retune_events = [e for o in out for e in o.events if e["kind"] == "retune"]
    assert [e["choice"] for e in retune_events][0] is None
    assert retune_events[1]["score"] == retune_events[1]["scores"][retune_events[1]["choice"]]


def test_future_candles_do_not_change_any_decision():
    last_bar_day = 10 + 25                          # decide 26 bars; the candle opening at the last bar is still open
    clean, f = make_series(80), funding_for(80)
    scrambled = make_series(80, scramble_from=last_bar_day)      # ...and everything after it is nonsense
    cutoff = T0 + last_bar_day * DAY_MS + 60_000          # a settlement stamped just after the mark still counts
    f_bad = {c: [(ts, r if ts <= cutoff else 0.5) for ts, r in recs] for c, recs in f.items()}
    _, a = run(clean, f, bars(10, 26))
    _, b = run(scrambled, f_bad, bars(10, 26))
    for oa, ob in zip(a, b, strict=True):
        assert oa.chosen == ob.chosen and oa.refit == ob.refit
        assert oa.weights == ob.weights
        assert oa.events[-1]["candidate_returns"] == ob.events[-1]["candidate_returns"]


def test_state_round_trips_through_json_and_resumes_identically():
    series, f = make_series(80), funding_for(80)
    all_bars = bars(10, 40)
    full_state, full = run(series, f, all_bars)
    half_state, _ = run(series, f, all_bars[:19])
    restored = RetuneState.from_dict(json.loads(json.dumps(half_state.to_dict())), PARAMS)
    assert restored.to_dict() == half_state.to_dict()
    resumed_state, resumed = run(series, f, all_bars[19:], state=restored)
    assert [o.weights for o in resumed] == [o.weights for o in full[19:]]
    assert resumed_state.to_dict() == full_state.to_dict()


def test_candidate_weights_are_normalised_over_the_whole_universe_then_masked_like_qlab():
    series, f = make_series(60), funding_for(60)
    bar = bars(30, 1)[0]
    state, out = run(series, f, [bar])
    day = build_day(series, f, bar, None)
    cand = state.candidates["cap/ens"]
    tp = PARAMS.candidate_trend((2, 4), tuple(sorted(day.closes)))
    full = target_weights(day.closes, tp, size_scale=cand.scale)
    assert set(cand.weights) <= {"AAA", "BBB", "CCC"}                    # the cap universe (top 3)
    for c, w in cand.weights.items():
        assert math.isclose(w, full[c])                                   # not re-normalised to the 3 coins
    # the leverage cap acted on all eight coins, so the masked gross is a fraction of it
    assert sum(abs(w) for w in cand.weights.values()) < sum(abs(w) for w in full.values())


def test_a_realised_row_is_gross_plus_funding_minus_the_cost_of_getting_there():
    series, f = make_series(60), funding_for(60)
    b1, b2 = bars(30, 2)
    state, _ = run(series, f, [b1])
    cand = state.candidates["cap/l2"]
    w_prev = dict(cand.prev_weights)
    cost = cand.prev_cost
    assert w_prev and math.isclose(cost, sum(abs(w) for w in w_prev.values()) * PARAMS.cost_rate)
    state, _ = run(series, f, [b2], state=state)
    day = build_day(series, f, b2, b1)
    gross = sum(w * (day.price_now[c] / day.price_prev[c] - 1) for c, w in w_prev.items())
    acc = sum(-w * day.funding[c] for c, w in w_prev.items())
    (t, net), = state.candidates["cap/l2"].returns
    assert t == b1 and math.isclose(net, gross + acc - cost)
    # equity fed to book_vol_scale carries gross + funding but NOT the trading cost (q-lab)
    assert math.isclose(state.candidates["cap/l2"].equity_hist[-1], 1.0 + gross + acc)


def test_params_validation_and_json_round_trip():
    d = PARAMS.to_dict()
    assert RetuneParams.from_dict(d).to_dict() == d
    assert [n for n, _, _ in PARAMS.candidates()] == [
        "cap/ens", "cap/l2", "cap/l4", "volume/ens", "volume/l2", "volume/l4"]
    for bad in ({"lookback_sets": {"x": [0]}}, {"universe_rules": ["nope"]}, {"top_k_by_volume": 0},
                {"lookback_sets": {"only": [5]}, "universe_rules": ["cap"]}, {"objective": "total"}):
        try:
            RetuneParams.from_dict({**d, **bad})
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
