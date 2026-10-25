"""Stateless fee-lookup helper used by open/close actions to resolve real HL fee."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Callable

from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.wire import HLUserFill

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderFee:
    """What one order paid in fees, summed over all of its fills.

    ``usdc`` is the whole fee in USDC. ``by_token`` holds the raw amount per
    non-USDC fee token: a spot BUY pays its fee in the bought coin, so the wallet
    receives the filled size minus that amount.
    """
    usdc: float
    by_token: dict[str, float] = field(default_factory=dict)


def _fills_for_oid(fills: list[HLUserFill], oid: int) -> list[HLUserFill]:
    # One order can fill in several pieces; HL lists each piece as its own row.
    return [f for f in fills if f.oid == oid]


async def fetch_order_fee(
    *,
    client: HLClient,
    address: str,
    oid: int,
    since_ms: int,
    clock_fn: Callable[[], datetime] | None = None,
    attempts: int = 3,
    sleep_s: float = 0.5,
    spot_token_inverse: dict[str, str] | None = None,
) -> OrderFee | None:
    """Look up the fee of a specific oid from HL userFillsByTime.

    The fee is summed over every fill of the order and converted to USDC
    (wrapped-token fees multiplied by the fill price); the raw amount per
    non-USDC fee token is kept alongside. Returns None if the fill hasn't
    appeared after `attempts` polls — caller should fall back to a taker-rate
    estimate.

    ``spot_token_inverse`` — registry-derived {wrapped_token: canonical_coin} map.
    When not supplied, an empty dict is used (fee token falls through to raw return).
    """
    _inverse = spot_token_inverse if spot_token_inverse is not None else {}
    clock_fn = clock_fn or (lambda: datetime.now(UTC))
    end_ms = int(clock_fn().timestamp() * 1000) + 60_000
    for i in range(attempts):
        try:
            fills = await client.user_fills_by_time(address, since_ms, end_ms)
        except Exception as exc:
            logger.warning("user_fills_by_time failed (attempt %d): %s", i + 1, exc)
            fills = []
        matches = _fills_for_oid(fills, oid)
        if matches:
            usdc = 0.0
            by_token: dict[str, float] = {}
            for match in matches:
                fee_raw = match.fee_raw
                fee_token = match.fee_token
                if fee_token in ("USDC", "USD"):
                    usdc += fee_raw
                    continue
                by_token[fee_token] = by_token.get(fee_token, 0.0) + fee_raw
                if fee_token in _inverse and match.px > 0:
                    usdc += fee_raw * match.px
                else:
                    logger.warning(
                        "unknown feeToken %r oid=%s — skipping conversion", fee_token, oid
                    )
                    usdc += fee_raw
            return OrderFee(usdc=usdc, by_token=by_token)
        if i + 1 < attempts:
            await asyncio.sleep(sleep_s)
    logger.warning(
        "fee for oid=%s not found in userFillsByTime after %d attempts", oid, attempts
    )
    return None


async def fetch_real_fee_usdc(
    *,
    client: HLClient,
    address: str,
    oid: int,
    since_ms: int,
    clock_fn: Callable[[], datetime] | None = None,
    attempts: int = 3,
    sleep_s: float = 0.5,
    spot_token_inverse: dict[str, str] | None = None,
) -> float | None:
    """The fee of an oid in USDC, or None if the fill never appeared (see fetch_order_fee)."""
    fee = await fetch_order_fee(
        client=client, address=address, oid=oid, since_ms=since_ms, clock_fn=clock_fn,
        attempts=attempts, sleep_s=sleep_s, spot_token_inverse=spot_token_inverse,
    )
    return None if fee is None else fee.usdc
