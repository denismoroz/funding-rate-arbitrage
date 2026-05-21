"""Tests for LiveHLExecutor."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hyperliquid.utils import constants

from frab.exchanges.base import FillReport, Leg, OrderRequest, PositionState, Side
from frab.exchanges.hyperliquid_live import LiveHLExecutor, PartialFillError

_FIXED_DT = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
_CLOCK = lambda: _FIXED_DT  # noqa: E731


def _filled_resp(qty: float = 0.5, px: float = 30000.0, fee: float = 0.1):
    return {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{
            "filled": {"totalSz": str(qty), "avgPx": str(px), "oid": 12345, "fee": str(fee)}
        }]}},
    }


def _perp_req(coin: str = "BTC", side: Side = Side.BUY, qty: float = 0.5, client_ref: str | None = "ref-1") -> OrderRequest:
    return OrderRequest(coin=coin, leg=Leg.PERP, side=side, qty=qty, client_ref=client_ref)


def _spot_req(coin: str = "PURR", side: Side = Side.BUY, qty: float = 0.5, client_ref: str | None = "ref-2") -> OrderRequest:
    return OrderRequest(coin=coin, leg=Leg.SPOT, side=side, qty=qty, client_ref=client_ref)


def _make_executor(mocker, *, spot_token_map=None, slippage=0.01, account_address="0x" + "b" * 40):
    info = mocker.MagicMock()
    exchange = mocker.MagicMock()
    return LiveHLExecutor(
        info=info,
        exchange=exchange,
        account_address=account_address,
        spot_token_map=spot_token_map,
        slippage=slippage,
        clock_fn=_CLOCK,
    ), info, exchange


# 1. submit perp filled → FillReport fields
async def test_submit_perp_filled_returns_fillreport(mocker):
    ex, _, exchange = _make_executor(mocker)
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp(qty=0.5, px=30000.0, fee=0.1)))

    req = _perp_req(coin="BTC", side=Side.BUY, qty=0.5, client_ref="ref-xyz")
    fill = await ex.submit(req)

    assert fill.coin == "BTC"
    assert fill.leg == Leg.PERP
    assert fill.side == Side.BUY
    assert fill.qty == pytest.approx(0.5)
    assert fill.price == pytest.approx(30000.0)
    assert fill.fee == pytest.approx(0.1)
    assert fill.slippage_bps == pytest.approx(100.0)  # 0.01 * 1e4
    assert fill.client_ref == "ref-xyz"
    assert fill.ts == _FIXED_DT


# 2. submit SPOT uses pair name PURR/USDC
async def test_submit_spot_uses_pair_name(mocker):
    ex, _, exchange = _make_executor(mocker)
    mock_to_thread = mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp()))

    req = _spot_req(coin="PURR", side=Side.BUY, qty=0.5)
    await ex.submit(req)

    call_args = mock_to_thread.call_args
    # Second positional arg to to_thread is the name passed to market_open
    assert call_args.args[1] == "PURR/USDC"


# 3. submit SPOT with token map: BTC → UBTC/USDC
async def test_submit_spot_with_token_map(mocker):
    ex, _, exchange = _make_executor(mocker, spot_token_map={"BTC": "UBTC"})
    mock_to_thread = mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp()))

    req = OrderRequest(coin="BTC", leg=Leg.SPOT, side=Side.BUY, qty=0.1)
    await ex.submit(req)

    call_args = mock_to_thread.call_args
    assert call_args.args[1] == "UBTC/USDC"


# 4. submit PERP uses bare coin name
async def test_submit_perp_uses_bare_coin_name(mocker):
    ex, _, exchange = _make_executor(mocker)
    mock_to_thread = mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp()))

    req = _perp_req(coin="BTC")
    await ex.submit(req)

    call_args = mock_to_thread.call_args
    assert call_args.args[1] == "BTC"


# 5. BUY → is_buy=True; SELL → is_buy=False
async def test_submit_buy_vs_sell_sets_is_buy(mocker):
    ex, _, exchange = _make_executor(mocker)

    mock_to_thread = mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp()))
    await ex.submit(_perp_req(side=Side.BUY))
    assert mock_to_thread.call_args.args[2] is True  # is_buy

    mock_to_thread.reset_mock()
    mock_to_thread.return_value = _filled_resp()
    await ex.submit(_perp_req(side=Side.SELL))
    assert mock_to_thread.call_args.args[2] is False


# 6. top-level status != "ok" → RuntimeError
async def test_submit_top_level_status_err_raises(mocker):
    ex, _, exchange = _make_executor(mocker)
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(
        return_value={"status": "err", "response": "insufficient margin"}
    ))
    with pytest.raises(RuntimeError, match="HL order rejected"):
        await ex.submit(_perp_req())


# 7. inner error status → RuntimeError mentioning the error string
async def test_submit_inner_error_status_raises(mocker):
    ex, _, exchange = _make_executor(mocker)
    resp = {"status": "ok", "response": {"data": {"statuses": [{"error": "min size"}]}}}
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=resp))
    with pytest.raises(RuntimeError, match="min size"):
        await ex.submit(_perp_req())


# 8. resting status → RuntimeError
async def test_submit_resting_is_treated_as_failure(mocker):
    ex, _, exchange = _make_executor(mocker)
    resp = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 123}}]}}}
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=resp))
    with pytest.raises(RuntimeError, match="unexpectedly resting"):
        await ex.submit(_perp_req())


# 9. unrecognized status key → RuntimeError
async def test_submit_unrecognized_status_raises(mocker):
    ex, _, exchange = _make_executor(mocker)
    resp = {"status": "ok", "response": {"data": {"statuses": [{"weird": {}}]}}}
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=resp))
    with pytest.raises(RuntimeError, match="unrecognized status"):
        await ex.submit(_perp_req())


# 10. malformed response (missing nested keys) → RuntimeError
async def test_submit_malformed_response_raises_runtime_error(mocker):
    ex, _, exchange = _make_executor(mocker)
    resp = {"status": "ok", "response": {}}  # missing "data" key
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=resp))
    with pytest.raises(RuntimeError, match="shape unexpected"):
        await ex.submit(_perp_req())


# 11. blocking call wrapped in to_thread
async def test_submit_calls_in_to_thread(mocker):
    ex, _, exchange = _make_executor(mocker, slippage=0.02)
    mock_to_thread = mocker.patch(
        "asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp(qty=1.0, px=100.0))
    )
    req = _perp_req(coin="ETH", side=Side.BUY, qty=1.0)
    await ex.submit(req)

    mock_to_thread.assert_called_once()
    call = mock_to_thread.call_args
    # First arg is the callable (exchange.market_open)
    assert call.args[0] == exchange.market_open
    # Third arg is the name
    assert call.args[1] == "ETH"
    # Fourth arg is is_buy
    assert call.args[2] is True
    # Fifth arg is qty
    assert call.args[3] == pytest.approx(1.0)
    # Sixth arg is px=None
    assert call.args[4] is None
    # Seventh arg is slippage
    assert call.args[5] == pytest.approx(0.02)


# 12. get_position combines perp and spot
async def test_get_position_combines_perp_and_spot(mocker):
    ex, info, _ = _make_executor(mocker)

    perp_resp = {
        "assetPositions": [
            {"position": {"coin": "BTC", "szi": "-0.5", "entryPx": "30000"}}
        ]
    }
    spot_resp = {
        "balances": [
            {"coin": "BTC", "total": "0.5", "entryNtl": "15050"}
        ]
    }

    async def fake_gather(*coros):
        return (perp_resp, spot_resp)

    mocker.patch("asyncio.gather", side_effect=fake_gather)

    pos = await ex.get_position("BTC")
    assert pos is not None
    assert pos.perp_units == pytest.approx(-0.5)
    assert pos.spot_units == pytest.approx(0.5)
    assert pos.avg_entry_perp == pytest.approx(30000.0)
    assert pos.avg_entry_spot == pytest.approx(30100.0)  # 15050 / 0.5


# 13. spot token map used for lookup
async def test_get_position_uses_spot_token_map_for_lookup(mocker):
    ex, info, _ = _make_executor(mocker, spot_token_map={"BTC": "UBTC"})

    perp_resp = {"assetPositions": [{"position": {"coin": "BTC", "szi": "0.1", "entryPx": "50000"}}]}
    spot_resp = {"balances": [{"coin": "UBTC", "total": "0.1", "entryNtl": "5000"}]}

    async def fake_gather(*coros):
        return (perp_resp, spot_resp)

    mocker.patch("asyncio.gather", side_effect=fake_gather)

    pos = await ex.get_position("BTC")
    assert pos is not None
    assert pos.spot_units == pytest.approx(0.1)


# 14. both zero → None
async def test_get_position_returns_none_when_zero_everywhere(mocker):
    ex, info, _ = _make_executor(mocker)

    async def fake_gather(*coros):
        return ({"assetPositions": []}, {"balances": []})

    mocker.patch("asyncio.gather", side_effect=fake_gather)
    pos = await ex.get_position("BTC")
    assert pos is None


# 15. only perp present
async def test_get_position_handles_only_perp(mocker):
    ex, info, _ = _make_executor(mocker)

    perp_resp = {"assetPositions": [{"position": {"coin": "BTC", "szi": "-1.0", "entryPx": "40000"}}]}
    spot_resp = {"balances": []}

    async def fake_gather(*coros):
        return (perp_resp, spot_resp)

    mocker.patch("asyncio.gather", side_effect=fake_gather)

    pos = await ex.get_position("BTC")
    assert pos is not None
    assert pos.perp_units == pytest.approx(-1.0)
    assert pos.spot_units == pytest.approx(0.0)
    assert pos.avg_entry_spot is None
    assert pos.avg_entry_perp == pytest.approx(40000.0)


# 16. only spot present
async def test_get_position_handles_only_spot(mocker):
    ex, info, _ = _make_executor(mocker)

    perp_resp = {"assetPositions": []}
    spot_resp = {"balances": [{"coin": "BTC", "total": "2.0", "entryNtl": "60000"}]}

    async def fake_gather(*coros):
        return (perp_resp, spot_resp)

    mocker.patch("asyncio.gather", side_effect=fake_gather)

    pos = await ex.get_position("BTC")
    assert pos is not None
    assert pos.spot_units == pytest.approx(2.0)
    assert pos.perp_units == pytest.approx(0.0)
    assert pos.avg_entry_spot == pytest.approx(30000.0)  # 60000 / 2.0
    assert pos.avg_entry_perp is None


# 17. get_position without address raises
async def test_get_position_without_address_raises(mocker):
    info = mocker.MagicMock()
    exchange = mocker.MagicMock()
    ex = LiveHLExecutor(info=info, exchange=exchange, account_address=None)
    with pytest.raises(RuntimeError, match="account_address required"):
        await ex.get_position("BTC")


# 18. reconcile is no-op
async def test_reconcile_is_noop(mocker):
    ex, _, _ = _make_executor(mocker)
    result = await ex.reconcile()
    assert result is None


# 19. constructor builds Info for testnet when not injected
async def test_constructor_builds_info_for_testnet_when_not_injected(mocker):
    mock_info_cls = mocker.patch("frab.exchanges.hyperliquid_live.Info")
    mock_exchange_cls = mocker.patch("frab.exchanges.hyperliquid_live.Exchange")
    mocker.patch("frab.exchanges.hyperliquid_live.Account")

    LiveHLExecutor(
        private_key="0x" + "a" * 64,
        account_address="0x" + "b" * 40,
        network="testnet",
    )

    mock_info_cls.assert_called_once_with(constants.TESTNET_API_URL, skip_ws=True)
    call_kwargs = mock_exchange_cls.call_args.kwargs
    assert call_kwargs["base_url"] == constants.TESTNET_API_URL


# 20. constructor uses mainnet URL
async def test_constructor_uses_mainnet_url(mocker):
    mock_info_cls = mocker.patch("frab.exchanges.hyperliquid_live.Info")
    mock_exchange_cls = mocker.patch("frab.exchanges.hyperliquid_live.Exchange")
    mocker.patch("frab.exchanges.hyperliquid_live.Account")

    LiveHLExecutor(
        private_key="0x" + "a" * 64,
        account_address="0x" + "b" * 40,
        network="mainnet",
    )

    mock_info_cls.assert_called_once_with(constants.MAINNET_API_URL, skip_ws=True)
    call_kwargs = mock_exchange_cls.call_args.kwargs
    assert call_kwargs["base_url"] == constants.MAINNET_API_URL


# 21. fetch_account_state returns combined dict
async def test_fetch_account_state_returns_combined(mocker):
    ex, info, _ = _make_executor(mocker)

    perp_data = {"assetPositions": [], "crossMarginSummary": {}}
    spot_data = {"balances": []}

    async def fake_gather(*coros):
        return (perp_data, spot_data)

    mocker.patch("asyncio.gather", side_effect=fake_gather)

    result = await ex.fetch_account_state()
    assert result == {"perp": perp_data, "spot": spot_data}


# 22. partial fill below tolerance raises PartialFillError carrying the fill
async def test_submit_partial_fill_raises_partial_fill_error(mocker):
    ex, _, exchange = _make_executor(mocker)
    # Requested 0.5, filled 0.05 (10%) — way below 1% tolerance
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp(qty=0.05, px=24.0, fee=0.0)))
    with pytest.raises(PartialFillError) as exc_info:
        await ex.submit(_perp_req(qty=0.5))
    err = exc_info.value
    assert err.requested_qty == 0.5
    assert err.filled_qty == 0.05
    assert err.fill.qty == 0.05
    assert err.fill.price == 24.0


# 23. fill within tolerance (e.g. lot-size rounding) does NOT raise
async def test_submit_near_full_fill_within_tolerance_ok(mocker):
    ex, _, exchange = _make_executor(mocker)
    # Requested 1.0, filled 0.995 (99.5%) — within default 1% tolerance
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp(qty=0.995, px=10.0, fee=0.0)))
    fill = await ex.submit(_perp_req(qty=1.0))
    assert fill.qty == 0.995


# 24. partial fill tolerance is configurable
async def test_submit_partial_fill_with_custom_tolerance(mocker):
    info = mocker.MagicMock()
    exchange = mocker.MagicMock()
    # Wider tolerance: 10% — allows 0.91 fill on 1.0 request
    ex = LiveHLExecutor(
        info=info, exchange=exchange, account_address="0x" + "b" * 40,
        partial_fill_tolerance=0.10, clock_fn=_CLOCK,
    )
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(return_value=_filled_resp(qty=0.91, px=10.0, fee=0.0)))
    fill = await ex.submit(_perp_req(qty=1.0))
    assert fill.qty == 0.91


def _route_to_thread(routes):
    """Build an asyncio.to_thread side_effect that dispatches by `fn` identity
    AND invokes `fn` so MagicMock call tracking works for assert_called_once_with.
    `routes` maps the mock attribute → response dict.
    """
    async def fake(fn, *args, **kwargs):
        fn(*args, **kwargs)  # record call on the underlying mock
        for ref, resp in routes:
            if fn is ref:
                return resp
        raise AssertionError(f"unexpected to_thread fn: {fn}")
    return fake


# 25. close_position covers short and passes executor slippage (not SDK default 5%)
async def test_close_position_short_uses_executor_slippage(mocker):
    ex, info, exchange = _make_executor(mocker, slippage=0.01)
    user_state = {
        "assetPositions": [{"position": {"coin": "BTC", "szi": "-0.00001", "entryPx": "76300"}}]
    }
    spot_state = {"balances": []}
    close_resp = _filled_resp(qty=0.00001, px=77005.0, fee=0.0)

    mocker.patch("asyncio.to_thread", side_effect=_route_to_thread([
        (info.user_state, user_state),
        (info.spot_user_state, spot_state),
        (exchange.market_close, close_resp),
    ]))

    fill = await ex.close_position("BTC")

    assert fill is not None
    assert fill.coin == "BTC"
    assert fill.leg == Leg.PERP
    assert fill.side == Side.BUY  # covering a short
    assert fill.qty == pytest.approx(0.00001)
    assert fill.price == pytest.approx(77005.0)
    assert fill.slippage_bps == pytest.approx(100.0)  # 1% — NOT SDK default 5%
    exchange.market_close.assert_called_once_with("BTC", None, None, 0.01)


# 26. close_position on a long uses SELL side
async def test_close_position_long_uses_sell_side(mocker):
    ex, info, exchange = _make_executor(mocker, slippage=0.01)
    user_state = {
        "assetPositions": [{"position": {"coin": "ETH", "szi": "0.5", "entryPx": "3000"}}]
    }
    spot_state = {"balances": []}
    close_resp = _filled_resp(qty=0.5, px=2995.0, fee=0.0)

    mocker.patch("asyncio.to_thread", side_effect=_route_to_thread([
        (info.user_state, user_state),
        (info.spot_user_state, spot_state),
        (exchange.market_close, close_resp),
    ]))

    fill = await ex.close_position("ETH")
    assert fill is not None
    assert fill.side == Side.SELL


# 27. close_position returns None when there is no position (no market_close call)
async def test_close_position_returns_none_when_flat(mocker):
    ex, info, exchange = _make_executor(mocker)
    mocker.patch("asyncio.to_thread", side_effect=_route_to_thread([
        (info.user_state, {"assetPositions": []}),
        (info.spot_user_state, {"balances": []}),
    ]))

    fill = await ex.close_position("BTC")
    assert fill is None
    exchange.market_close.assert_not_called()


# 28. close_position raises on error status
async def test_close_position_raises_on_error(mocker):
    ex, info, exchange = _make_executor(mocker)
    user_state = {
        "assetPositions": [{"position": {"coin": "BTC", "szi": "-0.00001", "entryPx": "76300"}}]
    }
    err_resp = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"error": "no position"}]}},
    }
    mocker.patch("asyncio.to_thread", side_effect=_route_to_thread([
        (info.user_state, user_state),
        (info.spot_user_state, {"balances": []}),
        (exchange.market_close, err_resp),
    ]))

    with pytest.raises(RuntimeError, match="market_close error"):
        await ex.close_position("BTC")


# 29. round_qty floors at szDecimals (used for self-sizing — must not exceed budget)
async def test_round_qty_floors_at_sz_decimals(mocker):
    ex, info, _ = _make_executor(mocker)
    info.meta.return_value = {"universe": [
        {"name": "BTC", "szDecimals": 5},
        {"name": "ETH", "szDecimals": 4},
    ]}
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)))

    # 0.000149895 → 0.00014 (NOT 0.00015 — floor truncates the trailing 9895)
    assert await ex.round_qty("BTC", 0.000149895) == pytest.approx(0.00014, abs=1e-9)
    # Already on-step: passes through unchanged
    assert await ex.round_qty("BTC", 0.00014) == pytest.approx(0.00014, abs=1e-9)
    # ETH at 4 decimals
    assert await ex.round_qty("ETH", 0.00333333) == pytest.approx(0.0033, abs=1e-9)


# 30. round_qty_to_nearest uses HALF_UP — solves the hedge-residual problem
async def test_round_qty_to_nearest_uses_half_up(mocker):
    ex, info, _ = _make_executor(mocker)
    info.meta.return_value = {"universe": [
        {"name": "BTC", "szDecimals": 5},
        {"name": "ETH", "szDecimals": 4},
    ]}
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)))

    # 0.000149895 → 0.00015 (rounds up because 6th digit is 9)
    assert await ex.round_qty_to_nearest("BTC", 0.000149895) == pytest.approx(0.00015, abs=1e-9)
    # Exact half — Python Decimal HALF_UP rounds away from zero
    assert await ex.round_qty_to_nearest("BTC", 0.000145) == pytest.approx(0.00015, abs=1e-9)
    # Below half rounds down
    assert await ex.round_qty_to_nearest("BTC", 0.000144) == pytest.approx(0.00014, abs=1e-9)
    # ETH
    assert await ex.round_qty_to_nearest("ETH", 0.00335) == pytest.approx(0.0034, abs=1e-9)


# 31. round_qty raises ValueError on unknown coin
async def test_round_qty_raises_on_unknown_coin(mocker):
    ex, info, _ = _make_executor(mocker)
    info.meta.return_value = {"universe": [{"name": "BTC", "szDecimals": 5}]}
    mocker.patch("asyncio.to_thread", new=mocker.AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw)))

    with pytest.raises(ValueError, match="unknown coin"):
        await ex.round_qty("DOGE", 1.0)
    with pytest.raises(ValueError, match="unknown coin"):
        await ex.round_qty_to_nearest("DOGE", 1.0)


# 32. fetch_wallet_state normalizes UBTC→BTC and computes total_usd
async def test_fetch_wallet_state_normalizes_spot_coins(mocker):
    """UBTC in spot balances is mapped to BTC via the inverse spot_token_map."""
    ex, info, _ = _make_executor(mocker, spot_token_map={"BTC": "UBTC", "ETH": "UETH"})

    perp_state = {
        "marginSummary": {"accountValue": "1000.0"},
        "assetPositions": [
            {"position": {"coin": "BTC", "unrealizedPnl": "-50.0"}},
        ],
    }
    spot_state = {
        "balances": [
            {"coin": "UBTC", "total": "0.001"},   # → BTC at $95000 = $95
            {"coin": "UETH", "total": "0.5"},      # → ETH at $2000 = $1000
            {"coin": "USDC", "total": "200.0"},    # → usdc_spot
        ]
    }

    async def fake_gather(*coros):
        return (perp_state, spot_state)

    mocker.patch("asyncio.gather", side_effect=fake_gather)

    mark_prices = {"BTC": 95_000.0, "ETH": 2_000.0}
    result = await ex.fetch_wallet_state(mark_prices=mark_prices)

    # perp_account_value from marginSummary
    assert result["perp_account_value"] == pytest.approx(1_000.0)

    # perp_unrealized_pnl from assetPositions
    assert result["perp_unrealized_pnl"] == pytest.approx(-50.0)

    # spot_balances: UBTC → BTC, UETH → ETH; USDC excluded
    coins_in_balances = {b["coin"] for b in result["spot_balances"]}
    assert coins_in_balances == {"BTC", "ETH"}

    btc_bal = next(b for b in result["spot_balances"] if b["coin"] == "BTC")
    assert btc_bal["qty"] == pytest.approx(0.001)
    assert btc_bal["mark"] == pytest.approx(95_000.0)
    assert btc_bal["usd_value"] == pytest.approx(95.0)

    eth_bal = next(b for b in result["spot_balances"] if b["coin"] == "ETH")
    assert eth_bal["qty"] == pytest.approx(0.5)
    assert eth_bal["usd_value"] == pytest.approx(1_000.0)

    # usdc_spot
    assert result["usdc_spot"] == pytest.approx(200.0)

    # total_usd = perp_account_value + spot_tokens + usdc_spot
    # = 1000 + (95 + 1000) + 200 = 2295
    assert result["total_usd"] == pytest.approx(2_295.0)


# 33. fetch_wallet_state falls back to raw coin name for unknown tokens
async def test_fetch_wallet_state_unknown_coin_uses_raw_name(mocker):
    """A coin not in the inverse map is passed through as-is."""
    ex, info, _ = _make_executor(mocker, spot_token_map={"BTC": "UBTC"})

    perp_state = {"marginSummary": {"accountValue": "0.0"}, "assetPositions": []}
    spot_state = {
        "balances": [
            {"coin": "PURR", "total": "100.0"},   # not in inverse map → stays as PURR
        ]
    }

    async def fake_gather(*coros):
        return (perp_state, spot_state)

    mocker.patch("asyncio.gather", side_effect=fake_gather)

    result = await ex.fetch_wallet_state(mark_prices={"PURR": 0.5})

    assert len(result["spot_balances"]) == 1
    assert result["spot_balances"][0]["coin"] == "PURR"
    assert result["spot_balances"][0]["usd_value"] == pytest.approx(50.0)


# 34. fetch_wallet_state without account_address raises
async def test_fetch_wallet_state_without_address_raises(mocker):
    info = mocker.MagicMock()
    exchange = mocker.MagicMock()
    ex = LiveHLExecutor(info=info, exchange=exchange, account_address=None)
    with pytest.raises(RuntimeError, match="account_address required"):
        await ex.fetch_wallet_state()
