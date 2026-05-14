from time import time
from typing import Optional

from sqlalchemy import ForeignKey, Index, JSON, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_ms() -> int:
    return int(time() * 1000)


class Base(DeclarativeBase):
    pass


class Exchange(Base):
    __tablename__ = "exchanges"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    funding_interval_h: Mapped[int]
    spot_taker_bps: Mapped[float]
    perp_taker_bps: Mapped[float]
    created_at_ms: Mapped[int] = mapped_column(default=now_ms)


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
        UniqueConstraint("exchange_id", "coin", "ts_ms"),
        Index("ix_funding_rates_lookup", "coin", "ts_ms"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id", ondelete="CASCADE"))
    coin: Mapped[str]
    ts_ms: Mapped[int]
    rate: Mapped[float]
    premium: Mapped[Optional[float]] = mapped_column(nullable=True)
    annualized_pct: Mapped[float]


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (
        UniqueConstraint("exchange_id", "coin", "ts_ms"),
        Index("ix_prices_lookup", "coin", "ts_ms"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id", ondelete="CASCADE"))
    coin: Mapped[str]
    ts_ms: Mapped[int]
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
    started_at_ms: Mapped[Optional[int]] = mapped_column(nullable=True)
    stopped_at_ms: Mapped[Optional[int]] = mapped_column(nullable=True)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("strategy_id", "coin", "ts_ms"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    coin: Mapped[str]
    ts_ms: Mapped[int]
    signal_value: Mapped[float]
    regime_pass: Mapped[bool] = mapped_column(default=True)
    action: Mapped[str]


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_status", "strategy_id", "status"),
        Index("ix_positions_coin_time", "coin", "opened_at_ms"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    mode: Mapped[str]
    coin: Mapped[str]
    status: Mapped[str]
    opened_at_ms: Mapped[int]
    closed_at_ms: Mapped[Optional[int]] = mapped_column(nullable=True)
    spot_units: Mapped[float]
    perp_units: Mapped[float]
    entry_spot_price: Mapped[float]
    entry_perp_price: Mapped[float]
    exit_spot_price: Mapped[Optional[float]] = mapped_column(nullable=True)
    exit_perp_price: Mapped[Optional[float]] = mapped_column(nullable=True)
    realized_pnl: Mapped[float] = mapped_column(default=0.0)
    funding_collected: Mapped[float] = mapped_column(default=0.0)
    fees_paid: Mapped[float] = mapped_column(default=0.0)


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"))
    ts_ms: Mapped[int]
    leg: Mapped[str]
    side: Mapped[str]
    qty: Mapped[float]
    price: Mapped[float]
    fee: Mapped[float]
    slippage_bps: Mapped[float]
    is_paper: Mapped[bool] = mapped_column(default=True)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (Index("ix_equity_lookup", "strategy_id", "ts_ms"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    ts_ms: Mapped[int]
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
        Index("ix_events_time", "ts_ms"),
        Index("ix_events_level", "level"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ts_ms: Mapped[int]
    level: Mapped[str]
    source: Mapped[str]
    kind: Mapped[str]
    message: Mapped[str]
    payload_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
