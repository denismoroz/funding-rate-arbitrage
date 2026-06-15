"""XsmomRebalance — reconciles the current position book to the target book.

The reconcile logic is the heart of Phase D:
  KEEP  — coin in target AND held with the same side → do nothing, no churn.
  ADD   — coin in target, not held (or held with opposite side after flip out) → create NEW row.
  DROP  — coin held (OPENED) but not in target → transition OPENED→CLOSE.
  FLIP  — coin held (OPENED) with the OPPOSITE side from target → DROP (transition to CLOSE)
          AND ADD (create a NEW row for the target side). Flip = DROP + ADD.

In-flight NEW positions for a target coin count as already-held and are NOT duplicated.

Reconcile does NOT call exchange.open/close_position directly. It only:
  - Creates XsmomPosition rows in NEW state (state machine drives them to OPENED).
  - Transitions OPENED rows to CLOSE state (state machine drives them to CLOSED).

This keeps reconcile fast + idempotent and fully reuses the tested state machine.

Scheduling helper
-----------------
``is_rebalance_due(now_ms, last_rebalance_ms, params)`` is a pure UTC function.
Due if: (a) never rebalanced, OR (b) >= rebalance_days since last AND now_dow == anchor_dow.

last_rebalance_ms storage
-------------------------
Stored in Strategy.params_json["last_rebalance_ms"] (int ms). Read and written by the
orchestrator (strategy.py) via session_scope on the StrategyRow. This avoids adding a
dedicated column — params_json is already a mutable JSON field on the Strategy model.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from frab.db.models import Strategy as StrategyRow
from frab.db.session import session_scope
from frab.domain import Side, XsmomState
from frab.exchanges.protocol import Exchange
from frab.repo.xsmom_repo import XsmomRepo, XsmomStateConflict
from frab.strategy.two_phase.states._helpers import publish_event
from frab.strategy.xsmom.evaluators.signal import compute_scores
from frab.strategy.xsmom.params import XsmomParams

logger = logging.getLogger(__name__)

_DAY_MS = 86_400_000


# ── Scheduling helper (pure, UTC) ─────────────────────────────────────────────

def is_rebalance_due(
    now_ms: int,
    last_rebalance_ms: int | None,
    params: XsmomParams,
) -> bool:
    """Return True if a rebalance should run now.

    Rules:
    1. Never rebalanced (last_rebalance_ms is None) → always due.
    2. Days since last rebalance < params.rebalance_days → not due.
    3. Days since last >= rebalance_days AND today's weekday == anchor_dow → due.
       (anchor_dow=3 → Thursday in Python's isoweekday/weekday; we use weekday()
       where Monday=0, so anchor_dow=3 → Thursday.)

    The anchor-dow gate prevents spurious re-fires on the same day: once reconcile
    runs on Thursday, last_rebalance_ms is updated, so rule 2 suppresses any
    subsequent Thursday hour ticks within the same week.
    """
    if last_rebalance_ms is None:
        return True
    elapsed_days = (now_ms - last_rebalance_ms) / _DAY_MS
    if elapsed_days < params.rebalance_days:
        return False
    # On or after the next anchor day
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    return now_dt.weekday() == params.anchor_dow


# ── Reconcile ─────────────────────────────────────────────────────────────────

class XsmomRebalance:
    """Reconcile the live book to the target momentum book.

    Constructor args: exchange, xsmom_repo, params, strategy_id, session_factory, event_bus.
    """

    def __init__(
        self,
        *,
        strategy_id: int,
        exchange: Exchange,
        xsmom_repo: XsmomRepo,
        params: XsmomParams,
        session_factory,
        event_bus=None,
    ) -> None:
        self._strategy_id = strategy_id
        self._exchange = exchange
        self._repo = xsmom_repo
        self._params = params
        self._sf = session_factory
        self._bus = event_bus

    async def reconcile(
        self,
        *,
        now_ms: int,
        scores: dict[str, float] | None = None,
        closes: dict[str, list[tuple[int, float]]] | None = None,
    ) -> dict:
        """Diff the target book against the live book and apply KEEP/ADD/DROP/FLIP.

        Parameters
        ----------
        now_ms:
            Current epoch-ms timestamp.
        scores:
            Pre-computed scores dict from a prior scan (avoids recompute).
            If None, closes must be provided or will be fetched from the repo.
        closes:
            Pre-fetched closes dict. Used only when scores is None.

        Returns
        -------
        dict with keys ``kept``, ``opened`` (new rows created), ``dropped``, ``flipped``.
        """
        # ── 1. Obtain scores ──────────────────────────────────────────────────
        if scores is None:
            if closes is None:
                closes = await self._repo.get_daily_closes(list(self._params.universe))
            scores = compute_scores(closes, self._params.lookbacks)

        # ── 2. Build target book ──────────────────────────────────────────────
        universe = self._params.universe
        universe_len = len(universe)
        k = self._params.compute_k(universe_len)

        # Rank coins that have a finite score; handle degenerate case where
        # fewer than 2k coins have scores: take min(k, available // 2) per side.
        scored = sorted(
            [(coin, sc) for coin, sc in scores.items() if coin in set(universe)],
            key=lambda t: t[1],
            reverse=True,
        )
        available = len(scored)
        effective_k = min(k, available // 2) if available >= 2 else 0

        target: dict[str, Side] = {}
        if effective_k > 0:
            for coin, _ in scored[:effective_k]:
                target[coin] = Side.LONG
            for coin, _ in scored[-effective_k:]:
                target[coin] = Side.SHORT

        # ── 3. Load current book (OPENED + in-flight NEW) ─────────────────────
        opened_fps = await self._repo.list_active(self._strategy_id)   # OPENED
        new_fps = await self._repo.list_in_state(self._strategy_id, XsmomState.NEW)

        # held[coin] = (side, xsmom_position) — prefer OPENED over NEW for same coin
        held: dict[str, tuple[Side, object]] = {}
        for fp in new_fps:
            held[fp.coin] = (fp.side, fp)
        for fp in opened_fps:
            held[fp.coin] = (fp.side, fp)  # OPENED wins over in-flight NEW

        # ── 4. Diff: KEEP / ADD / DROP / FLIP ────────────────────────────────
        kept: list[str] = []
        opened: list[int] = []   # new xsmom_position ids created
        dropped: list[int] = []  # xsmom_position ids transitioned to CLOSE
        flipped: list[str] = []  # coins that had FLIP (old dropped + new opened)

        notional = self._params.compute_notional_per_position(k if effective_k > 0 else 1)
        required_margin = self._params.compute_required_margin(notional)

        # Process target coins first
        for coin, target_side in target.items():
            if coin in held:
                held_side, fp = held[coin]
                if held_side == target_side:
                    # KEEP — same coin + same side → no action
                    kept.append(coin)
                    continue
                # FLIP — held with opposite side: drop the existing, then add target
                try:
                    await self._repo.transition(
                        fp.id,
                        from_state=XsmomState.OPENED,
                        to_state=XsmomState.CLOSE,
                        state_data={
                            **fp.state_data,
                            "exit_decision": "rebalance_flip",
                        },
                    )
                    dropped.append(fp.id)
                    flipped.append(coin)
                except XsmomStateConflict as exc:
                    logger.warning(
                        "xsmom reconcile: state_conflict on flip coin=%s id=%s: %s — skipping",
                        coin, fp.id, exc,
                    )
                    continue  # skip creating the new side too (race condition)
            # ADD (or post-flip ADD): create a NEW row
            quote = await self._exchange.get_quote(coin)
            price = quote.mark if quote.spot is None else quote.spot
            qty = notional / price
            new_fp = await self._repo.create(
                strategy_id=self._strategy_id,
                coin=coin,
                side=target_side,
                target_qty=qty,
                initial_state=XsmomState.NEW,
                state_data={
                    "notional": notional,
                    "required_margin": required_margin,
                    "score": scores.get(coin, 0.0),
                    "side": target_side.value,
                    "entry_ts_ms": now_ms,
                },
            )
            opened.append(new_fp.id)

        # DROP — held (OPENED only, not in-flight NEW) but not in target
        for coin, (held_side, fp) in held.items():
            if coin in target:
                continue
            if fp.state != XsmomState.OPENED:
                continue  # in-flight NEW positions not in target are left to expire
            try:
                await self._repo.transition(
                    fp.id,
                    from_state=XsmomState.OPENED,
                    to_state=XsmomState.CLOSE,
                    state_data={
                        **fp.state_data,
                        "exit_decision": "rebalance_drop",
                    },
                )
                dropped.append(fp.id)
            except XsmomStateConflict as exc:
                logger.warning(
                    "xsmom reconcile: state_conflict on drop coin=%s id=%s: %s — skipping",
                    coin, fp.id, exc,
                )

        summary = {
            "kept": kept,
            "opened": opened,
            "dropped": dropped,
            "flipped": flipped,
        }
        await publish_event(
            self._bus,
            level="INFO",
            kind="xsmom.rebalanced",
            message=(
                f"rebalance complete: kept={len(kept)} opened={len(opened)} "
                f"dropped={len(dropped)} flipped={len(flipped)}"
            ),
            payload={
                "strategy_id": self._strategy_id,
                "k": k,
                "effective_k": effective_k,
                **{kk: vv for kk, vv in summary.items()},
            },
        )
        logger.info(
            "xsmom reconcile strategy_id=%s k=%d kept=%d opened=%d dropped=%d flipped=%d",
            self._strategy_id, k,
            len(kept), len(opened), len(dropped), len(flipped),
        )
        return summary
