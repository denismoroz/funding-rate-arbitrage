"""The re-tuned engine with a backfill: a new book replays the past, then runs live; several books side by side."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from frab.db.models import Strategy, TrendEvent
from frab.db.session import init_db, make_session_factory, session_scope
from frab.exchanges.hyperliquid.wire import HLFundingRecord
from frab.repo.trend_repo import TrendRepo
from frab.strategy.trend.cap_history import CapHistory
from frab.strategy.trend.coin_selector import DAY_MS
from frab.strategy.trend.retune import RetuneState
from frab.strategy.trend.retune_book import RetuneBook
from frab.strategy.trend.retune_engine import HOUR_MS, TrendRetuneEngine
from frab.strategy.trend.retune_history import HistoryStore
from frab.strategy.trend.retune_params import RetuneParams
from frab.strategy.trend.tests.test_retune_backfill import cap_doc, iso
from frab.strategy.trend.tests.test_retune_engine import COINS, T0, FakeGecko, FakeHL, no_network  # noqa: F401

START_DAY = -50                                   # the replay window starts 50 days before T0
LAUNCH = T0 + 70 * DAY_MS + 5 * HOUR_MS + 60_000  # 05:01 UTC: the first live hour is not a rebalance bar
FIRST_HOUR = T0 + 70 * DAY_MS + 4 * HOUR_MS
BASE = {"capital_usd": 10_000.0, "vol_window": 3, "min_history_days": 8, "risk_scale": 0.2,
        "lookback_sets": {"ens": [2, 4], "l2": [2], "l4": [4]}, "top_k_by_market_cap": 3,
        "top_k_by_volume": 3, "daily_fetch_pause_s": 0.0, "backfill": True,
        "backfill_start": iso(T0 + START_DAY * DAY_MS), "backfill_candle_pause_s": 0.0,
        "backfill_funding_page_pause_s": 0.0}


class BackfillHL(FakeHL):
    """FakeHL with a long funding history, request logs and one-shot failures."""

    def __init__(self, fail_daily_once: str | None = None, fail_funding_once: str | None = None):
        super().__init__()
        self.daily_log: list[tuple[str, int, int]] = []
        self.funding_log: list[tuple[str, int]] = []
        self.fail_daily_once = fail_daily_once
        self.fail_funding_once = fail_funding_once

    async def candle_snapshot(self, coin, interval, start_ms, end_ms):
        if coin not in COINS:
            return []                                    # a delisted perp whose candles HL erased
        if interval == "1d":
            self.daily_log.append((coin, start_ms, end_ms))
            if coin == self.fail_daily_once:
                self.fail_daily_once = None
                raise ConnectionError("boom")
        return await super().candle_snapshot(coin, interval, start_ms, end_ms)

    async def funding_history(self, coin, since_ms, *, end_ms=None, page_pause_s=0.0):
        self.funding_log.append((coin, since_ms))
        if coin == self.fail_funding_once:
            self.fail_funding_once = None
            raise ConnectionError("boom")
        i = COINS.index(coin)
        return [HLFundingRecord(coin=coin, ts_ms=t + 20, rate=0.00001 * (1 + i % 2), premium=0.0)
                for t in range(since_ms - since_ms % HOUR_MS, T0 + 200 * DAY_MS if end_ms is None else end_ms,
                               HOUR_MS)]


def history_pulls(hl: BackfillHL) -> list[str]:
    """Coins whose funding was pulled for the backfill (an old `since`), not by the book's hourly refresh."""
    return [c for c, since in hl.funding_log if since < FIRST_HOUR - DAY_MS]


def params(**over) -> dict:
    return BASE | over


def cap() -> CapHistory:
    return CapHistory.from_dict(cap_doc(T0 + (START_DAY - 14) * DAY_MS, 30))


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    yield make_session_factory(engine)
    await engine.dispose()


async def add_row(sf, name="trend_retune", **over) -> int:
    async with session_scope(sf) as s:
        row = Strategy(name=name, version="v1-paper", params_json=RetuneParams.from_dict(params(**over)).to_dict(),
                       status="active")
        s.add(row)
        await s.flush()
        return row.id


def make(sf, sid, now_ms, tmp_path, *, hl=None, gecko=None, cap_history="default", store=None):
    return TrendRetuneEngine(
        session_factory=sf, client=hl or BackfillHL(), strategy_id=sid, repo=TrendRepo(sf, RetuneBook),
        clock=lambda: now_ms, coingecko=gecko or FakeGecko(),
        cap_history=cap() if cap_history == "default" else cap_history,
        history_store=store or HistoryStore(tmp_path / "cache", candle_pause_s=0.0, funding_page_pause_s=0.0))


async def state_of(sf, sid, **over):
    book = await TrendRepo(sf, RetuneBook).get_book(sid)
    return book, RetuneState.from_dict(book.retune, RetuneParams.from_dict(params(**over)))


async def events_of(sf, sid, *kinds):
    return await TrendRepo(sf, RetuneBook).events(sid, limit=5000, kinds=kinds or None)


@pytest.mark.asyncio
async def test_a_new_book_replays_the_past_then_starts_at_launch_with_the_implied_choice(db, tmp_path):
    sid = await add_row(db)
    hl, gecko = BackfillHL(), FakeGecko()
    out = await make(db, sid, LAUNCH, tmp_path, hl=hl, gecko=gecko).tick()
    book, st = await state_of(db, sid)

    assert out["hours"] == 1 and out["bars"] == 0 and gecko.calls == 0       # the past needs no live ranking
    assert st.last_bar_ms == T0 + 70 * DAY_MS                                # replayed through the last full day
    assert st.refits >= 9 and st.chosen is not None and st.backfill["bars"] == 70 - START_DAY
    assert st.backfill["cap_history"]["last_used"] <= iso(T0 + 69 * DAY_MS)  # nothing newer than the bar's label
    assert set(st.cap_sources.values()) == {"cmc_history"}
    assert {c for c, _, _ in hl.daily_log} == set(COINS)
    assert (tmp_path / "cache" / "daily" / "DEAD.json").is_file()           # a delisted perp is asked too
    # the book starts at launch: positions sized to the chosen candidate, no fills before the launch hour
    assert book.started and book.units and book.rebalances == 1 and book.fees > 0
    fills = [e for e in await events_of(db, sid) if e.kind in ("open", "increase", "close", "reduce", "flip")]
    assert fills and {e.ts_ms for e in fills} == {FIRST_HOUR + HOUR_MS}
    assert len(await TrendRepo(db, RetuneBook).equity_series(sid)) == 1
    # the history is in the event log as weekly re-tunes, and the launch records the paper-period start
    retunes = await events_of(db, sid, "retune")
    history = [e for e in retunes if e.details_json["phase"] == "history"]
    assert len(history) == st.refits and not any(e.details_json["first_choice"] for e in history)
    launch = [e for e in retunes if e.details_json["first_choice"]]
    assert len(launch) == 1 and launch[0].details_json["phase"] == "launch"
    assert launch[0].details_json["choice"] == st.chosen and launch[0].details_json["implied_by_backfill"]
    assert launch[0].ts_ms == FIRST_HOUR + HOUR_MS == st.first_choice_ms
    assert len(await events_of(db, sid, "backfill")) == 1
    assert not await events_of(db, sid, "retune_day")                        # 70 daily records stay out of the log


@pytest.mark.asyncio
async def test_the_first_live_bar_takes_coingecko_and_each_weeks_source_is_recorded(db, tmp_path):
    sid = await add_row(db)
    hl, gecko = BackfillHL(), FakeGecko()
    store = HistoryStore(tmp_path / "cache", candle_pause_s=0.0, funding_page_pause_s=0.0)
    await make(db, sid, LAUNCH, tmp_path, hl=hl, gecko=gecko, store=store).tick()
    out = await make(db, sid, T0 + 71 * DAY_MS + HOUR_MS + 90_000, tmp_path, hl=hl, gecko=gecko,
                     store=store).tick()                    # past the 00:00 bar of day 71
    book, st = await state_of(db, sid)
    assert out["bars"] == 1 and gecko.calls == 1
    assert st.cap_snapshot["source"] == "coingecko"
    assert list(st.cap_sources.values()).count("coingecko") == 1 and "cmc_history" in st.cap_sources.values()
    live_day = [e for e in await events_of(db, sid, "retune_day")]
    assert len(live_day) == 1 and live_day[0].details_json["cap_source"] == "coingecko"
    assert live_day[0].details_json["phase"] == "live"
    assert st.first_choice_ms == FIRST_HOUR + HOUR_MS                       # unchanged by the live bar
    assert len([e for e in await events_of(db, sid, "retune") if e.details_json["first_choice"]]) == 1


@pytest.mark.asyncio
async def test_a_restart_after_the_backfill_does_not_backfill_again(db, tmp_path):
    sid = await add_row(db)
    await make(db, sid, LAUNCH, tmp_path).tick()
    book1, st1 = await state_of(db, sid)

    hl2 = BackfillHL()                                              # a new process: new engine, new store
    store2 = HistoryStore(tmp_path / "cache", candle_pause_s=0.0, funding_page_pause_s=0.0)
    out = await make(db, sid, LAUNCH + HOUR_MS, tmp_path, hl=hl2, store=store2).tick()
    assert out["hours"] == 1 and not hl2.daily_log and not history_pulls(hl2)
    out = await make(db, sid, T0 + 71 * DAY_MS + HOUR_MS + 90_000, tmp_path, hl=hl2, store=store2).tick()
    assert out["bars"] == 1
    # the live daily fetch is one request per LISTED perp (no delisted name, no replay-sized refetch)
    assert sorted(c for c, _, _ in hl2.daily_log) == sorted(COINS)
    _, st2 = await state_of(db, sid)
    assert st2.backfill == st1.backfill and len(await events_of(db, sid, "backfill")) == 1
    assert st2.refits - st1.refits in (0, 1)


@pytest.mark.asyncio
async def test_an_interrupted_backfill_resumes_from_what_is_already_on_disk(db, tmp_path):
    sid = await add_row(db)
    hl = BackfillHL(fail_daily_once="EEE")
    with pytest.raises(RuntimeError, match="EEE"):
        await make(db, sid, LAUNCH, tmp_path, hl=hl).tick()
    assert await TrendRepo(db, RetuneBook).get_book(sid) is None            # nothing half-built
    assert sorted(p.stem for p in (tmp_path / "cache" / "daily").glob("*.json")) == ["AAA", "BBB", "CCC", "DDD", "DEAD"]

    hl2 = BackfillHL(fail_funding_once="BBB")                               # dies again, this time in funding
    with pytest.raises(RuntimeError, match="BBB"):
        await make(db, sid, LAUNCH, tmp_path, hl=hl2).tick()
    assert sorted(c for c, _, _ in hl2.daily_log) == ["EEE", "FFF", "GGG", "HHH"]       # the first four: from disk

    hl3 = BackfillHL()
    out = await make(db, sid, LAUNCH, tmp_path, hl=hl3).tick()
    assert out["hours"] == 1 and not hl3.daily_log                          # all candles were on disk by now
    fetched = set(history_pulls(hl3))
    assert "BBB" in fetched and "AAA" not in fetched                      # AAA's funding was already on disk
    _, st = await state_of(db, sid)
    assert st.backfill["bars"] == 70 - START_DAY and st.chosen is not None


@pytest.mark.asyncio
async def test_the_history_gets_the_same_answer_whether_or_not_it_was_interrupted(db, tmp_path):
    a = await add_row(db, "trend_retune")
    await make(db, a, LAUNCH, tmp_path / "a").tick()
    b = await add_row(db, "trend_retune_2")
    with pytest.raises(RuntimeError):
        await make(db, b, LAUNCH, tmp_path / "b", hl=BackfillHL(fail_funding_once="CCC")).tick()
    await make(db, b, LAUNCH, tmp_path / "b").tick()
    (_, sa), (_, sb) = await state_of(db, a), await state_of(db, b)
    assert sa.to_dict()["candidates"] == sb.to_dict()["candidates"] and sa.chosen == sb.chosen


@pytest.mark.asyncio
async def test_two_books_with_different_capital_keep_separate_state_and_share_the_fetched_history(db, tmp_path):
    small = await add_row(db, "trend_retune_3k", capital_usd=3_000.0)
    big = await add_row(db, "trend_retune_10k", capital_usd=10_000.0)
    store = HistoryStore(tmp_path / "cache", candle_pause_s=0.0, funding_page_pause_s=0.0)
    hl_small, hl_big = BackfillHL(), BackfillHL()
    await make(db, small, LAUNCH, tmp_path, hl=hl_small, store=store).tick()
    await make(db, big, LAUNCH, tmp_path, hl=hl_big, store=store).tick()
    assert hl_small.daily_log and not hl_big.daily_log                      # fetched once, shared
    assert all(since >= FIRST_HOUR - DAY_MS for _, since in hl_big.funding_log)     # only the book's own hourly pulls

    (bs, ss), (bb, sb) = await state_of(db, small), await state_of(db, big)
    assert bs.capital == 3_000.0 and bb.capital == 10_000.0
    assert ss.to_dict()["candidates"] == sb.to_dict()["candidates"]         # the choice does not depend on capital
    assert ss.chosen == sb.chosen and ss.first_choice_ms == sb.first_choice_ms
    assert bb.gross_notional(bb.prices) > 2.5 * bs.gross_notional(bs.prices)
    eq_s = await TrendRepo(db, RetuneBook).latest_equity(small)
    eq_b = await TrendRepo(db, RetuneBook).latest_equity(big)
    assert eq_s.equity < 3_100.0 and eq_b.equity > 9_000.0
    for sid in (small, big):                                                # events never cross over
        rows = await events_of(db, sid)
        assert rows and {r.strategy_id for r in rows} == {sid}
        assert len([r for r in rows if r.kind == "fund"]) == 1


@pytest.mark.asyncio
async def test_a_backfilling_book_without_the_cap_file_refuses_to_start_empty(db, tmp_path):
    sid = await add_row(db, cap_history_path=str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="market-cap history"):
        await make(db, sid, LAUNCH, tmp_path, cap_history=None).tick()
    assert await TrendRepo(db, RetuneBook).get_book(sid) is None


@pytest.mark.asyncio
async def test_without_the_backfill_flag_the_book_starts_empty_as_before(db, tmp_path):
    sid = await add_row(db, backfill=False)
    hl = BackfillHL()
    out = await make(db, sid, LAUNCH, tmp_path, hl=hl).tick()
    book, st = await state_of(db, sid)
    assert out["hours"] == 1 and not hl.daily_log and not book.units
    assert st.last_bar_ms is None and st.backfill is None and st.first_choice_ms is None


@pytest.mark.asyncio
async def test_the_paper_period_starts_at_the_first_live_choice_when_there_is_no_backfill(db, tmp_path):
    sid = await add_row(db, backfill=False)
    gecko, hl = FakeGecko(), BackfillHL()
    await make(db, sid, T0 + 70 * DAY_MS + HOUR_MS + 60_000, tmp_path, hl=hl, gecko=gecko).tick()
    for day in range(71, 71 + 36):
        await make(db, sid, T0 + day * DAY_MS + HOUR_MS + 200_000, tmp_path, hl=hl, gecko=gecko).tick()
    book, st = await state_of(db, sid, backfill=False)
    retunes = (await events_of(db, sid, "retune"))[::-1]
    flagged = [e for e in retunes if e.details_json["first_choice"]]
    first_with_choice = next(e for e in retunes if e.details_json["choice"])
    assert len(flagged) == 1 and flagged[0].id == first_with_choice.id
    assert st.first_choice_ms == flagged[0].ts_ms and flagged[0].details_json["phase"] == "live"
    assert flagged[0].details_json["cap_source"] == "coingecko"
    assert min(e.ts_ms for e in await events_of(db, sid) if e.kind in ("open", "increase")) >= st.first_choice_ms


@pytest.mark.asyncio
async def test_old_state_without_the_new_fields_keeps_running(db, tmp_path):
    """A T52 book (state written before first_choice existed) gets an approximate first choice, no event."""
    sid = await add_row(db, backfill=False)
    gecko = FakeGecko()
    for day in range(70, 108):
        await make(db, sid, T0 + day * DAY_MS + HOUR_MS + 200_000, tmp_path, gecko=gecko).tick()
    book = await TrendRepo(db, RetuneBook).get_book(sid)
    for key in ("first_choice_ms", "cap_sources", "last_retune", "backfill"):
        book.retune.pop(key, None)
    await TrendRepo(db, RetuneBook).save_progress(sid, book, [], [])
    before = max(e.id for e in await events_of(db, sid))
    await make(db, sid, T0 + 108 * DAY_MS + HOUR_MS + 200_000, tmp_path, gecko=gecko).tick()
    _, st = await state_of(db, sid)
    assert st.first_choice_ms is not None
    new = [e for e in await events_of(db, sid) if e.id > before]
    assert new and not [e for e in new if e.kind == "retune" and e.details_json["first_choice"]]
    async with db() as s:
        assert (await s.execute(TrendEvent.__table__.select().where(TrendEvent.kind == "backfill"))).first() is None


@pytest.mark.asyncio
async def test_launching_in_the_first_hour_after_midnight_replays_to_the_day_before(db, tmp_path):
    sid = await add_row(db)
    early = T0 + 70 * DAY_MS + 20 * 60_000                       # 00:20 UTC: the 00:00 bar is not closed yet
    await make(db, sid, early, tmp_path).tick()
    book, st = await state_of(db, sid)
    assert st.last_bar_ms == T0 + 69 * DAY_MS and book.started and book.units
    hl = BackfillHL()
    await make(db, sid, early + 30 * 60_000, tmp_path, hl=hl).tick()          # the next process: nothing to redo
    assert not hl.daily_log and not history_pulls(hl)


@pytest.mark.asyncio
async def test_launching_on_a_rebalance_hour_makes_the_first_live_bar_the_one_that_flags_the_first_choice(db, tmp_path):
    sid = await add_row(db)
    gecko = FakeGecko()
    out = await make(db, sid, T0 + 71 * DAY_MS + HOUR_MS + 90_000, tmp_path, gecko=gecko).tick()   # day 71 = a Tuesday
    book, st = await state_of(db, sid)
    assert out["bars"] == 1 and out["hours"] == 1 and gecko.calls == 1
    assert st.last_bar_ms == T0 + 71 * DAY_MS and st.cap_snapshot["source"] == "coingecko"
    assert st.backfill["last_bar_ms"] == T0 + 70 * DAY_MS                       # the history stops before the live bar
    flagged = [e for e in await events_of(db, sid, "retune") if e.details_json["first_choice"]]
    assert len(flagged) == 1 and flagged[0].details_json["phase"] == "live" and flagged[0].details_json["choice"]
    assert st.first_choice_ms == flagged[0].ts_ms and book.units
