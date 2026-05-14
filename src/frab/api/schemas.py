from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class StrategyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    version: str
    params_json: dict
    status: str
    started_at: datetime | None
    stopped_at: datetime | None


class EquityOut(BaseModel):
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


class FillOut(BaseModel):
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


class PositionOut(BaseModel):
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


class SignalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: int
    market_id: int
    coin: str
    ts: datetime
    signal_value: float
    regime_pass: bool
    action: str


class FundingRateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    market_id: int
    coin: str
    ts: datetime
    rate: float
    premium: float | None
    annualized_pct: float


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    level: str
    source: str
    kind: str
    message: str
    payload_json: dict | None
