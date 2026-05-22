"""AtomicExecutor: bounded retry + spot-first paired open/close.

Spot-first rationale
--------------------
Hyperliquid spot BUYs pay the taker fee in the bought asset (e.g. a BUY of
UBTC/USDC pays fee in UBTC, not USDC). The on-chain delta is therefore
``totalSz - fee_in_asset``, smaller than the requested order size. Hedging
a spot leg using the requested-size figure leaves a residual unhedged
asset balance and breaks the symmetric-leg invariant: when the strategy
later tries to close with the requested-size, HL caps the sell to the
actual balance and the executor sees that as a partial fill.

By executing the spot leg first and reading back the actual balance delta
via ``executor.get_position`` snapshots, the perp leg is sized to the
*real* spot fill — both legs end up perfectly matched and close-time
partials disappear.

We give up the hedge-first protection (a defined perp hedge if the second
leg fails) in exchange for correct qty accounting. At $10-$50 position
sizes the 0.5s price-move risk during the leg window is sub-cent and
strictly dominated by the partial-close-fail cost. For larger sizes this
trade-off should be revisited.

Idempotency replay is NOT implemented here. The DB-level UNIQUE constraint
on ``fills.client_ref`` (added in migration f4c1d9e2a7b3) guarantees that
a duplicate fill record cannot be persisted; recovery from a duplicated
submit (e.g. after an engine crash mid-write) is the reconcile path's job.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Awaitable, Callable, Literal

from frab.events.bus import Event, EventBus
from frab.exchanges.base import Executor, FillReport, Leg, OrderRequest, PositionState, Side

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PairedOpenResult:
    status: Literal["ok", "failed"]
    perp_fill: FillReport | None      # None if perp leg failed before any fill
    spot_fill: FillReport | None      # None if spot leg failed before any fill
    perp_attempts: int                # 0 if not attempted, else 1..max_attempts
    spot_attempts: int
    errors: tuple[str, ...]           # repr() of exceptions, chronological order


@dataclass(frozen=True, slots=True)
class PairedCloseResult:
    status: Literal["ok", "failed"]
    perp_fill: FillReport | None
    spot_fill: FillReport | None
    perp_attempts: int
    spot_attempts: int
    errors: tuple[str, ...]


# ---------------------------------------------------------------------------
# Default transient-error classifier
# ---------------------------------------------------------------------------


def default_is_transient(exc: BaseException) -> bool:
    """Return True if *exc* is a transient network error worth retrying."""
    transient_types: tuple[type[BaseException], ...] = (
        asyncio.TimeoutError,
        ConnectionError,
    )
    try:
        import httpx  # noqa: PLC0415 — intentionally lazy
        transient_types = transient_types + (httpx.TransportError,)
    except ImportError:
        pass

    return isinstance(exc, transient_types)


# ---------------------------------------------------------------------------
# Internal result from _submit_counting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SubmitOutcome:
    fill: FillReport | None
    attempts: int
    errors: list[BaseException]
    final_exc: BaseException | None   # None on success


# ---------------------------------------------------------------------------
# AtomicExecutor
# ---------------------------------------------------------------------------


class AtomicExecutor:
    """Wraps an underlying Executor with bounded retry + spot-first paired ops.

    Spot-first principle (rationale in module docstring):
      - On open: spot buy first, then perp short sized to actual spot delta.
      - On close: spot sell first, then perp cover sized to actual spot delta.

    Idempotency replay is NOT implemented here. The DB-level UNIQUE constraint
    on ``fills.client_ref`` (added in migration f4c1d9e2a7b3) guarantees that a
    duplicate fill record cannot be persisted; recovery from a duplicated submit
    (e.g. after an engine crash mid-write) is the reconcile path's job.
    """

    def __init__(
        self,
        underlying: Executor,
        event_bus: EventBus,
        *,
        max_attempts: int = 3,
        sleep_between_attempts: tuple[float, ...] = (2.0, 5.0),
        is_transient: Callable[[BaseException], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        if len(sleep_between_attempts) < max_attempts - 1:
            raise ValueError(
                f"sleep_between_attempts must have at least {max_attempts - 1} element(s) "
                f"for max_attempts={max_attempts}, got {len(sleep_between_attempts)}"
            )
        for i, s in enumerate(sleep_between_attempts):
            if s < 0:
                raise ValueError(
                    f"sleep_between_attempts[{i}]={s} must be >= 0"
                )

        self._underlying = underlying
        self._bus = event_bus
        self._max_attempts = max_attempts
        self._sleeps = sleep_between_attempts
        self._is_transient = is_transient if is_transient is not None else default_is_transient
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._sleep = sleep if sleep is not None else asyncio.sleep

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_event(
        self,
        *,
        level: str,
        kind: str,
        message: str,
        payload_json: dict | None = None,
    ) -> Event:
        return Event(
            ts=self._clock(),
            level=level,
            source="atomic_executor",
            kind=kind,
            message=message,
            payload_json=payload_json,
        )

    async def _submit_counting(self, req: OrderRequest) -> _SubmitOutcome:
        """Low-level retry loop that tracks attempt count but does NOT publish events.

        Returns a _SubmitOutcome. The caller (submit_with_retry or paired ops) is
        responsible for event publishing and re-raising.
        """
        errs: list[BaseException] = []

        for attempt in range(1, self._max_attempts + 1):
            try:
                fill = await self._underlying.submit(req)
                return _SubmitOutcome(fill=fill, attempts=attempt, errors=errs, final_exc=None)
            except BaseException as exc:  # noqa: BLE001
                if not self._is_transient(exc):
                    # Non-transient: surface immediately without retry
                    errs.append(exc)
                    return _SubmitOutcome(fill=None, attempts=attempt, errors=errs, final_exc=exc)

                errs.append(exc)
                logger.warning(
                    "submit_with_retry: transient error on attempt %d/%d for %s/%s: %r",
                    attempt,
                    self._max_attempts,
                    req.coin,
                    req.leg,
                    exc,
                )

                if attempt < self._max_attempts:
                    await self._sleep(self._sleeps[attempt - 1])

        # All attempts exhausted (only transient errors here)
        return _SubmitOutcome(
            fill=None,
            attempts=self._max_attempts,
            errors=errs,
            final_exc=errs[-1],
        )

    async def _observe_spot_delta(self, coin: str, snap_before: PositionState | None) -> float:
        """Snapshot spot balance after a fill and return delta vs snap_before.

        Used to size the second (perp) leg to the actual spot fill.
        """
        snap_after = await self._underlying.get_position(coin)
        spot_after = snap_after.spot_units if snap_after is not None else 0.0
        spot_before = snap_before.spot_units if snap_before is not None else 0.0
        return spot_after - spot_before

    # ------------------------------------------------------------------
    # Core retry logic
    # ------------------------------------------------------------------

    async def submit_with_retry(self, req: OrderRequest) -> FillReport:
        """Submit *req* via underlying.submit, retrying on transient errors.

        Behavior:
          - Try up to ``max_attempts`` times.
          - Between attempts N and N+1, await sleep(sleep_between_attempts[N-1]).
          - On non-transient exception: re-raise immediately (no retry).
          - On transient exception: catch, log via logger.warning, retry if
            attempts remain.
          - After exhausting attempts on transient errors:
              * Publish Event(level="ERROR", source="atomic_executor",
                              kind="retry_exhausted", ...).
              * Re-raise the last exception (caller decides what to do).
        """
        outcome = await self._submit_counting(req)

        if outcome.fill is not None:
            return outcome.fill

        assert outcome.final_exc is not None
        n = outcome.attempts

        # Publish retry_exhausted only when we exhausted transient retries
        # (non-transient errors are surfaced immediately without event)
        exhausted = n == self._max_attempts and all(
            self._is_transient(e) for e in outcome.errors
        )
        if exhausted:
            await self._bus.publish(
                self._make_event(
                    level="ERROR",
                    kind="retry_exhausted",
                    message=f"submit failed after {n} attempts: {repr(outcome.final_exc)}",
                    payload_json={
                        "coin": req.coin,
                        "leg": req.leg.value,
                        "side": req.side.value,
                        "client_ref": req.client_ref,
                        "attempts": n,
                        "errors": [repr(e) for e in outcome.errors],
                    },
                )
            )

        raise outcome.final_exc

    # ------------------------------------------------------------------
    # Paired open / close
    # ------------------------------------------------------------------

    def _validate_open_reqs(self, perp_req: OrderRequest, spot_req: OrderRequest) -> None:
        if perp_req.leg != Leg.PERP:
            raise ValueError(f"perp_req.leg must be PERP, got {perp_req.leg!r}")
        if perp_req.side != Side.SELL:
            raise ValueError(f"perp_req.side must be SELL (short), got {perp_req.side!r}")
        if spot_req.leg != Leg.SPOT:
            raise ValueError(f"spot_req.leg must be SPOT, got {spot_req.leg!r}")
        if spot_req.side != Side.BUY:
            raise ValueError(f"spot_req.side must be BUY, got {spot_req.side!r}")
        if perp_req.coin != spot_req.coin:
            raise ValueError(
                f"coin mismatch: perp_req.coin={perp_req.coin!r} vs "
                f"spot_req.coin={spot_req.coin!r}"
            )

    def _validate_close_reqs(self, perp_req: OrderRequest, spot_req: OrderRequest) -> None:
        if perp_req.leg != Leg.PERP:
            raise ValueError(f"perp_req.leg must be PERP, got {perp_req.leg!r}")
        if perp_req.side != Side.BUY:
            raise ValueError(f"perp_req.side must be BUY (cover), got {perp_req.side!r}")
        if spot_req.leg != Leg.SPOT:
            raise ValueError(f"spot_req.leg must be SPOT, got {spot_req.leg!r}")
        if spot_req.side != Side.SELL:
            raise ValueError(f"spot_req.side must be SELL, got {spot_req.side!r}")
        if perp_req.coin != spot_req.coin:
            raise ValueError(
                f"coin mismatch: perp_req.coin={perp_req.coin!r} vs "
                f"spot_req.coin={spot_req.coin!r}"
            )

    async def open_paired(
        self,
        perp_req: OrderRequest,
        spot_req: OrderRequest,
    ) -> PairedOpenResult:
        """Open spot-first: spot buy, then perp short sized to the actual spot delta.

        Preconditions (validate; raise ValueError):
          - perp_req.leg == Leg.PERP and perp_req.side == Side.SELL
          - spot_req.leg == Leg.SPOT and spot_req.side == Side.BUY
          - perp_req.coin == spot_req.coin
        """
        self._validate_open_reqs(perp_req, spot_req)
        coin = perp_req.coin

        # --- Leg 1: spot buy ---
        snap_before = await self._underlying.get_position(coin)
        rounded_spot_qty = await self._underlying.round_qty(coin, spot_req.qty)
        spot_req = replace(spot_req, qty=rounded_spot_qty)
        spot_outcome = await self._submit_counting(spot_req)
        spot_attempts = spot_outcome.attempts
        all_errors: list[str] = [repr(e) for e in spot_outcome.errors]

        if spot_outcome.final_exc is not None:
            exc = spot_outcome.final_exc
            exhausted = spot_attempts == self._max_attempts and all(
                self._is_transient(e) for e in spot_outcome.errors
            )
            if exhausted:
                await self._bus.publish(
                    self._make_event(
                        level="ERROR",
                        kind="retry_exhausted",
                        message=f"submit failed after {spot_attempts} attempts: {repr(exc)}",
                        payload_json={
                            "coin": coin,
                            "leg": spot_req.leg.value,
                            "side": spot_req.side.value,
                            "client_ref": spot_req.client_ref,
                            "attempts": spot_attempts,
                            "errors": [repr(e) for e in spot_outcome.errors],
                        },
                    )
                )
            await self._bus.publish(
                self._make_event(
                    level="ERROR",
                    kind="paired_open_failed",
                    message="spot leg failed",
                    payload_json={"coin": coin, "error": repr(exc)},
                )
            )
            return PairedOpenResult(
                status="failed",
                perp_fill=None,
                spot_fill=None,
                perp_attempts=0,
                spot_attempts=spot_attempts,
                errors=tuple(all_errors),
            )

        # Observe actual spot delta (accounts for asset-denominated fees on HL)
        spot_delta = await self._observe_spot_delta(coin, snap_before)

        if abs(spot_delta) < 1e-12:
            logger.warning(
                "open_paired: spot fill observed zero delta for %s (snap_before=%s)",
                coin, snap_before,
            )
            await self._bus.publish(
                self._make_event(
                    level="ERROR",
                    kind="paired_open_failed",
                    message="spot fill observed zero delta",
                    payload_json={"coin": coin},
                )
            )
            return PairedOpenResult(
                status="failed",
                perp_fill=None,
                spot_fill=None,
                perp_attempts=0,
                spot_attempts=spot_attempts,
                errors=tuple(all_errors),
            )

        # Round spot delta to perp asset's szDecimals — HL refuses to sign
        # orders with finer qty precision. HALF_UP (not FLOOR) minimizes the
        # unhedged residual: for a 0.000149895 BTC spot fill the floor gives
        # 0.00014 (leaving ~$0.76 long dust at $77k) while half-up gives
        # 0.00015 (~$0.008 short dust, ~100x smaller).
        rounded_qty = await self._underlying.round_qty_to_nearest(perp_req.coin, abs(spot_delta))
        if rounded_qty < 1e-12:
            await self._bus.publish(self._make_event(
                level="ERROR", kind="paired_open_failed",
                message=f"spot delta {spot_delta} rounds to zero at perp precision",
                payload_json={"coin": coin, "spot_delta": spot_delta},
            ))
            return PairedOpenResult(
                status="failed", perp_fill=None,
                spot_fill=replace(spot_outcome.fill, qty=spot_delta),
                perp_attempts=0, spot_attempts=spot_attempts,
                errors=tuple(all_errors),
            )

        # Both legs record the same rounded qty; spot residual remains as dust
        adjusted_spot_fill = replace(spot_outcome.fill, qty=rounded_qty)

        # --- Leg 2: perp short sized to actual spot delta ---
        effective_perp_req = replace(perp_req, qty=rounded_qty)
        perp_outcome = await self._submit_counting(effective_perp_req)
        perp_attempts = perp_outcome.attempts
        all_errors.extend(repr(e) for e in perp_outcome.errors)

        if perp_outcome.final_exc is not None:
            exc = perp_outcome.final_exc
            perp_exhausted = perp_attempts == self._max_attempts and all(
                self._is_transient(e) for e in perp_outcome.errors
            )
            if perp_exhausted:
                await self._bus.publish(
                    self._make_event(
                        level="ERROR",
                        kind="retry_exhausted",
                        message=f"submit failed after {perp_attempts} attempts: {repr(exc)}",
                        payload_json={
                            "coin": coin,
                            "leg": perp_req.leg.value,
                            "side": perp_req.side.value,
                            "client_ref": perp_req.client_ref,
                            "attempts": perp_attempts,
                            "errors": [repr(e) for e in perp_outcome.errors],
                        },
                    )
                )
            await self._bus.publish(
                self._make_event(
                    level="ERROR",
                    kind="paired_open_failed",
                    message=f"perp leg failed after spot filled (spot_delta={spot_delta})",
                    payload_json={
                        "coin": coin,
                        "spot_client_ref": spot_req.client_ref,
                        "spot_delta": spot_delta,
                        "error": repr(exc),
                    },
                )
            )
            return PairedOpenResult(
                status="failed",
                perp_fill=None,
                spot_fill=adjusted_spot_fill,
                perp_attempts=perp_attempts,
                spot_attempts=spot_attempts,
                errors=tuple(all_errors),
            )

        return PairedOpenResult(
            status="ok",
            perp_fill=perp_outcome.fill,
            spot_fill=adjusted_spot_fill,
            perp_attempts=perp_attempts,
            spot_attempts=spot_attempts,
            errors=(),
        )

    async def close_paired(
        self,
        perp_req: OrderRequest,
        spot_req: OrderRequest,
    ) -> PairedCloseResult:
        """Close spot-first: spot sell, then perp cover (BUY) sized to actual spot delta.

        Preconditions:
          - perp_req.leg == Leg.PERP and perp_req.side == Side.BUY
          - spot_req.leg == Leg.SPOT and spot_req.side == Side.SELL
          - perp_req.coin == spot_req.coin
        """
        self._validate_close_reqs(perp_req, spot_req)
        coin = perp_req.coin

        # --- Leg 1: spot sell ---
        snap_before = await self._underlying.get_position(coin)
        rounded_spot_qty = await self._underlying.round_qty(coin, spot_req.qty)
        spot_req = replace(spot_req, qty=rounded_spot_qty)
        spot_outcome = await self._submit_counting(spot_req)
        spot_attempts = spot_outcome.attempts
        all_errors: list[str] = [repr(e) for e in spot_outcome.errors]

        if spot_outcome.final_exc is not None:
            exc = spot_outcome.final_exc
            exhausted = spot_attempts == self._max_attempts and all(
                self._is_transient(e) for e in spot_outcome.errors
            )
            if exhausted:
                await self._bus.publish(
                    self._make_event(
                        level="ERROR",
                        kind="retry_exhausted",
                        message=f"submit failed after {spot_attempts} attempts: {repr(exc)}",
                        payload_json={
                            "coin": coin,
                            "leg": spot_req.leg.value,
                            "side": spot_req.side.value,
                            "client_ref": spot_req.client_ref,
                            "attempts": spot_attempts,
                            "errors": [repr(e) for e in spot_outcome.errors],
                        },
                    )
                )
            await self._bus.publish(
                self._make_event(
                    level="ERROR",
                    kind="paired_close_failed",
                    message="spot leg failed",
                    payload_json={"coin": coin, "error": repr(exc)},
                )
            )
            return PairedCloseResult(
                status="failed",
                perp_fill=None,
                spot_fill=None,
                perp_attempts=0,
                spot_attempts=spot_attempts,
                errors=tuple(all_errors),
            )

        # Observe actual spot delta (negative: spot decreased on sell)
        spot_delta = await self._observe_spot_delta(coin, snap_before)
        abs_delta = abs(spot_delta)

        if abs_delta < 1e-12:
            logger.warning(
                "close_paired: spot sell observed zero delta for %s (snap_before=%s)",
                coin, snap_before,
            )
            await self._bus.publish(
                self._make_event(
                    level="ERROR",
                    kind="paired_close_failed",
                    message="spot fill observed zero delta",
                    payload_json={"coin": coin},
                )
            )
            return PairedCloseResult(
                status="failed",
                perp_fill=None,
                spot_fill=None,
                perp_attempts=0,
                spot_attempts=spot_attempts,
                errors=tuple(all_errors),
            )

        # Round to perp asset szDecimals (HL refuses finer precision)
        rounded_qty = await self._underlying.round_qty(perp_req.coin, abs_delta)
        if rounded_qty < 1e-12:
            await self._bus.publish(self._make_event(
                level="ERROR", kind="paired_close_failed",
                message=f"spot delta {abs_delta} rounds to zero at perp precision",
                payload_json={"coin": coin, "spot_delta": spot_delta},
            ))
            return PairedCloseResult(
                status="failed", perp_fill=None,
                spot_fill=replace(spot_outcome.fill, qty=abs_delta),
                perp_attempts=0, spot_attempts=spot_attempts,
                errors=tuple(all_errors),
            )

        adjusted_spot_fill = replace(spot_outcome.fill, qty=rounded_qty)

        # --- Leg 2: perp cover sized to actual spot delta ---
        effective_perp_req = replace(perp_req, qty=rounded_qty)
        perp_outcome = await self._submit_counting(effective_perp_req)
        perp_attempts = perp_outcome.attempts
        all_errors.extend(repr(e) for e in perp_outcome.errors)

        if perp_outcome.final_exc is not None:
            exc = perp_outcome.final_exc
            perp_exhausted = perp_attempts == self._max_attempts and all(
                self._is_transient(e) for e in perp_outcome.errors
            )
            if perp_exhausted:
                await self._bus.publish(
                    self._make_event(
                        level="ERROR",
                        kind="retry_exhausted",
                        message=f"submit failed after {perp_attempts} attempts: {repr(exc)}",
                        payload_json={
                            "coin": coin,
                            "leg": perp_req.leg.value,
                            "side": perp_req.side.value,
                            "client_ref": perp_req.client_ref,
                            "attempts": perp_attempts,
                            "errors": [repr(e) for e in perp_outcome.errors],
                        },
                    )
                )
            await self._bus.publish(
                self._make_event(
                    level="ERROR",
                    kind="paired_close_failed",
                    message=f"perp leg failed after spot sold (spot_delta={spot_delta})",
                    payload_json={
                        "coin": coin,
                        "spot_client_ref": spot_req.client_ref,
                        "spot_delta": spot_delta,
                        "error": repr(exc),
                    },
                )
            )
            return PairedCloseResult(
                status="failed",
                perp_fill=None,
                spot_fill=adjusted_spot_fill,
                perp_attempts=perp_attempts,
                spot_attempts=spot_attempts,
                errors=tuple(all_errors),
            )

        return PairedCloseResult(
            status="ok",
            perp_fill=perp_outcome.fill,
            spot_fill=adjusted_spot_fill,
            perp_attempts=perp_attempts,
            spot_attempts=spot_attempts,
            errors=(),
        )

    # ------------------------------------------------------------------
    # Transfer helpers — forwarded to the underlying executor
    # ------------------------------------------------------------------

    async def transfer_spot_to_perp(self, usdc_amount: float) -> dict:
        """Transfer USDC from spot wallet to perp margin wallet."""
        return await self._underlying.transfer_spot_to_perp(usdc_amount)

    async def transfer_perp_to_spot(self, usdc_amount: float) -> dict:
        """Transfer USDC from perp margin wallet to spot wallet."""
        return await self._underlying.transfer_perp_to_spot(usdc_amount)
