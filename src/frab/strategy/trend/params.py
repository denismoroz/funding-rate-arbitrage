"""Trend paper-test parameters.

The book is the one committed in research/trend_following (FINDINGS.md): an equal-weight ensemble of
time-series-momentum signs over 30/60/90/120 daily lookbacks, each position scaled to a per-asset daily
volatility target, the whole book capped by gross leverage, rebalanced once a day. Nothing here is fitted
in production — changing any of it needs a new research pass.

`risk_scale` is the one number that is NOT from the research config: at vol_target 0.02/day and a gross
cap of 3 the book runs at ~150% annual volatility, which is a research scale, not a deployable one.
The paper test runs the same shape at 0.2 of that size (~30% annual vol) — the scale every return in
FINDINGS.md is quoted at. risk_scale 1.0 reproduces the research book exactly.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Liquid HL perps with enough history for a 120-day lookback. The engine drops any coin HL does not
# serve enough candles for, so this list is a wish, not a promise.
DEFAULT_COINS: tuple[str, ...] = (
    "BTC", "ETH", "SOL", "AVAX", "BNB", "XRP", "DOGE", "LINK", "LTC", "ADA",
    "ARB", "OP", "SUI", "APT", "NEAR", "TIA", "INJ", "AAVE", "UNI", "ATOM",
    "FIL", "BCH", "TRX", "SEI", "WLD",
)


@dataclass(frozen=True)
class TrendParams:
    coins: tuple[str, ...] = DEFAULT_COINS
    capital_usd: float = 1000.0
    # Signal: sign of the trailing return over each lookback (days), averaged.
    lookbacks: tuple[int, ...] = (30, 60, 90, 120)
    vol_window: int = 30
    # Sizing: position = signal * vol_target / realised daily vol, book capped at leverage_cap gross,
    # then the whole book scaled by risk_scale.
    vol_target_daily: float = 0.02
    leverage_cap: float = 3.0
    risk_scale: float = 0.2
    # A coin needs this many daily closes before it can be traded (longest lookback + vol window).
    min_history_days: int = 151
    # Execution model (paper): taker fee comes from frab.constants, slippage on top.
    slippage: float = 0.0005
    min_order_usd: float = 10.0
    # Rebalance once a day at this UTC hour, on the daily candles closed by then.
    rebalance_hour_utc: int = 0
    # HL cross margin: maintenance margin = 1 / (2 * max leverage) of each position's notional.
    maint_margin_rate: dict[str, float] = field(
        default_factory=lambda: {"BTC": 0.0125, "ETH": 0.02, "SOL": 0.025})
    default_maint_margin_rate: float = 0.05
    # "paper" is the only mode implemented: no signing key is ever used.
    mode: str = "paper"

    def mmr(self, coin: str) -> float:
        return float(self.maint_margin_rate.get(coin, self.default_maint_margin_rate))

    @property
    def history_days(self) -> int:
        """Daily candles to pull so every lookback and the vol window are warm."""
        return max(self.lookbacks) + self.vol_window + 30

    @classmethod
    def from_dict(cls, d: dict | None) -> "TrendParams":
        d = dict(d or {})
        known = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in d.items() if k in known}
        for key in ("coins", "lookbacks"):
            if key in kw:
                kw[key] = tuple(kw[key])
        if "maint_margin_rate" in kw:
            kw["maint_margin_rate"] = {c: float(v) for c, v in kw["maint_margin_rate"].items()}
        p = cls(**kw)
        if p.mode != "paper":
            raise ValueError(f"trend mode {p.mode!r} is not implemented; only 'paper' is supported")
        if not p.coins:
            raise ValueError("trend needs at least one coin")
        if not p.lookbacks or any(int(lb) <= 0 for lb in p.lookbacks):
            raise ValueError("trend lookbacks must be positive")
        if p.vol_target_daily <= 0 or p.leverage_cap <= 0 or p.risk_scale <= 0:
            raise ValueError("trend vol target, leverage cap and risk scale must be positive")
        if not 0 <= p.rebalance_hour_utc <= 23:
            raise ValueError("trend rebalance hour must be 0..23")
        return p

    def to_dict(self) -> dict:
        d = asdict(self)
        d["coins"] = list(self.coins)
        d["lookbacks"] = list(self.lookbacks)
        return d
