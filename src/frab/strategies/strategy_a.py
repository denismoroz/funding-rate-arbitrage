"""Strategy A: funding-harvest with concurrency cap."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from frab.engine.signals import Decision, decide
from frab.engine.state import MarketState
from frab.exchanges.base import (
    Executor,
    FillReport,
    FundingTick,
    Leg,
    OrderRequest,
    Quote,
    Side,
)
from frab.strategies.base import EquitySnapshot, SignalEvent, Strategy, TickReport


@dataclass(frozen=True, slots=True)
class StrategyAParams:
    coins: tuple[str, ...]
    entry_threshold: float = 0.30
    exit_threshold: float = -0.15
    min_hold_hours: int = 120
    signal_window_hours: int = 12
    concurrency_cap: int = 3
    position_size_usdc: float = 1000.0

    def __post_init__(self) -> None:
        if self.concurrency_cap <= 0:
            raise ValueError("concurrency_cap must be positive")
        if self.position_size_usdc <= 0:
            raise ValueError("position_size_usdc must be positive")
        if self.signal_window_hours <= 0:
            raise ValueError("signal_window_hours must be positive")
        if len(self.coins) == 0:
            raise ValueError("coins must be non-empty")


@dataclass
class _PositionRecord:
    opened_at: datetime
    spot_qty: float         # positive: units of base
    perp_qty: float         # positive: magnitude of short
    entry_spot_price: float
    entry_perp_price: float
    funding_collected: float = 0.0
    fees_paid: float = 0.0


class StrategyA(Strategy):
    name = "strategy_a"
    version = "v1"

    def __init__(self, params: StrategyAParams, executor: Executor) -> None:
        self._params = params
        self._executor = executor
        self._market_state = MarketState(params.coins, params.signal_window_hours)
        self._positions: dict[str, _PositionRecord] = {}
        self._last_quotes: dict[str, Quote] = {}
        self._realized_pnl_cum: float = 0.0
        self._funding_cum: float = 0.0
        self._fees_cum: float = 0.0
        self._cash: float = params.concurrency_cap * params.position_size_usdc * 2

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

    def open_positions(self) -> list[str]:
        return list(self._positions.keys())

    async def on_minute_tick(self, now: datetime, quotes: dict[str, Quote]) -> None:
        for coin, quote in quotes.items():
            self._last_quotes[coin] = quote

    async def on_hour_tick(self, now: datetime, funding: dict[str, FundingTick]) -> TickReport:
        # Step 1: apply funding ticks to MarketState
        for coin in self._market_state.coins():
            if coin in funding:
                self._market_state.add_funding(funding[coin])

        # Step 2: funding accrual on open positions
        for coin, pos in self._positions.items():
            if coin not in funding or coin not in self._last_quotes:
                continue
            mark = self._last_quotes[coin].mark
            rate = funding[coin].rate
            f = pos.perp_qty * mark * rate
            self._cash += f
            pos.funding_collected += f
            self._funding_cum += f

        # Step 3: compute decisions for every coin in MarketState
        signals_log: list[SignalEvent] = []
        coin_decisions: dict[str, Decision] = {}
        for coin in self._market_state.coins():
            cs = self._market_state.get(coin)
            in_pos = coin in self._positions
            hours_in = int((now - self._positions[coin].opened_at).total_seconds() // 3600) if in_pos else 0
            smoothed = cs.smoothed_signal()
            current_annual = cs.current_annual_rate() if cs.current_annual_rate() is not None else 0.0
            dec = decide(
                in_position=in_pos,
                smoothed_signal=smoothed,
                current_annual_rate=current_annual,
                hours_in_position=hours_in,
                entry_threshold=self._params.entry_threshold,
                exit_threshold=self._params.exit_threshold,
                min_hold_hours=self._params.min_hold_hours,
            )
            coin_decisions[coin] = dec
            signals_log.append(SignalEvent(coin=coin, ts=now, signal_value=smoothed, regime_pass=True, action=dec.value))

        # Step 4: execute CLOSE decisions
        fills_log: list[FillReport] = []
        closed: list[str] = []
        for coin, dec in coin_decisions.items():
            if dec == Decision.CLOSE:
                close_fills = await self._close_position(coin, now)
                fills_log.extend(close_fills)
                closed.append(coin)

        # Step 5: execute OPEN decisions with concurrency cap
        opened: list[str] = []
        slots_free = self._params.concurrency_cap - len(self._positions)
        if slots_free > 0:
            candidates: list[tuple[str, float]] = []
            for coin, dec in coin_decisions.items():
                if dec == Decision.OPEN:
                    smoothed = self._market_state.get(coin).smoothed_signal()
                    if smoothed is None or coin not in self._last_quotes:
                        continue  # need quote + signal to open
                    candidates.append((coin, smoothed))
            candidates.sort(key=lambda x: -x[1])  # strongest first
            for coin, _ in candidates[:slots_free]:
                open_fills = await self._open_position(coin, now)
                fills_log.extend(open_fills)
                opened.append(coin)

        # Step 6: assemble report
        return TickReport(
            ts=now,
            signals=tuple(signals_log),
            fills=tuple(fills_log),
            opened=tuple(opened),
            closed=tuple(closed),
        )

    async def _open_position(self, coin: str, now: datetime) -> list[FillReport]:
        quote = self._last_quotes[coin]
        qty = self._params.position_size_usdc / quote.mark

        spot_req = OrderRequest(
            coin=coin, leg=Leg.SPOT, side=Side.BUY, qty=qty,
            client_ref=f"open-spot-{coin}-{now.isoformat()}",
        )
        fill_spot = await self._executor.submit(spot_req)

        perp_req = OrderRequest(
            coin=coin, leg=Leg.PERP, side=Side.SELL, qty=qty,
            client_ref=f"open-perp-{coin}-{now.isoformat()}",
        )
        fill_perp = await self._executor.submit(perp_req)

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
        )
        return [fill_spot, fill_perp]

    async def _close_position(self, coin: str, now: datetime) -> list[FillReport]:
        pos = self._positions.pop(coin)

        spot_req = OrderRequest(
            coin=coin, leg=Leg.SPOT, side=Side.SELL, qty=pos.spot_qty,
            client_ref=f"close-spot-{coin}-{now.isoformat()}",
        )
        fill_spot = await self._executor.submit(spot_req)

        perp_req = OrderRequest(
            coin=coin, leg=Leg.PERP, side=Side.BUY, qty=pos.perp_qty,
            client_ref=f"close-perp-{coin}-{now.isoformat()}",
        )
        fill_perp = await self._executor.submit(perp_req)

        # Spot sell credits notional minus fee
        self._cash += fill_spot.qty * fill_spot.price - fill_spot.fee

        # Perp short close: realized = perp_qty * (entry - exit). Then debit close fee.
        realized_perp = pos.perp_qty * (pos.entry_perp_price - fill_perp.price)
        self._cash += realized_perp - fill_perp.fee

        self._realized_pnl_cum += realized_perp
        self._fees_cum += fill_spot.fee + fill_perp.fee
        return [fill_spot, fill_perp]

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
