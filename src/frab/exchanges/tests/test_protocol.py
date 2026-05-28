"""Tests for Exchange Protocol structural conformance."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from frab.exchanges.protocol import Exchange, Quote, FundingTick, MarketSpec, OpenRequest, WalletKind
from frab.exchanges.hyperliquid.exchange import HLExchange
from frab.exchanges.paper import PaperExchange
from frab.domain import Instrument, Side


# ---------------------------------------------------------------------------
# 1. HLExchange satisfies Exchange Protocol (runtime_checkable)
# ---------------------------------------------------------------------------

def test_hl_exchange_satisfies_exchange_protocol():
    info = MagicMock()
    ex = HLExchange(info=info)
    assert isinstance(ex, Exchange)


# ---------------------------------------------------------------------------
# 2. PaperExchange satisfies Exchange Protocol (runtime_checkable)
# ---------------------------------------------------------------------------

def test_paper_exchange_satisfies_exchange_protocol():
    upstream = MagicMock(spec=Exchange)
    session_factory = MagicMock()
    paper = PaperExchange(
        upstream=upstream,
        session_factory=session_factory,
        fee_bps_spot=7.0,
        fee_bps_perp=3.5,
    )
    assert isinstance(paper, Exchange)


# ---------------------------------------------------------------------------
# 3. Protocol DTOs are importable and usable
# ---------------------------------------------------------------------------

def test_protocol_dtos_importable():
    q = Quote(coin="BTC", mark=100.0, spot=None, bid=99.0, ask=101.0, ts_ms=1000)
    assert q.coin == "BTC"

    ft = FundingTick(coin="BTC", ts_ms=1000, rate=0.0001, premium=0.0, annualized_pct=0.876)
    assert ft.ts_ms == 1000

    ms = MarketSpec(coin="BTC", has_spot=False, has_perp=True, min_size=0.001, tick_size=1.0, sz_decimals=5)
    assert ms.sz_decimals == 5

    req = OpenRequest(coin="BTC", instrument=Instrument.PERP, side=Side.SHORT, qty=0.5)
    assert req.farb_position_id is None


# ---------------------------------------------------------------------------
# 4. WalletKind enum values
# ---------------------------------------------------------------------------

def test_wallet_kind_values():
    assert WalletKind.SPOT == "spot"
    assert WalletKind.PERP == "perp"


# ---------------------------------------------------------------------------
# 5. HLExchange has name attribute
# ---------------------------------------------------------------------------

def test_hl_exchange_has_name():
    info = MagicMock()
    ex = HLExchange(info=info)
    assert ex.name == "hyperliquid"


# ---------------------------------------------------------------------------
# 6. PaperExchange has name attribute
# ---------------------------------------------------------------------------

def test_paper_exchange_has_name():
    upstream = MagicMock(spec=Exchange)
    session_factory = MagicMock()
    paper = PaperExchange(
        upstream=upstream,
        session_factory=session_factory,
        fee_bps_spot=7.0,
        fee_bps_perp=3.5,
    )
    assert paper.name == "paper"
