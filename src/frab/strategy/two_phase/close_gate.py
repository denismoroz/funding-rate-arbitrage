"""CloseGate: the pre-close check every exit of a FarbPosition goes through.

An exit closes the perp short first and the spot long second. If the spot cannot be sold
afterwards (what this position may sell, at today's price, is below HL's minimum order),
the exit leaves an unhedged spot long stuck in CLOSING_LONG. So before ANY leg is closed
the gate asks the exchange whether the whole position can be closed within the venue's
limits. If it cannot, NOTHING is closed: the position stays fully hedged in the state it
is in, and the gate records that it wants to exit but cannot (owner's decision,
2026-10-25: check at the frab level, and if it cannot be closed, write it in red).

State kept in `state_data` (read by /api/farb-positions and the dashboard):
    close_blocked               True while blocked
    close_blocked_since_ms      when the position first became blocked
    close_blocked_wanted        human-readable exit the position wants
    close_blocked_decision      the raw decision code (TwoPhaseDecision value / "forced")
    close_blocked_reason        why it cannot be closed, with the numbers
    close_blocked_check         the numbers behind the reason
    close_blocked_checked_ms    last time the check ran
    close_blocked_alerted_ms    last time the ERROR event was published
    pending_forced_exit         {decision, requested_at_ms} for an exit the owner or the
                                margin watchdog asked for: the hourly evaluator repeats it
                                until the check passes (an exit the strategy itself wants
                                is re-derived from the signals every hour instead)

Events: `close.blocked` (ERROR) once when the position becomes blocked and at most once a
day while it stays blocked; `close.unblocked` (INFO) when it can close again or no longer
needs to.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.domain import FarbPosition
from frab.events.bus import EventBus
from frab.exchanges.protocol import CloseCheck, Exchange
from frab.repo.farb_repo import FarbRepo
from frab.strategy.two_phase.states._base import CloseBlocked  # noqa: F401 - re-exported
from frab.strategy.two_phase.states._helpers import load_position, publish_event

logger = logging.getLogger(__name__)

REALERT_INTERVAL_MS = 24 * 3600 * 1000

BLOCKED_KEYS = (
    "close_blocked",
    "close_blocked_since_ms",
    "close_blocked_wanted",
    "close_blocked_decision",
    "close_blocked_reason",
    "close_blocked_check",
    "close_blocked_checked_ms",
    "close_blocked_alerted_ms",
)
PENDING_FORCED_KEY = "pending_forced_exit"

# What each exit decision means to the owner, for "can't close - needs to: ...".
WANTED_LABELS: dict[str, str] = {
    "CLOSE_PRE_BE_NEG": "exit: funding negative for too long before break-even",
    "CLOSE_PRE_BE_CAP": "exit: break-even too far away at the current rate",
    "CLOSE_PRE_BE_NEGSTOP": "exit: neg-stop (funding sharply negative)",
    "CLOSE_POST_BE": "exit: funding fell below the threshold and profit is being given back",
    "forced": "manual close",
    "watchdog_forced": "margin watchdog forced close",
}


def wanted_label(decision: str) -> str:
    return WANTED_LABELS.get(decision, f"exit ({decision})")


@dataclass(frozen=True)
class GateResult:
    ok: bool
    state_data: dict          # ok: state_data to transition with; blocked: what was persisted
    check: CloseCheck
    reason: str | None = None


def _without_blocked(state_data: dict) -> dict:
    return {
        k: v for k, v in state_data.items()
        if k not in BLOCKED_KEYS and k != PENDING_FORCED_KEY
    }


class CloseGate:
    def __init__(
        self,
        *,
        exchange: Exchange,
        farb_repo: FarbRepo,
        session_factory: async_sessionmaker[AsyncSession],
        event_bus: EventBus | None = None,
        realert_interval_ms: int = REALERT_INTERVAL_MS,
    ) -> None:
        self._exchange = exchange
        self._farb_repo = farb_repo
        self._sf = session_factory
        self._bus = event_bus
        self._realert_interval_ms = realert_interval_ms

    async def check(self, fp: FarbPosition) -> CloseCheck:
        """Run the exchange's check on the position's legs. A check that itself fails
        (wallet or price unreadable) counts as 'cannot close': what cannot be verified
        is not closed."""
        try:
            spot = (
                await load_position(self._sf, fp.spot_position_id)
                if fp.spot_position_id is not None else None
            )
            perp = (
                await load_position(self._sf, fp.perp_position_id)
                if fp.perp_position_id is not None else None
            )
            return await self._exchange.check_close(spot, perp)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("close check failed farb_position_id=%s: %s", fp.id, exc)
            return CloseCheck(ok=False, reason=f"the close check itself failed: {exc}")

    async def admit(
        self,
        fp: FarbPosition,
        *,
        decision: str,
        state_data: dict,
        now_ms: int,
        forced: bool = False,
    ) -> GateResult:
        """May `fp` start closing for `decision`?

        ok: the caller transitions with `result.state_data` (the blocked markers removed).
        Not ok: nothing may be closed; the blocked markers were persisted (merged onto
        `state_data`, which the caller passes in so its own counters are kept) and the
        events published as described in the module docstring. `forced` records the exit
        as pending so the hourly evaluator repeats it until the check passes."""
        check = await self.check(fp)
        was_blocked = bool(state_data.get("close_blocked"))

        if check.ok:
            cleaned = _without_blocked(state_data)
            if was_blocked:
                await self._publish_unblocked(
                    fp, now_ms=now_ms, state_data=state_data,
                    message=f"{fp.coin} FP#{fp.id} can be closed again; the exit proceeds",
                )
            return GateResult(ok=True, state_data=cleaned, check=check)

        reason = check.reason or "cannot be closed within the venue's limits"
        since_ms = int(state_data.get("close_blocked_since_ms") or now_ms) if was_blocked else now_ms
        last_alert = state_data.get("close_blocked_alerted_ms")
        alert_due = (
            not was_blocked
            or last_alert is None
            or now_ms - int(last_alert) >= self._realert_interval_ms
        )
        new_sd = {
            **state_data,
            "close_blocked": True,
            "close_blocked_since_ms": since_ms,
            "close_blocked_wanted": wanted_label(decision),
            "close_blocked_decision": decision,
            "close_blocked_reason": reason,
            "close_blocked_check": {
                k: v for k, v in asdict(check).items() if k not in ("ok", "reason")
            },
            "close_blocked_checked_ms": now_ms,
            "close_blocked_alerted_ms": now_ms if alert_due else last_alert,
        }
        if forced:
            pending = state_data.get(PENDING_FORCED_KEY) or {}
            new_sd[PENDING_FORCED_KEY] = {
                "decision": decision,
                "requested_at_ms": pending.get("requested_at_ms", now_ms),
            }
        await self._farb_repo.update_state_data(fp.id, new_sd)

        if alert_due:
            logger.error(
                "close blocked farb_position_id=%s coin=%s wanted=%s: %s",
                fp.id, fp.coin, decision, reason,
            )
            await publish_event(
                self._bus,
                level="ERROR",
                kind="close.blocked",
                message=(
                    f"{fp.coin} FP#{fp.id} can't close - needs to: {wanted_label(decision)}; "
                    f"{reason}. Nothing was closed; the position stays hedged."
                ),
                payload={
                    "farb_position_id": fp.id,
                    "coin": fp.coin,
                    "decision": decision,
                    "wanted": wanted_label(decision),
                    "reason": reason,
                    "blocked_since_ms": since_ms,
                    "check": new_sd["close_blocked_check"],
                },
            )
        return GateResult(ok=False, state_data=new_sd, check=check, reason=reason)

    async def release(self, fp: FarbPosition, *, state_data: dict, now_ms: int) -> dict:
        """The position no longer needs to exit (the signal recovered): drop the blocked
        markers and say so. Returns the state_data to write; unchanged when not blocked."""
        if not state_data.get("close_blocked"):
            return state_data
        await self._publish_unblocked(
            fp, now_ms=now_ms, state_data=state_data,
            message=f"{fp.coin} FP#{fp.id} no longer needs to exit; the close block is lifted",
        )
        return _without_blocked(state_data)

    async def _publish_unblocked(
        self, fp: FarbPosition, *, now_ms: int, state_data: dict, message: str,
    ) -> None:
        since_ms = state_data.get("close_blocked_since_ms")
        logger.info("close unblocked farb_position_id=%s coin=%s", fp.id, fp.coin)
        await publish_event(
            self._bus,
            level="INFO",
            kind="close.unblocked",
            message=message,
            payload={
                "farb_position_id": fp.id,
                "coin": fp.coin,
                "blocked_since_ms": since_ms,
                "blocked_for_ms": (now_ms - int(since_ms)) if since_ms else None,
            },
        )
