# Task C3: Engine watchdog — auto top-up / forced-close on margin breach

## Goal
Add a minute-tick safety check that monitors the cross-margin perp wallet ratio. When `margin_ratio` falls below `top_up_trigger`, either transfer spot→perp to restore the healthy ratio, or — if spot cash is insufficient — force-close the weakest open position. Backwards-compatible: when the strategy has no MarginManager, the watchdog is a no-op and existing behavior is preserved byte-for-byte.

## Files

Modify:
- `src/frab/strategies/base.py` — add `WatchdogReport` dataclass, abstract `margin_watchdog` method on `Strategy` (default returns `None`).
- `src/frab/strategies/strategy_a.py` — implement `async def margin_watchdog(self, now)` + helper `_marks_snapshot()`.
- `src/frab/strategies/two_phase_dynamic.py` — same implementation pattern as `strategy_a.py`.
- `src/frab/engine/loop.py` — after `on_minute_tick`, call `await self._strategy.margin_watchdog(now)`; if report is not None and action != "NONE", publish an Event.

Add:
- `src/frab/strategies/tests/test_margin_watchdog.py` — strategy-level tests for both strategies.
- `src/frab/engine/tests/test_loop_margin_watchdog.py` — engine integration test (just confirms strategy.margin_watchdog is awaited each minute and events are published).

DO NOT create a separate `margin_watchdog.py` module — the watchdog logic lives **on the strategy** because it needs `_cash`, `_perp_cash`, `_positions`, `_market_state`, `_last_quotes`, and `_close_position`. Putting it elsewhere would force a leaky public interface.

## Public surface to add

In `src/frab/strategies/base.py`:

```python
from enum import Enum

class WatchdogAction(str, Enum):
    NONE = "NONE"
    TOP_UP = "TOP_UP"
    FORCED_CLOSE = "FORCED_CLOSE"
    EMERGENCY = "EMERGENCY"

@dataclass(frozen=True, slots=True)
class WatchdogReport:
    ts: datetime
    action: WatchdogAction
    ratio: float           # margin_ratio at decision time
    coin: str | None       # which coin was force-closed (None for NONE/TOP_UP)
    amount_transferred: float  # USDC moved (0.0 if no transfer)
    reason: str            # short human-readable explanation

class Strategy(ABC):
    ...
    async def margin_watchdog(self, now: datetime) -> "WatchdogReport | None":
        """Default: no-op. Strategies with margin management override.

        Return None when no watchdog is configured (margin_manager is None)
        OR when there are no open positions to monitor.
        """
        return None
```

## Logic (identical for StrategyA and TwoPhaseDynamic)

```
async def margin_watchdog(self, now):
    if self._margin_manager is None:
        return None
    if not self._positions:
        return None

    # Need current marks for every open coin. If any missing, skip with NONE
    # action (can't compute ratio safely).
    marks: dict[str, float] = {}
    for coin in self._positions:
        if coin not in self._last_quotes:
            return None  # quiet skip — wait for the next minute tick
        marks[coin] = self._last_quotes[coin].mark

    opens = self._open_position_snapshots_for_manager()
    total_maint = self._margin_manager.compute_total_maintenance(opens, marks)
    ratio = self._margin_manager.compute_margin_ratio(self._perp_cash, opens, marks)

    # Branch 1: healthy
    if ratio >= self._margin_manager.top_up_trigger:
        return WatchdogReport(now, WatchdogAction.NONE, ratio, None, 0.0,
                              "margin healthy")

    # Branch 2: emergency (ratio close to maintenance) → force close weakest
    if ratio < 1.0:
        coin = self._select_weakest_open()  # returns None only when no opens
        if coin is None:
            return WatchdogReport(now, WatchdogAction.NONE, ratio, None, 0.0,
                                  "no opens to close on emergency")
        ok = await self._watchdog_force_close(coin, now)
        action = WatchdogAction.EMERGENCY if ok else WatchdogAction.NONE
        reason = f"emergency close {coin}" if ok else f"emergency close FAILED {coin}"
        return WatchdogReport(now, action, ratio, coin if ok else None, 0.0, reason)

    # Branch 3: below trigger but above 1.0 → try top-up, else forced close
    top_up = self._margin_manager.compute_top_up_amount(self._perp_cash, total_maint)
    if self._cash >= top_up and top_up > 0.0:
        try:
            await self._executor.transfer_spot_to_perp(top_up)
            self._cash -= top_up
            self._perp_cash += top_up
            return WatchdogReport(now, WatchdogAction.TOP_UP, ratio, None, top_up,
                                  f"topped up perp by ${top_up:.2f}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("watchdog top_up failed: %r — falling through to forced close", exc)

    # Spot cash too low (or transfer failed) → force-close weakest
    coin = self._select_weakest_open()
    if coin is None:
        return WatchdogReport(now, WatchdogAction.NONE, ratio, None, 0.0,
                              "no opens to close")
    ok = await self._watchdog_force_close(coin, now)
    action = WatchdogAction.FORCED_CLOSE if ok else WatchdogAction.NONE
    reason = f"forced close {coin} (spot cash insufficient)" if ok \
             else f"forced close FAILED {coin}"
    return WatchdogReport(now, action, ratio, coin if ok else None, 0.0, reason)
```

Helpers (private, same on both strategies):

```python
def _select_weakest_open(self) -> str | None:
    """Coin with lowest smoothed_signal among currently-open coins."""
    if not self._positions:
        return None
    signals = self._market_state.signals()  # dict[str, float|None]
    open_signals = {c: (signals.get(c) or 0.0) for c in self._positions}
    return self._margin_manager.select_weakest_for_close(
        self._open_position_snapshots_for_manager(),
        open_signals,
    )

async def _watchdog_force_close(self, coin: str, now: datetime) -> bool:
    """Close `coin` via the same path used in hour-tick CLOSE, plus release
    the locked margin back to spot cash. Returns True on success."""
    if self._margin_manager is None or coin not in self._positions:
        return False
    required_margin = self._margin_manager.compute_required_margin_for_open(coin)
    _, ok = await self._close_position(coin, now)
    if not ok:
        return False
    # Release the locked margin: it's currently sitting in self._perp_cash
    # because OPEN debited self._cash and credited self._perp_cash. Reverse it.
    try:
        await self._executor.transfer_perp_to_spot(required_margin)
    except Exception as exc:  # noqa: BLE001
        logger.error("watchdog: transfer_perp_to_spot failed after close of %s: %r", coin, exc)
        # We still update bookkeeping — position is closed on exchange already.
    self._cash += required_margin
    self._perp_cash -= required_margin
    return True
```

## Engine integration

In `src/frab/engine/loop.py`, **after** step 3 (`await self._strategy.on_minute_tick(...)`) and **before** step 4 (hour boundary check), insert:

```python
# 3b. Margin watchdog (no-op when strategy has no MarginManager)
try:
    watchdog_report = await self._strategy.margin_watchdog(now)
except Exception as exc:  # noqa: BLE001
    logger.error("margin_watchdog failed: %s", exc, exc_info=True)
    watchdog_report = None

if watchdog_report is not None and watchdog_report.action.value != "NONE":
    level = "WARNING" if watchdog_report.action.value == "TOP_UP" else "ERROR"
    await self._publish(Event(
        ts=now,
        level=level,
        source="margin_watchdog",
        kind=f"margin.{watchdog_report.action.value.lower()}",
        message=watchdog_report.reason,
        payload_json={
            "ratio": watchdog_report.ratio,
            "coin": watchdog_report.coin,
            "amount_transferred": watchdog_report.amount_transferred,
            "action": watchdog_report.action.value,
        },
    ))
```

## Out of scope
- Refactoring `_close_position` accounting (released-margin transfer is a best-effort patch documented as a known approximation).
- Multiple forced closes in a single tick (one per tick is enough; ratio is re-checked next minute).
- Updates to dashboard / API / DB schema. D2 covers those.
- Changes to MarginManager.

## Constraints
1. **Backwards compat**: every existing test must still pass. If a test breaks because a strategy's `_positions` is empty + `margin_manager` is None, the method should return `None` and skip everything cleanly.
2. **Strategy ABC default**: the base class returns `None`, so `LegacyMockStrategy` etc. that don't override still work.
3. No new dependencies.
4. pytest-mock (`mocker` fixture) exclusively — no `unittest.mock` directly.
5. ≤350 lines added total across all touched files. Removed ≤30.
6. `MarginManager.top_up_trigger` already exists as a public attribute set in `__init__`. Reuse it.
7. Use existing `executor.transfer_spot_to_perp` / `transfer_perp_to_spot` from `AtomicExecutor` Protocol — both forward to the wrapped HL/paper executor.

## Acceptance criteria
1. `uv run pytest` exits 0 (all 432+ existing tests still pass plus new tests).
2. ≥12 new tests across the two new test files covering: no manager → None; no opens → None; missing quote → None; healthy ratio → NONE; ratio<trigger + cash sufficient → TOP_UP succeeds; ratio<trigger + transfer fails → falls through to FORCED_CLOSE; ratio<trigger + cash insufficient → FORCED_CLOSE; ratio<1.0 → EMERGENCY; weakest coin selected correctly.
3. Engine test confirms `strategy.margin_watchdog` is awaited every minute tick and that events are published with correct `kind` and `level`.
4. `git diff --stat` shows ≤7 files changed, ≤350 added.
5. No emojis. No `TODO` comments. Russian comments fine where existing code has them, but no new Russian comments needed.

## Tests to run
```bash
uv run pytest src/frab/strategies/tests/test_margin_watchdog.py -v
uv run pytest src/frab/engine/tests/test_loop_margin_watchdog.py -v
uv run pytest -x  # full suite
```

## Risks
- Forgetting `self._margin_manager.top_up_trigger` access pattern (it's a public attr — `__init__` saves it). Double-check.
- Forgetting that `MarginManager.select_weakest_for_close` raises KeyError on missing signals. The helper builds the dict from `self._positions` ∩ `market_state.signals()` with a 0.0 fallback to avoid KeyError.
- Forgetting that `_close_position` returns `(fills, ok)` — must unpack carefully.
- Forgetting that exceptions inside `transfer_spot_to_perp` must not crash the tick — wrap in try/except.
- Don't double-publish events: only one Event per tick from the watchdog.

## Progress reporting
Append START/DONE lines to `/tmp/C3.progress`:
```
2026-05-22T<TS>Z START — spec read, scanning files
2026-05-22T<TS>Z DONE base.py — Strategy.margin_watchdog default + WatchdogReport added
2026-05-22T<TS>Z DONE strategy_a.py — margin_watchdog implemented
...
2026-05-22T<TS>Z DONE all — N tests, all green, ready for review
```
