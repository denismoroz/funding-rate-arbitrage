from __future__ import annotations

from datetime import UTC, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _UtcAwareOut(BaseModel):
    """Base for response models — stamps UTC tzinfo on naive datetime fields.

    DB stores datetimes in UTC but without tzinfo (SQLite limitation). Without
    this, Pydantic emits `2026-05-15T06:26:00` which JavaScript parses as the
    user's local time and renders the dashboard hours off.
    """

    @model_validator(mode="after")
    def _attach_utc_to_naive(self) -> Self:
        for name in self.__class__.model_fields:
            value = getattr(self, name)
            if isinstance(value, datetime) and value.tzinfo is None:
                object.__setattr__(self, name, value.replace(tzinfo=UTC))
        return self


class StrategyOut(_UtcAwareOut):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    version: str
    params_json: dict
    status: str
    started_at: datetime | None
    stopped_at: datetime | None


class EquityOut(_UtcAwareOut):
    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: int
    ts: datetime
    total_equity: float
    cash: float
    spot_value: float
    perp_unrealized: float
    perp_realized_cum: float
    funding_cum: float
    fees_cum: float


class FillOut(_UtcAwareOut):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position_id: int
    ts: datetime
    leg: str
    side: str
    qty: float
    price: float
    fee: float
    slippage_bps: float
    is_paper: bool


class PositionOut(_UtcAwareOut):
    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: int
    market_id: int
    coin: str
    mode: str
    status: str
    opened_at: datetime
    closed_at: datetime | None
    spot_units: float
    perp_units: float
    entry_spot_price: float
    entry_perp_price: float
    exit_spot_price: float | None
    exit_perp_price: float | None
    realized_pnl: float
    funding_collected: float
    fees_paid: float
    fills: list[FillOut]


class SignalOut(_UtcAwareOut):
    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: int
    market_id: int
    coin: str
    ts: datetime
    signal_value: float
    regime_pass: bool
    action: str


class FundingRateOut(_UtcAwareOut):
    model_config = ConfigDict(from_attributes=True)

    id: int
    market_id: int
    coin: str
    ts: datetime
    rate: float
    premium: float | None
    annualized_pct: float


class EventOut(_UtcAwareOut):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    level: str
    source: str
    kind: str
    message: str
    payload_json: dict | None


class PositionFundingAccrualOut(_UtcAwareOut):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position_id: int
    ts: datetime
    delta: float


class StrategyParamsOut(BaseModel):
    coins: list[str]
    entry_threshold: float
    exit_threshold: float
    min_hold_hours: int
    signal_window_hours: int
    concurrency_cap: int
    position_size_usdc: float


class StrategyParamsIn(BaseModel):
    entry_threshold: float = Field(gt=0, le=5.0)
    exit_threshold: float = Field(ge=-2.0, le=5.0)
    min_hold_hours: int = Field(ge=0, le=720)
    concurrency_cap: int = Field(ge=1, le=20)
    position_size_usdc: float = Field(gt=0, le=1_000_000)

    @model_validator(mode="after")
    def _check_exit_below_entry(self) -> "StrategyParamsIn":
        if self.exit_threshold >= self.entry_threshold:
            raise ValueError("exit_threshold must be strictly less than entry_threshold")
        return self
