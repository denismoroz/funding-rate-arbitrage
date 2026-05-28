# Task F1: Portfolio domain + PortfolioService

Phase 1 рефакторинга по `.ai/architecture-review.md`.

## Goal

Материализовать domain value objects (`Position`, `WalletInfo`, `Portfolio`,
`MarketSpec`) и забрать у Strategy ownership всех accumulators
(`_cash`, `_perp_cash`, `_fees_cum`, `_funding_cum`, `_realized_pnl_cum`,
`_positions`). Они переезжают в `PortfolioService` — single source of truth.
`Strategy` остаётся в роли decision-maker'а, читает Portfolio для расчётов,
вызывает PortfolioService для применения mutations.

Reconcilers (`FeeSync`, `FundingSync`) пишут в PortfolioService вместо
`strategy.set_fees_cum`/`set_funding_cum` — этот публичный интерфейс
стратегии уходит.

**Не делаем в этой фазе** (отложено в F2+):
- `ExchangeAdapter` Protocol и `HyperliquidAdapter` — strategies продолжают
  использовать `AtomicExecutor` напрямую.
- `DryRunGuard` — обрабатываем через временный compromise (см. ниже).
- Engine pipeline / TickComponent — Engine продолжает звать
  `on_minute_tick`/`on_hour_tick`/`margin_watchdog` отдельно.
- `FundingHarvestStrategy` базис — два strategy-класса остаются как сейчас.

## Pre-condition

База данных будет пересоздана с нуля (`alembic downgrade base && alembic
upgrade head`). Старые миграции не пытаемся сохранить data-compatible —
просто пишем новую финальную схему.

## Name collision: domain.Exchange vs db.models.Exchange

Domain enum `Exchange` (HYPERLIQUID, …) и SQLAlchemy модель
`frab.db.models.Exchange` (таблица `exchanges`) — разные сущности.
Живут в разных модулях, не пересекаются в большинстве файлов.

Там где нужны оба (server.py при `_resolve_exchange`):

```python
from frab.domain.exchange import Exchange
from frab.db.models import Exchange as ExchangeRow
```

Альтернатива: на F1 решить что строки `Position.exchange: str` (значение enum)
достаточно, а `exchanges` таблица остаётся только для `Market.exchange_id`
join'а. `_resolve_exchange` остаётся неизменным; новый код использует
`Exchange.HYPERLIQUID.value == "hyperliquid"` совпадение с `Exchange.name`
из таблицы.

## Files

### Add

- `src/frab/domain/__init__.py`
- `src/frab/domain/exchange.py` — `Exchange` enum (HYPERLIQUID, DRIFT, BINANCE).
- `src/frab/domain/position.py` — `Position`, `ClosedPosition` (импортит Exchange).
- `src/frab/domain/wallet.py` — `WalletInfo`.
- `src/frab/domain/portfolio.py` — `Portfolio` (immutable snapshot) + `Equity`.
- `src/frab/domain/market_spec.py` — `MarketSpec`.
- `src/frab/domain/exchange_profile.py` — `ExchangeProfile` (per-exchange static facts:
  funding_interval_hours, periods_per_year, default fees).
- `src/frab/application/__init__.py`
- `src/frab/application/portfolio_service.py` — `PortfolioService`.
- `src/frab/domain/tests/__init__.py`
- `src/frab/domain/tests/test_position.py`
- `src/frab/domain/tests/test_portfolio.py`
- `src/frab/domain/tests/test_wallet.py`
- `src/frab/application/tests/__init__.py`
- `src/frab/application/tests/test_portfolio_service.py`
- `src/frab/db/migrations/versions/<new_id>_phase1_domain_schema.py` —
  заменяет position fields + добавляет exchange, state.

### Modify

- `src/frab/strategies/strategy_a.py` — убрать `_cash`, `_perp_cash`,
  `_fees_cum`, `_funding_cum`, `_realized_pnl_cum`, `_positions`,
  `_n_skipped_opens_capital`. Принимать `portfolio_service: PortfolioService`
  и `exchange_profile: ExchangeProfile` в конструкторе. Все обращения к
  accumulators → через `self._portfolio_service.current()`. Применение
  mutations → через `await self._portfolio_service.apply_open(pos)` и
  `.apply_close(...)`. `MarketState(coins, signal_window_hours,
  funding_interval_hours=exchange_profile.funding_interval_hours)` — больше
  не хардкод 1.0. Удалить `set_fees_cum`/`set_funding_cum`/`cash`/`perp_cash`/... properties.
- `src/frab/strategies/two_phase_dynamic.py` — то же симметрично. Удалить
  `fee_round_trip_annual` из `TwoPhaseDynamicParams` — теперь
  `exchange_profile.fee_round_trip_annual_pct`. `signal_window_hours` остаётся
  параметром но `funding_interval_hours` для MarketState приходит из profile.
- `src/frab/strategies/base.py` — удалить `set_fees_cum`/`set_funding_cum`
  из ABC. `EquitySnapshot` теперь возвращается из `PortfolioService.equity()`,
  не из `Strategy.compute_equity()` — удалить `compute_equity` из ABC.
  Strategies больше не нужны для equity вычисления.
- `src/frab/engine/loop.py` — `Engine.tick_once` берёт equity из
  `portfolio_service.equity(marks)` вместо `strategy.compute_equity(now)`.
  `Engine.__init__` принимает `portfolio_service: PortfolioService`.
- `src/frab/engine/fee_reconciler.py` — `__init__` принимает
  `portfolio_service: PortfolioService | None` вместо `strategy: Strategy`.
  В конце `run_once`: `await portfolio_service.set_fees_cum(total)` вместо
  `strategy.set_fees_cum(total)`.
- `src/frab/engine/funding_reconciler.py` — то же симметрично.
- `src/frab/engine/margin_manager.py` — `OpenPosition` → переезд в domain
  как `Position` (с дополнительными полями `notional_usd`,
  `margin_reserve_usd`). MarginManager продолжает существовать но принимает
  `list[Position]` вместо локального `OpenPosition`. (Полное удаление
  MarginManager — в F3.)
- `src/frab/db/models.py` — добавить `Position.exchange` (String, default
  "hyperliquid"), `Position.state` (JSON, default `{}`); удалить
  `Position.position_min_hold_hours`, `Position.consec_negative_hours`.
  Удалить `from frab.engine.signals import Decision` — `Signal.action`
  хранить как plain String.
- `src/frab/db/recorder.py` — `save_tick_report`: переехать с
  `report.opened_min_holds` и `report.consec_negative_updates` на
  `report.position_state_updates: tuple[(coin, state_patch_dict), ...]`.
  Записывать в `Position.state` через JSON-merge.
- `src/frab/strategies/base.py` (TickReport) — заменить
  `opened_min_holds` + `consec_negative_updates` на
  `position_state_updates: tuple[tuple[str, dict], ...]`.
- `src/frab/server.py` — wire `PortfolioService` + `ExchangeProfile`:
  ```python
  HL_PROFILE = ExchangeProfile(
      exchange=Exchange.HYPERLIQUID,
      funding_interval_hours=1.0,
      periods_per_year=24 * 365,
      default_spot_taker_bps=7.0,
      default_perp_taker_bps=3.5,
  )
  portfolio_service = PortfolioService(session_factory, strategy_id=strategy_id)
  await portfolio_service.rehydrate_from_db()  # вместо _rehydrate_strategy_from_db
  strategy = StrategyA(
      params, executor=atomic,
      portfolio_service=portfolio_service,
      exchange_profile=HL_PROFILE, ...
  )
  engine = Engine(..., portfolio_service=portfolio_service)
  ```
  Удалить `_rehydrate_strategy_from_db`. `HL_PROFILE` определён прямо в server
  на время F1 (в F2 переедет в `HyperliquidAdapter.get_exchange_profile()`).
- `src/frab/api/routes/margin.py` — читать `strategy._margin_manager`
  заменить на `app.state.portfolio_service` + `app.state.margin_manager`.
  Поля `n_open_positions`, `concurrency_cap` etc. брать из
  `portfolio_service.current()` и `strategy._params` (params читать ОК,
  это immutable config).
- `src/frab/api/routes/wallet.py` — `_synthesize_paper_wallet`: брать
  cash из `portfolio_service.current().cash_per_exchange[hyperliquid]`
  вместо `strategy.cash`.
- `src/frab/api/app.py` — добавить `portfolio_service` в `app.state`.
- Все существующие тесты — обновить fixture-конструкторы.

## Public surface

### Domain

`src/frab/domain/position.py`:

```python
from datetime import datetime
from dataclasses import dataclass
from enum import StrEnum


class Exchange(StrEnum):
    HYPERLIQUID = "hyperliquid"


@dataclass(frozen=True, slots=True)
class Position:
    """One delta-neutral spot+perp pair on a single exchange.

    `notional_usd`: market value of one leg (spot leg or perp leg — they
    match by construction). `margin_reserve_usd`: USDC locked at the
    exchange's perp wallet beyond the initial margin requirement.
    """
    exchange: Exchange
    coin: str
    spot_qty: float                  # >0
    perp_qty: float                  # >0 magnitude (short implied)
    notional_usd: float              # entry_spot_price * spot_qty
    margin_reserve_usd: float        # locked at exchange
    entry_spot_price: float
    entry_perp_price: float
    opened_at: datetime
    funding_collected: float = 0.0
    fees_paid: float = 0.0
    state: dict = None               # per-strategy state; default empty dict in __post_init__


@dataclass(frozen=True, slots=True)
class ClosedPosition:
    """Result of close_position; carries realized PnL for portfolio update."""
    exchange: Exchange
    coin: str
    closed_at: datetime
    realized_pnl: float
    fees_paid_total: float
    funding_collected_total: float
    released_margin_usd: float       # what was margin_reserve_usd at open
```

`src/frab/domain/wallet.py`:

```python
@dataclass(frozen=True, slots=True)
class WalletInfo:
    exchange: Exchange
    available_usdc: float        # free для использования стратегией
    reserved_usdc: float         # locked under margin / open spot positions
    total_value_usd: float       # including spot tokens at mark
```

`src/frab/domain/market_spec.py`:

```python
@dataclass(frozen=True, slots=True)
class MarketSpec:
    coin: str
    has_spot: bool
    has_perp: bool
    max_leverage: int                       # for perp
    maint_ratio: float
    min_size: float
    tick_size: float
    spot_taker_bps: float | None = None     # None → fallback to ExchangeProfile default
    perp_taker_bps: float | None = None
```

`src/frab/domain/exchange_profile.py`:

```python
@dataclass(frozen=True, slots=True)
class ExchangeProfile:
    """Per-exchange static facts. Strategy queries on init для annualization
    сигналов, breakeven математики, default fees.

    Note: эти факты per-exchange, не per-coin. Per-coin fee tiers (Binance:
    BTC дешевле altcoin'ов) выражаются через MarketSpec.spot_taker_bps /
    perp_taker_bps overrides.
    """
    exchange: Exchange
    funding_interval_hours: float       # HL: 1.0  Binance USDM: 8.0  Drift: 1.0
    periods_per_year: float             # = 24*365/funding_interval_hours
    default_spot_taker_bps: float
    default_perp_taker_bps: float

    @property
    def fee_round_trip_annual_pct(self) -> float:
        """Annualized cost of one open+close round-trip in percent.
        Used by TwoPhase breakeven математика (заменяет хардкод 18.396)."""
        rt_bps = 2 * (self.default_spot_taker_bps + self.default_perp_taker_bps)
        return rt_bps / 1e4 * 100 * self.periods_per_year / 8760  # normalized
```

В F1: `ExchangeProfile` создаётся в коде вручную (для HL) и передаётся в
strategy при wiring. F2 переносит создание в `HyperliquidAdapter.get_exchange_profile()`.

`src/frab/domain/portfolio.py`:

```python
@dataclass(frozen=True, slots=True)
class Equity:
    ts: datetime
    total_equity: float
    cash: float
    spot_value: float
    perp_unrealized: float
    perp_realized_cum: float
    funding_cum: float
    fees_cum: float


@dataclass(frozen=True, slots=True)
class Portfolio:
    """Immutable snapshot used within one tick."""
    ts: datetime
    positions: tuple[Position, ...]
    wallet_per_exchange: dict[Exchange, WalletInfo]
    fees_cum: float
    funding_cum: float
    realized_pnl_cum: float

    def position(self, exchange: Exchange, coin: str) -> Position | None: ...
    def open_coins(self, exchange: Exchange) -> list[str]: ...
    def total_committed(self, exchange: Exchange) -> float: ...  # sum(notional + margin_reserve)
    def equity(self, marks: dict[tuple[Exchange, str], float]) -> Equity: ...
```

### Application

`src/frab/application/portfolio_service.py`:

```python
class PortfolioService:
    """Mutable owner of portfolio state. Single source of truth.

    `current()` returns immutable Portfolio snapshot — safe to share
    across tick components.
    """
    def __init__(self, session_factory, strategy_id: int) -> None: ...

    async def rehydrate_from_db(self) -> None:
        """Load OPEN positions + latest equity snapshot into memory.
        Called once on engine startup."""

    async def current(self) -> Portfolio:
        """Return immutable snapshot."""

    async def apply_open(self, pos: Position) -> None:
        """Add position; debit notional from cash; reserve margin."""

    async def apply_close(self, closed: ClosedPosition) -> None:
        """Remove position; credit realized PnL + released margin to cash."""

    async def apply_margin_adjustment(
        self, exchange: Exchange, coin: str, delta_usd: float
    ) -> None:
        """Top-up (+) or release (-) margin reserve for `coin`. Updates
        cash and position.margin_reserve_usd."""

    async def record_fill_fees(self, exchange: Exchange, coin: str, fees: float) -> None:
        """Debit fees from cash, increment fees_cum, update position.fees_paid."""

    async def accrue_funding(self, exchange: Exchange, coin: str, amount: float) -> None:
        """Credit funding to cash, increment funding_cum + position.funding_collected."""

    # Reconciler entry points — overwrite accumulators with authoritative SUM
    async def set_fees_cum(self, value: float) -> None: ...
    async def set_funding_cum(self, value: float) -> None: ...

    def equity(self, marks: dict[tuple[Exchange, str], float]) -> Equity:
        """Compute equity snapshot at current marks (sync — no I/O)."""
```

Internally PortfolioService maintains `_positions: dict[(Exchange, coin), Position]`,
`_cash_per_exchange: dict[Exchange, float]`, `_fees_cum`, `_funding_cum`,
`_realized_pnl_cum`. Persists every mutation through `session_factory`
to the existing DB tables (Position, EquitySnapshot).

## Strategy changes (StrategyA shown — TwoPhase symmetric)

```python
class StrategyA(Strategy):
    def __init__(
        self,
        params: StrategyAParams,
        executor: AtomicExecutor,
        portfolio_service: PortfolioService,
        *,
        dry_run: bool = False,
        margin_manager: MarginManager | None = None,
    ) -> None:
        self._params = params
        self._executor = executor
        self._portfolio_service = portfolio_service
        self._dry_run = dry_run
        self._margin_manager = margin_manager
        self._market_state = MarketState(params.coins, params.signal_window_hours, ...)
        self._last_quotes: dict[str, Quote] = {}
        self._n_skipped_opens_capital: int = 0      # tick-local stat, kept on strategy
        # NO _cash, _perp_cash, _fees_cum, _funding_cum, _realized_pnl_cum, _positions

    @property
    def n_skipped_opens_capital(self) -> int:
        return self._n_skipped_opens_capital

    # Removed: cash, perp_cash, fees_cum, funding_cum, realized_pnl_cum,
    #          set_fees_cum, set_funding_cum, compute_equity, rehydrate.

    async def on_hour_tick(self, now, funding):
        portfolio = await self._portfolio_service.current()
        open_coins = portfolio.open_coins(Exchange.HYPERLIQUID)
        # ...decisions...
        # CLOSE:
        if self._dry_run: continue
        ok, closed = await self._close_position(coin, now)
        if ok: await self._portfolio_service.apply_close(closed)
        # OPEN:
        if self._dry_run: continue
        pos = await self._open_position(coin, now, portfolio)
        if pos: await self._portfolio_service.apply_open(pos)
```

The `_open_position` / `_close_position` helpers still use `self._executor`
directly (same as today). They now construct the `Position`/`ClosedPosition`
DTOs from the AtomicExecutor results and return them — PortfolioService
applies them.

Margin watchdog (`margin_watchdog`) теперь читает `portfolio = await
self._portfolio_service.current()` и вызывает
`await self._portfolio_service.apply_margin_adjustment(exchange, coin, +amount)`
после успешного `transfer_spot_to_perp`. На force-close —
`apply_close(closed)` после успешного `_close_position`.

## DB schema (new initial migration)

Поскольку DB пересоздаётся — пишем ОДНУ новую миграцию которая reset'ит
схему под текущее состояние + изменения F1. Все старые миграции остаются
в репозитории для истории, но `alembic upgrade head` от пустой DB должен
пройти линейно.

Изменения относительно текущего `models.py`:
- `Position.exchange: Mapped[str]` (новое; default "hyperliquid", NOT NULL).
- `Position.state: Mapped[dict] = mapped_column(JSON, default=dict)` (новое).
- Удалить `Position.position_min_hold_hours`, `Position.consec_negative_hours`.
- `Signal.action: Mapped[str]` (вместо Decision enum).

Все остальное — без изменений.

## TickReport adjustment

`src/frab/strategies/base.py`:

```python
@dataclass(frozen=True, slots=True)
class TickReport:
    ts: datetime
    signals: tuple[SignalEvent, ...]
    fills: tuple[FillReport, ...]
    opened: tuple[str, ...]
    closed: tuple[str, ...]
    funding_accrued: tuple[tuple[str, float], ...] = ()
    # Generic per-strategy state patches (replaces opened_min_holds +
    # consec_negative_updates): each entry is (coin, dict_to_merge_into_state).
    position_state_updates: tuple[tuple[str, dict], ...] = ()
    failed_opens: tuple[FailedOpen, ...] = ()
```

DbRecorder.save_tick_report применяет `position_state_updates` через JSON
merge на `Position.state`.

## Out of scope

- `ExchangeAdapter` Protocol — Phase 2.
- Удаление `MarginManager` — Phase 3 (после переезда на Adapter).
- `FundingHarvestStrategy` базис / dedup стратегий — Phase 6.
- Migration старого DB content. Пользователь подтвердил: wipe.

## Constraints

1. **Один source of truth для accumulators** — после F1 ни одно поле
   `_cash`/`_fees_cum`/`_funding_cum`/`_realized_pnl_cum`/`_positions`
   не должно остаться в Strategy.
2. **Backwards compat для Engine interface** — `Strategy.on_minute_tick`,
   `on_hour_tick`, `margin_watchdog` сигнатуры не меняются (Engine
   изменения только в добавлении portfolio_service в конструктор).
3. **Reconcilers пишут в PortfolioService** — никаких ссылок на Strategy.
4. **DB schema чистая** — никаких deprecated полей, никаких
   placeholder migrations. Одна финальная миграция.
5. **pytest-mock (`mocker`)** — без прямого `unittest.mock`.
6. **No new dependencies**.
7. **≤1500 lines added total, ≤500 removed**.
8. **Никакие emojis, никакие TODO comments**.

## Acceptance criteria

1. `uv run pytest` exits 0. Все существующие тесты обновлены.
2. ≥30 новых тестов в `domain/tests/` + `application/tests/`:
   - Position/ClosedPosition equality, frozen.
   - Portfolio.equity() корректно с margin reserve.
   - PortfolioService.apply_open/apply_close idempotent over restarts (rehydrate).
   - PortfolioService persists to DB on each mutation.
   - PortfolioService.set_fees_cum overwrites без double-count.
3. `git grep -n "self\._cash\b\|self\._perp_cash\b\|self\._fees_cum\b\|self\._funding_cum\b\|self\._realized_pnl_cum\b\|self\._positions\b" src/frab/strategies/` — пусто (за исключением `_n_skipped_opens_capital`).
4. `git grep -n "strategy\.set_fees_cum\|strategy\.set_funding_cum\|strategy\.cash\|strategy\.perp_cash" src/frab/` — пусто.
5. Свежий `alembic downgrade base && alembic upgrade head` на пустой
   DB проходит без ошибок.
6. `git diff --stat` — ≤25 files changed.

## Tests to run

```bash
uv run pytest src/frab/domain/ -v
uv run pytest src/frab/application/ -v
uv run pytest src/frab/strategies/tests/ -v
uv run pytest src/frab/engine/tests/ -v
uv run pytest -x  # full suite
```

## Risks

- **Equity computation moved** — `Strategy.compute_equity` уходит,
  `Engine` теперь вызывает `portfolio_service.equity(marks)`. Marks
  собирать из `engine._market_data` снепшота — не из strategy.
- **Rehydrate order matters** — PortfolioService.rehydrate должен быть
  вызван ДО `Strategy` warmup, так как Strategy будет читать portfolio
  на первом on_hour_tick.
- **Reconciler timing** — fee/funding reconciler пишут в PortfolioService;
  убедиться что не вызываются параллельно с apply_open/apply_close
  (asyncio single-thread это гарантирует, но проверить).
- **JSON column на SQLite** — sqlalchemy JSON type работает через TEXT;
  убедиться что `Position.state` mutations persist'ятся (могут потребоваться
  `MutableDict.as_mutable(JSON)`).
- **Test fixtures** — много тестов создают Strategy с `_cash=...` direct
  init kwarg или property mock. Все обновить.

## Progress reporting

Append START/DONE lines to `/tmp/F1.progress`:
```
<TS>Z START — spec read, scanning files
<TS>Z DONE domain/position.py — Position, ClosedPosition, Exchange added
<TS>Z DONE domain/portfolio.py — Portfolio + Equity added
<TS>Z DONE application/portfolio_service.py — service + DB persistence
<TS>Z DONE strategy_a.py — accumulators moved to portfolio_service
<TS>Z DONE two_phase_dynamic.py — symmetric changes
<TS>Z DONE reconcilers — write to portfolio_service instead of strategy
<TS>Z DONE engine/loop.py — equity via portfolio_service
<TS>Z DONE db/models.py — Position.exchange, state; removed strategy-specific fields
<TS>Z DONE db migration — new initial schema
<TS>Z DONE server.py — wire portfolio_service
<TS>Z DONE api/routes/margin.py + wallet.py — read from portfolio_service
<TS>Z DONE all tests — N pass
```
