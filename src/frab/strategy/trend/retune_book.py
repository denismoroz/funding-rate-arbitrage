"""The paper book of the re-tuned trend variant: a TrendBook plus the selector/re-tune state.

The state rides in the same `trend_books.state_json` row as the book, so a tick saves both in ONE
transaction (TrendRepo.save_progress): the book can never be ahead of the candidate histories it was
sized from. No new table, no migration.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from frab.strategy.trend.book import TrendBook


@dataclass
class RetuneBook(TrendBook):
    retune: dict = field(default_factory=dict)       # RetuneState.to_dict()
