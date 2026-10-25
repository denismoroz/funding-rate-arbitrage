"""Backfill replay: point-in-time inputs only, snapshot visibility, same numbers as the per-bar path (T52b)."""
from __future__ import annotations

import copy
import json
import random
from datetime import datetime, timezone

import pytest

from frab.strategy.trend.cap_history import DEFAULT_PATH, CapHistory
from frab.strategy.trend.coin_selector import DAY_MS, week_key
from frab.strategy.trend.retune import RetuneState, build_day
from frab.strategy.trend.retune_backfill import (
    IndexedHistory, funding_needs, history_bars, iter_replay, replay, summarize)
from frab.strategy.trend.retune_params import RetuneParams
from frab.strategy.trend.tests.test_retune import (
    CAP_ORDER, COINS, PARAMS, T0, funding_for, make_series, run)

SINCE = T0 - 30 * DAY_MS                       # the replay window starts here (a Saturday midnight)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def cap_doc(first_ms: int, weeks: int, order_for_week=None) -> dict:
    """Sunday snapshots from the Sunday on/after `first_ms`; the ranking rotates so universes change."""
    first = first_ms + (-(first_ms // DAY_MS - 3) % 7) * DAY_MS         # day 3 of the epoch week = Sunday
    snaps = []
    for w in range(weeks):
        order = order_for_week(w) if order_for_week else COINS[w % 3:] + COINS[:w % 3]
        snaps.append({"date": iso(first + 7 * w * DAY_MS),
                      "ranked": [[c, 1e12 / (i + 1 + w), i + 1] for i, c in enumerate(order)]})
    return {"version": 1, "source": "test", "snapshots": snaps}


def history_for(days: int) -> tuple[dict, dict]:
    return make_series(days), funding_for(days)


def test_history_bars_start_after_the_first_candle_and_stop_before_the_launch_hour():
    bars = history_bars(SINCE, T0 + 3 * DAY_MS + 5 * 3_600_000)
    assert bars[0] == SINCE + DAY_MS and bars[-1] == T0 + 3 * DAY_MS and len(bars) == 33
    assert history_bars(SINCE, SINCE) == []
    with pytest.raises(ValueError):
        history_bars(SINCE + 1, T0)


def test_snapshot_is_visible_from_the_first_bar_strictly_after_its_date():
    sunday = T0 - DAY_MS                                         # 2026-01-04
    hist = CapHistory.from_dict({"version": 1, "snapshots": [
        {"date": iso(sunday - 7 * DAY_MS), "ranked": [["OLD", 1.0, 1]]},
        {"date": iso(sunday), "ranked": [["NEW", 1.0, 1]]}]})
    # a bar is labelled by its candle's open: the decision at Monday 00:00 closes SUNDAY's candle
    assert hist.visible(sunday + DAY_MS).date == iso(sunday - 7 * DAY_MS)       # label Sunday == D: not yet
    assert hist.visible(sunday + 2 * DAY_MS).date == iso(sunday)                # label Monday: visible
    assert hist.visible(sunday + DAY_MS - 1).date == iso(sunday - 7 * DAY_MS)
    assert hist.visible(sunday - 7 * DAY_MS + DAY_MS) is None                   # nothing before the first
    snap = hist.snapshot_for_bar(sunday + 2 * DAY_MS)
    assert snap["source"] == "cmc_history" and snap["snapshot_date"] == iso(sunday)
    assert snap["week"] == week_key(sunday + 2 * DAY_MS) and snap["ranked"] == [["NEW", 1.0, 1]]
    # the first bar of an ISO week is the first one that sees the Sunday before it
    assert week_key(sunday + 2 * DAY_MS) != week_key(sunday + DAY_MS)


def test_the_shipped_cap_history_covers_the_window_start_and_ranks_no_stablecoin():
    hist = CapHistory.load(DEFAULT_PATH)
    first_bar = datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp() * 1000          # closes 2025-01-01
    assert hist.visible(int(first_bar)) is not None             # a snapshot dated before the first bar exists
    assert hist.snapshots[0].date < "2025-01-01"
    for snap in hist.snapshots:
        names = [r[0] for r in snap.ranked]
        assert len(names) == len(set(names)) and 25 <= len(names) <= 80
        assert not {"USDT", "USDC", "DAI", "USDE", "USD1", "PYUSD"} & set(names)
        assert [r[1] for r in snap.ranked] == sorted((r[1] for r in snap.ranked), reverse=True)


def test_indexed_history_cuts_exactly_like_build_day_on_messy_data():
    rng = random.Random(7)
    series, funding = make_series(40), funding_for(40)
    for coin in COINS:                                           # gaps, a missing volume, a late listing
        for d in rng.sample(range(0, 40), 3):
            series[coin].pop(T0 + d * DAY_MS, None)
    series["CCC"][T0 + 10 * DAY_MS] = (series["CCC"][T0 + 10 * DAY_MS][0], None)
    series["HHH"] = {o: v for o, v in series["HHH"].items() if o >= T0 + 5 * DAY_MS}
    for coin in COINS:                                           # unsorted records, ms offsets around the hour
        recs = [(ts + rng.choice([-30_000, 0, 59_999, 61_000]), r) for ts, r in funding[coin]]
        rng.shuffle(recs)
        funding[coin] = recs
    since = T0 - 10 * DAY_MS
    cut = {c: {o: v for o, v in by.items() if o >= since} for c, by in series.items()}
    hist = IndexedHistory(series, funding, since)
    prev = None
    for bar in history_bars(since, T0 + 39 * DAY_MS):
        mine, ref = hist.day(bar, prev), build_day(cut, {c: sorted(r) for c, r in funding.items()}, bar, prev)
        assert mine == ref
        counts, tradeable, volume = hist.universe_inputs(bar)
        assert counts == {c: len(v) for c, v in ref.closes.items()}
        assert tradeable == set(ref.price_now) and volume == ref.volume_usd
        prev = bar


def _tamper(series, funding, cap, cutoff):
    """Replace every input dated at or after what bar `cutoff` may see with nonsense."""
    rng = random.Random(1)
    series, funding, cap = copy.deepcopy(series), copy.deepcopy(funding), copy.deepcopy(cap)
    for by in series.values():
        for o in list(by):
            if o >= cutoff:                                      # the candle opening at the bar closes after it
                by[o] = (by[o][0] * rng.uniform(0.2, 5.0), by[o][1] * rng.uniform(0.0, 50.0))
    for recs in funding.values():
        # a settlement stamped a few ms past the mark still closes the period ending on it (q-lab alignment)
        recs[:] = [(ts, r * rng.uniform(-30.0, 30.0) if ts >= cutoff + 60_000 else r) for ts, r in recs]
    for snap in cap["snapshots"]:
        if datetime.fromisoformat(snap["date"]).replace(tzinfo=timezone.utc).timestamp() * 1000 >= cutoff - DAY_MS:
            rng.shuffle(snap["ranked"])                          # not visible to the bar: label must be > date
    return series, funding, cap


def test_backfill_uses_only_data_before_each_bar():
    series, funding = history_for(90)
    cap = cap_doc(SINCE - 14 * DAY_MS, 20)
    cutoff = T0 + 40 * DAY_MS
    t_series, t_funding, t_cap = _tamper(series, funding, cap, cutoff)
    bars = history_bars(SINCE, T0 + 85 * DAY_MS)

    def outcomes(s, f, c):
        hist = IndexedHistory(s, f, SINCE)
        return {bar: (o.weights, o.chosen, [e.get("choice") for e in o.events if e["kind"] == "retune"],
                      o.signals) for bar, o, _ in iter_replay(hist, CapHistory.from_dict(c), PARAMS, bars)}

    clean, dirty = outcomes(series, funding, cap), outcomes(t_series, t_funding, t_cap)
    past = [b for b in bars if b <= cutoff]
    assert len(past) > 60 and all(clean[b] == dirty[b] for b in past)
    assert any(clean[b] != dirty[b] for b in bars if b > cutoff + 3 * DAY_MS)   # the tampering does bite later


def test_backfill_equals_the_per_bar_path_of_the_live_engine():
    """The replay and the T52 bar-by-bar helper (build_day + advance_day) end in the same state."""
    series, funding = history_for(70)
    snaps = {"version": 1, "snapshots": [{"date": iso(SINCE - 8 * DAY_MS),
                                          "ranked": [[c, 1e9 - i, i + 1] for i, c in enumerate(CAP_ORDER)]}]}
    bars = history_bars(SINCE, T0 + 66 * DAY_MS)
    cut = {c: {o: v for o, v in by.items() if o >= SINCE} for c, by in series.items()}
    ref_state, _ = run(cut, funding, bars)
    result = replay(IndexedHistory(series, funding, SINCE), CapHistory.from_dict(snaps), PARAMS, bars)
    got, want = result.state.to_dict(), ref_state.to_dict()
    for key in ("cap_snapshot", "cap_sources", "last_retune", "backfill"):
        got.pop(key), want.pop(key)
    assert got == want and result.state.refits >= 8 and result.state.chosen is not None


def test_replay_events_keep_only_the_weekly_retunes_labelled_as_history():
    series, funding = history_for(70)
    bars = history_bars(SINCE, T0 + 66 * DAY_MS)
    result = replay(IndexedHistory(series, funding, SINCE), CapHistory.from_dict(cap_doc(SINCE - 14 * DAY_MS, 14)),
                    PARAMS, bars)
    assert len(result.events) == result.state.refits >= 8
    assert {e["phase"] for e in result.events} == {"history"} and not any(e["first_choice"] for e in result.events)
    assert {e["cap_source"] for e in result.events} == {"cmc_history"}
    assert result.events[0]["choice"] is None                          # the first re-tune never chooses
    assert result.state.cap_sources and set(result.state.cap_sources.values()) == {"cmc_history"}
    info = summarize(result, CapHistory.from_dict(cap_doc(SINCE - 14 * DAY_MS, 14)), PARAMS, bars)
    assert info["bars"] == len(bars) and info["refits"] == result.state.refits
    assert json.loads(json.dumps(info)) == info                         # storable in the state JSON


def test_state_written_by_the_replay_round_trips_and_older_state_still_loads():
    series, funding = history_for(60)
    bars = history_bars(SINCE, T0 + 56 * DAY_MS)
    result = replay(IndexedHistory(series, funding, SINCE), CapHistory.from_dict(cap_doc(SINCE - 14 * DAY_MS, 12)),
                    PARAMS, bars)
    result.state.first_choice_ms = 123
    again = RetuneState.from_dict(json.loads(json.dumps(result.state.to_dict())), PARAMS)
    assert again.to_dict() == result.state.to_dict() and again.first_choice_ms == 123
    old = {k: v for k, v in result.state.to_dict().items()
           if k not in ("first_choice_ms", "cap_sources", "last_retune", "backfill")}      # a T52 state
    loaded = RetuneState.from_dict(old, PARAMS)
    assert loaded.first_choice_ms is None and loaded.cap_sources == {} and loaded.chosen == result.state.chosen


def test_a_bar_with_no_visible_ranking_fails_loudly_instead_of_leaving_a_hole():
    series, funding = history_for(40)
    late = CapHistory.from_dict({"version": 1, "snapshots": [{"date": iso(T0), "ranked": [["AAA", 1.0, 1]]}]})
    with pytest.raises(ValueError, match="no ranking is visible"):
        replay(IndexedHistory(series, funding, SINCE), late, PARAMS, history_bars(SINCE, T0 + 5 * DAY_MS))


def test_funding_is_needed_only_for_coins_a_candidate_could_hold_and_from_their_first_bar():
    series, funding = history_for(70)
    narrow = CapHistory.from_dict(cap_doc(SINCE - 14 * DAY_MS, 14, order_for_week=lambda w: ["AAA", "BBB", "CCC"]))
    hist = IndexedHistory(series, funding, SINCE)
    bars = history_bars(SINCE, T0 + 66 * DAY_MS)
    needs = funding_needs(hist, narrow, PARAMS, bars)
    assert {"AAA", "BBB", "CCC"} <= set(needs)                          # the cap rule's coins
    assert all(bars[0] <= first <= last <= bars[-1] for first, last in needs.values())
    # with the history gate (8 days) nothing can be held before the 9th candle
    assert min(first for first, _ in needs.values()) >= SINCE + 8 * DAY_MS
    # a coin that drops out of every universe stops needing funding: a window, not "everything since"
    rotating = CapHistory.from_dict(cap_doc(SINCE - 14 * DAY_MS, 14, order_for_week=lambda w: COINS[w:w + 3]))
    window = funding_needs(hist, rotating, PARAMS, bars)
    assert window["DDD"][1] < bars[-1] - 50 * DAY_MS < window["GGG"][1]


def test_params_carry_the_backfill_keys_and_reject_a_bad_start_date():
    p = RetuneParams.from_dict({"backfill": True, "backfill_start": "2025-01-01", "lookback_sets": {"a": [3], "b": [4]},
                                "min_history_days": 10})
    assert p.backfill and p.backfill_start_ms == 1_735_689_600_000
    assert RetuneParams.from_dict(p.to_dict()) == p
    assert not RetuneParams.from_dict({}).backfill                        # off unless asked for
    with pytest.raises(ValueError, match="backfill_start"):
        RetuneParams.from_dict({"backfill_start": "last year"})
