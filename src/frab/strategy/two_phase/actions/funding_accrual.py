"""FundingAccrual — refreshes funding accruals for active (PRE/POST_BREAKEVEN) FarbPositions."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.events.bus import EventBus
from frab.exchanges.protocol import Exchange
from frab.repo.farb_repo import FarbRepo
import frab.strategy.two_phase as _pkg  # logger looked up at call time so patch.object works
from frab.strategy.two_phase.evaluators.signal import SignalComputer
from frab.strategy.two_phase.states._helpers import load_funding_peak, load_position, publish_event

# Full sweep from pos.opened_at is done on first call and once every 24 h to
# repair any gaps or corrections; all other calls are incremental.
_FULL_SWEEP_INTERVAL_MS = 24 * 60 * 60 * 1000

# Daily reconciliation of the stored sum against HL's own running total
# (cumFunding.sinceOpen). The two calls are seconds apart, so a settlement
# between them puts them at most one hourly payment apart: about $0.0025 on a
# $25 slot at 0.01%/h. $0.01 allows a few such payments and is far below the
# HL daily-summary doubling this check exists to catch ($0.51 on ETH #19).
_RECONCILE_TOLERANCE_USDC = 0.01


class FundingAccrual:
    """Refreshes funding accruals from the exchange for each active (PRE/POST_BREAKEVEN) FarbPosition."""

    def __init__(
        self,
        *,
        strategy_id: int,
        exchange: Exchange,
        farb_repo: FarbRepo,
        session_factory: async_sessionmaker[AsyncSession],
        signal_computer: SignalComputer,
        event_bus: EventBus | None = None,
    ) -> None:
        self._strategy_id = strategy_id
        self._bus = event_bus
        self._exchange = exchange
        self._farb_repo = farb_repo
        self._sf = session_factory
        self._signal_computer = signal_computer
        self._last_full_sweep_ms: int | None = None

    async def accrue(self, *, now_ms: int) -> None:
        """For each OPEN FP, refresh funding accruals from HL's authoritative
        userFunding endpoint via Exchange.get_accrued_funding. That helper is
        already idempotent (dedupes by (position_id, ts_ms) before insert) and
        returns the cumulative DB sum. We just mirror that sum into state_data
        and refresh the cached current smoothed signal.

        First call after process start is always a full sweep (heals gaps on
        restart); subsequent calls within 24 h are incremental; full sweep
        repeats every 24 h.
        """
        full = (
            self._last_full_sweep_ms is None
            or (now_ms - self._last_full_sweep_ms) >= _FULL_SWEEP_INTERVAL_MS
        )

        open_fps = await self._farb_repo.list_active(self._strategy_id)
        stored: dict[int, tuple[str, float]] = {}
        for fp in open_fps:
            if fp.perp_position_id is None:
                continue
            perp_pos = await load_position(self._sf, fp.perp_position_id)
            try:
                gross = await self._exchange.get_accrued_funding(perp_pos, full=full)
            except Exception as exc:  # noqa: BLE001
                _pkg.logger.warning(
                    "accrue_funding: get_accrued_funding failed fp=%s coin=%s: %s",
                    fp.id, fp.coin, exc,
                )
                continue

            sd = dict(fp.state_data)
            sd["gross_funding_so_far"] = float(gross)
            stored[fp.id] = (fp.coin, float(gross))
            # POST_BREAKEVEN reads its giveback as peak - gross; the peak is
            # re-derived from the same rows so both move together.
            sd["peak_gross_funding"] = await load_funding_peak(self._sf, perp_pos.id)
            smoothed = await self._signal_computer.compute(fp.coin)
            if smoothed is not None:
                sd["current_signal_apr"] = smoothed
            await self._farb_repo.update_state_data(fp.id, sd)

            _pkg.logger.info(
                "funding accrued fp=%s coin=%s gross_from_HL=%.6f full=%s",
                fp.id, fp.coin, gross, full,
            )

        if full:
            self._last_full_sweep_ms = now_ms
            await self._reconcile(stored)

    async def _reconcile(self, stored: dict[int, tuple[str, float]]) -> None:
        """Compare each position's stored funding with HL's cumFunding.sinceOpen
        and raise a WARNING event when they part. The stored sum is built from
        userFunding records; nothing else checked it, which is how doubled
        daily summaries stood for months (two_phase gross was ~2x HL's)."""
        read = getattr(self._exchange, "get_perp_funding_received_by_coin", None)
        if read is None or not stored:
            return
        received = await read()
        if not received:
            return  # HL unreachable: logged by the adapter, next sweep retries
        for fp_id, (coin, gross) in stored.items():
            if coin not in received:
                continue
            diff = gross - received[coin]
            if abs(diff) <= _RECONCILE_TOLERANCE_USDC:
                _pkg.logger.info(
                    "funding reconciled fp=%s coin=%s db=%.6f hl=%.6f",
                    fp_id, coin, gross, received[coin],
                )
                continue
            _pkg.logger.warning(
                "funding mismatch fp=%s coin=%s db=%.6f hl_cum_funding=%.6f diff=%.6f",
                fp_id, coin, gross, received[coin], diff,
            )
            await publish_event(
                self._bus,
                level="WARNING",
                kind="funding.mismatch",
                message=(f"{coin}: funding in DB ${gross:.4f} vs HL cumFunding "
                         f"${received[coin]:.4f} (diff ${diff:+.4f})"),
                payload={"farb_position_id": fp_id, "coin": coin, "db": gross,
                         "hl_cum_funding_since_open": received[coin], "diff": diff},
            )
