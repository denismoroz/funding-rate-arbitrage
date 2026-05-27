# Task F1.4: PortfolioService cutover

Phase 1.4 рефакторинга по `.ai/architecture-review.md` и `.ai/task-f1-portfolio-domain.md`.
**Зависит от F1.1-F1.3d (foundation + dual-track) и F2.1-F2.5 (Adapter + DryRunGuard).**

## Goal

Перевести consumers — `Engine.equity`, `api/routes/margin`, `api/routes/wallet` — на чтение из `PortfolioService`. Удалить из strategies `_cash`, `_perp_cash`, `_fees_cum`, `_funding_cum`, `_realized_pnl_cum`, `_positions` accumulators. Завершить миграцию `position_min_hold_hours` / `consec_negative_hours` в `Position.state` JSON. Удалить deprecated DB колонки. После F1.4: `PortfolioService` — единственный source of truth для portfolio state, `Strategy` — pure decision-maker.

## Pre-condition

- F1.1-F1.3d закоммичены: PortfolioService существует, dual-track для `_fees_cum`/`_funding_cum` активен.
- F2.1-F2.5 закоммичены: HyperliquidAdapter существует, wrapped DryRunGuard в server, но strategies всё ещё используют `AtomicExecutor`.
- Suite 810 green.

## Current state audit

### Что работает в prod

- `PortfolioService` создаётся в `server.py` lifespan после strategy rehydrate. Initial cash = `strategy.cash + strategy.perp_cash + committed_from_DB`. Attached to `app.state.portfolio_service`.
- `strategy.set_portfolio_service(ps)` вызывается после построения portfolio_service (late-bind).
- Reconcilers (`FeeReconciler`, `FundingReconciler`) пишут `await portfolio_service.set_*_cum(total)`.
- StrategyA + TwoPhase: после каждой мутации `self._fees_cum` / `self._funding_cum`, зеркалят итог через `await self._portfolio_service.set_*_cum(self._fees_cum)`.

### Что dormant

- `PortfolioService._positions` дикт пустой после rehydrate (если нет OPEN rows в DB). После opens — не заполняется т.к. apply_open не вызывается.
- `PortfolioService._cash_per_exchange` обновляется только через `set_fees_cum` / `set_funding_cum` пути (а они не трогают cash). После opens — desync с реальным cash.
- `portfolio_service.equity(marks)` сейчас вернёт `cash_total + 0 (spot) + 0 (perp_unrealized) + 0 (margin) + 0 (realized) + funding - fees`. Cash_total залипший на initial.

### Где Engine + API сейчас читают

- [src/frab/engine/loop.py:286](src/frab/engine/loop.py#L286) — `equity = self._strategy.compute_equity(now)` — strategy is source.
- [src/frab/api/routes/margin.py:98](src/frab/api/routes/margin.py#L98) — `perp_cash = float(strategy.perp_cash)`.
- [src/frab/api/routes/wallet.py:74](src/frab/api/routes/wallet.py#L74) — `usdc_spot = strategy.cash` в paper-synth.
- [src/frab/server.py:476](src/frab/server.py#L476) — `strategy.rehydrate(positions=, accumulators=)` — заливает initial state в strategy.

## Decisions

### D1. Position INSERT ownership

**Decision: PortfolioService — единственный owner Position row INSERT.**

Rationale: One writer per table = no double-insert. DbRecorder downgrades to "appender" — пишет Fill rows, FundingAccrual rows, обновляет Position поля (funding_collected, fees_paid), но не делает INSERT новых Position rows. Strategy на успешный `_open_position` вызывает `await self._portfolio_service.apply_open(domain_position)`; на close — `apply_close(closed)`.

Concrete changes:
- `DbRecorder.save_tick_report`: убрать `Position(...)` инсёрты (lines 209, 354 в текущем коде). Оставить SELECT для существующих `_open_positions[coin]` mapping; обновления fields оставить. Если coin отсутствует — это bug (нет вызова apply_open) → warning log, skip.
- `PortfolioService.apply_open`: оставляет существующий INSERT. Дополнительно идемпотентность не нужна — single writer.
- StrategyA._open_position: после успеха construct domain Position + await portfolio_service.apply_open + await portfolio_service.record_fill_fees.
- StrategyA._close_position: similarly construct ClosedPosition + await apply_close.
- TwoPhaseDynamic._open_position / _close_position: симметрично.

### D2. Cross-margin model: per-exchange, не per-coin

**Decision: `apply_margin_adjustment` принимает `(exchange, coin, delta)` где coin — это **attributed coin** для логирования, но кэш-движение — на уровне exchange.**

Текущий `apply_margin_adjustment(exchange, coin, delta_usd)` обновляет `position.margin_reserve_usd` плюс `cash_per_exchange[exchange] -= delta`. Это close enough: per-coin attribution позволяет отслеживать "сколько маржи привязано к BTC vs ETH"; cross-margin nature живёт в том что watchdog в HL дёргает один perp wallet.

Для margin_watchdog top-up: distributing across coins proportional to their `notional_usd`. На emergency close одного coin — release его reserve целиком.

Concrete changes:
- `StrategyA.margin_watchdog`: после успешного `transfer_spot_to_perp(top_up)`:
  ```python
  open_coins = list(self._positions.keys())
  notional_total = sum(self._positions[c].spot_qty * self._last_quotes[c].mark for c in open_coins)
  for coin in open_coins:
      pos_notional = self._positions[coin].spot_qty * self._last_quotes[coin].mark
      share = (pos_notional / notional_total) if notional_total > 0 else (1.0 / len(open_coins))
      attributed = top_up * share
      if self._portfolio_service is not None:
          await self._portfolio_service.apply_margin_adjustment(
              Exchange.HYPERLIQUID, coin, +attributed
          )
  ```

- Forced-close: после `_watchdog_force_close(coin)`:
  ```python
  if self._portfolio_service is not None:
      released_margin = ... # из _perp_cash share before close (track separately)
      await self._portfolio_service.apply_close(ClosedPosition(
          ..., released_margin_usd=released_margin
      ))
  ```

### D3. Engine.equity cutover

**Decision: Engine принимает `portfolio_service` mandatory; equity всегда через `portfolio_service.equity(marks)`. `strategy.compute_equity` удаляется.**

Concrete changes:
- `Engine.__init__`: добавить `portfolio_service: PortfolioService` (required kwarg).
- `Engine.tick_once`: после quotes fetch, build marks dict:
  ```python
  marks = {(Exchange.HYPERLIQUID, coin): q.mark for coin, q in quotes.items()}
  equity = self._portfolio_service.equity(marks)
  ```
- Удалить `Strategy.compute_equity` из ABC. Concrete strategies могут оставить как dead code; F1.5 cleanup удалит.
- `engine/tests/test_loop.py`: ~13 references на `strategy.compute_equity`. Заменить на `portfolio_service.equity` mock.

### D4. API routes cutover

**Decision: routes читают `portfolio_service.current()` напрямую.**

Concrete changes:
- `api/routes/margin.py`: `perp_cash = float(strategy.perp_cash)` → `portfolio = await app.state.portfolio_service.current(); perp_cash = portfolio.wallet_per_exchange[Exchange.HYPERLIQUID].reserved_usdc`.
- `api/routes/wallet.py` `_synthesize_paper_wallet`: `usdc_spot = strategy.cash` → `usdc_spot = (await app.state.portfolio_service.current()).wallet_per_exchange[Exchange.HYPERLIQUID].available_usdc`. Spot tokens из portfolio.positions.
- Удалить `strategy.cash`, `strategy.perp_cash`, `strategy.fees_cum`, `strategy.funding_cum`, `strategy.realized_pnl_cum` properties из StrategyA + TwoPhase.

### D5. Strategy accumulators removal

**Decision: убрать `_cash`, `_perp_cash`, `_fees_cum`, `_funding_cum`, `_realized_pnl_cum`, `_positions` из StrategyA + TwoPhase.**

Strategy keeps только:
- `_params` (config, immutable per session)
- `_executor` (AtomicExecutor, до F3)
- `_market_state` (rolling funding history)
- `_last_quotes` (per-tick cache)
- `_n_skipped_opens_capital` (per-tick stat)
- `_dry_run`, `_margin_manager`, `_portfolio_service` (config)

Все обращения внутри strategy:
- `self._positions[coin]` → `(await self._portfolio_service.current()).position(Exchange.HYPERLIQUID, coin)`. Кэшировать в локальной переменной в начале tick для эффективности.
- `self._cash` (для can_open + sizing checks) → `portfolio.wallet_per_exchange[HL].available_usdc`.
- `self._perp_cash` → `portfolio.wallet_per_exchange[HL].reserved_usdc`.
- `self._fees_cum` / `_funding_cum` (для signal computation) → не используются внутри strategy decisions, только для compute_equity.

`StrategyA.rehydrate` упрощается: portfolio_service.rehydrate_from_db уже делает всё. Strategy.rehydrate становится тонкой обёрткой устанавливающей `_market_state` (warmup) — или удаляется целиком если warmup идёт через отдельный путь.

`server.py`: убрать `_rehydrate_strategy_from_db` (теперь portfolio_service.rehydrate_from_db делает работу).

### D6. Position.state migration для two_phase

**Decision: `position_min_hold_hours` и `consec_negative_hours` уезжают в `Position.state` JSON.**

Concrete changes:
- `db/models.py`: удалить колонки `position_min_hold_hours`, `consec_negative_hours`. Новая Alembic migration drops columns.
- `two_phase_signals.py`: signature не меняется (всё ещё принимает int parameters). Caller (TwoPhase.on_hour_tick) извлекает из `position.state.get('min_hold_hours', 0)` и `state.get('consec_negative_hours', 0)`.
- `TickReport.position_state_updates: tuple[tuple[str, dict], ...]` — заменяет `opened_min_holds` и `consec_negative_updates`. Каждый entry — `(coin, dict_patch_для_position.state)`.
- `DbRecorder.save_tick_report`: применяет position_state_updates через UPDATE `Position.state` = JSON merge.
- TwoPhase tests refactored: `strat._positions["BTC"].position_min_hold_hours` → читать через portfolio_service.current().position(...).state['min_hold_hours'].

### D7. AccumulatorsSnapshot / OpenPositionSnapshot

`AccumulatorsSnapshot` (cash, realized_pnl_cum, funding_cum, fees_cum) → удалить. PortfolioService rehydrate_from_db уже грузит accumulators из EquitySnapshot.

`OpenPositionSnapshot` → удалить. Position domain class заменяет.

## Sub-chunks

### F1.4a — DbRecorder опускает Position INSERT

- `DbRecorder.save_tick_report`: убрать INSERT новых Position rows на opens; оставить SELECT mapping + UPDATE на close.
- Если coin в `report.opened` не имеет existing OPEN row в DB (потому что apply_open не вызывался) — log warning, skip.
- Tests: один новый тест что save_tick_report без предварительного apply_open не падает (просто warning).

Constraint: ≤200 LOC change. Не трогать strategies / Engine / API.

### F1.4b — StrategyA + TwoPhase вызывают apply_open / apply_close

- В _open_position после успеха: construct Position, await portfolio_service.apply_open(pos). Also await record_fill_fees(total_fees).
- В _close_position после успеха: construct ClosedPosition (release_margin from _perp_cash share), await apply_close(closed).
- Margin watchdog: top_up → distribute share + apply_margin_adjustment per coin.
- Тесты: parameterized, проверяют что portfolio_service метки вызваны с правильными values.

Constraint: ≤400 LOC change. После F1.4b: portfolio_service.current() имеет точные positions.

### F1.4c — Engine.equity через portfolio_service

- `Engine.__init__`: required portfolio_service kwarg.
- `Engine.tick_once`: marks dict + `portfolio_service.equity(marks)`.
- `engine/tests/test_loop.py`: rewire ~13 references (strategy.compute_equity → portfolio_service.equity).
- server.py: pass portfolio_service to Engine.

Constraint: ≤300 LOC change.

### F1.4d — API routes на portfolio_service

- `api/routes/margin.py`: читать perp_cash из portfolio_service.
- `api/routes/wallet.py` _synthesize_paper_wallet: usdc_spot, spot_balances из portfolio_service.

Constraint: ≤200 LOC change.

### F1.4e — Position.state migration + TickReport rework

- `db/models.py`: drop `position_min_hold_hours`, `consec_negative_hours`. Alembic migration.
- `TickReport`: добавить `position_state_updates`, удалить `opened_min_holds`, `consec_negative_updates`.
- `DbRecorder`: apply state JSON merge.
- `two_phase_dynamic.py`: extract from state dict, populate position_state_updates.
- TwoPhase tests rewired.

Constraint: ≤500 LOC change.

### F1.4f — Strategy accumulator removal + cleanup

- Remove `_cash`, `_perp_cash`, `_fees_cum`, `_funding_cum`, `_realized_pnl_cum`, `_positions` from StrategyA + TwoPhase.
- Remove `cash`, `perp_cash`, `fees_cum`, `funding_cum`, `realized_pnl_cum` properties.
- Remove `set_fees_cum`, `set_funding_cum`, `compute_equity` from ABC.
- Remove `AccumulatorsSnapshot`, `OpenPositionSnapshot`.
- server.py: убрать `_rehydrate_strategy_from_db`. portfolio_service.rehydrate_from_db делает работу.
- Все internal references в strategy: portfolio_service.current() в начале каждого tick.
- Большой test rewire.

Constraint: ≤700 LOC change. Это самый рискованный chunk.

## Public surface after F1.4

```python
class StrategyA:
    def __init__(self, params, executor, portfolio_service,
                 exchange_profile=None, *, dry_run=False, margin_manager=None) -> None
    
    @property
    def n_skipped_opens_capital(self) -> int
    
    async def on_minute_tick(self, now, quotes) -> None
    async def on_hour_tick(self, now, funding) -> TickReport
    async def margin_watchdog(self, now) -> WatchdogReport | None
```

NO: cash, perp_cash, fees_cum, funding_cum, realized_pnl_cum, set_*_cum, compute_equity, rehydrate, _cash, _perp_cash, _fees_cum, _funding_cum, _realized_pnl_cum, _positions.

```python
class Engine:
    def __init__(self, *, market_data, strategy, portfolio_service, coins, ...) -> None
```

`PortfolioService` API не меняется.

## Acceptance criteria

1. `uv run pytest` exits 0.
2. `git grep -n "self\._cash\b\|self\._perp_cash\b\|self\._fees_cum\b\|self\._funding_cum\b\|self\._realized_pnl_cum\b\|self\._positions\b" src/frab/strategies/` — empty (за исключением `_n_skipped_opens_capital`).
3. `git grep -n "strategy\.cash\|strategy\.perp_cash\|strategy\.compute_equity\|strategy\.set_fees_cum\|strategy\.set_funding_cum\|strategy\.rehydrate" src/frab/` — empty.
4. `git grep -n "position_min_hold_hours\|consec_negative_hours" src/frab/db/models.py` — empty.
5. Alembic upgrade head на свежей DB проходит.
6. Engine equity is computed by portfolio_service.equity (verified by test).
7. Suite size grows by ~40 new tests (mostly migrations of existing).

## Constraints

1. **Один source of truth для accumulators** — после F1.4 ни одно поле `_cash`/`_fees_cum`/etc не остаётся в Strategy.
2. **One writer per DB table** — Position rows INSERT только PortfolioService; DbRecorder только UPDATE.
3. **Backwards compat для Engine API** — signature `on_minute_tick`, `on_hour_tick`, `margin_watchdog` не меняется.
4. **pytest-mock (`mocker`)** — без прямого `unittest.mock`.
5. **No new dependencies**.
6. **Никакие emojis, TODO**.

## Risks

- **Race condition**: apply_open / apply_close / record_fill_fees / accrue_funding — async-shared cash bucket. Asyncio single-threaded → no actual race, но порядок вызовов важен. Все мутации должны быть serial inside a single tick.
- **Cash drift при первом запуске**: initial cash = strategy_cash + strategy_perp_cash + committed_from_DB; после F1.4 strategy_cash больше не существует. server.py initial cash должен браться из settings (`hl_position_size_usd * concurrency_cap * 2` или similar). Если значение неверное — portfolio_service.equity вернёт wrong total.
- **Watchdog top-up distribution** — distribute proportionally is approximate. Если стратегия не открывает все coins равномерно, attribution divергирует от реального HL state. Не критично т.к. cash_per_exchange total остаётся корректным.
- **Test rewiring** — много тестов в `test_strategy_a.py` (1024 LOC), `test_two_phase_dynamic.py` (1072 LOC) используют `strat._positions["BTC"]`. F1.4f должен переписать аккуратно.
- **Position.state JSON mutations** — SQLAlchemy JSON column не auto-detects mutations внутри dict. Use `MutableDict.as_mutable(JSON)` или явно flag_modified после updates в DbRecorder.

## Progress reporting

Append START/DONE to `/tmp/F14.progress`:
```
<TS>Z START — F1.4 spec read
<TS>Z DONE F1.4a — DbRecorder Position INSERT removed
<TS>Z DONE F1.4b — apply_open/close wired in strategies
<TS>Z DONE F1.4c — Engine.equity via portfolio_service
<TS>Z DONE F1.4d — API routes on portfolio_service
<TS>Z DONE F1.4e — Position.state migration
<TS>Z DONE F1.4f — accumulators removed
<TS>Z DONE all tests — N pass
```
