"""Tests for exchange-agnostic contracts and DTOs in base.py."""
from datetime import UTC, datetime

import pytest

from frab.exchanges.base import (
    Executor,
    FillReport,
    FundingTick,
    Leg,
    MarketDataSource,
    MarketSpec,
    OrderRequest,
    OrderType,
    PositionState,
    Quote,
    Side,
)

_DT = datetime(2024, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Enum string values
# ---------------------------------------------------------------------------

def test_side_enum_values():
    assert Side.BUY == "buy"
    assert Side.SELL == "sell"


def test_leg_enum_values():
    assert Leg.SPOT == "spot"
    assert Leg.PERP == "perp"


def test_order_type_enum_values():
    assert OrderType.MARKET == "market"


# ---------------------------------------------------------------------------
# Dataclass immutability
# ---------------------------------------------------------------------------

def test_quote_is_frozen():
    q = Quote(coin="BTC", ts=_DT, bid=29999.0, ask=30001.0, mark=30000.0, spot=None)
    with pytest.raises((AttributeError, TypeError)):
        q.bid = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Dataclass equality
# ---------------------------------------------------------------------------

def test_funding_tick_equality():
    tick_a = FundingTick(coin="BTC", ts=_DT, rate=0.0001, premium=None, annualized_pct=10.95)
    tick_b = FundingTick(coin="BTC", ts=_DT, rate=0.0001, premium=None, annualized_pct=10.95)
    assert tick_a == tick_b


def test_funding_tick_inequality():
    tick_a = FundingTick(coin="BTC", ts=_DT, rate=0.0001, premium=None, annualized_pct=10.95)
    tick_b = FundingTick(coin="ETH", ts=_DT, rate=0.0001, premium=None, annualized_pct=10.95)
    assert tick_a != tick_b


# ---------------------------------------------------------------------------
# OrderRequest defaults
# ---------------------------------------------------------------------------

def test_order_request_defaults():
    req = OrderRequest(coin="BTC", leg=Leg.SPOT, side=Side.BUY, qty=1.0)
    assert req.order_type == OrderType.MARKET
    assert req.client_ref is None


# ---------------------------------------------------------------------------
# Protocol structural checks — MarketDataSource
# ---------------------------------------------------------------------------

class GoodMarketDataSource:
    name = "test_exchange"

    async def fetch_funding(self, coin: str) -> FundingTick: ...
    async def fetch_funding_history(self, coin: str, since_ms: int) -> list[FundingTick]: ...
    async def fetch_quote(self, coin: str) -> Quote: ...
    async def fetch_meta(self) -> list[MarketSpec]: ...


def test_market_data_source_isinstance():
    assert isinstance(GoodMarketDataSource(), MarketDataSource)


# ---------------------------------------------------------------------------
# Protocol structural checks — Executor
# ---------------------------------------------------------------------------

class GoodExecutor:
    async def submit(self, req: OrderRequest) -> FillReport: ...
    async def get_position(self, coin: str) -> PositionState | None: ...
    async def reconcile(self) -> None: ...
    async def round_qty(self, coin: str, qty: float) -> float: ...
    async def round_qty_to_nearest(self, coin: str, qty: float) -> float: ...
    async def transfer_spot_to_perp(self, usdc_amount: float) -> dict: ...
    async def transfer_perp_to_spot(self, usdc_amount: float) -> dict: ...


def test_executor_isinstance():
    assert isinstance(GoodExecutor(), Executor)


# ---------------------------------------------------------------------------
# Negative structural check — missing method
# ---------------------------------------------------------------------------

class BadExecutor:
    async def get_position(self, coin: str) -> PositionState | None: ...
    async def reconcile(self) -> None: ...
    # missing: submit


def test_bad_executor_not_isinstance():
    assert not isinstance(BadExecutor(), Executor)
