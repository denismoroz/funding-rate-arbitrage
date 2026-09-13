"""Strategy B v2 parameters.

B v2 = spot long + trend-timed perp hedge + funding carry on the idle cash reserve,
selected and validated in research/strategy_b_v2 (train/test/forward). Defaults ARE the
validated configuration; change them only with a new forward test.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class B2Params:
    coins: tuple[str, ...] = ("BTC", "ETH", "SOL", "AVAX")
    # Total paper capital split equally across coin books.
    capital_usd: float = 250.0
    # Share of each coin book held as spot; the rest is a cash reserve.
    spot_share: float = 0.5
    # Hedge ON unless both 14d and 30d returns exceed this threshold.
    hedge_threshold: float = 0.0
    # Exit the hedge only after the signal has been OFF this many hours in a row.
    sticky_exit_hours: int = 12
    # Sell spot back to its target when it grows above target * (1 + threshold).
    ratchet_threshold: float = 0.50
    # Funding carry on the idle reserve.
    carry_enabled: bool = True
    carry_fraction: float = 0.6
    carry_entry_apr: float = 0.10
    carry_exit_hours: int = 24
    # Execution model.
    slippage: float = 0.0005
    min_order_usd: float = 10.0
    # "paper" is the only mode implemented: no signing key is ever used.
    mode: str = "paper"

    @property
    def book_capital(self) -> float:
        return self.capital_usd / len(self.coins)

    @classmethod
    def from_dict(cls, d: dict | None) -> "B2Params":
        d = dict(d or {})
        known = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in d.items() if k in known}
        if "coins" in kw:
            kw["coins"] = tuple(kw["coins"])
        p = cls(**kw)
        if p.mode != "paper":
            raise ValueError(f"B2 mode {p.mode!r} is not implemented; only 'paper' is supported")
        if not p.coins:
            raise ValueError("B2 needs at least one coin")
        return p

    def to_dict(self) -> dict:
        d = asdict(self)
        d["coins"] = list(self.coins)
        return d
