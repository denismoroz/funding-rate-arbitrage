from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from frab.application.portfolio_service import PortfolioService
from frab.db.models import (
    Base,
    EquitySnapshot,
    Exchange as ExchangeRow,
    Market,
    Position as DbPosition,
    PositionMode,
    PositionStatus,
    Strategy,
)
from frab.db.session import make_session_factory, session_scope
from frab.domain.exchange import Exchange
from frab.domain.position import ClosedPosition, Position


def _now() -> datetime:
    return datetime.now(UTC)


def _make_position(
    coin: str = "BTC",
    exchange: Exchange = Exchange.HYPERLIQUID,
    notional: float = 500.0,
    margin: float = 100.0,
    state: dict | None = None,
) -> Position:
    return Position(
        exchange=exchange,
        coin=coin,
        spot_qty=0.01,
        perp_qty=0.01,
        notional_usd=notional,
        margin_reserve_usd=margin,
        entry_spot_price=50000.0,
        entry_perp_price=50010.0,
        opened_at=_now(),
        state=state or {},
    )


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        ex = ExchangeRow(
            name="hyperliquid",
            funding_interval_h=1,
            spot_taker_bps=7,
            perp_taker_bps=3.5,
        )
        session.add(ex)
        await session.flush()
        for coin in ("BTC", "ETH", "SOL"):
            session.add(
                Market(
                    exchange_id=ex.id,
                    coin=coin,
                    min_size=0.001,
                    tick_size=0.01,
                )
            )
        session.add(Strategy(name="test_strategy", version="v1", params_json={}))
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------------------
# 1. Construction
# ---------------------------------------------------------------------------

async def test_construction_initializes_empty(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    assert svc._positions == {}
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == 1000.0


# ---------------------------------------------------------------------------
# 2. rehydrate empty DB
# ---------------------------------------------------------------------------

async def test_rehydrate_empty_db_cash_unchanged(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    await svc.rehydrate_from_db()
    assert svc._positions == {}
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == 1000.0
    assert svc._fees_cum == 0.0
    assert svc._funding_cum == 0.0
    assert svc._realized_pnl_cum == 0.0


# ---------------------------------------------------------------------------
# 3. rehydrate with seeded position — fields round-trip
# ---------------------------------------------------------------------------

async def test_rehydrate_with_seeded_position(session_factory):
    async with session_scope(session_factory) as session:
        mkt = (
            await session.execute(
                select(Market).join(ExchangeRow).where(
                    ExchangeRow.name == "hyperliquid", Market.coin == "BTC"
                )
            )
        ).scalar_one()
        db_pos = DbPosition(
            strategy_id=1,
            market_id=mkt.id,
            mode=PositionMode.LIVE,
            status=PositionStatus.OPEN,
            opened_at=_now(),
            spot_units=0.01,
            perp_units=0.01,
            entry_spot_price=50000.0,
            entry_perp_price=50010.0,
            exchange="hyperliquid",
            state={"k": "v"},
            notional_usd=500.0,
            margin_reserve_usd=100.0,
        )
        session.add(db_pos)

    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    await svc.rehydrate_from_db()

    key = (Exchange.HYPERLIQUID, "BTC")
    assert key in svc._positions
    pos = svc._positions[key]
    assert pos.exchange == Exchange.HYPERLIQUID
    assert pos.notional_usd == 500.0
    assert pos.margin_reserve_usd == 100.0
    assert pos.state == {"k": "v"}


# ---------------------------------------------------------------------------
# 4. rehydrate restores cash (initial - committed)
# ---------------------------------------------------------------------------

async def test_rehydrate_restores_cash(session_factory):
    async with session_scope(session_factory) as session:
        mkt = (
            await session.execute(
                select(Market).join(ExchangeRow).where(
                    ExchangeRow.name == "hyperliquid", Market.coin == "BTC"
                )
            )
        ).scalar_one()
        session.add(
            DbPosition(
                strategy_id=1,
                market_id=mkt.id,
                mode=PositionMode.LIVE,
                status=PositionStatus.OPEN,
                opened_at=_now(),
                spot_units=0.01,
                perp_units=0.01,
                entry_spot_price=50000.0,
                entry_perp_price=50010.0,
                exchange="hyperliquid",
                state={},
                notional_usd=500.0,
                margin_reserve_usd=100.0,
            )
        )

    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    await svc.rehydrate_from_db()
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(400.0)


# ---------------------------------------------------------------------------
# 5. rehydrate restores accumulators from EquitySnapshot
# ---------------------------------------------------------------------------

async def test_rehydrate_restores_accumulators(session_factory):
    async with session_scope(session_factory) as session:
        session.add(
            EquitySnapshot(
                strategy_id=1,
                ts=_now(),
                total_equity=1000.0,
                cash=900.0,
                spot_value=50.0,
                perp_unrealized=-5.0,
                perp_realized_cum=5.0,
                funding_cum=20.0,
                fees_cum=10.0,
            )
        )

    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    await svc.rehydrate_from_db()
    assert svc._fees_cum == 10.0
    assert svc._funding_cum == 20.0
    assert svc._realized_pnl_cum == 5.0


# ---------------------------------------------------------------------------
# 6. apply_open inserts DB row
# ---------------------------------------------------------------------------

async def test_apply_open_inserts_db_row(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = _make_position(coin="BTC", notional=500.0, margin=100.0, state={"x": 1})
    await svc.apply_open(pos)

    async with session_scope(session_factory) as session:
        rows = (
            await session.execute(select(DbPosition).where(DbPosition.strategy_id == 1))
        ).scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.status == PositionStatus.OPEN
    assert row.exchange == "hyperliquid"
    assert row.state == {"x": 1}
    assert row.notional_usd == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# 7. apply_open debits cash
# ---------------------------------------------------------------------------

async def test_apply_open_debits_cash(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = _make_position(notional=500.0, margin=100.0)
    await svc.apply_open(pos)
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(400.0)


# ---------------------------------------------------------------------------
# 8. apply_close marks DB row CLOSED
# ---------------------------------------------------------------------------

async def test_apply_close_marks_row_closed(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = _make_position(notional=500.0, margin=100.0)
    await svc.apply_open(pos)

    closed_at = _now()
    closed = ClosedPosition(
        exchange=Exchange.HYPERLIQUID,
        coin="BTC",
        closed_at=closed_at,
        realized_pnl=25.0,
        fees_paid_total=2.0,
        funding_collected_total=5.0,
        released_margin_usd=100.0,
    )
    await svc.apply_close(closed)

    async with session_scope(session_factory) as session:
        row = (
            await session.execute(select(DbPosition).where(DbPosition.strategy_id == 1))
        ).scalar_one()

    assert row.status == PositionStatus.CLOSED
    assert row.realized_pnl == pytest.approx(25.0)
    assert row.closed_at is not None


# ---------------------------------------------------------------------------
# 9. apply_close credits cash and bumps realized_pnl_cum
# ---------------------------------------------------------------------------

async def test_apply_close_credits_cash_and_bumps_pnl_cum(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = _make_position(notional=500.0, margin=100.0)
    await svc.apply_open(pos)
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(400.0)

    closed = ClosedPosition(
        exchange=Exchange.HYPERLIQUID,
        coin="BTC",
        closed_at=_now(),
        realized_pnl=25.0,
        fees_paid_total=2.0,
        funding_collected_total=5.0,
        released_margin_usd=100.0,
        released_notional_usd=500.0,
    )
    await svc.apply_close(closed)
    # cash += released_notional + released_margin + realized_pnl = 400 + 500 + 100 + 25 = 1025
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(1025.0)
    assert svc._realized_pnl_cum == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# 10. apply_close removes from _positions
# ---------------------------------------------------------------------------

async def test_apply_close_removes_from_positions(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = _make_position()
    await svc.apply_open(pos)
    assert (Exchange.HYPERLIQUID, "BTC") in svc._positions

    closed = ClosedPosition(
        exchange=Exchange.HYPERLIQUID,
        coin="BTC",
        closed_at=_now(),
        realized_pnl=0.0,
        fees_paid_total=0.0,
        funding_collected_total=0.0,
        released_margin_usd=100.0,
    )
    await svc.apply_close(closed)
    assert (Exchange.HYPERLIQUID, "BTC") not in svc._positions

    portfolio = await svc.current()
    assert portfolio.position(Exchange.HYPERLIQUID, "BTC") is None


# ---------------------------------------------------------------------------
# 11. apply_margin_adjustment top-up
# ---------------------------------------------------------------------------

async def test_apply_margin_adjustment_topup(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = _make_position(notional=500.0, margin=100.0)
    await svc.apply_open(pos)
    cash_before = svc._cash_per_exchange[Exchange.HYPERLIQUID]

    await svc.apply_margin_adjustment(Exchange.HYPERLIQUID, "BTC", 50.0)

    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(cash_before - 50.0)
    assert svc._positions[(Exchange.HYPERLIQUID, "BTC")].margin_reserve_usd == pytest.approx(150.0)

    async with session_scope(session_factory) as session:
        row = (
            await session.execute(select(DbPosition).where(DbPosition.strategy_id == 1))
        ).scalar_one()
    assert row.margin_reserve_usd == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# 12. apply_margin_adjustment release
# ---------------------------------------------------------------------------

async def test_apply_margin_adjustment_release(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = _make_position(notional=500.0, margin=100.0)
    await svc.apply_open(pos)
    cash_before = svc._cash_per_exchange[Exchange.HYPERLIQUID]

    await svc.apply_margin_adjustment(Exchange.HYPERLIQUID, "BTC", -30.0)

    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(cash_before + 30.0)
    assert svc._positions[(Exchange.HYPERLIQUID, "BTC")].margin_reserve_usd == pytest.approx(70.0)


# ---------------------------------------------------------------------------
# 13. record_fill_fees
# ---------------------------------------------------------------------------

async def test_record_fill_fees(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = _make_position(notional=500.0, margin=100.0)
    await svc.apply_open(pos)
    cash_before = svc._cash_per_exchange[Exchange.HYPERLIQUID]

    await svc.record_fill_fees(Exchange.HYPERLIQUID, "BTC", 3.5)

    # real-wallet: fees debit cash
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(cash_before - 3.5)
    assert svc._fees_cum == pytest.approx(3.5)
    assert svc._positions[(Exchange.HYPERLIQUID, "BTC")].fees_paid == pytest.approx(3.5)

    async with session_scope(session_factory) as session:
        row = (
            await session.execute(select(DbPosition).where(DbPosition.strategy_id == 1))
        ).scalar_one()
    assert row.fees_paid == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# 14. accrue_funding
# ---------------------------------------------------------------------------

async def test_accrue_funding(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = _make_position(notional=500.0, margin=100.0)
    await svc.apply_open(pos)
    cash_before = svc._cash_per_exchange[Exchange.HYPERLIQUID]

    await svc.accrue_funding(Exchange.HYPERLIQUID, "BTC", 7.0)

    # real-wallet: funding credits cash
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(cash_before + 7.0)
    assert svc._funding_cum == pytest.approx(7.0)
    assert svc._positions[(Exchange.HYPERLIQUID, "BTC")].funding_collected == pytest.approx(7.0)

    async with session_scope(session_factory) as session:
        row = (
            await session.execute(select(DbPosition).where(DbPosition.strategy_id == 1))
        ).scalar_one()
    assert row.funding_collected == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# 15. set_fees_cum overwrites
# ---------------------------------------------------------------------------

async def test_set_fees_cum_overwrites(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    svc._fees_cum = 5.0
    await svc.set_fees_cum(42.0)
    assert svc._fees_cum == 42.0


# ---------------------------------------------------------------------------
# 16. set_funding_cum overwrites
# ---------------------------------------------------------------------------

async def test_set_funding_cum_overwrites(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    svc._funding_cum = 5.0
    await svc.set_funding_cum(99.0)
    assert svc._funding_cum == 99.0


# ---------------------------------------------------------------------------
# 17. current() returns Portfolio with right wallet
# ---------------------------------------------------------------------------

async def test_current_returns_portfolio_with_wallet(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = _make_position(notional=500.0, margin=100.0)
    await svc.apply_open(pos)

    portfolio = await svc.current()
    wallet = portfolio.wallet_per_exchange[Exchange.HYPERLIQUID]

    assert wallet.available_usdc == pytest.approx(400.0)
    assert wallet.reserved_usdc == pytest.approx(600.0)  # notional + margin
    assert wallet.total_value_usd == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# 18. equity(marks) returns Equity matching manual arithmetic
# ---------------------------------------------------------------------------

async def test_equity_returns_equity(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = Position(
        exchange=Exchange.HYPERLIQUID,
        coin="BTC",
        spot_qty=0.01,
        perp_qty=0.01,
        notional_usd=500.0,
        margin_reserve_usd=100.0,
        entry_spot_price=50000.0,
        entry_perp_price=50010.0,
        opened_at=_now(),
    )
    await svc.apply_open(pos)
    # cash after open = 1000 - 600 = 400

    marks = {(Exchange.HYPERLIQUID, "BTC"): 51000.0}
    eq = svc.equity(marks)

    cash = 400.0
    spot_value = 0.01 * 51000.0  # 510
    perp_unrealized = (50010.0 - 51000.0) * 0.01  # -9.9
    margin_reserved = 100.0
    expected_total = cash + spot_value + perp_unrealized + margin_reserved

    assert eq.cash == pytest.approx(cash)
    assert eq.spot_value == pytest.approx(spot_value)
    assert eq.perp_unrealized == pytest.approx(perp_unrealized)
    assert eq.total_equity == pytest.approx(expected_total)


# ---------------------------------------------------------------------------
# 19. equity no-double-count invariant after full cycle
# ---------------------------------------------------------------------------

async def test_equity_matches_domain_formula_after_full_cycle(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = Position(
        exchange=Exchange.HYPERLIQUID,
        coin="BTC",
        spot_qty=0.01,
        perp_qty=0.01,
        notional_usd=500.0,
        margin_reserve_usd=50.0,
        entry_spot_price=50000.0,
        entry_perp_price=50000.0,
        opened_at=_now(),
    )
    await svc.apply_open(pos)
    # cash = 1000 - 500 - 50 = 450
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(450.0)

    await svc.record_fill_fees(Exchange.HYPERLIQUID, "BTC", 5.0)
    # real-wallet: fees debit cash: 450 - 5 = 445
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(445.0)
    assert svc._fees_cum == pytest.approx(5.0)

    await svc.accrue_funding(Exchange.HYPERLIQUID, "BTC", 3.0)
    # real-wallet: funding credits cash: 445 + 3 = 448
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(448.0)
    assert svc._funding_cum == pytest.approx(3.0)

    # flat mark: entry_spot_price == entry_perp_price == 50000
    marks = {(Exchange.HYPERLIQUID, "BTC"): 50000.0}
    eq = svc.equity(marks)

    # 448 (cash) + spot_value(500) + perp_unrealized(0) + margin_reserved(50) = 998
    # fees and funding are already baked into cash — no double-count
    assert eq.total_equity == pytest.approx(998.0)


# ---------------------------------------------------------------------------
# 20. round-trip at flat close returns equity to initial
# ---------------------------------------------------------------------------

async def test_full_cycle_returns_to_initial_at_flat_close(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = Position(
        exchange=Exchange.HYPERLIQUID,
        coin="BTC",
        spot_qty=1.0,
        perp_qty=1.0,
        notional_usd=500.0,
        margin_reserve_usd=50.0,
        entry_spot_price=500.0,
        entry_perp_price=500.0,
        opened_at=_now(),
    )
    await svc.apply_open(pos)
    # cash = 1000 - 500 - 50 = 450
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(450.0)

    marks = {(Exchange.HYPERLIQUID, "BTC"): 500.0}
    eq_open = svc.equity(marks)
    # 450 + spot_value(500) + perp_unrealized(0) + margin_reserved(50) = 1000
    assert eq_open.total_equity == pytest.approx(1000.0)

    closed = ClosedPosition(
        exchange=Exchange.HYPERLIQUID,
        coin="BTC",
        closed_at=_now(),
        realized_pnl=0.0,
        fees_paid_total=0.0,
        funding_collected_total=0.0,
        released_margin_usd=50.0,
        released_notional_usd=500.0,
    )
    await svc.apply_close(closed)

    # cash = 450 + 500 + 50 = 1000
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(1000.0)
    eq_closed = svc.equity(marks)
    # 1000 + 0 + 0 + 0 + 0 + 0 - 0 = 1000
    assert eq_closed.total_equity == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# 21. flat close does not change total equity (fees/funding already accounted)
# ---------------------------------------------------------------------------

async def test_equity_unchanged_by_flat_close(session_factory):
    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: 1000.0},
    )
    pos = Position(
        exchange=Exchange.HYPERLIQUID,
        coin="BTC",
        spot_qty=0.01,
        perp_qty=0.01,
        notional_usd=500.0,
        margin_reserve_usd=50.0,
        entry_spot_price=50000.0,
        entry_perp_price=50000.0,
        opened_at=_now(),
    )
    await svc.apply_open(pos)
    # cash = 1000 - 500 - 50 = 450

    await svc.record_fill_fees(Exchange.HYPERLIQUID, "BTC", 5.0)
    await svc.accrue_funding(Exchange.HYPERLIQUID, "BTC", 3.0)

    marks = {(Exchange.HYPERLIQUID, "BTC"): 50000.0}
    eq_before = svc.equity(marks)
    assert eq_before.total_equity == pytest.approx(998.0)

    closed = ClosedPosition(
        exchange=Exchange.HYPERLIQUID,
        coin="BTC",
        closed_at=_now(),
        realized_pnl=0.0,
        fees_paid_total=5.0,
        funding_collected_total=3.0,
        released_margin_usd=50.0,
        released_notional_usd=500.0,
    )
    await svc.apply_close(closed)

    # cash = 448 (after fees/funding) + 500 (released_notional) + 50 (released_margin) + 0 (realized_pnl) = 998
    assert svc._cash_per_exchange[Exchange.HYPERLIQUID] == pytest.approx(998.0)

    # equity = 998 + 0(spot) + 0(perp_unreal) + 0(margin) = 998
    eq_after = svc.equity({})
    assert eq_after.total_equity == pytest.approx(998.0)


# ---------------------------------------------------------------------------
# 22. Real-wallet invariant: open → fees → funding → close round-trip
#     equity must equal a single closed-form derived from real-wallet bookkeeping.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("entry_price,exit_price,fees,funding,notional,margin", [
    (100.0, 100.0, 0.0,  0.0,   500.0, 50.0),   # flat, no cost
    (100.0, 110.0, 2.0,  1.5,  500.0, 50.0),   # spot up, perp down, fees/funding
    (100.0,  90.0, 3.0,  5.0,  500.0, 75.0),   # spot down, perp up, funding covers
    (100.0, 100.0, 4.0, 10.0,  800.0, 80.0),   # flat price, net gain from funding
])
async def test_equity_invariant_open_close_round_trip(
    session_factory,
    entry_price: float,
    exit_price: float,
    fees: float,
    funding: float,
    notional: float,
    margin: float,
) -> None:
    """Equity equals the real-wallet closed-form after a full round-trip.

    Cash flow model (real-wallet semantics):
      - apply_open:        cash -= notional + margin
      - record_fill_fees:  cash -= fees
      - accrue_funding:    cash += funding
      - apply_close:       cash += released_notional (qty*exit_price)
                                 + released_margin
                                 + realized_pnl (perp PnL only: (entry-exit)*qty)

    Net cash after round-trip:
        initial
        - notional - margin          (open)
        - fees                       (record_fill_fees)
        + funding                    (accrue_funding)
        + qty*exit_price             (released_notional)
        + margin                     (released_margin)
        + (entry-exit)*qty           (perp PnL)
      = initial - fees + funding
        + qty*(entry_price + exit_price - exit_price - entry_price + exit_price)
        ...simplifying: initial - fees + funding
        (the spot leg proceeds plus the perp settlement exactly recover the original notional)

    Concretely:
        released_notional = qty * exit_price
        perp_pnl          = (entry - exit) * qty
        released_notional + perp_pnl = qty*exit_price + (entry-exit)*qty
                                     = qty * entry_price   (= original notional)
        + released_margin → total = original_notional + original_margin

    So equity = initial_cash - fees + funding.
    """
    initial_cash = 2000.0
    qty = notional / entry_price  # spot_qty = perp_qty

    svc = PortfolioService(
        session_factory,
        strategy_id=1,
        initial_cash_per_exchange={Exchange.HYPERLIQUID: initial_cash},
    )
    pos = Position(
        exchange=Exchange.HYPERLIQUID,
        coin="BTC",
        spot_qty=qty,
        perp_qty=qty,
        notional_usd=notional,
        margin_reserve_usd=margin,
        entry_spot_price=entry_price,
        entry_perp_price=entry_price,
        opened_at=_now(),
    )
    await svc.apply_open(pos)
    await svc.record_fill_fees(Exchange.HYPERLIQUID, "BTC", fees)
    await svc.accrue_funding(Exchange.HYPERLIQUID, "BTC", funding)

    # Perp PnL only (short position: profit when price falls)
    perp_pnl = (entry_price - exit_price) * qty

    closed = ClosedPosition(
        exchange=Exchange.HYPERLIQUID,
        coin="BTC",
        closed_at=_now(),
        realized_pnl=perp_pnl,
        fees_paid_total=fees,
        funding_collected_total=funding,
        released_margin_usd=margin,
        released_notional_usd=qty * exit_price,
    )
    await svc.apply_close(closed)

    # Real-wallet closed-form: perp PnL + spot proceeds = original notional
    expected_equity = initial_cash - fees + funding

    eq = svc.equity({})
    assert eq.total_equity == pytest.approx(expected_equity, rel=1e-9)
