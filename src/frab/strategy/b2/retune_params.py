"""Parameters of the monthly re-tuned Bv2 book (`b2_retune`).

Counterpart of q-lab's spec `bv2-research-headroom-retune` (specs/research/bv2/): the Bv2 book of
`B2Params` is run in two candidates that differ only in `hedge_margin_headroom` (1.0 = production `b2`,
1.5 = production `b2_cold`), and once a month code picks the one with the better past Sharpe.

The JSON stored in `strategies.params_json` is flat: every `B2Params` key at the top level (capital, coins,
margin model, ...) plus the `retune_*` keys below. `B2Params.from_dict` ignores the `retune_*` keys, so the
plain B2 routes can read the same row.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from frab.strategy.b2.engine import HISTORY_BARS, HOUR_MS, WARMUP_BARS
from frab.strategy.b2.params import B2Params

RETUNE_KEYS = ("retune_candidates", "retune_history_start", "retune_coin_starts", "retune_refit_every",
               "retune_history_seed_path", "retune_history_cache_dir", "retune_candle_pause_s",
               "retune_funding_page_pause_s")

# Hours of closes a coin's book needs before its first bar (engine.py: HISTORY_BARS + WARMUP_BARS).
MIN_HISTORY_BARS = HISTORY_BARS + WARMUP_BARS

DEFAULT_CANDIDATES: dict[str, dict] = {
    "b2": {"hedge_margin_headroom": 1.0},
    "b2_cold": {"hedge_margin_headroom": 1.5},
}


def iso_to_ms(text: str) -> int:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class B2RetuneParams:
    base: B2Params
    # Candidate name -> B2Params overrides, in grid order (ties go to the earlier one, as in q-lab).
    candidates: dict[str, dict] = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_CANDIDATES.items()})
    # The first hour of the history the books are replayed from (the open of its first candle). It is the
    # first bar of the q-lab stand's panel: the books start `MIN_HISTORY_BARS` hours later at the earliest.
    history_start: str = "2026-03-08T02:00:00Z"
    # Coin -> the hour its book starts. For a coin that has existed since the history start this is
    # `history_start + MIN_HISTORY_BARS` hours; AVAX has its own date because its spot token was listed later
    # (q-lab panel: UAVAX tradeable from 2026-05-06 14:00). Frab has no spot candles to find this out itself.
    coin_starts: dict[str, str] = field(default_factory=dict)
    refit_every: str = "M"
    # Hourly history shipped with the repo (the part Hyperliquid no longer serves) and the cache that is
    # extended from it. The cache is NEVER purged by age: hours older than ~5000 cannot be fetched again.
    history_seed_path: str = "deploy/b2_retune_history_seed.json"
    history_cache_dir: str = "data/b2_retune_cache"
    candle_pause_s: float = 2.0
    funding_page_pause_s: float = 2.7

    # ── derived ──────────────────────────────────────────────────────────
    @property
    def coins(self) -> tuple[str, ...]:
        return self.base.coins

    @property
    def history_start_ms(self) -> int:
        return iso_to_ms(self.history_start)

    def start_ms(self, coin: str) -> int:
        return iso_to_ms(self.coin_starts[coin])

    def candidate_params(self, name: str) -> B2Params:
        return replace(self.base, **self.candidates[name])

    def fingerprint(self) -> dict:
        """What a persisted state must have been made with; a different value means the state is stale."""
        return {"candidates": {k: dict(v) for k, v in self.candidates.items()},
                "base": self.base.to_dict(), "history_start": self.history_start,
                "coin_starts": dict(self.coin_starts)}

    # ── (de)serialisation ────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, d: dict | None) -> "B2RetuneParams":
        d = dict(d or {})
        extra = {k.removeprefix("retune_"): d[k] for k in RETUNE_KEYS if k in d}
        base = B2Params.from_dict(d)
        if "candidates" in extra:
            extra["candidates"] = {str(k): dict(v) for k, v in extra["candidates"].items()}
        if "coin_starts" in extra:
            extra["coin_starts"] = {str(k): str(v) for k, v in extra["coin_starts"].items()}
        p = cls(base=base, **extra)
        p.validate()
        return p

    def validate(self) -> None:
        fields = set(B2Params.__dataclass_fields__)
        if len(self.candidates) < 2:
            raise ValueError("retune needs at least two candidates to choose between")
        for name, overrides in self.candidates.items():
            bad = set(overrides) - fields
            if bad:
                raise ValueError(f"candidate {name!r} overrides unknown B2Params keys {sorted(bad)}")
            self.candidate_params(name)                       # B2Params-level validation is not repeated here
        if self.refit_every != "M":
            raise ValueError("only monthly re-tuning ('M') is implemented")
        if self.base.mode != "paper":
            raise ValueError("b2_retune is paper only")
        try:
            origin = self.history_start_ms
        except ValueError as exc:
            raise ValueError(f"history_start must be an ISO timestamp, got {self.history_start!r}") from exc
        if origin % HOUR_MS:
            raise ValueError("history_start must be a whole hour")
        if set(self.coin_starts) != set(self.coins):
            raise ValueError(f"coin_starts must name exactly the coins {list(self.coins)}")
        for coin in self.coins:
            start = self.start_ms(coin)
            if start % HOUR_MS:
                raise ValueError(f"coin_starts[{coin}] must be a whole hour")
            if start < origin + MIN_HISTORY_BARS * HOUR_MS:
                raise ValueError(f"coin_starts[{coin}] leaves fewer than {MIN_HISTORY_BARS} hours of history "
                                 f"before the book starts (the live engine loads HISTORY_BARS + WARMUP_BARS)")
        if self.candle_pause_s < 0 or self.funding_page_pause_s < 0:
            raise ValueError("pauses must be >= 0")

    def to_dict(self) -> dict:
        d = self.base.to_dict()
        d.update(retune_candidates={k: dict(v) for k, v in self.candidates.items()},
                 retune_history_start=self.history_start, retune_coin_starts=dict(self.coin_starts),
                 retune_refit_every=self.refit_every, retune_history_seed_path=self.history_seed_path,
                 retune_history_cache_dir=self.history_cache_dir, retune_candle_pause_s=self.candle_pause_s,
                 retune_funding_page_pause_s=self.funding_page_pause_s)
        return d
