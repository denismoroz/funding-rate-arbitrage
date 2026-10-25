"""Parameters of the weekly re-tuned trend variant (`trend_retune`).

This mirrors q-lab's `trend-retune-weekly` spec (q-lab: specs/trend-retune-weekly.yaml, validated 2026-10-02):
the trend book of `params.TrendParams` is unchanged, but the coin list is not hand-written. Ten candidates
are simulated every day from the strategy's own history: 2 coin-selection rules (top-K by market cap
excluding stablecoins / top-K by traded volume) x 5 lookback sets (the 30/60/90/120 ensemble and each
lookback alone). Once a week the candidate with the best Sharpe after costs on the returns it realised
BEFORE that day is the one the paper book trades, until the next re-tune.

The JSON stored in `strategies.params_json` is flat: every TrendParams key at the top level (sizing,
capital, ...) plus the keys below. `coins` is ignored (the universe is chosen by code); `lookbacks` is
ignored too (the candidates carry their own lookback sets).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from frab.strategy.trend.params import TrendParams

# Same names and order as q-lab's grid (expand_grid takes the product axis by axis, the first axis is the
# outer loop), because ties are broken by candidate order.
UNIVERSE_RULES: tuple[str, ...] = ("cap", "volume")
DEFAULT_LOOKBACK_SETS: dict[str, tuple[int, ...]] = {
    "ens": (30, 60, 90, 120),
    "l30": (30,),
    "l60": (60,),
    "l90": (90,),
    "l120": (120,),
}
RETUNE_KEYS = ("top_k_by_market_cap", "top_k_by_volume", "top_k_requires_history", "lookback_sets",
               "universe_rules", "score_taker_fee_bps", "score_slippage_bps", "objective",
               "market_cap_rank_limit", "selection_max_age_hours", "coingecko_base_url",
               "daily_fetch_pause_s", "backfill", "backfill_start", "cap_history_path", "backfill_cache_dir",
               "backfill_candle_pause_s", "backfill_funding_page_pause_s", "cap_history_max_age_days")


@dataclass(frozen=True)
class RetuneParams:
    trend: TrendParams
    top_k_by_market_cap: int = 25
    top_k_by_volume: int = 25
    # Rank only coins that already hold `trend.min_history_days` daily closes (q-lab: top_k_requires_history).
    top_k_requires_history: bool = True
    lookback_sets: dict[str, tuple[int, ...]] = field(default_factory=lambda: dict(DEFAULT_LOOKBACK_SETS))
    universe_rules: tuple[str, ...] = UNIVERSE_RULES
    # Costs used to SCORE candidates (q-lab spec score_costs = the spec's costs: taker 3.5 + slippage 0.9 bps).
    # They are not the paper book's own execution costs (TrendParams.slippage).
    score_taker_fee_bps: float = 3.5
    score_slippage_bps: float = 0.9
    objective: str = "sharpe_net"
    # q-lab's snapshots hold the 200 largest coins; coins ranked lower are not ranked at all.
    market_cap_rank_limit: int = 200
    # A selection snapshot (market caps) is only point-in-time for a bar at most this old; older bars replayed
    # during a catch-up reuse the last known one instead of today's.
    selection_max_age_hours: float = 12.0
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    # Pause between the daily-candle requests of the whole HL universe (~230 coins, once a day).
    daily_fetch_pause_s: float = 0.6
    # ── backfill of the candidates' past (T52b) ──────────────────────────
    # A new book with no history rebuilds each candidate's daily returns from `backfill_start` (q-lab's spec
    # window start) with point-in-time inputs only, so its first choice is the one the history implies instead
    # of 2-3 weeks away. Off by default for a library user; the deploy params files switch it on.
    backfill: bool = False
    backfill_start: str = "2025-01-01"                  # the first replayed bar is the candle of this date
    cap_history_path: str = "deploy/trend_retune_cap_history.json"      # relative to the repository root
    backfill_cache_dir: str = "data/trend_retune_cache"                 # fetched history survives a restart
    # HL's info limit is 1200 weight/min per IP: a ~660-candle daily request weighs ~31, a 500-record funding
    # page ~45. These pauses keep the backfill near 60-75% of it, leaving room for the other engines.
    backfill_candle_pause_s: float = 2.0
    backfill_funding_page_pause_s: float = 2.7
    # The ranking in use is the latest snapshot dated before the bar; warn when the file is older than this.
    cap_history_max_age_days: float = 21.0

    @property
    def backfill_start_ms(self) -> int:
        return int(datetime.fromisoformat(self.backfill_start).replace(tzinfo=timezone.utc).timestamp() * 1000)

    @property
    def cost_rate(self) -> float:
        return (self.score_taker_fee_bps + self.score_slippage_bps) / 1e4

    def candidates(self) -> list[tuple[str, str, tuple[int, ...]]]:
        """[(name, universe rule, lookbacks)] in q-lab's grid order: rule outer, lookback set inner."""
        return [(f"{u}/{name}", u, tuple(lb)) for u in self.universe_rules
                for name, lb in self.lookback_sets.items()]

    def top_k(self, rule: str) -> int:
        return self.top_k_by_market_cap if rule == "cap" else self.top_k_by_volume

    def candidate_trend(self, lookbacks: tuple[int, ...], coins: tuple[str, ...]) -> TrendParams:
        return replace(self.trend, lookbacks=lookbacks, coins=coins)

    @classmethod
    def from_dict(cls, d: dict | None) -> "RetuneParams":
        d = dict(d or {})
        extra = {k: d.pop(k) for k in RETUNE_KEYS if k in d}
        d["coins"] = ["BTC"]                       # placeholder, replaced per candidate and per day
        d.setdefault("lookbacks", [30, 60, 90, 120])
        trend = TrendParams.from_dict(d)
        if "lookback_sets" in extra:
            extra["lookback_sets"] = {str(k): tuple(int(x) for x in v) for k, v in extra["lookback_sets"].items()}
        if "universe_rules" in extra:
            extra["universe_rules"] = tuple(extra["universe_rules"])
        p = cls(trend=trend, **extra)
        if not p.lookback_sets or any(not lb or any(x <= 0 for x in lb) for lb in p.lookback_sets.values()):
            raise ValueError("retune lookback sets must be non-empty and positive")
        if set(p.universe_rules) - set(UNIVERSE_RULES) or not p.universe_rules:
            raise ValueError(f"retune universe rules must be a non-empty subset of {UNIVERSE_RULES}")
        if len(p.candidates()) < 2:
            raise ValueError("retune needs at least two candidates to choose between")
        if p.top_k_by_market_cap <= 0 or p.top_k_by_volume <= 0:
            raise ValueError("retune top-K must be positive")
        if p.objective != "sharpe_net":
            raise ValueError("only the 'sharpe_net' objective is implemented")
        if p.score_taker_fee_bps < 0 or p.score_slippage_bps < 0:
            raise ValueError("retune score costs must be >= 0")
        try:
            p.backfill_start_ms
        except ValueError as exc:
            raise ValueError(f"backfill_start must be an ISO date, got {p.backfill_start!r}") from exc
        if p.backfill_candle_pause_s < 0 or p.backfill_funding_page_pause_s < 0:
            raise ValueError("backfill pauses must be >= 0")
        max_lb = max(max(lb) for lb in p.lookback_sets.values())
        if p.trend.min_history_days < max_lb + 1:
            raise ValueError("min_history_days must exceed the longest lookback")
        return p

    def to_dict(self) -> dict:
        d = self.trend.to_dict()
        d.pop("coins", None)
        d.pop("lookbacks", None)
        d.update(top_k_by_market_cap=self.top_k_by_market_cap, top_k_by_volume=self.top_k_by_volume,
                 top_k_requires_history=self.top_k_requires_history,
                 lookback_sets={k: list(v) for k, v in self.lookback_sets.items()},
                 universe_rules=list(self.universe_rules),
                 score_taker_fee_bps=self.score_taker_fee_bps, score_slippage_bps=self.score_slippage_bps,
                 objective=self.objective, market_cap_rank_limit=self.market_cap_rank_limit,
                 selection_max_age_hours=self.selection_max_age_hours,
                 coingecko_base_url=self.coingecko_base_url, daily_fetch_pause_s=self.daily_fetch_pause_s,
                 backfill=self.backfill, backfill_start=self.backfill_start,
                 cap_history_path=self.cap_history_path, backfill_cache_dir=self.backfill_cache_dir,
                 backfill_candle_pause_s=self.backfill_candle_pause_s,
                 backfill_funding_page_pause_s=self.backfill_funding_page_pause_s,
                 cap_history_max_age_days=self.cap_history_max_age_days)
        return d
