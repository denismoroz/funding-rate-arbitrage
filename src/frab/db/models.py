from datetime import UTC, datetime
from enum import StrEnum
from typing import Optional

from sqlalchemy import DateTime, Enum as SQLEnum, ForeignKey, Index, JSON, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from frab.engine.signals import Decision
from frab.exchanges.base import Leg, Side


def now_utc() -> datetime:
    return datetime.now(UTC)


class PositionMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class Base(DeclarativeBase):
    pass


class Exchange(Base):
    __tablename__ = "exchanges"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    funding_interval_h: Mapped[int]
    spot_taker_bps: Mapped[float]
    perp_taker_bps: Mapped[float]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Market(Base):
    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("exchange_id", "coin"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id", ondelete="CASCADE"))
    coin: Mapped[str]
    has_spot: Mapped[bool] = mapped_column(default=True)
    has_perp: Mapped[bool] = mapped_column(default=True)
    min_size: Mapped[float] = mapped_column(default=0.0)
    tick_size: Mapped[float] = mapped_column(default=0.0)


class FundingRate(Base):
    __tablename__ = "funding_rates"
    __table_args__ = (
        UniqueConstraint("market_id", "ts"),
        Index("ix_funding_rates_lookup", "market_id", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rate: Mapped[float]
    premium: Mapped[Optional[float]] = mapped_column(nullable=True)
    annualized_pct: Mapped[float]


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (
        UniqueConstraint("market_id", "ts"),
        Index("ix_prices_lookup", "market_id", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mark: Mapped[float]
    spot: Mapped[Optional[float]] = mapped_column(nullable=True)
    bid: Mapped[Optional[float]] = mapped_column(nullable=True)
    ask: Mapped[Optional[float]] = mapped_column(nullable=True)


class Strategy(Base):
    __tablename__ = "strategies"
    __table_args__ = (UniqueConstraint("name", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    version: Mapped[str]
    params_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(default="idle")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("strategy_id", "market_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    signal_value: Mapped[float]
    regime_pass: Mapped[bool] = mapped_column(default=True)
    action: Mapped[Decision] = mapped_column(SQLEnum(Decision, native_enum=False, length=10))


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_status", "strategy_id", "status"),
        Index("ix_positions_market_time", "market_id", "opened_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"))
    mode: Mapped[PositionMode] = mapped_column(SQLEnum(PositionMode, native_enum=False, length=10))
    status: Mapped[PositionStatus] = mapped_column(SQLEnum(PositionStatus, native_enum=False, length=10))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    spot_units: Mapped[float]
    perp_units: Mapped[float]
    entry_spot_price: Mapped[float]
    entry_perp_price: Mapped[float]
    exit_spot_price: Mapped[Optional[float]] = mapped_column(nullable=True)
    exit_perp_price: Mapped[Optional[float]] = mapped_column(nullable=True)
    realized_pnl: Mapped[float] = mapped_column(default=0.0)
    funding_collected: Mapped[float] = mapped_column(default=0.0)
    fees_paid: Mapped[float] = mapped_column(default=0.0)
    # StrategyC (two_phase_dynamic) fields:
    position_min_hold_hours: Mapped[int] = mapped_column(default=0)
    consec_negative_hours: Mapped[int] = mapped_column(default=0)


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    leg: Mapped[Leg] = mapped_column(SQLEnum(Leg, native_enum=False, length=10))
    side: Mapped[Side] = mapped_column(SQLEnum(Side, native_enum=False, length=10))
    qty: Mapped[float]
    price: Mapped[float]
    fee: Mapped[float]
    slippage_bps: Mapped[float]
    is_paper: Mapped[bool] = mapped_column(default=True)


class PositionFundingAccrual(Base):
    """Per-tick funding delta attributed to a single open position.

    Paper mode: written by the strategy via DbRecorder on each hour-tick.
    Live mode (future): can be written from exchange `userFundingHistory` feed.
    """
    __tablename__ = "position_funding_accruals"
    __table_args__ = (Index("ix_pfa_position_ts", "position_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delta: Mapped[float]


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (Index("ix_equity_lookup", "strategy_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    total_equity: Mapped[float]
    cash: Mapped[float]
    spot_value: Mapped[float]
    perp_unrealized: Mapped[float]
    perp_realized_cum: Mapped[float]
    funding_cum: Mapped[float]
    fees_cum: Mapped[float]


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_time", "ts"),
        Index("ix_events_level", "level"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    level: Mapped[str]
    source: Mapped[str]
    kind: Mapped[str]
    message: Mapped[str]
    payload_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
