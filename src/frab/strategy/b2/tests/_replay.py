"""Replay of a stored hourly history through a B2 coin book, reduced to a digest.

Used by the parity tests: the digest of every bar's fills, book state and equity is compared
with one recorded from the book as it was before the margin pool and delayed top-ups were
added (`fixtures/golden_book_digests.json`). Equal digests mean the book, with the new params
at their defaults, is bit-for-bit the book that runs in production.

The loop is the engine's own (`engine.B2PaperEngine._advance_coin`): signals warmed up over
`WARMUP_BARS` bars, the book started at the close of the first bar with a full history, then
stepped bar by bar on the last `HISTORY_BARS - 9` closes and 9 funding rates.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path

from frab.strategy.b2.book import CoinBook, advance_signals, start_book, step
from frab.strategy.b2.params import B2Params

FIXTURES = Path(__file__).parent / "fixtures"
HISTORY_BARS = 740
WARMUP_BARS = 48
HOUR_MS = 3_600_000


def load_history(coin: str) -> tuple[list[float], list[float], list[float]]:
    """(closes, highs, hourly funding rates) of a stored fixture."""
    closes, highs, rates = [], [], []
    with gzip.open(FIXTURES / f"{coin}_1h_8000.csv.gz", "rt") as fh:
        next(fh)
        for line in fh:
            _, c, h, r = line.strip().split(",")
            closes.append(float(c))
            highs.append(float(h))
            rates.append(float(r))
    return closes, highs, rates


def replay(coin: str, params: B2Params, data: tuple[list[float], list[float], list[float]],
           *, first: int = HISTORY_BARS + WARMUP_BARS, last: int | None = None) -> dict:
    """Step a fresh book over `data` from bar `first`; return its digest and event counts."""
    closes, highs, rates = data
    last = len(closes) if last is None else last
    book = CoinBook.new(coin, params)
    for k in range(max(0, first - WARMUP_BARS), first):
        advance_signals(book, closes[max(0, k - HISTORY_BARS + 9):k + 1], rates[max(0, k - 8):k + 1], params)
    digest = hashlib.sha256()
    kinds: Counter = Counter()
    for e in start_book(book, bar_ms=first * HOUR_MS, price=closes[first], params=params):
        kinds[e["kind"]] += 1
        digest.update(json.dumps(e, sort_keys=True).encode())
    for i in range(first, last):
        events = step(book, bar_ms=i * HOUR_MS, price=closes[i], funding_rate=rates[i],
                      closes=closes[max(0, i - HISTORY_BARS + 9):i + 1],
                      funding_hist=rates[max(0, i - 8):i + 1], params=params, high=highs[i])
        for e in events:
            kinds[e["kind"]] += 1
        digest.update(json.dumps([events, book.to_state(), book.equity(closes[i])], sort_keys=True).encode())
    return {"digest": digest.hexdigest(), "kinds": dict(sorted(kinds.items())),
            "equity": book.equity(closes[last - 1]), "bars": last - first}


# Parameter sets the parity test replays. `prod_b2` and `cold_b2` are the production books'
# own settings (server.py: capital 257 over four coins, carry off, headroom 1.0 / 1.5); the
# others reach the paths those never take: the research sizing without margin, the carry
# sleeve with margin, and a stressed pool whose shorts are topped up and liquidated.
VARIANTS: dict[str, dict] = {
    "prod_b2": dict(carry_enabled=False),
    "cold_b2": dict(carry_enabled=False, hedge_margin_headroom=1.5),
    "research": dict(margin_enabled=False, carry_enabled=True),
    "carry_margin": dict(carry_enabled=True),
    "stress": dict(carry_enabled=False, hedge_margin_headroom=0.3, default_leverage=8.0,
                   short_leverage={"BTC": 8.0, "SOL": 8.0, "DOGE": 8.0},
                   maint_margin_rate={"BTC": 0.0125, "SOL": 0.025, "DOGE": 0.05}),
    "stress_no_sticky": dict(carry_enabled=False, hedge_margin_headroom=0.1, sticky_exit_hours=0,
                             rebalance_at_im_share=0.8, default_leverage=12.0,
                             short_leverage={"BTC": 12.0, "SOL": 12.0, "DOGE": 12.0},
                             maint_margin_rate={"BTC": 0.0125, "SOL": 0.025, "DOGE": 0.05}),
}


def variant_params(coin: str, name: str) -> B2Params:
    return B2Params(coins=(coin,), capital_usd=64.25, **VARIANTS[name])
