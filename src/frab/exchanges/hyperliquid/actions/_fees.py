"""Stateless fee-lookup helper used by open/close actions to resolve real HL fee."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, UTC
from typing import Callable

from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.wire import HLUserFill

logger = logging.getLogger(__name__)


def _match_by_oid(fills: list[HLUserFill], oid: int) -> HLUserFill | None:
    for f in fills:
        if f.oid == oid:
            return f
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
    """Look up the fee for a specific oid from HL userFillsByTime.

    Returns fee converted to USDC (wrapped-token fees multiplied by fill price).
    Returns None if the fill hasn't appeared after `attempts` polls — caller should
    fall back to a taker-rate estimate.

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
        match = _match_by_oid(fills, oid)
        if match is not None:
            fee_raw = match.fee_raw
            fee_token = match.fee_token
            if fee_token in ("USDC", "USD"):
                return fee_raw
            px = match.px
            if fee_token in _inverse and px > 0:
                return fee_raw * px
            logger.warning(
                "unknown feeToken %r oid=%s — skipping conversion", fee_token, oid
            )
            return fee_raw
        if i + 1 < attempts:
            await asyncio.sleep(sleep_s)
    logger.warning(
        "fee for oid=%s not found in userFillsByTime after %d attempts", oid, attempts
    )
    return None
