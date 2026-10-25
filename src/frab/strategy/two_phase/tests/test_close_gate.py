"""CloseGate and every exit path that goes through it.

Real in-memory DB and FarbRepo, a fake exchange whose `check_close` is scriptable and which
records every `close_position`, a fake event bus. The point of every test: when the check
says no, NOTHING is closed and the position stays as it was; when it says yes, the normal
exit proceeds.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from frab.db.models import Exchange as ExchangeRow, Position as PositionRow, Strategy
from frab.db.session import session_scope
from frab.domain import FarbState, Instrument, PositionStatus, Side
from frab.engine.two_phase_signals import TwoPhaseDecision
from frab.exchanges.protocol import CloseCheck
from frab.repo.farb_repo import FarbRepo
from frab.strategy.two_phase.close_gate import (
    PENDING_FORCED_KEY,
    REALERT_INTERVAL_MS,
    CloseGate,
    wanted_label,
)
from frab.strategy.two_phase.evaluators.exit import ExitEvaluator
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.states._base import CloseBlocked, StrategyContext
from frab.strategy.two_phase.states.closing_short import ClosingShortState
from frab.strategy.two_phase.states.post_breakeven import PostBreakevenHandler
from frab.strategy.two_phase.states.pre_breakeven import PreBreakevenHandler

_NOW_MS = 1_704_067_200_000
_OPENED_MS = _NOW_MS - 100 * 3_600_000
_HOUR = 3_600_000

BLOCKED = CloseCheck(
    ok=False,
    reason="spot 76 PURR x $0.0985 = $7.49 < $11 the close needs (HL minimum order $10)",
    spot_qty=76.0, spot_price=0.0985, spot_notional_usdc=7.486,
    spot_min_notional_usdc=11.0, wallet_qty=76.948, reserved_qty=0.0, lot=1.0,
)
CLEAR = CloseCheck(
    ok=True, spot_qty=76.0, spot_price=0.2, spot_notional_usdc=15.2,
    spot_min_notional_usdc=11.0, wallet_qty=76.948, reserved_qty=0.0, lot=1.0,
)


class FakeExchange:
    name = "HL"

    def __init__(self) -> None:
        self.check = CLEAR
        self.check_error: Exception | None = None
        self.check_calls = 0
        self.closed: list[int] = []

    async def check_close(self, spot, perp) -> CloseCheck:
        self.check_calls += 1
        if self.check_error is not None:
            raise self.check_error
        return self.check

    async def close_position(self, pos):
        self.closed.append(pos.id)
        return pos


class FakeBus:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, event) -> None:
        self.events.append(event)

    def of(self, kind: str) -> list:
        return [e for e in self.events if e.kind == kind]


def _params() -> TwoPhaseParams:
    return TwoPhaseParams(
        entry_threshold_apr=0.10, phase2_exit_threshold=-0.10, base_min_hold_hours=0,
        cap_min_hold_hours=720, safety_mult=5.0, signal_window_hours=3, concurrency_cap=3,
        position_size_usdc=1000.0, budget_cap_usdc=3000.0, margin_buffer_factor=0.0,
        phase1_negative_patience=72, phase1_breakeven_cap_hours=720,
    )


@pytest.fixture
def exchange() -> FakeExchange:
    return FakeExchange()


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
async def farb_repo(session_factory) -> FarbRepo:
    return FarbRepo(session_factory)


@pytest.fixture
def gate(exchange, farb_repo, session_factory, bus) -> CloseGate:
    return CloseGate(
        exchange=exchange, farb_repo=farb_repo, session_factory=session_factory, event_bus=bus,
    )


async def _seed(session_factory, farb_repo, *, state=FarbState.PRE_BREAKEVEN, state_data=None):
    """A FarbPosition with a spot and a perp leg, in `state`."""
    async with session_scope(session_factory) as s:
        exc = ExchangeRow(name="HL", funding_interval_h=1, spot_taker_bps=7.0, perp_taker_bps=3.5)
        strat = Strategy(name="two_phase_test", version="v2", params_json={})
        s.add_all([exc, strat])
        await s.flush()
        ids = {}
        for instrument, side in ((Instrument.SPOT, Side.LONG), (Instrument.PERP, Side.SHORT)):
            row = PositionRow(
                exchange_id=exc.id, coin="PURR", instrument=instrument, side=side,
                qty=77.0, entry_price=0.14446, opened_at=_OPENED_MS, closed_at=None,
                status=PositionStatus.OPEN, farb_position_id=None,
            )
            s.add(row)
            await s.flush()
            ids[instrument] = row.id
        strategy_id = strat.id
    fp = await farb_repo.create(
        strategy_id=strategy_id, coin="PURR", initial_state=state,
        state_data=state_data or {"opened_at_ms": _OPENED_MS, "total_fees_paid": 10.0},
    )
    await farb_repo.set_leg(fp.id, instrument=Instrument.SPOT, position_id=ids[Instrument.SPOT])
    await farb_repo.set_leg(fp.id, instrument=Instrument.PERP, position_id=ids[Instrument.PERP])
    return await farb_repo.get(fp.id)


def _signal_computer(mocker, value):
    from frab.strategy.two_phase.evaluators.signal import SignalComputer

    sc = mocker.AsyncMock(spec=SignalComputer)
    sc.compute.return_value = value
    return sc


def _settings(mocker):
    from frab.coin_registry import RegistryAwareSettings

    settings = mocker.MagicMock(spec=RegistryAwareSettings)
    spec = mocker.MagicMock()
    spec.leverage = 10
    settings.get_coin_spec.return_value = spec
    return settings


def _handler(cls, mocker, farb_repo, gate, *, signal):
    return cls(
        strategy_id=1, farb_repo=farb_repo, params=_params(),
        signal_computer=_signal_computer(mocker, signal), settings=_settings(mocker),
        close_gate=gate,
    )


# --------------------------------------------------------------------------------------
# The gate itself
# --------------------------------------------------------------------------------------

async def test_blocked_records_the_need_and_publishes_one_error(
    session_factory, farb_repo, gate, exchange, bus,
):
    fp = await _seed(session_factory, farb_repo)
    exchange.check = BLOCKED

    res = await gate.admit(fp, decision="CLOSE_PRE_BE_NEG", state_data=fp.state_data, now_ms=_NOW_MS)

    assert not res.ok
    stored = (await farb_repo.get(fp.id)).state_data
    assert stored["close_blocked"] is True
    assert stored["close_blocked_since_ms"] == _NOW_MS
    assert stored["close_blocked_wanted"] == wanted_label("CLOSE_PRE_BE_NEG")
    assert stored["close_blocked_decision"] == "CLOSE_PRE_BE_NEG"
    assert "$7.49 < $11" in stored["close_blocked_reason"]
    assert stored["close_blocked_check"]["spot_notional_usdc"] == pytest.approx(7.486)
    assert stored["total_fees_paid"] == 10.0  # the rest of state_data is kept
    assert PENDING_FORCED_KEY not in stored
    errors = bus.of("close.blocked")
    assert len(errors) == 1 and errors[0].level == "ERROR"
    assert errors[0].payload_json["farb_position_id"] == fp.id
    assert "can't close" in errors[0].message


async def test_blocked_again_within_a_day_is_silent_then_reemits(
    session_factory, farb_repo, gate, exchange, bus,
):
    fp = await _seed(session_factory, farb_repo)
    exchange.check = BLOCKED
    await gate.admit(fp, decision="CLOSE_PRE_BE_NEG", state_data=fp.state_data, now_ms=_NOW_MS)

    for hours in (1, 2, 12, 23):
        fp = await farb_repo.get(fp.id)
        await gate.admit(
            fp, decision="CLOSE_PRE_BE_NEG", state_data=fp.state_data,
            now_ms=_NOW_MS + hours * _HOUR,
        )
    assert len(bus.of("close.blocked")) == 1
    stored = (await farb_repo.get(fp.id)).state_data
    assert stored["close_blocked_since_ms"] == _NOW_MS          # not reset by re-checks
    assert stored["close_blocked_checked_ms"] == _NOW_MS + 23 * _HOUR

    fp = await farb_repo.get(fp.id)
    await gate.admit(
        fp, decision="CLOSE_PRE_BE_NEG", state_data=fp.state_data,
        now_ms=_NOW_MS + REALERT_INTERVAL_MS,
    )
    assert len(bus.of("close.blocked")) == 2
    fp = await farb_repo.get(fp.id)
    await gate.admit(
        fp, decision="CLOSE_PRE_BE_NEG", state_data=fp.state_data,
        now_ms=_NOW_MS + REALERT_INTERVAL_MS + _HOUR,
    )
    assert len(bus.of("close.blocked")) == 2   # the day restarts from the re-emit


async def test_unblock_clears_markers_and_publishes_info(
    session_factory, farb_repo, gate, exchange, bus,
):
    fp = await _seed(session_factory, farb_repo)
    exchange.check = BLOCKED
    await gate.admit(fp, decision="CLOSE_PRE_BE_NEG", state_data=fp.state_data, now_ms=_NOW_MS,
                     forced=True)
    fp = await farb_repo.get(fp.id)
    assert PENDING_FORCED_KEY in fp.state_data

    exchange.check = CLEAR
    res = await gate.admit(fp, decision="forced", state_data=fp.state_data, now_ms=_NOW_MS + _HOUR)

    assert res.ok
    assert not any(k.startswith("close_blocked") for k in res.state_data)
    assert PENDING_FORCED_KEY not in res.state_data
    assert res.state_data["total_fees_paid"] == 10.0
    infos = bus.of("close.unblocked")
    assert len(infos) == 1 and infos[0].level == "INFO"
    assert infos[0].payload_json["blocked_for_ms"] == _HOUR


async def test_ok_on_a_position_that_was_never_blocked_is_silent(
    session_factory, farb_repo, gate, bus,
):
    fp = await _seed(session_factory, farb_repo)
    res = await gate.admit(fp, decision="forced", state_data=fp.state_data, now_ms=_NOW_MS)
    assert res.ok and res.state_data == fp.state_data
    assert bus.events == []


async def test_a_check_that_fails_blocks_instead_of_letting_the_exit_through(
    session_factory, farb_repo, gate, exchange, bus,
):
    fp = await _seed(session_factory, farb_repo)
    exchange.check_error = RuntimeError("no live spot price for PURR")

    res = await gate.admit(fp, decision="CLOSE_POST_BE", state_data=fp.state_data, now_ms=_NOW_MS)

    assert not res.ok
    assert "no live spot price" in res.reason
    assert (await farb_repo.get(fp.id)).state_data["close_blocked"] is True
    assert len(bus.of("close.blocked")) == 1


async def test_release_lifts_the_block_when_the_exit_is_no_longer_wanted(
    session_factory, farb_repo, gate, exchange, bus,
):
    fp = await _seed(session_factory, farb_repo)
    exchange.check = BLOCKED
    await gate.admit(fp, decision="CLOSE_PRE_BE_NEG", state_data=fp.state_data, now_ms=_NOW_MS)
    fp = await farb_repo.get(fp.id)

    cleaned = await gate.release(fp, state_data=fp.state_data, now_ms=_NOW_MS + _HOUR)

    assert not any(k.startswith("close_blocked") for k in cleaned)
    assert len(bus.of("close.unblocked")) == 1
    # releasing a position that is not blocked says nothing
    again = await gate.release(fp, state_data=cleaned, now_ms=_NOW_MS + 2 * _HOUR)
    assert again == cleaned and len(bus.of("close.unblocked")) == 1


# --------------------------------------------------------------------------------------
# Phase 1 exits: negative funding, break-even cap, neg-stop
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("decision", [
    TwoPhaseDecision.CLOSE_PRE_BE_NEG,
    TwoPhaseDecision.CLOSE_PRE_BE_CAP,
    TwoPhaseDecision.CLOSE_PRE_BE_NEGSTOP,
])
async def test_phase1_exit_blocked_closes_nothing_then_proceeds_when_clear(
    decision, mocker, session_factory, farb_repo, gate, exchange, bus,
):
    fp = await _seed(session_factory, farb_repo, state=FarbState.PRE_BREAKEVEN)
    handler = _handler(PreBreakevenHandler, mocker, farb_repo, gate, signal=-0.5)
    mocker.patch(
        "frab.strategy.two_phase.states.pre_breakeven.decide_pre_breakeven", return_value=decision,
    )
    exchange.check = BLOCKED

    await handler.evaluate_one(fp, now_ms=_NOW_MS)

    after = await farb_repo.get(fp.id)
    assert after.state == FarbState.PRE_BREAKEVEN            # still hedged, state unchanged
    assert exchange.closed == []
    assert after.state_data["close_blocked"] is True
    assert after.state_data["close_blocked_decision"] == decision.value
    assert "exit_decision" not in after.state_data            # it is not exiting
    assert len(bus.of("close.blocked")) == 1

    # next hour: still blocked -> no second event, still nothing closed
    await handler.evaluate_one(after, now_ms=_NOW_MS + _HOUR)
    after = await farb_repo.get(fp.id)
    assert after.state == FarbState.PRE_BREAKEVEN and exchange.closed == []
    assert len(bus.of("close.blocked")) == 1

    # the check passes: the normal exit proceeds
    exchange.check = CLEAR
    await handler.evaluate_one(after, now_ms=_NOW_MS + 2 * _HOUR)
    done = await farb_repo.get(fp.id)
    assert done.state == FarbState.CLOSING_SHORT
    assert done.state_data["exit_decision"] == decision.value
    assert not any(k.startswith("close_blocked") for k in done.state_data)
    assert len(bus.of("close.unblocked")) == 1


async def test_phase1_blocked_exit_that_is_no_longer_wanted_is_released(
    mocker, session_factory, farb_repo, gate, exchange, bus,
):
    fp = await _seed(session_factory, farb_repo, state=FarbState.PRE_BREAKEVEN)
    handler = _handler(PreBreakevenHandler, mocker, farb_repo, gate, signal=-0.5)
    patched = mocker.patch(
        "frab.strategy.two_phase.states.pre_breakeven.decide_pre_breakeven",
        return_value=TwoPhaseDecision.CLOSE_PRE_BE_NEG,
    )
    exchange.check = BLOCKED
    await handler.evaluate_one(fp, now_ms=_NOW_MS)
    fp = await farb_repo.get(fp.id)

    patched.return_value = TwoPhaseDecision.NONE            # the funding recovered
    await handler.evaluate_one(fp, now_ms=_NOW_MS + _HOUR)

    after = await farb_repo.get(fp.id)
    assert after.state == FarbState.PRE_BREAKEVEN
    assert not any(k.startswith("close_blocked") for k in after.state_data)
    assert len(bus.of("close.unblocked")) == 1


async def test_phase1_without_a_gate_exits_as_before(mocker, session_factory, farb_repo, exchange):
    fp = await _seed(session_factory, farb_repo, state=FarbState.PRE_BREAKEVEN)
    handler = _handler(PreBreakevenHandler, mocker, farb_repo, None, signal=-0.5)
    mocker.patch(
        "frab.strategy.two_phase.states.pre_breakeven.decide_pre_breakeven",
        return_value=TwoPhaseDecision.CLOSE_PRE_BE_NEG,
    )
    await handler.evaluate_one(fp, now_ms=_NOW_MS)
    assert (await farb_repo.get(fp.id)).state == FarbState.CLOSING_SHORT
    assert exchange.check_calls == 0


# --------------------------------------------------------------------------------------
# Phase 2 exit
# --------------------------------------------------------------------------------------

async def test_phase2_exit_blocked_then_proceeds(
    mocker, session_factory, farb_repo, gate, exchange, bus,
):
    fp = await _seed(
        session_factory, farb_repo, state=FarbState.POST_BREAKEVEN,
        state_data={"opened_at_ms": _OPENED_MS, "total_fees_paid": 10.0,
                    "gross_funding_so_far": 50.0, "peak_gross_funding": 60.0},
    )
    handler = _handler(PostBreakevenHandler, mocker, farb_repo, gate, signal=-0.5)
    exchange.check = BLOCKED

    await handler.evaluate_one(fp, now_ms=_NOW_MS)

    after = await farb_repo.get(fp.id)
    assert after.state == FarbState.POST_BREAKEVEN and exchange.closed == []
    assert after.state_data["close_blocked_decision"] == "CLOSE_POST_BE"
    assert len(bus.of("close.blocked")) == 1
    assert after.state_data["peak_gross_funding"] == 60.0     # the handler's counters are kept

    exchange.check = CLEAR
    await handler.evaluate_one(after, now_ms=_NOW_MS + _HOUR)
    done = await farb_repo.get(fp.id)
    assert done.state == FarbState.CLOSING_SHORT
    assert done.state_data["exit_decision"] == "CLOSE_POST_BE"
    assert len(bus.of("close.unblocked")) == 1


# --------------------------------------------------------------------------------------
# Manual close and the hourly retry of a forced exit
# --------------------------------------------------------------------------------------

async def test_forced_exit_pending_is_retried_by_the_hourly_evaluator(
    mocker, session_factory, farb_repo, gate, exchange, bus,
):
    fp = await _seed(session_factory, farb_repo, state=FarbState.PRE_BREAKEVEN)
    exchange.check = BLOCKED
    res = await gate.admit(fp, decision="forced", state_data=fp.state_data, now_ms=_NOW_MS,
                           forced=True)
    assert not res.ok
    fp = await farb_repo.get(fp.id)
    assert fp.state_data[PENDING_FORCED_KEY]["decision"] == "forced"
    assert fp.state_data["close_blocked_wanted"] == "manual close"

    evaluator = ExitEvaluator(
        strategy_id=1, farb_repo=farb_repo, params=_params(),
        signal_computer=_signal_computer(mocker, 0.5), settings=_settings(mocker),
        close_gate=gate,
    )
    # still blocked: nothing closes, no new event, the strategy's own handler does not run
    await evaluator.evaluate_one(fp, now_ms=_NOW_MS + _HOUR)
    mid = await farb_repo.get(fp.id)
    assert mid.state == FarbState.PRE_BREAKEVEN and exchange.closed == []
    assert len(bus.of("close.blocked")) == 1

    # the check passes: the owner's request goes through
    exchange.check = CLEAR
    await evaluator.evaluate_one(mid, now_ms=_NOW_MS + 2 * _HOUR)
    done = await farb_repo.get(fp.id)
    assert done.state == FarbState.CLOSING_SHORT
    assert done.state_data["exit_decision"] == "forced"
    assert done.state_data["exit_requested_at_ms"] == _NOW_MS
    assert PENDING_FORCED_KEY not in done.state_data
    assert not any(k.startswith("close_blocked") for k in done.state_data)
    assert len(bus.of("close.unblocked")) == 1


async def test_evaluator_without_a_pending_forced_exit_runs_the_handlers(
    mocker, session_factory, farb_repo, gate, exchange,
):
    fp = await _seed(session_factory, farb_repo, state=FarbState.PRE_BREAKEVEN)
    evaluator = ExitEvaluator(
        strategy_id=1, farb_repo=farb_repo, params=_params(),
        signal_computer=_signal_computer(mocker, 0.5), settings=_settings(mocker),
        close_gate=gate,
    )
    await evaluator.evaluate_one(fp, now_ms=_NOW_MS)
    assert (await farb_repo.get(fp.id)).state == FarbState.PRE_BREAKEVEN
    assert exchange.check_calls == 0   # no exit wanted, nothing to check


# --------------------------------------------------------------------------------------
# The closing state: last look before the perp goes
# --------------------------------------------------------------------------------------

def _ctx(exchange, farb_repo, session_factory, gate, bus) -> StrategyContext:
    return StrategyContext(
        exchange=exchange, farb_repo=farb_repo, params=_params(),
        session_factory=session_factory, settings=None, event_bus=bus, close_gate=gate,
    )


async def test_closing_short_blocked_closes_no_leg(
    session_factory, farb_repo, gate, exchange, bus,
):
    fp = await _seed(
        session_factory, farb_repo, state=FarbState.CLOSING_SHORT,
        state_data={"opened_at_ms": _OPENED_MS, "exit_decision": "CLOSE_PRE_BE_NEG"},
    )
    exchange.check = BLOCKED
    state = ClosingShortState(_ctx(exchange, farb_repo, session_factory, gate, bus))

    with pytest.raises(CloseBlocked):
        await state.execute(fp)

    after = await farb_repo.get(fp.id)
    assert after.state == FarbState.CLOSING_SHORT and exchange.closed == []
    assert after.state_data["close_blocked"] is True
    assert len(bus.of("close.blocked")) == 1


async def test_closing_short_proceeds_and_clears_a_block_once_clear(
    session_factory, farb_repo, gate, exchange, bus,
):
    fp = await _seed(
        session_factory, farb_repo, state=FarbState.CLOSING_SHORT,
        state_data={"opened_at_ms": _OPENED_MS, "exit_decision": "CLOSE_PRE_BE_NEG"},
    )
    state = ClosingShortState(_ctx(exchange, farb_repo, session_factory, gate, bus))
    exchange.check = BLOCKED
    with pytest.raises(CloseBlocked):
        await state.execute(fp)
    fp = await farb_repo.get(fp.id)

    exchange.check = CLEAR
    new_state = await state.execute(fp)

    assert new_state == FarbState.CLOSING_LONG
    assert exchange.closed == [fp.perp_position_id]
    after = await farb_repo.get(fp.id)
    assert after.state == FarbState.CLOSING_LONG
    assert not any(k.startswith("close_blocked") for k in after.state_data)
    assert after.state_data["exit_decision"] == "CLOSE_PRE_BE_NEG"


async def test_advance_loop_stops_on_a_blocked_close_without_failing_the_position(
    mocker, session_factory, farb_repo, gate, exchange, bus,
):
    from frab.strategy.two_phase.strategy import TwoPhaseStrategy

    fp = await _seed(
        session_factory, farb_repo, state=FarbState.CLOSING_SHORT,
        state_data={"opened_at_ms": _OPENED_MS, "exit_decision": "forced"},
    )
    exchange.check = BLOCKED
    strategy = TwoPhaseStrategy(
        strategy_id=fp.strategy_id, exchange=exchange, farb_repo=farb_repo,
        session_factory=session_factory, params=_params(), settings=_settings(mocker),
        event_bus=bus, close_gate=gate,
    )

    await strategy.advance_all_pending()

    after = await farb_repo.get(fp.id)
    assert after.state == FarbState.CLOSING_SHORT      # not FAILED, not stepped further
    assert exchange.closed == []
    assert exchange.check_calls == 1                   # one look per tick, no spinning
    assert bus.of("farb.failed") == []

    exchange.check = CLEAR
    await strategy.advance_all_pending()
    assert exchange.closed  # perp (then spot) closed once the check passes
    assert (await farb_repo.get(fp.id)).state != FarbState.FAILED


# --------------------------------------------------------------------------------------
# Margin watchdog forced close
# --------------------------------------------------------------------------------------

async def test_margin_watchdog_forced_close_blocked_closes_nothing(
    mocker, session_factory, farb_repo, gate, exchange, bus,
):
    from frab.engine.margin_manager import MarginStatus
    from frab.engine.margin_watchdog import MarginWatchdog

    fp = await _seed(session_factory, farb_repo, state=FarbState.POST_BREAKEVEN)
    watchdog = MarginWatchdog(
        strategy_id=fp.strategy_id, exchange=exchange, farb_repo=farb_repo,
        margin_manager=mocker.MagicMock(), settings=mocker.MagicMock(), event_bus=bus,
        close_gate=gate,
    )
    assessment = mocker.MagicMock()
    assessment.account_status = MarginStatus.FORCED_CLOSE
    assessment.weakest_fp_id = fp.id
    assessment.account_ratio = 1.1
    assessment.per_fp = [mocker.MagicMock(farb_position_id=fp.id, virtual_ratio=1.0)]
    mocker.patch.object(watchdog, "_compute_assessment", mocker.AsyncMock(return_value=assessment))
    mocker.patch.object(watchdog, "_emit_event", mocker.AsyncMock())

    exchange.check = BLOCKED
    report = await watchdog.run_check(now_ms=_NOW_MS)

    after = await farb_repo.get(fp.id)
    assert after.state == FarbState.POST_BREAKEVEN
    assert f"close_blocked:fp={fp.id}" in report.actions_taken
    assert after.state_data["close_blocked_decision"] == "watchdog_forced"
    assert PENDING_FORCED_KEY not in after.state_data     # re-asked every hour, not queued
    assert len(bus.of("close.blocked")) == 1

    exchange.check = CLEAR
    report = await watchdog.run_check(now_ms=_NOW_MS + _HOUR)
    after = await farb_repo.get(fp.id)
    assert after.state == FarbState.CLOSING_SHORT
    assert after.state_data["exit_decision"] == "watchdog_forced"
    assert f"forced_close:fp={fp.id}" in report.actions_taken
