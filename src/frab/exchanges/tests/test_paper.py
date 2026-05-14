"""Tests for PaperExecutor."""
from __future__ import annotations

import pytest

from frab.exchanges.base import Leg, MarketDataSource, OrderRequest, Quote, Side
from frab.exchanges.paper import PaperExecutor

_CLOCK = lambda: 1700000000000  # noqa: E731
_COIN = "BTC"


def _make_quote(
    bid: float = 100.0,
    ask: float = 100.0,
    spot: float | None = None,
    mark: float = 100.0,
) -> Quote:
    return Quote(coin=_COIN, ts_ms=0, bid=bid, ask=ask, mark=mark, spot=spot)


def _req(
    side: Side,
    leg: Leg,
    qty: float = 1.0,
    client_ref: str | None = None,
) -> OrderRequest:
    return OrderRequest(coin=_COIN, leg=leg, side=side, qty=qty, client_ref=client_ref)


@pytest.fixture
def md(mocker):
    return mocker.AsyncMock(spec=MarketDataSource)


@pytest.fixture
def ex(md):
    return PaperExecutor(
        market_data=md,
        spot_taker_bps=7.0,
        perp_taker_bps=3.5,
        extra_slip_bps=2.0,
        clock_ms=_CLOCK,
    )


# 1. Spot BUY price
@pytest.mark.asyncio
async def test_spot_buy_price_no_spot_field(md, ex):
    md.fetch_quote.return_value = _make_quote(ask=100.0, spot=None)
    fill = await ex.submit(_req(Side.BUY, Leg.SPOT))
    assert fill.price == pytest.approx(100.0 * (1 + 2e-4))
    assert fill.fee == pytest.approx(1.0 * fill.price * 7.0 / 1e4)


# 2. Spot SELL price
@pytest.mark.asyncio
async def test_spot_sell_price_no_spot_field(md, ex):
    md.fetch_quote.return_value = _make_quote(bid=100.0, spot=None)
    fill = await ex.submit(_req(Side.SELL, Leg.SPOT))
    assert fill.price == pytest.approx(100.0 * (1 - 2e-4))


# 3. Spot uses spot field when available
@pytest.mark.asyncio
async def test_spot_buy_uses_spot_field(md, ex):
    md.fetch_quote.return_value = _make_quote(bid=10.0, ask=200.0, spot=50.0)
    fill = await ex.submit(_req(Side.BUY, Leg.SPOT))
    assert fill.price == pytest.approx(50.0 * (1 + 2e-4))


# 4. Perp SELL (open short)
@pytest.mark.asyncio
async def test_perp_sell_open_short(md, ex):
    md.fetch_quote.return_value = _make_quote(bid=200.0, ask=200.0)
    fill = await ex.submit(_req(Side.SELL, Leg.PERP))
    assert fill.price == pytest.approx(200.0 * (1 - 2e-4))
    assert fill.fee == pytest.approx(1.0 * fill.price * 3.5 / 1e4)


# 5. Perp BUY (cover short)
@pytest.mark.asyncio
async def test_perp_buy_cover(md, ex):
    md.fetch_quote.return_value = _make_quote(bid=200.0, ask=200.0)
    # First open the short
    await ex.submit(_req(Side.SELL, Leg.PERP))
    fill = await ex.submit(_req(Side.BUY, Leg.PERP))
    assert fill.price == pytest.approx(200.0 * (1 + 2e-4))


# 6. Fill metadata
@pytest.mark.asyncio
async def test_fill_metadata(md, ex):
    md.fetch_quote.return_value = _make_quote(ask=100.0)
    fill = await ex.submit(_req(Side.BUY, Leg.SPOT, client_ref="ref-42"))
    assert fill.is_paper is True
    assert fill.slippage_bps == 2.0
    assert fill.client_ref == "ref-42"
    assert fill.ts_ms == 1700000000000


# 7. Position empty initially
@pytest.mark.asyncio
async def test_position_empty_initially(ex):
    result = await ex.get_position(_COIN)
    assert result is None


# 8. Single spot buy creates position
@pytest.mark.asyncio
async def test_single_spot_buy_creates_position(md, ex):
    md.fetch_quote.return_value = _make_quote(ask=100.0, spot=None)
    fill = await ex.submit(_req(Side.BUY, Leg.SPOT, qty=1.0))
    pos = await ex.get_position(_COIN)
    assert pos is not None
    assert pos.spot_units == pytest.approx(1.0)
    assert pos.avg_entry_spot == pytest.approx(fill.price)
    assert pos.perp_units == pytest.approx(0.0)
    assert pos.avg_entry_perp is None


# 9. Weighted average on add
@pytest.mark.asyncio
async def test_weighted_average_spot_buys(md, ex):
    md.fetch_quote.return_value = _make_quote(ask=100.0, spot=None)
    fill1 = await ex.submit(_req(Side.BUY, Leg.SPOT, qty=1.0))
    md.fetch_quote.return_value = _make_quote(ask=110.0, spot=None)
    fill2 = await ex.submit(_req(Side.BUY, Leg.SPOT, qty=1.0))
    pos = await ex.get_position(_COIN)
    expected_avg = (fill1.price + fill2.price) / 2
    assert pos.avg_entry_spot == pytest.approx(expected_avg)
    assert pos.spot_units == pytest.approx(2.0)


# 10. Reducing keeps avg
@pytest.mark.asyncio
async def test_reducing_keeps_avg(md, ex):
    md.fetch_quote.return_value = _make_quote(ask=100.0, bid=100.0, spot=None)
    fill = await ex.submit(_req(Side.BUY, Leg.SPOT, qty=2.0))
    buy_avg = fill.price
    md.fetch_quote.return_value = _make_quote(ask=120.0, bid=120.0, spot=None)
    await ex.submit(_req(Side.SELL, Leg.SPOT, qty=1.0))
    pos = await ex.get_position(_COIN)
    assert pos.spot_units == pytest.approx(1.0)
    assert pos.avg_entry_spot == pytest.approx(buy_avg)


# 11. Closing zeroes leg → get_position returns None
@pytest.mark.asyncio
async def test_closing_zeroes_leg(md, ex):
    md.fetch_quote.return_value = _make_quote(ask=100.0, bid=100.0, spot=None)
    await ex.submit(_req(Side.BUY, Leg.SPOT, qty=1.0))
    await ex.submit(_req(Side.SELL, Leg.SPOT, qty=1.0))
    pos = await ex.get_position(_COIN)
    assert pos is None


# 12. Combined legs
@pytest.mark.asyncio
async def test_combined_legs(md, ex):
    md.fetch_quote.return_value = _make_quote(ask=100.0, bid=100.0, spot=None)
    spot_fill = await ex.submit(_req(Side.BUY, Leg.SPOT, qty=1.0))
    perp_fill = await ex.submit(_req(Side.SELL, Leg.PERP, qty=1.0))
    pos = await ex.get_position(_COIN)
    assert pos is not None
    assert pos.spot_units == pytest.approx(1.0)
    assert pos.perp_units == pytest.approx(-1.0)
    assert pos.avg_entry_spot == pytest.approx(spot_fill.price)
    assert pos.avg_entry_perp == pytest.approx(perp_fill.price)


# 13. Flip raises ValueError
@pytest.mark.asyncio
async def test_flip_raises(md, ex):
    md.fetch_quote.return_value = _make_quote(bid=100.0, ask=100.0)
    await ex.submit(_req(Side.SELL, Leg.PERP, qty=1.0))
    with pytest.raises(ValueError, match="flip not supported"):
        await ex.submit(_req(Side.BUY, Leg.PERP, qty=2.0))


# 14. reconcile is no-op
@pytest.mark.asyncio
async def test_reconcile_noop(ex):
    result = await ex.reconcile()
    assert result is None


# 15. Fee math explicit
@pytest.mark.asyncio
async def test_fee_math_explicit(md, ex):
    # perp sell at bid=100.0, slip=2bps → price = 100 * (1 - 0.0002) = 99.98
    md.fetch_quote.return_value = _make_quote(bid=100.0, ask=100.0)
    fill = await ex.submit(_req(Side.SELL, Leg.PERP, qty=2.0))
    expected_fee = 2.0 * fill.price * 3.5 / 1e4
    assert fill.fee == pytest.approx(expected_fee)
