from typing import Optional

from sqlalchemy import (
    Boolean,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from frab.domain.enums import FarbState, Instrument, PositionStatus, Side


class Base(DeclarativeBase):
    pass


class Exchange(Base):
    __tablename__ = "exchanges"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    funding_interval_h: Mapped[int]
    spot_taker_bps: Mapped[float]
    perp_taker_bps: Mapped[float]


class Market(Base):
    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("exchange_id", "coin"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id", ondelete="CASCADE"))
    coin: Mapped[str] = mapped_column(String, nullable=False)
    has_spot: Mapped[bool] = mapped_column(Boolean, default=True)
    has_perp: Mapped[bool] = mapped_column(Boolean, default=True)
    min_size: Mapped[float] = mapped_column(default=0.0)
    tick_size: Mapped[float] = mapped_column(default=0.0)


class FundingRate(Base):
    __tablename__ = "funding_rates"
    __table_args__ = (
        UniqueConstraint("exchange_id", "coin", "ts_ms"),
        Index("ix_funding_rates_lookup", "exchange_id", "coin", "ts_ms"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id", ondelete="CASCADE"))
    coin: Mapped[str] = mapped_column(String, nullable=False)
    ts_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    rate: Mapped[float]
    premium: Mapped[Optional[float]] = mapped_column(nullable=True)
    annualized_pct: Mapped[float]


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (
        UniqueConstraint("exchange_id", "coin", "ts_ms"),
        Index("ix_prices_lookup", "exchange_id", "coin", "ts_ms"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id", ondelete="CASCADE"))
    coin: Mapped[str] = mapped_column(String, nullable=False)
    ts_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    mark: Mapped[float]
    spot: Mapped[Optional[float]] = mapped_column(nullable=True)
    bid: Mapped[Optional[float]] = mapped_column(nullable=True)
    ask: Mapped[Optional[float]] = mapped_column(nullable=True)


class Strategy(Base):
    __tablename__ = "strategies"
    __table_args__ = (Index("ix_strategies_status", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    params_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, default="idle", nullable=False)
    started_at_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stopped_at_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


# Positions and FarbPositions have a circular FK:
#   positions.farb_position_id → farb_positions.id
#   farb_positions.{spot,perp,margin}_position_id → positions.id
#
# Resolution: use use_alter=True on the farb_positions → positions FK side so
# the FK constraint is added as a separate ALTER TABLE after both tables exist.
# SQLite ignores FK DDL at CREATE time anyway (requires PRAGMA foreign_keys=ON
# at runtime), but this keeps Alembic's autogenerate from complaining.

class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_farb", "farb_position_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id", ondelete="CASCADE"))
    coin: Mapped[str] = mapped_column(String, nullable=False)
    instrument: Mapped[str] = mapped_column(
        SQLEnum(Instrument, native_enum=False, length=12),
        nullable=False,
    )
    side: Mapped[str] = mapped_column(
        SQLEnum(Side, native_enum=False, length=8),
        nullable=False,
    )
    qty: Mapped[float]
    entry_price: Mapped[float]
    opened_at: Mapped[int] = mapped_column(Integer, nullable=False)   # Unix ms UTC
    closed_at: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        SQLEnum(PositionStatus, native_enum=False, length=8),
        nullable=False,
    )
    farb_position_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("farb_positions.id", use_alter=True, name="fk_positions_farb_position_id"),
        nullable=True,
    )


class FarbPosition(Base):
    __tablename__ = "farb_positions"
    __table_args__ = (
        Index("ix_farb_positions_state", "state"),
        Index("ix_farb_positions_strategy", "strategy_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    coin: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(
        SQLEnum(FarbState, native_enum=False, length=20),
        nullable=False,
    )
    state_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    spot_position_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("positions.id", use_alter=True, name="fk_farb_positions_spot_position_id"),
        nullable=True,
    )
    perp_position_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("positions.id", use_alter=True, name="fk_farb_positions_perp_position_id"),
        nullable=True,
    )
    margin_position_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("positions.id", use_alter=True, name="fk_farb_positions_margin_position_id"),
        nullable=True,
    )
    opened_at: Mapped[int] = mapped_column(Integer, nullable=False)   # Unix ms UTC
    closed_at: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"))
    ts_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    qty: Mapped[float]
    price: Mapped[float]
    fee: Mapped[float]
    slippage_bps: Mapped[float]
    is_paper: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class FundingAccrual(Base):
    __tablename__ = "funding_accruals"

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"))
    ts_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[float]


class WalletSnapshot(Base):
    __tablename__ = "wallet_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id", ondelete="CASCADE"))
    coin: Mapped[str] = mapped_column(String, nullable=False)
    ts_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    balance: Mapped[float]
    source: Mapped[str] = mapped_column(String, nullable=False)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (Index("ix_equity_snapshots_lookup", "strategy_id", "ts_ms"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    ts_ms: Mapped[int] = mapped_column(Integer, nullable=False)
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
        Index("ix_events_ts_ms", "ts_ms"),
        Index("ix_events_level", "level"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ts_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
