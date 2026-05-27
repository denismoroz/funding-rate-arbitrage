from __future__ import annotations

from datetime import datetime, timezone

import pytest

from frab.domain.exchange import Exchange
from frab.domain.portfolio import Equity, Portfolio
from frab.domain.position import Position
from frab.domain.wallet import WalletInfo


_EX = Exchange.HYPERLIQUID
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _wallet(available: float = 5000.0, reserved: float = 0.0) -> WalletInfo:
    return WalletInfo(
        exchange=_EX,
        available_usdc=available,
        reserved_usdc=reserved,
        total_value_usd=available + reserved,
    )


def _position(
    coin: str = "BTC",
    spot_qty: float = 0.02,
    perp_qty: float = 0.02,
    notional: float = 1000.0,
    margin: float = 200.0,
    entry_spot: float = 50_000.0,
    entry_perp: float = 50_100.0,
) -> Position:
    return Position(
        exchange=_EX,
        coin=coin,
        spot_qty=spot_qty,
        perp_qty=perp_qty,
        notional_usd=notional,
        margin_reserve_usd=margin,
        entry_spot_price=entry_spot,
        entry_perp_price=entry_perp,
        opened_at=_NOW,
    )


def _empty_portfolio(
    available: float = 5000.0,
    fees_cum: float = 0.0,
    funding_cum: float = 0.0,
    realized_pnl_cum: float = 0.0,
) -> Portfolio:
    return Portfolio(
        ts=_NOW,
        positions=(),
        wallet_per_exchange={_EX: _wallet(available)},
        fees_cum=fees_cum,
        funding_cum=funding_cum,
        realized_pnl_cum=realized_pnl_cum,
    )


# ---- equity with no positions ----

def test_equity_no_positions_cash_only():
    port = _empty_portfolio(available=3000.0)
    eq = port.equity({})
    assert eq.cash == 3000.0
    assert eq.spot_value == 0.0
    assert eq.perp_unrealized == 0.0
    assert eq.total_equity == 3000.0


def test_equity_no_positions_with_cumulatives():
    port = _empty_portfolio(
        available=2000.0,
        fees_cum=50.0,
        funding_cum=30.0,
        realized_pnl_cum=100.0,
    )
    eq = port.equity({})
    # 2000 + 100 + 30 - 50 = 2080
    assert eq.total_equity == pytest.approx(2080.0)


# ---- equity with one position ----

def test_equity_one_position_hand_computed():
    pos = _position(
        coin="BTC",
        spot_qty=0.02,
        perp_qty=0.02,
        notional=1000.0,
        margin=200.0,
        entry_spot=50_000.0,
        entry_perp=50_100.0,
    )
    port = Portfolio(
        ts=_NOW,
        positions=(pos,),
        wallet_per_exchange={_EX: _wallet(available=4000.0)},
        fees_cum=10.0,
        funding_cum=5.0,
        realized_pnl_cum=0.0,
    )
    mark = 51_000.0
    marks = {(_EX, "BTC"): mark}
    eq = port.equity(marks)

    cash = 4000.0
    spot_value = 0.02 * mark          # 1020.0
    perp_unrealized = (50_100.0 - mark) * 0.02  # (50100-51000)*0.02 = -18.0
    margin_reserved = 200.0
    fees_cum = 10.0
    funding_cum = 5.0
    expected_total = cash + spot_value + perp_unrealized + margin_reserved + 0.0 + funding_cum - fees_cum
    # 4000 + 1020 - 18 + 200 + 0 + 5 - 10 = 5197

    assert eq.cash == pytest.approx(cash)
    assert eq.spot_value == pytest.approx(spot_value)
    assert eq.perp_unrealized == pytest.approx(perp_unrealized)
    assert eq.total_equity == pytest.approx(expected_total)
    assert eq.fees_cum == 10.0
    assert eq.funding_cum == 5.0


def test_equity_ts_matches_portfolio_ts():
    port = _empty_portfolio()
    eq = port.equity({})
    assert eq.ts == _NOW


# ---- open_coins ----

def test_open_coins_empty():
    port = _empty_portfolio()
    assert port.open_coins(_EX) == []


def test_open_coins_filters_by_exchange():
    pos_btc = _position(coin="BTC")
    pos_eth = _position(coin="ETH")
    port = Portfolio(
        ts=_NOW,
        positions=(pos_btc, pos_eth),
        wallet_per_exchange={_EX: _wallet()},
        fees_cum=0.0,
        funding_cum=0.0,
        realized_pnl_cum=0.0,
    )
    coins = port.open_coins(_EX)
    assert coins == ["BTC", "ETH"]


def test_open_coins_order_preserved():
    positions = tuple(_position(coin=c) for c in ["SOL", "BTC", "ETH"])
    port = Portfolio(
        ts=_NOW,
        positions=positions,
        wallet_per_exchange={_EX: _wallet()},
        fees_cum=0.0,
        funding_cum=0.0,
        realized_pnl_cum=0.0,
    )
    assert port.open_coins(_EX) == ["SOL", "BTC", "ETH"]


# ---- total_committed ----

def test_total_committed_empty():
    port = _empty_portfolio()
    assert port.total_committed(_EX) == 0.0


def test_total_committed_sums_notional_and_margin():
    pos1 = _position(coin="BTC", notional=1000.0, margin=200.0)
    pos2 = _position(coin="ETH", notional=500.0, margin=100.0)
    port = Portfolio(
        ts=_NOW,
        positions=(pos1, pos2),
        wallet_per_exchange={_EX: _wallet()},
        fees_cum=0.0,
        funding_cum=0.0,
        realized_pnl_cum=0.0,
    )
    assert port.total_committed(_EX) == pytest.approx(1800.0)


# ---- position() lookup ----

def test_position_found():
    pos = _position(coin="BTC")
    port = Portfolio(
        ts=_NOW,
        positions=(pos,),
        wallet_per_exchange={_EX: _wallet()},
        fees_cum=0.0,
        funding_cum=0.0,
        realized_pnl_cum=0.0,
    )
    assert port.position(_EX, "BTC") is pos


def test_position_not_found():
    port = _empty_portfolio()
    assert port.position(_EX, "BTC") is None


def test_position_returns_none_wrong_coin():
    pos = _position(coin="ETH")
    port = Portfolio(
        ts=_NOW,
        positions=(pos,),
        wallet_per_exchange={_EX: _wallet()},
        fees_cum=0.0,
        funding_cum=0.0,
        realized_pnl_cum=0.0,
    )
    assert port.position(_EX, "BTC") is None
