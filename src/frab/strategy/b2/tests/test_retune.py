"""Monthly re-tuning of Bv2: refit rule, eligibility, scoring, composite accounting incl. the switch cost."""
from __future__ import annotations

import math
import pandas as pd
import pytest

from frab.constants import PERP_TAKER, SPOT_TAKER
from frab.strategy.b2 import retune as R
from frab.strategy.b2.book import CoinBook, advance_signals, start_book, step
from frab.strategy.b2.params import B2Params
from frab.strategy.b2.retune import (
    Candidate, RetuneState, advance, choose, is_refit_bar, next_refit_ms, sharpe)
from frab.strategy.b2.retune_history import CoinSeries
from frab.strategy.b2.retune_params import B2RetuneParams, iso_to_ms, ms_to_iso

H = 3_600_000
ORIGIN = iso_to_ms("2026-02-01T00:00:00Z")
N = 3300                                                    # hours: to the middle of June
START = {"BTC": ORIGIN + 788 * H, "ETH": ORIGIN + 888 * H}  # 3 March 08:00 and 12:00-ish; ETH 100 h later


def ms(text: str) -> int:
    return iso_to_ms(text)


def price(h: int, coin: str) -> float:
    base = {"BTC": 60000.0, "ETH": 3000.0}[coin]
    return base * (1 + 0.25 * math.sin(h / 400.0) + 0.01 * math.sin(h / 7.0))


def make_series() -> dict[str, CoinSeries]:
    out = {}
    for coin in ("BTC", "ETH"):
        close = [price(h, coin) for h in range(N)]
        out[coin] = CoinSeries(coin, ORIGIN, close, [c * 1.002 for c in close],
                               [0.00002 + 0.00003 * math.sin(h / 50) for h in range(N)])
    return out


def make_params(**base) -> B2RetuneParams:
    p = B2RetuneParams(
        base=B2Params(coins=("BTC", "ETH"), capital_usd=2000.0, min_order_usd=0.0, carry_enabled=False, **base),
        history_start=ms_to_iso(ORIGIN), coin_starts={c: ms_to_iso(t) for c, t in START.items()})
    p.validate()
    return p


def run(params, series, *, last=N - 1, state=None, hook=None):
    state = state or RetuneState.new(params)
    outs = []
    for k in range((state.last_bar_ms - ORIGIN) // H + 1 if state.last_bar_ms else 0, last + 1):
        out = advance(state, params, series, ORIGIN + k * H)
        outs.append(out)
        if hook:
            hook(state, out)
    return state, outs


# ── a direct, independent stepping of one candidate (what the stand's inner strategy does) ──

def reference(params: B2RetuneParams, series, name: str) -> dict[int, dict]:
    """bar_ms -> {equity, spot: {coin: units}, perp: {coin: units}} of the candidate run alone."""
    p = params.candidate_params(name)
    books = {c: CoinBook.new(c, p) for c in params.coins}
    out = {}
    for k in range(N):
        bar = ORIGIN + k * H
        total, spot, perp = 0.0, {}, {}
        for coin in params.coins:
            s, b = series[coin], books[coin]
            if not b.started:
                if bar < params.start_ms(coin):
                    total += b.capital
                    spot[coin] = perp[coin] = 0.0
                    continue
                for i in range(k - 48, k):
                    advance_signals(b, s.close[max(0, i - 731):i + 1], s.funding[max(0, i - 8):i + 1], p)
                start_book(b, bar_ms=bar, price=s.close[k], params=p)
            step(b, bar_ms=bar, price=s.close[k], funding_rate=s.funding[k], closes=s.close[max(0, k - 731):k + 1],
                 funding_hist=s.funding[max(0, k - 8):k + 1], params=p, high=s.high[k])
            total += b.equity(s.close[k])
            spot[coin], perp[coin] = b.units_spot, b.hedge_units()
        out[bar] = {"equity": total, "spot": spot, "perp": perp}
    return out


def book_by_bar(outs):
    """Book equity after each bar (the row stamped with the bar's close), summed over coins."""
    acc: dict[int, float] = {}
    for o in outs:
        for r in o.equity_rows:
            if r["ts_ms"] == o.bar_ms + H:
                acc[o.bar_ms] = acc.get(o.bar_ms, 0.0) + r["equity"]
    return acc


# ── the refit rule ──────────────────────────────────────────────────────────

def test_a_refit_is_the_first_hourly_bar_of_a_utc_month():
    assert is_refit_bar(ms("2026-06-01T00:00:00Z"))
    assert is_refit_bar(ms("2027-01-01T00:00:00Z"))                  # year change
    assert not is_refit_bar(ms("2026-06-01T01:00:00Z"))
    assert not is_refit_bar(ms("2026-05-31T23:00:00Z"))
    assert not is_refit_bar(ms("2026-06-15T00:00:00Z"))
    assert next_refit_ms(ms("2026-10-24T05:00:00Z")) == ms("2026-11-01T00:00:00Z")
    assert next_refit_ms(ms("2026-12-31T23:00:00Z")) == ms("2027-01-01T00:00:00Z")


def test_the_very_first_bar_of_the_history_is_never_a_refit():
    params = make_params()
    series = make_series()
    state = RetuneState.new(params)
    first = ms("2026-02-01T00:00:00Z")                             # the history starts on a month boundary
    assert is_refit_bar(first)
    out = advance(state, params, series, first)
    assert state.refits == 0 and not out.events
    # the next month start does count
    state, outs = run(params, series, last=(ms("2026-03-01T00:00:00Z") - ORIGIN) // H)
    assert state.refits == 1 and state.last_refit_ms == ms("2026-03-01T00:00:00Z")


def test_refits_happen_once_per_month_through_the_series():
    params = make_params()
    state, outs = run(params, make_series())
    bars = [e["bar_ms"] for o in outs for e in o.events if e["kind"] == "retune"]
    assert [ms_to_iso(b) for b in bars] == ["2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z",
                                            "2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z"]


# ── eligibility and no fallback ─────────────────────────────────────────────

def _cand(name, started, returns):
    c = Candidate(name, B2Params(), {})
    c.first_active_ms = started
    for r in returns:
        c.add_return(r)
    return c


def test_a_candidate_is_eligible_only_if_it_started_before_the_previous_refit():
    prev = ms("2026-05-01T00:00:00Z")
    cands = {"a": _cand("a", prev - H, [0.01, -0.005, 0.02]), "b": _cand("b", prev, [0.5, 0.4, 0.6])}
    best, scores = choose(cands, prev)
    assert best == "a" and scores["a"]["eligible"] and not scores["b"]["eligible"]    # b started AT the refit
    assert scores["b"]["score"] is not None                                          # scored, but not eligible


def test_with_no_previous_refit_nobody_is_eligible_and_the_book_does_not_fall_back_to_a_favourite():
    cands = {"a": _cand("a", ms("2026-01-01T00:00:00Z"), [0.01, 0.02, -0.01])}
    assert choose(cands, None)[0] is None
    assert choose({"a": _cand("a", None, []), "b": _cand("b", None, [])}, ms("2026-05-01T00:00:00Z"))[0] is None


def test_a_nan_score_never_wins_and_ties_go_to_the_earlier_candidate():
    prev = ms("2026-05-01T00:00:00Z")
    old = prev - 100 * H
    flat = _cand("flat", old, [0.01, 0.01, 0.01])                # zero deviation: Sharpe undefined
    single = _cand("single", old, [0.01])                        # one return: undefined
    assert choose({"flat": flat, "single": single}, prev)[0] is None
    twin_a, twin_b = _cand("a", old, [0.01, 0.02, 0.0]), _cand("b", old, [0.01, 0.02, 0.0])
    assert choose({"a": twin_a, "b": twin_b}, prev)[0] == "a"
    # the best of losers is still chosen: the rule is "best Sharpe", not "positive Sharpe"
    losers = {"a": _cand("a", old, [-0.03, 0.0, -0.02]), "b": _cand("b", old, [-0.01, 0.0, -0.02])}
    assert choose(losers, prev)[0] == "b"


def test_sharpe_from_running_sums_equals_pandas():
    returns = [0.001, -0.002, 0.0035, 0.0, 0.0011, -0.0007, 0.002]
    c = _cand("x", 0, returns)
    series = pd.Series(returns)
    assert c.score == pytest.approx(series.mean() / series.std(ddof=1), rel=1e-12)
    assert math.isnan(sharpe(1, 0.01, 0.0)) and math.isnan(sharpe(0, 0.0, 0.0))


def test_no_eligible_candidate_means_cash_and_no_published_book():
    """Natural run: the books start 3 March, so at the 1 April refit (previous refit 1 March) a candidate that
    started on 3 March is NOT eligible -> no choice, no book; at 1 May (previous 1 April) it is."""
    params = make_params()
    state, outs = run(params, make_series())
    retunes = [e for o in outs for e in o.events if e["kind"] == "retune"]
    assert [e["choice"] for e in retunes] == [None, None, retunes[2]["choice"], retunes[3]["choice"]]
    assert retunes[2]["choice"] in ("b2", "b2_cold") and retunes[2]["first_choice"]
    assert retunes[1]["scores"]["b2"]["eligible"] is False
    # nothing is published before the first choice
    first_choice_out = next(o for o in outs if any(e["kind"] == "retune" and e["first_choice"] for e in o.events))
    assert all(not o.equity_rows for o in outs if o.bar_ms < first_choice_out.bar_ms)
    assert first_choice_out.equity_rows


def test_the_score_is_the_sharpe_of_returns_through_the_refit_candle_since_the_first_active_bar():
    params = make_params()
    series = make_series()
    state, outs = run(params, series)
    refs = {n: reference(params, series, n) for n in params.candidates}
    retune = [e for o in outs for e in o.events if e["kind"] == "retune"][2]            # the 1 May refit
    tau = retune["bar_ms"]
    for name, ref in refs.items():
        first_active = min(params.start_ms(c) for c in params.coins)
        rets = [ref[b]["equity"] / ref[b - H]["equity"] - 1 for b in range(first_active + H, tau + H, H)]
        expect = pd.Series(rets).mean() / pd.Series(rets).std(ddof=1)
        assert retune["scores"][name]["score"] == pytest.approx(expect, rel=1e-9)
        assert retune["scores"][name]["n"] == len(rets)
    best = max(retune["scores"], key=lambda n: retune["scores"][n]["score"])
    assert retune["choice"] == best


# ── candidates run continuously, whoever is chosen ──────────────────────────

def test_every_candidate_equals_its_own_direct_stepping_at_every_bar():
    params = make_params()
    series = make_series()
    seen: dict[str, dict[int, float]] = {n: {} for n in params.candidates}

    def hook(state, out):
        for n, c in state.candidates.items():
            seen[n][out.bar_ms] = c.last_equity
    run(params, series, hook=hook)
    for name in params.candidates:
        ref = reference(params, series, name)
        assert max(abs(seen[name][b] - ref[b]["equity"]) for b in ref) < 1e-9
    assert any(seen["b2"][b] != pytest.approx(seen["b2_cold"][b]) for b in seen["b2"])    # they differ


# ── composite accounting ────────────────────────────────────────────────────

def _scripted(monkeypatch, sequence):
    """Replace the selection with a script: refit n picks sequence[n] (None = no choice)."""
    calls = iter(sequence)

    def fake(candidates, previous_refit_ms):
        best = next(calls)
        return best, {n: {"eligible": True, "score": 0.0, "n": c.n, "first_active_ms": c.first_active_ms}
                      for n, c in candidates.items()}
    monkeypatch.setattr(R, "choose", fake)


def _positions(ref_bar, k):
    return {c: (k * ref_bar["spot"][c], k * ref_bar["perp"][c]) for c in ref_bar["spot"]}


def test_between_refits_the_book_moves_by_the_chosen_candidates_equity_return(monkeypatch):
    params = make_params()
    series = make_series()
    _scripted(monkeypatch, [None, "b2", "b2_cold", "b2_cold"])           # 1 Mar, 1 Apr, 1 May, 1 Jun
    state, outs = run(params, series)
    refs = {n: reference(params, series, n) for n in params.candidates}
    book = book_by_bar(outs)
    apr1, may1, jun1 = (ms(f"2026-0{m}-01T00:00:00Z") for m in (4, 5, 6))

    # the first choice (1 April, decided at the close of that candle) starts the book at its capital: a row at
    # the open of the refit bar holds the capital, the opening cost is the first move
    assert min(book) == apr1 and state.first_choice_ms == apr1 + H
    baseline = [r for o in outs for r in o.equity_rows if r["ts_ms"] == apr1]
    assert len(baseline) == 2 and sum(r["equity"] for r in baseline) == pytest.approx(2000.0)
    assert book[apr1] < 2000.0
    for b in range(apr1 + H, may1, H):                                    # b2 earns every candle until 1 May
        assert book[b] / book[b - H] == pytest.approx(refs["b2"][b]["equity"] / refs["b2"][b - H]["equity"], rel=1e-12)
    # the 1 May candle itself still belongs to b2; the choice changes at its close
    assert (book[may1] + _switch_fee(outs, may1)) / book[may1 - H] == pytest.approx(
        refs["b2"][may1]["equity"] / refs["b2"][may1 - H]["equity"], rel=1e-9)
    for b in range(may1 + H, jun1, H):                                    # then b2_cold
        assert book[b] / book[b - H] == pytest.approx(
            refs["b2_cold"][b]["equity"] / refs["b2_cold"][b - H]["equity"], rel=1e-12)


def _switch_fee(outs, bar):
    out = next(o for o in outs if o.bar_ms == bar)
    return sum(e["fee"] for e in out.events if e["kind"] == "retune_switch")


def test_the_first_choice_opens_the_positions_and_the_book_starts_at_capital_less_that_cost(monkeypatch):
    params = make_params()
    series = make_series()
    _scripted(monkeypatch, [None, "b2_cold", "b2_cold", "b2_cold"])
    state, outs = run(params, series)
    apr1 = ms("2026-04-01T00:00:00Z")
    out = next(o for o in outs if o.bar_ms == apr1)
    ref = reference(params, series, "b2_cold")[apr1]
    fills = [e for e in out.events if e["kind"] == "retune_switch"]
    slip = params.base.slippage
    # solve k with the exact cost: k = (C - cost(k)) / E, cost(k) = k * c   ->   k = C / (E + c)
    c = sum(series[coin].close[(apr1 - ORIGIN) // H] * (ref["spot"][coin] * (SPOT_TAKER + slip)
                                                         + ref["perp"][coin] * (PERP_TAKER + slip))
            for coin in params.coins)
    k = 2000.0 / (ref["equity"] + c)
    assert state.scale is not None
    assert sum(e["fee"] for e in fills) == pytest.approx(k * c, rel=1e-9)
    assert book_by_bar([out])[apr1] == pytest.approx(2000.0 - k * c, rel=1e-12)
    assert {(e["coin"], e["leg"]) for e in fills} >= {("BTC", "spot"), ("ETH", "spot")}
    assert all(e["previous_choice"] is None and e["choice"] == "b2_cold" for e in fills)
    # the published positions are the candidate's scaled by book equity / candidate equity
    kk = (2000.0 - k * c) / ref["equity"]
    for coin in params.coins:
        view = out.views[coin]
        assert view.units_spot == pytest.approx(kk * ref["spot"][coin], rel=1e-9)
        assert view.hedge_units() == pytest.approx(kk * ref["perp"][coin], rel=1e-9)


def test_a_switch_charges_fees_and_slippage_on_the_absolute_difference_of_the_scaled_positions(monkeypatch):
    params = make_params()
    series = make_series()
    _scripted(monkeypatch, [None, "b2", "b2_cold", "b2_cold"])
    state, outs = run(params, series)
    refs = {n: reference(params, series, n) for n in params.candidates}
    may1 = ms("2026-05-01T00:00:00Z")
    out = next(o for o in outs if o.bar_ms == may1)
    prev = next(o for o in outs if o.bar_ms == may1 - H)
    # book equity before the switch: the old scale times the old candidate's equity
    k_before = book_by_bar([prev])[may1 - H] / refs["b2"][may1 - H]["equity"]
    b_before = k_before * refs["b2"][may1]["equity"]
    idx = (may1 - ORIGIN) // H
    slip = params.base.slippage

    def cost(k_new):
        total = 0.0
        for coin in params.coins:
            p = series[coin].close[idx]
            ds = k_new * refs["b2_cold"][may1]["spot"][coin] - k_before * refs["b2"][may1]["spot"][coin]
            dq = k_new * refs["b2_cold"][may1]["perp"][coin] - k_before * refs["b2"][may1]["perp"][coin]
            total += abs(ds) * p * (SPOT_TAKER + slip) + abs(dq) * p * (PERP_TAKER + slip)
        return total

    k_new = b_before / refs["b2_cold"][may1]["equity"]
    for _ in range(100):
        k_new = (b_before - cost(k_new)) / refs["b2_cold"][may1]["equity"]
    fills = [e for e in out.events if e["kind"] == "retune_switch"]
    assert fills, "the two candidates hold different positions, so a switch must trade"
    assert sum(e["fee"] for e in fills) == pytest.approx(cost(k_new), rel=1e-7)
    assert book_by_bar([out])[may1] == pytest.approx(b_before - cost(k_new), rel=1e-10)
    for e in fills:                                            # one fill per coin and leg, notional = units * price
        assert e["notional"] == pytest.approx(e["qty"] * e["price"])
        assert e["previous_choice"] == "b2" and e["choice"] == "b2_cold"


def test_a_cash_month_closes_the_positions_at_a_cost_and_then_holds_the_equity_flat(monkeypatch):
    params = make_params()
    series = make_series()
    _scripted(monkeypatch, [None, "b2", None, None])                       # 1 May: switch to cash
    state, outs = run(params, series)
    may1, jun1 = ms("2026-05-01T00:00:00Z"), ms("2026-06-01T00:00:00Z")
    close_out = next(o for o in outs if o.bar_ms == may1)
    fills = [e for e in close_out.events if e["kind"] == "retune_switch"]
    assert fills and all(e["choice"] is None and e["previous_choice"] == "b2" for e in fills)
    held = book_by_bar(outs)
    flat = [held[b] for b in range(may1, jun1 + H, H)]
    assert max(flat) - min(flat) < 1e-9                                    # cash earns nothing
    for coin in params.coins:                                              # no positions, the capital spread evenly
        view = outs[-1].views[coin]
        assert view.units_spot == 0 and view.hedge_units() == 0 and view.cash == pytest.approx(flat[-1] / 2)
    assert state.chosen is None and state.scale is None and state.book_equity == pytest.approx(flat[-1])
    # the published fills of the cash months are only the retune log
    assert not [e for o in outs if o.bar_ms > may1 for e in o.events if e["kind"] not in ("retune",)]


def test_published_rows_show_the_chosen_candidates_books_and_its_fills_scaled(monkeypatch):
    params = make_params()
    series = make_series()
    _scripted(monkeypatch, [None, "b2_cold", "b2", "b2"])
    seen = []

    def hook(state, out):
        if state.chosen and out.views:
            cand = state.candidates[state.chosen]
            seen.append((out, state.chosen, state.scale, {c: cand.books[c].in_pos for c in params.coins}))
    state, outs = run(params, series, hook=hook)
    assert seen
    for out, chosen, k, in_pos in seen:
        assert {r["coin"] for r in out.equity_rows if r["ts_ms"] == out.bar_ms + H} == {"BTC", "ETH"}
        for coin in params.coins:                                       # the hedge flag is the chosen candidate's
            assert out.views[coin].in_pos == in_pos[coin]
        for e in out.events:
            if e.get("candidate"):                                      # a scaled fill of the candidate held before
                assert e["scale"] > 0 and e["notional"] == pytest.approx(e["qty"] * e["price"])
    fills = [e for o in outs for e in o.events if e.get("candidate")]
    assert {e["kind"] for e in fills} >= {"hedge_open", "hedge_close"}, "the synthetic path should hedge while held"
    assert {e["candidate"] for e in fills} <= {"b2_cold", "b2"}


def test_state_round_trip_resumes_exactly(monkeypatch):
    import json
    params = make_params()
    series = make_series()
    whole, outs = run(params, series)
    half = ms("2026-04-20T00:00:00Z")
    part, first = run(params, series, last=(half - ORIGIN) // H)
    revived = RetuneState.from_dict(json.loads(json.dumps(part.to_dict())), params)
    rest_state, rest = run(params, series, state=revived)
    assert [o.bar_ms for o in first + rest] == [o.bar_ms for o in outs]
    assert rest_state.to_dict() == whole.to_dict()
    ours = [(o.bar_ms, r["coin"], r["equity"]) for o in first + rest for r in o.equity_rows]
    theirs = [(o.bar_ms, r["coin"], r["equity"]) for o in outs for r in o.equity_rows]
    assert ours == theirs


def test_state_from_other_params_is_refused():
    params = make_params()
    state = RetuneState.new(params)
    other = make_params(hedge_threshold=0.01)
    with pytest.raises(ValueError, match="params changed"):
        RetuneState.from_dict(state.to_dict(), other)


def test_bars_must_arrive_in_order():
    params = make_params()
    series = make_series()
    state = RetuneState.new(params)
    advance(state, params, series, ORIGIN)
    with pytest.raises(ValueError, match="in order"):
        advance(state, params, series, ORIGIN + 2 * H)


# ── params ──────────────────────────────────────────────────────────────────

def test_params_reject_a_book_start_without_enough_history_and_unknown_overrides():
    good = make_params()
    bad_start = good.to_dict() | {"retune_coin_starts": {"BTC": ms_to_iso(ORIGIN + 100 * H), "ETH": ms_to_iso(START["ETH"])}}
    with pytest.raises(ValueError, match="fewer than"):
        B2RetuneParams.from_dict(bad_start)
    with pytest.raises(ValueError, match="unknown B2Params keys"):
        B2RetuneParams.from_dict(good.to_dict() | {"retune_candidates": {"a": {"nope": 1}, "b": {}}})
    with pytest.raises(ValueError, match="at least two"):
        B2RetuneParams.from_dict(good.to_dict() | {"retune_candidates": {"a": {}}})
    assert B2RetuneParams.from_dict(good.to_dict()) == good
