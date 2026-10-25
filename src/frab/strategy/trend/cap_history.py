"""Weekly market-cap rankings of the PAST, for the backfill of the re-tuned trend variant (T52b).

The file `deploy/trend_retune_cap_history.json` is written by q-lab (`scripts/export_cap_history.py`) from
its stored CoinMarketCap snapshots, with the mapping and the stablecoin exclusion of the validated
`trend-retune-weekly` run: per snapshot date a ranked list `[hl_symbol, market_cap, cmc_rank]`, top 80.

Visibility rule (q-lab `coinmarketcap.as_of_frame`): a snapshot dated D describes the market at D 00:00 UTC
and is visible from the first bar STRICTLY AFTER D. A bar is labelled by its candle's OPEN time (see
`coin_selector.week_key`), so the bar decided at `decision_ms` is labelled `decision_ms - 1 day`, and the
snapshot it may use is the latest one dated before that label. With Sunday snapshots this is the Monday-
labelled bar (the Tuesday 00:00 decision) -- the first bar of an ISO week -- which is why a snapshot serves
exactly one ISO week.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from frab.strategy.trend.coin_selector import DAY_MS, week_key

SOURCE = "cmc_history"
# <repo>/src/frab/strategy/trend/cap_history.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PATH = REPO_ROOT / "deploy" / "trend_retune_cap_history.json"


@dataclass(frozen=True)
class CapSnapshot:
    date: str                       # ISO date of the snapshot (UTC midnight)
    date_ms: int
    ranked: tuple[tuple, ...]       # (hl_symbol, market_cap, cmc_rank), largest first


class CapHistory:
    """Snapshots sorted by date; `visible(decision_ms)` is the only way the backfill reads them."""

    def __init__(self, snapshots: list[CapSnapshot], *, path: str | None = None) -> None:
        if not snapshots:
            raise ValueError("cap history holds no snapshots")
        self.snapshots = sorted(snapshots, key=lambda s: s.date_ms)
        self.path = path
        self._dicts: dict[tuple[str, str], dict] = {}

    @classmethod
    def from_dict(cls, doc: dict, *, path: str | None = None) -> "CapHistory":
        if doc.get("version") != 1:
            raise ValueError(f"unsupported cap history version {doc.get('version')!r}")
        snaps = []
        for s in doc.get("snapshots") or []:
            day = datetime.fromisoformat(s["date"]).replace(tzinfo=timezone.utc)
            snaps.append(CapSnapshot(date=s["date"], date_ms=int(day.timestamp() * 1000),
                                     ranked=tuple((r[0], float(r[1]), r[2]) for r in s["ranked"])))
        return cls(snaps, path=path)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "CapHistory":
        p = Path(path) if path else DEFAULT_PATH
        if not p.is_absolute():
            p = REPO_ROOT / p
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")), path=str(p))

    @property
    def first_date(self) -> str:
        return self.snapshots[0].date

    @property
    def last_date(self) -> str:
        return self.snapshots[-1].date

    def visible(self, decision_ms: int) -> CapSnapshot | None:
        """The latest snapshot dated strictly before the label of the bar decided at `decision_ms`."""
        label_ms = decision_ms - DAY_MS
        best = None
        for s in self.snapshots:                  # ~100 entries; a linear scan keeps the rule obvious
            if s.date_ms < label_ms:
                best = s
            else:
                break
        return best

    def snapshot_for_bar(self, decision_ms: int) -> dict | None:
        """The same dict shape as `coin_selector.build_cap_snapshot`, with `source = cmc_history`."""
        snap = self.visible(decision_ms)
        if snap is None:
            return None
        week = week_key(decision_ms)
        key = (snap.date, week)
        if key not in self._dicts:
            self._dicts[key] = {"source": SOURCE, "taken_ms": snap.date_ms, "snapshot_date": snap.date,
                                "week": week, "ranked": [list(r) for r in snap.ranked]}
        return self._dicts[key]
