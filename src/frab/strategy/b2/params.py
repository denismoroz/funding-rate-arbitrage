"""Strategy B v2 parameters.

B v2 = spot long + trend-timed perp hedge + funding carry on the idle cash reserve,
selected and validated in research/strategy_b_v2 (train/test/forward). Defaults ARE the
validated configuration; change them only with a new forward test.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


# Params added later, with the value that means "the behaviour that existed before them".
OPTIONAL_PARAMS: dict[str, object] = {"shared_margin": False, "topup_delay_hours": 0,
                                       "short_window_days": 14, "long_window_days": 30,
                                       "partial_hedge_share": 1.0, "graded_hedge_depth": 0.0}


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
    # Margin model. Each coin book is its own HL cross-margin pool; spot is NOT counted as
    # collateral (the hedge spot may sit in a cold wallet). Books are sized so that spot +
    # carry spot + both shorts' initial margin + buffer fit the book capital.
    # False reproduces the research simulator exactly (margin treated as free).
    margin_enabled: bool = True
    short_leverage: dict[str, float] = field(
        default_factory=lambda: {"BTC": 3.0, "ETH": 2.0, "SOL": 1.5, "AVAX": 1.5})
    default_leverage: float = 1.0
    # HL maintenance margin = 1 / (2 * max leverage): BTC 40x, ETH 25x, SOL 20x, AVAX 10x.
    maint_margin_rate: dict[str, float] = field(
        default_factory=lambda: {"BTC": 0.0125, "ETH": 0.02, "SOL": 0.025, "AVAX": 0.05})
    default_maint_margin_rate: float = 0.05
    # Extra USDC per book as a share of the spot target: fees and top-ups.
    margin_buffer: float = 0.10
    # Hedge margin is reserved for spot this many times its target. The ratchet lets unhedged
    # spot grow to (1 + ratchet_threshold) x target before a hedge has to cover it; without a
    # carry to cut, a 1.0 pool leaves such hedges partial.
    hedge_margin_headroom: float = 1.0
    # Top up when the shorts' losses leave less than this share of initial margin:
    # sell spot worth the loss and cut the short by the same units (stays delta neutral).
    rebalance_at_im_share: float = 0.5
    # "paper" is the only mode implemented: no signing key is ever used.
    mode: str = "paper"
    # One HL cross-margin account for ALL coin books instead of one per book: the shorts are
    # backed by the sum of the books' USDC, free margin and the liquidation test are the pool's
    # (`book.MarginPool`). Needs margin_enabled; the carry sleeve is not modelled in a pool.
    # Default False: every book is its own pool, as the production books run.
    shared_margin: bool = False
    # Hours between the moment a top-up is due (account value below `rebalance_at_im_share` of
    # initial margin) and the moment it takes effect -- the coins' trip from a cold wallet to HL.
    # The shorts keep losing meanwhile and can be liquidated. 0 = at once, as the books run today.
    topup_delay_hours: int = 0
    # The hedge signal's two momentum windows, in days: hedge unless the price rose over both.
    # 14 and 30 are the validated values; the short one also confirms a refill (trend up).
    # At most 30 days: the engine keeps HISTORY_BARS (740) hourly closes.
    short_window_days: int = 14
    long_window_days: int = 30
    # How much of the spot a MIXED signal hedges (one window up, the other down -- a market
    # going sideways); both down always hedges in full. 1.0 = in full, as the books run today.
    # Below 1 the hedge is resized every bar the wanted share changes (sideways <-> down).
    partial_hedge_share: float = 1.0
    # Hedge by the strength of the signal: each window's strength is how far its return is below
    # `hedge_threshold`, as a share of this depth (capped at 1); the hedged share of the spot is
    # the mean of the two, so the whole spot is hedged once both returns are down by the depth
    # and nothing while both are at or above the threshold. One window down is enough to open a
    # partial hedge (it does not wait for both, unlike the on/off trigger). Resized every bar like a partial
    # hedge. 0 = off: the hedge is all or nothing, as the books run today. Excludes
    # partial_hedge_share.
    graded_hedge_depth: float = 0.0

    @property
    def book_capital(self) -> float:
        return self.capital_usd / len(self.coins)

    def leverage(self, coin: str) -> float:
        return float(self.short_leverage.get(coin, self.default_leverage))

    def mmr(self, coin: str) -> float:
        return float(self.maint_margin_rate.get(coin, self.default_maint_margin_rate))

    def book_sizes(self, coin: str) -> tuple[float, float, float]:
        """(spot target, cash reserve, carry notional) for one coin book.

        Research sizing: spot = spot_share of capital, carry = carry_fraction of the reserve.
        Margin sizing keeps the research ratio carry = carry_fraction x spot and solves
        capital = spot + carry spot + carry/L + headroom x spot/L + buffer x spot for the spot target;
        the reserve is everything that must stay on HL as USDC.
        """
        cap = self.book_capital
        if not self.margin_enabled:
            spot = cap * self.spot_share
            return spot, cap - spot, self.carry_fraction * (cap - spot)
        lev = self.leverage(coin)
        k = self.carry_fraction if self.carry_enabled else 0.0
        spot = cap / (1.0 + k + k / lev + self.hedge_margin_headroom / lev + self.margin_buffer)
        return spot, cap - spot, k * spot

    @classmethod
    def from_dict(cls, d: dict | None) -> "B2Params":
        d = dict(d or {})
        known = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in d.items() if k in known}
        if "coins" in kw:
            kw["coins"] = tuple(kw["coins"])
        for key in ("short_leverage", "maint_margin_rate"):
            if key in kw:
                kw[key] = {c: float(v) for c, v in kw[key].items()}
        p = cls(**kw)
        if p.mode != "paper":
            raise ValueError(f"B2 mode {p.mode!r} is not implemented; only 'paper' is supported")
        if not p.coins:
            raise ValueError("B2 needs at least one coin")
        if any(p.leverage(c) <= 0 for c in p.coins):
            raise ValueError("B2 short leverage must be positive")
        if p.topup_delay_hours < 0 or int(p.topup_delay_hours) != p.topup_delay_hours:
            raise ValueError("B2 topup_delay_hours must be a whole number of hours, 0 or more")
        if p.topup_delay_hours and not p.margin_enabled:
            raise ValueError("B2 topup_delay_hours needs margin_enabled: without margin there is no top-up")
        if p.shared_margin and not p.margin_enabled:
            raise ValueError("B2 shared_margin needs margin_enabled")
        for name in ("short_window_days", "long_window_days"):
            value = getattr(p, name)
            if int(value) != value or not 1 <= value <= 30:
                raise ValueError(f"B2 {name} must be a whole number of days from 1 to 30")
        if p.short_window_days > p.long_window_days:
            raise ValueError("B2 short_window_days must not exceed long_window_days")
        if not 0.0 < p.partial_hedge_share <= 1.0:
            raise ValueError("B2 partial_hedge_share must be above 0 and at most 1")
        if not p.graded_hedge_depth >= 0.0 or p.graded_hedge_depth == float("inf"):
            raise ValueError("B2 graded_hedge_depth must be a finite number, 0 (off) or more")
        if p.graded_hedge_depth > 0.0 and p.partial_hedge_share < 1.0:
            raise ValueError("B2 graded_hedge_depth and partial_hedge_share are alternatives: set one")
        if p.shared_margin and p.carry_enabled:
            raise ValueError("B2 shared_margin does not model the carry sleeve: set carry_enabled false")
        return p

    def to_dict(self) -> dict:
        d = asdict(self)
        d["coins"] = list(self.coins)
        # Params added after the production books were created are written only when they are
        # set, so a book on the defaults stores exactly the params it stored before they existed.
        for key, default in OPTIONAL_PARAMS.items():
            if d[key] == default:
                del d[key]
        return d
