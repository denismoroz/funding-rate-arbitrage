"""SignalComputer — DB-backed annualised smoothed funding-rate signal."""
from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.models import Exchange as ExchangeRow
from frab.db.models import FundingRate as FundingRateRow
from frab.db.session import session_scope

# HL hourly funding intervals per year
_HOURS_PER_YEAR = 8760


class SignalComputer:
    """Computes annualised smoothed funding-rate signals from DB history."""

    def __init__(
        self,
        *,
        exchange_name: str,
        session_factory: async_sessionmaker[AsyncSession],
        signal_window_hours: int,
    ) -> None:
        self._exchange_name = exchange_name
        self._sf = session_factory
        self._signal_window_hours = signal_window_hours

    async def compute(self, coin: str) -> float | None:
        """Annualized smoothed signal from last `signal_window_hours` funding rates."""
        window = self._signal_window_hours
        async with session_scope(self._sf) as session:
            # Look up exchange id
            result = await session.execute(
                select(ExchangeRow).where(ExchangeRow.name == self._exchange_name)
            )
            exc_row = result.scalar_one_or_none()
            if exc_row is None:
                return None
            intervals_per_year = _HOURS_PER_YEAR // exc_row.funding_interval_h

            # Fetch recent rates
            rates_result = await session.execute(
                select(FundingRateRow.rate)
                .where(
                    FundingRateRow.exchange_id == exc_row.id,
                    FundingRateRow.coin == coin,
                )
                .order_by(desc(FundingRateRow.ts_ms))
                .limit(window)
            )
            rates = [r for (r,) in rates_result.all()]

        if len(rates) < window:
            return None
        # Rates come newest-first from ORDER BY DESC; mean is order-independent
        mean_rate = sum(rates) / len(rates)
        return mean_rate * intervals_per_year

    async def latest_funding_rate(self, coin: str) -> float | None:
        """Most recent funding_rates.rate for the coin on this exchange."""
        async with session_scope(self._sf) as session:
            exc_row = (await session.execute(
                select(ExchangeRow).where(ExchangeRow.name == self._exchange_name)
            )).scalar_one_or_none()
            if exc_row is None:
                return None
            row = (await session.execute(
                select(FundingRateRow.rate)
                .where(
                    FundingRateRow.exchange_id == exc_row.id,
                    FundingRateRow.coin == coin,
                )
                .order_by(desc(FundingRateRow.ts_ms))
                .limit(1)
            )).first()
        return float(row[0]) if row is not None else None
