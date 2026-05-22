"""Two-phase exit + dynamic min_hold funding-harvest strategy."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from frab.engine.state import MarketState
from frab.engine.two_phase_signals import (
    TwoPhaseDecision,
    compute_position_min_hold,
    decide_two_phase,
    update_consec_negative,
)
from frab.exchanges.atomic import AtomicExecutor
from frab.exchanges.base import (
    FillReport,
    FundingTick,
    Leg,
    OrderRequest,
    Quote,
    Side,
)
from frab.strategies.base import (
    EquitySnapshot,
    FailedOpen,
    SignalEvent,
    Strategy,
    TickReport,
    WatchdogAction,
    WatchdogReport,
)
from frab.strategies.strategy_a import AccumulatorsSnapshot, OpenPositionSnapshot

if TYPE_CHECKING:
    from frab.engine.margin_manager import MarginManager, OpenPosition

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TwoPhaseDynamicParams:
    coins: tuple[str, ...]
    entry_threshold: float = 0.10
    signal_window_hours: int = 12
    base_min_hold_hours: int = 24
    safety_mult: float = 5.0
    cap_min_hold_hours: int = 720
    phase1_negative_patience: int = 72
    phase1_breakeven_cap_hours: int = 720
    phase2_exit_threshold: float = -0.10
    concurrency_cap: int = 3
    position_size_usdc: float = 1000.0
    fee_round_trip_annual: float = 18.396  # default HL retail; override per-exchange

    def __post_init__(self) -> None:
        if self.concurrency_cap <= 0:
            raise ValueError("concurrency_cap must be positive")
        if self.position_size_usdc <= 0:
            raise ValueError("position_size_usdc must be positive")
        if self.signal_window_hours <= 0:
            raise ValueError("signal_window_hours must be positive")
        if len(self.coins) == 0:
            raise ValueError("coins must be non-empty")
        if self.safety_mult <= 0:
            raise ValueError("safety_mult must be positive")
        if self.cap_min_hold_hours <= 0:
            raise ValueError("cap_min_hold_hours must be positive")


@dataclass
class _PositionRecord:
    opened_at: datetime
    spot_qty: float          # positive: units of base
    perp_qty: float          # positive: magnitude of short
    entry_spot_price: float
    entry_perp_price: float
    funding_collected: float = 0.0
    fees_paid: float = 0.0
    # TwoPhaseDynamic two-phase state:
    position_min_hold_hours: int = 0
    consec_negative_hours: int = 0


class TwoPhaseDynamic(Strategy):
    name = "two_phase_dynamic"
    version = "v1"

    def __init__(
        self,
        params: TwoPhaseDynamicParams,
        executor: AtomicExecutor,
        *,
        dry_run: bool = False,
        margin_manager: MarginManager | None = None,
    ) -> None:
        self._params = params
        self._executor = executor
        self._dry_run = dry_run
        self._margin_manager = margin_manager
        self._market_state = MarketState(params.coins, params.signal_window_hours, funding_interval_hours=1.0)
        self._positions: dict[str, _PositionRecord] = {}
        self._last_quotes: dict[str, Quote] = {}
        self._realized_pnl_cum: float = 0.0
        self._funding_cum: float = 0.0
        self._fees_cum: float = 0.0
        self._cash: float = params.concurrency_cap * params.position_size_usdc * 2
        self._perp_cash: float = 0.0
        self._n_skipped_opens_capital: int = 0

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def realized_pnl_cum(self) -> float:
        return self._realized_pnl_cum

    @property
    def funding_cum(self) -> float:
        return self._funding_cum

    @property
    def fees_cum(self) -> float:
        return self._fees_cum

    @property
    def perp_cash(self) -> float:
        return self._perp_cash

    @property
    def n_skipped_opens_capital(self) -> int:
        return self._n_skipped_opens_capital

    def set_fees_cum(self, value: float) -> None:
        """Replace the running fees counter with the DB-authoritative total."""
        self._fees_cum = value

    def set_funding_cum(self, value: float) -> None:
        """Replace the running funding counter with the DB-authoritative total."""
        self._funding_cum = value

    def open_positions(self) -> list[str]:
        return list(self._positions.keys())

    def rehydrate(
        self,
        *,
        positions: list[OpenPositionSnapshot],
        accumulators: AccumulatorsSnapshot | None = None,
    ) -> None:
        """Restore in-memory state from a DB snapshot after engine restart.

        Replaces `_positions` and (if `accumulators` provided) overrides
        cash/realized_pnl/funding/fees with persisted values. Must be called
        before `engine.run()` starts ticking.
        """
        self._positions = {
            snap.coin: _PositionRecord(
                opened_at=snap.opened_at,
                spot_qty=snap.spot_qty,
                perp_qty=snap.perp_qty,
                entry_spot_price=snap.entry_spot_price,
                entry_perp_price=snap.entry_perp_price,
                funding_collected=snap.funding_collected,
                fees_paid=snap.fees_paid,
                position_min_hold_hours=snap.position_min_hold_hours,
                consec_negative_hours=snap.consec_negative_hours,
            )
            for snap in positions
        }
        if accumulators is not None:
            self._cash = accumulators.cash
            self._realized_pnl_cum = accumulators.realized_pnl_cum
            self._funding_cum = accumulators.funding_cum
            self._fees_cum = accumulators.fees_cum
        # Reset perp_cash on rehydrate — it will be reconciled via live state
        self._perp_cash = 0.0

    def update_hot_params(
        self,
        *,
        entry_threshold: float,
        base_min_hold_hours: int,
        safety_mult: float,
        cap_min_hold_hours: int,
        phase1_negative_patience: int,
        phase1_breakeven_cap_hours: int,
        phase2_exit_threshold: float,
        concurrency_cap: int,
        position_size_usdc: float,
        fee_round_trip_annual: float,
    ) -> None:
        """Atomically swap hot params. Preserves cold fields (coins, signal_window_hours).
        Called between ticks; asyncio single-thread guarantees no race."""
        self._params = TwoPhaseDynamicParams(
            coins=self._params.coins,
            entry_threshold=entry_threshold,
            signal_window_hours=self._params.signal_window_hours,
            base_min_hold_hours=base_min_hold_hours,
            safety_mult=safety_mult,
            cap_min_hold_hours=cap_min_hold_hours,
            phase1_negative_patience=phase1_negative_patience,
            phase1_breakeven_cap_hours=phase1_breakeven_cap_hours,
            phase2_exit_threshold=phase2_exit_threshold,
            concurrency_cap=concurrency_cap,
            position_size_usdc=position_size_usdc,
            fee_round_trip_annual=fee_round_trip_annual,
        )

    def warmup_from_history(self, ticks_by_coin: dict[str, list[FundingTick]]) -> int:
        """Push historical funding ticks into MarketState (ascending ts assumed).

        Skips coins outside the strategy universe and ticks that would violate
        the monotonic-ts invariant of CoinState. Returns total ticks applied.
        """
        applied = 0
        for coin, ticks in ticks_by_coin.items():
            if coin not in self._market_state:
                continue
            cs = self._market_state.get(coin)
            for tick in ticks:
                last = cs.last_tick
                if last is not None and tick.ts <= last.ts:
                    continue
                cs.add_funding(tick)
                applied += 1
        return applied

    def _position_size_for(self, coin: str) -> float:
        """Spot leg notional for opening `coin`. Defers to MarginManager
        when configured (single source of truth), otherwise uses uniform
        strategy params."""
        if self._margin_manager is not None and coin in self._margin_manager._params:
            return self._margin_manager.position_size_for(coin)
        return self._params.position_size_usdc

    def _open_position_snapshots_for_manager(self) -> list[OpenPosition]:
        """Return list of OpenPosition snapshots for MarginManager.can_open."""
        from frab.engine.margin_manager import OpenPosition as MgrOpenPosition  # noqa: PLC0415
        result: list[MgrOpenPosition] = []
        for coin, pos in self._positions.items():
            if self._margin_manager is not None and coin in self._margin_manager._params:
                required_margin = self._margin_manager.compute_required_margin_for_open(coin)
            else:
                required_margin = 0.0
            result.append(MgrOpenPosition(
                coin=coin,
                spot_units=pos.spot_qty,
                short_size=pos.perp_qty,
                entry_perp_price=pos.entry_perp_price,
                required_margin=required_margin,
            ))
        return result

    async def on_minute_tick(self, now: datetime, quotes: dict[str, Quote]) -> None:
        for coin, quote in quotes.items():
            self._last_quotes[coin] = quote

    async def on_hour_tick(self, now: datetime, funding: dict[str, FundingTick]) -> TickReport:
        # Step 1: apply funding ticks to MarketState
        for coin in self._market_state.coins():
            if coin in funding:
                self._market_state.add_funding(funding[coin])

        # Step 2: funding accrual (local estimate for phase decisions).
        # FundingReconciler periodically overwrites position.funding_collected
        # with HL's authoritative SUM, so this local += is a short-lived
        # estimate bounded by the reconciler cadence.
        funding_accrued: list[tuple[str, float]] = []
        for coin, pos in self._positions.items():
            if coin not in funding or coin not in self._last_quotes:
                continue
            mark = self._last_quotes[coin].mark
            rate = funding[coin].rate
            f = pos.perp_qty * mark * rate
            self._cash += f
            pos.funding_collected += f
            self._funding_cum += f
            funding_accrued.append((coin, f))

        # Step 3: update consec_negative for every open position
        consec_updates: list[tuple[str, int]] = []
        for coin, pos in self._positions.items():
            cs = self._market_state.get(coin)
            smoothed = cs.smoothed_signal()
            pos.consec_negative_hours = update_consec_negative(
                prev_consec_negative=pos.consec_negative_hours,
                smoothed_signal_annual=smoothed,
            )
            consec_updates.append((coin, pos.consec_negative_hours))

        # Step 4: compute decisions for every coin
        signals_log: list[SignalEvent] = []
        coin_decisions: dict[str, TwoPhaseDecision] = {}
        for coin in self._market_state.coins():
            cs = self._market_state.get(coin)
            in_pos = coin in self._positions
            smoothed = cs.smoothed_signal()
            hours_in = (
                int((now - self._positions[coin].opened_at).total_seconds() // 3600)
                if in_pos
                else 0
            )

            if in_pos:
                pos = self._positions[coin]
                position_min_hold = pos.position_min_hold_hours
                gross_funding = pos.funding_collected
                total_fees = pos.fees_paid
                consec_neg = pos.consec_negative_hours
                if smoothed is not None and coin in self._last_quotes:
                    current_hourly_income = self._params.position_size_usdc * smoothed / 8760
                else:
                    current_hourly_income = 0.0
            else:
                position_min_hold = 0
                gross_funding = 0.0
                total_fees = 0.0
                consec_neg = 0
                current_hourly_income = 0.0

            dec = decide_two_phase(
                in_position=in_pos,
                smoothed_signal_annual=smoothed,
                entry_threshold=self._params.entry_threshold,
                hours_in_position=hours_in,
                position_min_hold_hours=position_min_hold,
                gross_funding_so_far=gross_funding,
                total_fees_paid=total_fees,
                consec_negative_hours=consec_neg,
                current_hourly_income_quote=current_hourly_income,
                phase1_negative_patience=self._params.phase1_negative_patience,
                phase1_breakeven_cap_hours=self._params.phase1_breakeven_cap_hours,
                phase2_exit_threshold=self._params.phase2_exit_threshold,
            )
            coin_decisions[coin] = dec

            # Map TwoPhaseDecision → signal action string
            if dec == TwoPhaseDecision.OPEN:
                action = "OPEN"
            elif dec in (
                TwoPhaseDecision.CLOSE_PHASE1_NEG,
                TwoPhaseDecision.CLOSE_PHASE1_CAP,
                TwoPhaseDecision.CLOSE_PHASE2,
            ):
                action = "CLOSE"
            else:
                action = "NONE"
            signals_log.append(SignalEvent(coin=coin, ts=now, signal_value=smoothed, regime_pass=True, action=action))

        # Step 5: execute CLOSE decisions
        fills_log: list[FillReport] = []
        closed: list[str] = []
        for coin, dec in coin_decisions.items():
            if dec in (
                TwoPhaseDecision.CLOSE_PHASE1_NEG,
                TwoPhaseDecision.CLOSE_PHASE1_CAP,
                TwoPhaseDecision.CLOSE_PHASE2,
            ):
                if self._dry_run:
                    logger.warning("dry-run: skipped CLOSE for %s", coin)
                    continue
                close_fills, ok = await self._close_position(coin, now)
                if ok:
                    fills_log.extend(close_fills)
                    closed.append(coin)
                # else: silent — AtomicExecutor's Event is the only record this tick

        # Step 6: execute OPEN decisions with concurrency cap
        opened: list[str] = []
        opened_min_holds: list[tuple[str, int]] = []
        failed_opens: list[FailedOpen] = []
        slots_free = self._params.concurrency_cap - len(self._positions)
        if slots_free > 0:
            candidates: list[tuple[str, float]] = []
            for coin, dec in coin_decisions.items():
                if dec == TwoPhaseDecision.OPEN:
                    smoothed = self._market_state.get(coin).smoothed_signal()
                    if smoothed is None or coin not in self._last_quotes:
                        continue
                    candidates.append((coin, smoothed))
            candidates.sort(key=lambda x: -x[1])  # strongest first
            for coin, entry_signal in candidates[:slots_free]:
                if self._dry_run:
                    logger.warning("dry-run: skipped OPEN for %s", coin)
                    continue
                if self._margin_manager is not None:
                    ok, reason = self._margin_manager.can_open(
                        coin,
                        spot_cash=self._cash,
                        opens=self._open_position_snapshots_for_manager(),
                        perp_cash=self._perp_cash,
                    )
                    if not ok:
                        logger.warning("margin pre-flight: skipping OPEN for %s — %s", coin, reason)
                        self._n_skipped_opens_capital += 1
                        continue
                    required_margin = self._margin_manager.compute_required_margin_for_open(coin)
                    try:
                        await self._executor.transfer_spot_to_perp(required_margin)
                        self._cash -= required_margin
                        self._perp_cash += required_margin
                    except Exception as exc:
                        logger.error("margin transfer failed for %s: %r — skipping OPEN", coin, exc)
                        self._n_skipped_opens_capital += 1
                        continue
                min_hold = compute_position_min_hold(
                    entry_signal_annual=entry_signal,
                    safety_mult=self._params.safety_mult,
                    base_min_hold_hours=self._params.base_min_hold_hours,
                    cap_min_hold_hours=self._params.cap_min_hold_hours,
                    fee_round_trip_annual=self._params.fee_round_trip_annual,
                )
                open_fills, failed = await self._open_position(coin, now, min_hold)
                if failed is None:
                    fills_log.extend(open_fills)
                    opened.append(coin)
                    opened_min_holds.append((coin, min_hold))
                else:
                    failed_opens.append(failed)

        # Step 7: filter consec_updates to coins still open
        consec_updates_filtered = [(c, v) for c, v in consec_updates if c in self._positions]

        return TickReport(
            ts=now,
            signals=tuple(signals_log),
            fills=tuple(fills_log),
            opened=tuple(opened),
            closed=tuple(closed),
            funding_accrued=tuple(funding_accrued),
            opened_min_holds=tuple(opened_min_holds),
            consec_negative_updates=tuple(consec_updates_filtered),
            failed_opens=tuple(failed_opens),
        )

    async def _open_position(
        self, coin: str, now: datetime, min_hold: int,
    ) -> tuple[list[FillReport], FailedOpen | None]:
        quote = self._last_quotes[coin]
        qty = self._position_size_for(coin) / quote.mark

        perp_req = OrderRequest(
            coin=coin, leg=Leg.PERP, side=Side.SELL, qty=qty,
            client_ref=f"open-perp-{coin}-{now.isoformat()}",
        )
        spot_req = OrderRequest(
            coin=coin, leg=Leg.SPOT, side=Side.BUY, qty=qty,
            client_ref=f"open-spot-{coin}-{now.isoformat()}",
        )
        result = await self._executor.open_paired(perp_req, spot_req)

        if result.status == "failed":
            return [], FailedOpen(
                coin=coin,
                ts=now,
                perp_fill=result.perp_fill,
                spot_fill=result.spot_fill,
                error="; ".join(result.errors) if result.errors else "unknown",
            )

        fill_perp = result.perp_fill
        fill_spot = result.spot_fill
        assert fill_perp is not None and fill_spot is not None  # status=ok invariant

        # Cash flow: spot buy debits notional + fee; perp short debits fee only
        spot_cost = fill_spot.qty * fill_spot.price + fill_spot.fee
        self._cash -= spot_cost
        self._cash -= fill_perp.fee
        self._fees_cum += fill_spot.fee + fill_perp.fee

        self._positions[coin] = _PositionRecord(
            opened_at=now,
            spot_qty=fill_spot.qty,
            perp_qty=fill_perp.qty,
            entry_spot_price=fill_spot.price,
            entry_perp_price=fill_perp.price,
            fees_paid=fill_spot.fee + fill_perp.fee,
            position_min_hold_hours=min_hold,
            consec_negative_hours=0,
        )
        return [fill_spot, fill_perp], None

    async def _close_position(
        self, coin: str, now: datetime,
    ) -> tuple[list[FillReport], bool]:
        """Returns (fills, success). On failure, in-memory state is unchanged
        and the caller should NOT add `coin` to `closed`. AtomicExecutor has
        already published an alert Event."""
        pos = self._positions[coin]

        perp_req = OrderRequest(
            coin=coin, leg=Leg.PERP, side=Side.BUY, qty=pos.perp_qty,
            client_ref=f"close-perp-{coin}-{now.isoformat()}",
        )
        spot_req = OrderRequest(
            coin=coin, leg=Leg.SPOT, side=Side.SELL, qty=pos.spot_qty,
            client_ref=f"close-spot-{coin}-{now.isoformat()}",
        )
        result = await self._executor.close_paired(perp_req, spot_req)

        if result.status == "failed":
            return [], False

        fill_perp = result.perp_fill
        fill_spot = result.spot_fill
        assert fill_perp is not None and fill_spot is not None

        del self._positions[coin]

        self._cash += fill_spot.qty * fill_spot.price - fill_spot.fee
        realized_perp = pos.perp_qty * (pos.entry_perp_price - fill_perp.price)
        self._cash += realized_perp - fill_perp.fee

        self._realized_pnl_cum += realized_perp
        self._fees_cum += fill_spot.fee + fill_perp.fee
        return [fill_spot, fill_perp], True

    def _select_weakest_open(self) -> str | None:
        if self._margin_manager is None or not self._positions:
            return None
        signals = self._market_state.signals()
        open_signals = {c: (signals.get(c) or 0.0) for c in self._positions}
        return self._margin_manager.select_weakest_for_close(
            self._open_position_snapshots_for_manager(),
            open_signals,
        )

    async def _watchdog_force_close(self, coin: str, now: datetime) -> bool:
        if self._margin_manager is None or coin not in self._positions:
            return False
        required_margin = self._margin_manager.compute_required_margin_for_open(coin)
        _, ok = await self._close_position(coin, now)
        if not ok:
            return False
        try:
            await self._executor.transfer_perp_to_spot(required_margin)
        except Exception as exc:  # noqa: BLE001
            logger.error("watchdog: transfer_perp_to_spot failed after close of %s: %r", coin, exc)
        self._cash += required_margin
        self._perp_cash -= required_margin
        return True

    async def margin_watchdog(self, now: datetime) -> WatchdogReport | None:
        if self._margin_manager is None:
            return None
        if not self._positions:
            return None
        marks: dict[str, float] = {}
        for coin in self._positions:
            if coin not in self._last_quotes:
                return None
            marks[coin] = self._last_quotes[coin].mark

        opens = self._open_position_snapshots_for_manager()
        total_maint = self._margin_manager.compute_total_maintenance(opens, marks)
        unrealized = self._margin_manager.compute_perp_unrealized(opens, marks)
        # HL semantics: perp_account_value = perp_cash + unrealized.
        effective_equity = self._perp_cash + unrealized
        ratio = self._margin_manager.compute_margin_ratio(self._perp_cash, opens, marks)

        if ratio >= self._margin_manager.top_up_trigger:
            return WatchdogReport(now, WatchdogAction.NONE, ratio, None, 0.0, "margin healthy")

        if ratio < 1.0:
            coin = self._select_weakest_open()
            if coin is None:
                return WatchdogReport(now, WatchdogAction.NONE, ratio, None, 0.0,
                                      "no opens to close on emergency")
            ok = await self._watchdog_force_close(coin, now)
            action = WatchdogAction.EMERGENCY if ok else WatchdogAction.NONE
            reason = f"emergency close {coin}" if ok else f"emergency close FAILED {coin}"
            return WatchdogReport(now, action, ratio, coin if ok else None, 0.0, reason)

        top_up = self._margin_manager.compute_top_up_amount(effective_equity, total_maint)
        if self._cash >= top_up and top_up > 0.0:
            try:
                await self._executor.transfer_spot_to_perp(top_up)
                self._cash -= top_up
                self._perp_cash += top_up
                return WatchdogReport(now, WatchdogAction.TOP_UP, ratio, None, top_up,
                                      f"topped up perp by ${top_up:.2f}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("watchdog top_up failed: %r — falling through to forced close", exc)

        coin = self._select_weakest_open()
        if coin is None:
            return WatchdogReport(now, WatchdogAction.NONE, ratio, None, 0.0, "no opens to close")
        ok = await self._watchdog_force_close(coin, now)
        action = WatchdogAction.FORCED_CLOSE if ok else WatchdogAction.NONE
        reason = (f"forced close {coin} (spot cash insufficient)"
                  if ok else f"forced close FAILED {coin}")
        return WatchdogReport(now, action, ratio, coin if ok else None, 0.0, reason)

    def compute_equity(self, now: datetime) -> EquitySnapshot:
        spot_value = 0.0
        perp_unrealized = 0.0
        for coin, pos in self._positions.items():
            if coin not in self._last_quotes:
                continue
            mark = self._last_quotes[coin].mark
            spot_value += pos.spot_qty * mark
            perp_unrealized += pos.perp_qty * (pos.entry_perp_price - mark)
        total = self._cash + spot_value + perp_unrealized
        return EquitySnapshot(
            ts=now,
            total_equity=total,
            cash=self._cash,
            spot_value=spot_value,
            perp_unrealized=perp_unrealized,
            perp_realized_cum=self._realized_pnl_cum,
            funding_cum=self._funding_cum,
            fees_cum=self._fees_cum,
        )
