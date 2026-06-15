"""XsmomScanAction — computes ensemble momentum scores and records an XsmomScan row.

Display-only: no positions are created or modified. Caller may optionally pass
``coins`` as tuple/list (default: params.universe). History refresh is a SEPARATE
explicit step that the orchestrator runs before calling scan.

Return value is a summary dict with ``scores`` and ``ranking`` so the rebalance
evaluator can reuse them without a redundant recompute.
"""
from __future__ import annotations

import logging

from frab.repo.xsmom_repo import XsmomRepo
from frab.strategy.xsmom.evaluators.signal import compute_scores
from frab.strategy.xsmom.params import XsmomParams

logger = logging.getLogger(__name__)


class XsmomScanAction:
    """Reads closes from the DB, computes ensemble scores, records an XsmomScan row."""

    def __init__(
        self,
        *,
        strategy_id: int,
        xsmom_repo: XsmomRepo,
        params: XsmomParams,
    ) -> None:
        self._strategy_id = strategy_id
        self._repo = xsmom_repo
        self._params = params

    async def scan(self, *, now_ms: int) -> dict:
        """Compute momentum scores, build ranking, record scan row.

        Returns::

            {
              "scores":  {coin: float, ...},
              "k":       int,
              "ranking": [{"coin": ..., "score": ..., "rank": i, "leg": "long"|"short"|null}, ...],
              "n_long":  int,
              "n_short": int,
              "scan_id": int,
            }

        The caller (orchestrator) must call ``XsmomHistoryRefresh.refresh`` BEFORE
        invoking this method so the DB has up-to-date data.
        """
        # ── 1. Load closes from DB ────────────────────────────────────────────
        closes = await self._repo.get_daily_closes(list(self._params.universe))

        # ── 2. Compute ensemble scores ────────────────────────────────────────
        scores: dict[str, float] = compute_scores(closes, self._params.lookbacks)

        # ── 3. Build ranking (desc by score) ─────────────────────────────────
        sorted_coins = sorted(scores.keys(), key=lambda c: scores[c], reverse=True)
        universe_len = len(self._params.universe)
        k = self._params.compute_k(universe_len)

        ranking: list[dict] = []
        for i, coin in enumerate(sorted_coins):
            if i < k:
                leg: str | None = "long"
            elif i >= len(sorted_coins) - k:
                leg = "short"
            else:
                leg = None
            ranking.append({"coin": coin, "score": scores[coin], "rank": i, "leg": leg})

        # ── 4. Record scan ────────────────────────────────────────────────────
        note = f"universe={universe_len} k={k} scored={len(scores)}"
        scan_id = await self._repo.record_scan(
            strategy_id=self._strategy_id,
            ts_ms=now_ms,
            ranking=ranking,
            n_long=k,
            n_short=k,
            note=note,
        )

        logger.info(
            "xsmom scan recorded strategy_id=%s k=%d scored=%d scan_id=%d",
            self._strategy_id, k, len(scores), scan_id,
        )

        return {
            "scores": scores,
            "k": k,
            "ranking": ranking,
            "n_long": k,
            "n_short": k,
            "scan_id": scan_id,
        }
