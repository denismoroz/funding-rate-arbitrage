"""The pre-close check at the API: manual Close / Close ALL, the fields on
/api/farb-positions, and the alert for a position that needs to exit but cannot."""
from __future__ import annotations

import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from frab.api.app import create_app
from frab.db.models import Exchange, FarbPosition as FarbPositionRow, Position, Strategy
from frab.db.session import session_scope
from frab.domain.enums import FarbState, Instrument, PositionStatus, Side
from frab.exchanges.protocol import CloseCheck
from frab.repo.farb_repo import FarbRepo
from frab.strategy.two_phase.close_gate import PENDING_FORCED_KEY, CloseGate

BLOCKED = CloseCheck(
    ok=False,
    reason="spot 76 PURR x $0.0985 = $7.49 < $11 the close needs (HL minimum order $10)",
    spot_qty=76.0, spot_price=0.0985, spot_notional_usdc=7.486, spot_min_notional_usdc=11.0,
)
CLEAR = CloseCheck(ok=True, spot_qty=76.0, spot_price=0.2, spot_notional_usdc=15.2,
                   spot_min_notional_usdc=11.0)


class FakeExchange:
    name = "HL"

    def __init__(self) -> None:
        self.check = BLOCKED

    async def check_close(self, spot, perp) -> CloseCheck:
        return self.check


class FakeBus:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def fake_exchange() -> FakeExchange:
    return FakeExchange()


@pytest.fixture
def fake_bus() -> FakeBus:
    return FakeBus()


@pytest_asyncio.fixture
async def gated_client(session_factory, farb_repo, fake_exchange, fake_bus):
    app = create_app(session_factory, farb_repo=farb_repo, event_bus=None)
    app.state.event_bus = fake_bus
    app.state.close_gate = CloseGate(
        exchange=fake_exchange, farb_repo=farb_repo, session_factory=session_factory,
        event_bus=fake_bus,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _seed_fp(session_factory, *, coin="PURR", state=FarbState.POST_BREAKEVEN, strategy_id=None):
    now = int(time.time() * 1000)
    async with session_scope(session_factory) as s:
        if strategy_id is None:
            strat = Strategy(name="t", version="v1", params_json={}, status="idle")
            s.add(strat)
            await s.flush()
            strategy_id = strat.id
        exc = Exchange(name=f"hl-{now}-{coin}", funding_interval_h=1, spot_taker_bps=2.5,
                       perp_taker_bps=2.5)
        s.add(exc)
        await s.flush()
        legs = {}
        for instrument, side in ((Instrument.SPOT, Side.LONG), (Instrument.PERP, Side.SHORT)):
            pos = Position(
                exchange_id=exc.id, coin=coin, instrument=instrument.value, side=side.value,
                qty=77.0, entry_price=0.14446, opened_at=now, closed_at=None,
                status=PositionStatus.OPEN.value, farb_position_id=None,
            )
            s.add(pos)
            await s.flush()
            legs[instrument] = pos.id
        fp = FarbPositionRow(
            strategy_id=strategy_id, coin=coin, state=state.value,
            state_data={"gross_funding_so_far": 1.0}, opened_at=now, closed_at=None,
            spot_position_id=legs[Instrument.SPOT], perp_position_id=legs[Instrument.PERP],
            margin_position_id=None,
        )
        s.add(fp)
        await s.flush()
        return fp.id, strategy_id


async def test_manual_close_blocked_closes_nothing_and_says_why(
    gated_client, session_factory, farb_repo, fake_bus,
):
    fp_id, _ = await _seed_fp(session_factory)

    resp = await gated_client.post(f"/api/farb-positions/{fp_id}/close")

    assert resp.status_code == 200
    body = resp.json()
    assert body["blocked"] is True
    assert "$7.49 < $11" in body["reason"]
    assert body["new_state"] == "POST_BREAKEVEN"
    fp = await farb_repo.get(fp_id)
    assert fp.state == FarbState.POST_BREAKEVEN                 # unchanged
    assert "exit_decision" not in fp.state_data
    assert fp.state_data["close_blocked"] is True
    assert fp.state_data["close_blocked_wanted"] == "manual close"
    assert fp.state_data[PENDING_FORCED_KEY]["decision"] == "forced"
    assert fp.state_data["gross_funding_so_far"] == 1.0
    errors = [e for e in fake_bus.events if e.kind == "close.blocked"]
    assert len(errors) == 1 and errors[0].level == "ERROR"

    # pressing Close again does not spam the event
    await gated_client.post(f"/api/farb-positions/{fp_id}/close")
    assert len([e for e in fake_bus.events if e.kind == "close.blocked"]) == 1


async def test_manual_close_proceeds_when_the_check_passes(
    gated_client, session_factory, farb_repo, fake_exchange, fake_bus,
):
    fp_id, _ = await _seed_fp(session_factory)
    fake_exchange.check = CLEAR

    resp = await gated_client.post(f"/api/farb-positions/{fp_id}/close")

    assert resp.status_code == 200
    assert resp.json()["new_state"] == "CLOSING_SHORT"
    assert "blocked" not in resp.json()
    fp = await farb_repo.get(fp_id)
    assert fp.state == FarbState.CLOSING_SHORT
    assert fp.state_data["exit_decision"] == "forced"
    assert not any(k.startswith("close_blocked") for k in fp.state_data)
    assert fake_bus.events and all(e.kind != "close.blocked" for e in fake_bus.events)


async def test_manual_close_after_a_block_clears_it_and_says_so(
    gated_client, session_factory, farb_repo, fake_exchange, fake_bus,
):
    fp_id, _ = await _seed_fp(session_factory)
    await gated_client.post(f"/api/farb-positions/{fp_id}/close")
    fake_exchange.check = CLEAR

    resp = await gated_client.post(f"/api/farb-positions/{fp_id}/close")

    assert resp.json()["new_state"] == "CLOSING_SHORT"
    fp = await farb_repo.get(fp_id)
    assert PENDING_FORCED_KEY not in fp.state_data
    assert [e.kind for e in fake_bus.events if e.kind == "close.unblocked"] == ["close.unblocked"]


async def test_close_all_lists_blocked_and_closes_the_rest(
    gated_client, session_factory, farb_repo, fake_exchange,
):
    blocked_id, strat_id = await _seed_fp(session_factory, coin="PURR")

    class PerCoin(FakeExchange):
        async def check_close(self, spot, perp) -> CloseCheck:
            return BLOCKED if spot.coin == "PURR" else CLEAR

    gated_client._transport.app.state.close_gate._exchange = PerCoin()
    clear_id, _ = await _seed_fp(session_factory, coin="ETH", strategy_id=strat_id)

    resp = await gated_client.post(f"/api/farb-positions/close-all?strategy_id={strat_id}")

    data = resp.json()
    assert [c["id"] for c in data["closed_ids"]] == [clear_id]
    assert [b["id"] for b in data["blocked"]] == [blocked_id]
    assert "$7.49" in data["blocked"][0]["reason"]
    assert data["failed"] == []
    assert (await farb_repo.get(blocked_id)).state == FarbState.POST_BREAKEVEN
    assert (await farb_repo.get(clear_id)).state == FarbState.CLOSING_SHORT


async def test_farb_positions_exposes_the_block_and_alerts_list_it(
    gated_client, session_factory,
):
    fp_id, strat_id = await _seed_fp(session_factory)
    healthy_id, _ = await _seed_fp(session_factory, coin="ETH", strategy_id=strat_id)
    await gated_client.post(f"/api/farb-positions/{fp_id}/close")

    rows = (await gated_client.get(f"/api/farb-positions?strategy_id={strat_id}")).json()
    by_id = {r["id"]: r for r in rows}
    blocked = by_id[fp_id]
    assert blocked["close_blocked"] is True
    assert blocked["close_blocked_wanted"] == "manual close"
    assert "$7.49 < $11" in blocked["close_blocked_reason"]
    assert blocked["close_blocked_since_ms"] > 0
    assert blocked["close_blocked_check"]["spot_notional_usdc"] == pytest.approx(7.486)
    assert by_id[healthy_id]["close_blocked"] is False
    assert by_id[healthy_id]["close_blocked_reason"] is None

    alerts = (await gated_client.get(f"/api/alerts?strategy_id={strat_id}")).json()
    assert len(alerts) == 1
    assert alerts[0]["type"] == "close_blocked" and alerts[0]["severity"] == "ERROR"
    assert alerts[0]["coin"] == "PURR" and alerts[0]["position_id"] == fp_id
    assert "can't close" in alerts[0]["message"] and "manual close" in alerts[0]["message"]


# ── leg values at live prices ────────────────────────────────────────────────

class _LiveExchange:
    """Just the two reads the enrichment uses."""

    def __init__(self, *, mids, position_value) -> None:
        self._mids = mids
        self._position_value = position_value

    async def get_account_snapshot(self):
        from frab.exchanges.hyperliquid.wire import HLPerpAssetPosition, HLPerpState

        ap = HLPerpAssetPosition(
            coin="PURR", szi=-77.0, unrealized_pnl=-0.1, cum_funding_since_open=0.9,
            margin_used=3.8, position_value=self._position_value, leverage_value=3,
        )
        return HLPerpState(account_value=100.0, asset_positions=[ap]), None

    async def get_spot_mids_by_coin(self):
        return self._mids


async def test_leg_values_are_current_not_entry(gated_client, session_factory):
    fp_id, strat_id = await _seed_fp(session_factory)          # 77 PURR, entry 0.14446
    gated_client._transport.app.state.exchange = _LiveExchange(
        mids={"PURR": 0.0985}, position_value=7.62,
    )

    row = (await gated_client.get(f"/api/farb-positions/{fp_id}")).json()

    assert row["spot_value_usdc"] == pytest.approx(77 * 0.0985)       # 7.58, not 11.12
    assert row["perp_value_usdc"] == pytest.approx(7.62)               # HL's notional at the mark
    assert row["legs"]["spot"]["entry_price"] == pytest.approx(0.14446)  # entry still available


async def test_leg_values_are_none_without_a_live_price(gated_client, session_factory):
    fp_id, strat_id = await _seed_fp(session_factory)
    # no exchange at all -> nothing live
    row = (await gated_client.get(f"/api/farb-positions/{fp_id}")).json()
    assert row["spot_value_usdc"] is None and row["perp_value_usdc"] is None

    # exchange up but no mid for the coin, and HL reports no notional
    gated_client._transport.app.state.exchange = _LiveExchange(mids={}, position_value=0.0)
    row = (await gated_client.get(f"/api/farb-positions/{fp_id}")).json()
    assert row["spot_value_usdc"] is None and row["perp_value_usdc"] is None
