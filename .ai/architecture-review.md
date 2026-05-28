# Architecture Review — 2026-05-27

## Контекст

Проект дошёл до точки, где накопление быстрых решений начало стоить реальных
денег. Прямой триггер — incident 2026-05-22: watchdog в `--dry-run` режиме
закрыл реальные позиции BTC/SOL на mainnet HL потому что `if self._dry_run`
guard'ы стояли в hour-tick OPEN/CLOSE логике стратегии, а новый watchdog
вызывал `_close_position` напрямую, обходя их. После этого:

1. Оба engine'а (local + 10.8.0.5 prod) остановлены через `launchctl`.
2. Сохранена memory `feedback-dry-run-invariant`.
3. Признана глубинная проблема — нет универсального уровня работы с биржей.
   Read/write не разделены, gateway-операции примитивны, поэтому
   гарантировать инвариант "в `dry_run` ничего не делается с позициями"
   невозможно на уровне адаптера.

Этот документ фиксирует:
- **Findings** — конкретные структурные проблемы с указанием `file:line`.
- **Целевую архитектуру** — куда движемся.
- **Migration plan** — как туда добраться без переписывания всего сразу.

### Цели рефакторинга (в порядке приоритета)

1. **Универсальный уровень работы с биржами**: один `ExchangeAdapter`
   Protocol с высокоуровневыми операциями (open/close position, get wallet,
   get positions). Все exchange-quirks (HL spot+perp wallets, transfer USDC
   между ними, spot-first paired open, margin reserve) спрятаны внутри
   адаптера. Strategy не знает на какой бирже работает.
2. **Dry-run safety as a global invariant** — `DryRunGuard` оборачивает
   мутирующие методы адаптера, после чего обойти guard невозможно.
3. **3-position модель** для одной монеты на одной бирже —
   ВНУТРИ адаптера. Strategy видит одну логическую позицию.
4. **Portfolio service** — единый источник правды для cash, accumulators,
   позиций. Strategy и Reconcilers — клиенты Portfolio.
5. **Multi-exchange readiness** — добавить Drift = новый адаптер, без
   изменений в Strategy / Engine.
6. **Уменьшить `server.py`** до настоящего orchestrator'а (<200 строк).
7. **Убрать дублирование** между двумя стратегиями (~80% общего кода).

### Что НЕ делаем в этой итерации (явно отложено)

- Drift как вторая биржа (но архитектура должна это поддерживать).
- Strategy B (stake & hedge).
- Расширение API/UI поверх новой модели.
- Авторизация, multi-tenant.
- Любые feature-additions, не относящиеся к рефакторингу.

---

## Inventory (текущая структура)

Прочитано: 17 production-модулей из ~28 (тесты, миграции, пустые `__init__`
пропущены). Не прочитано детально — `engine/{signals,state,two_phase_signals}.py`
(pure functions, low arch impact), `api/schemas.py` (DTOs), `api/ws.py`,
большинство `api/routes/*` кроме margin/wallet/strategies, `db/session.py`,
миграции.

```
src/frab/
├── server.py                    660 строк — "оркестратор" + куча домена
├── settings.py                  HL-flat config (нет per-exchange namespace)
├── cli.py                       CLI + live-smoke; reaches в server privates
├── __main__.py
├── conftest.py
│
├── db/
│   ├── models.py                schema; импортит Decision из engine — leak
│   ├── recorder.py              447 строк, god-class persistence слой
│   └── session.py / migrations/
│
├── exchanges/                   "abstractions" поверх HL
│   ├── base.py                  Executor Protocol — read+write вместе
│   ├── hyperliquid.py           HLMarketData (REST /info) — но имеет user_*
│   ├── hyperliquid_live.py      LiveHLExecutor — но имеет fetch_wallet_state
│   ├── atomic.py                AtomicExecutor — paired open/close обёртка
│   └── registry.py              только MarketData; Executor вне реестра
│
├── engine/
│   ├── loop.py                  tick loop; знает про watchdog/recorder/...
│   ├── margin_manager.py        pure logic margin policy
│   ├── reconcile.py             startup сканер FAILED/OPENING (observe-only)
│   ├── fee_reconciler.py        DB ↔ HL fee sync, ПИШЕТ в strategy
│   ├── funding_reconciler.py    тот же паттерн для funding
│   ├── signals.py               pure (Decision enum + decide())
│   ├── state.py                 MarketState (rolling funding window)
│   └── two_phase_signals.py     pure
│
├── strategies/
│   ├── base.py                  Strategy ABC + TickReport (с TwoPhase-specific полями)
│   ├── strategy_a.py            560 строк
│   ├── two_phase_dynamic.py     627 строк (~80% дубль strategy_a)
│   └── registry.py              spec factory + hot-param schema (тоже дубль)
│
├── events/bus.py                чистый pub-sub, в порядке
│
└── api/
    ├── app.py                   wiring routes, app.state.executor: object
    ├── deps.py                  только get_session
    ├── ws.py                    EventBus → WebSocket
    ├── schemas.py               DTOs
    └── routes/
        ├── strategies.py        deploy/force-tick — использует app.state.engine
        ├── margin.py            ⚠ читает strategy._margin_manager (private)
        ├── wallet.py            ⚠ duck-typing executor по методам
        └── …                    остальные routes — DB-only чтение
```

---

## Findings

Номер — приоритет (1 = блокер для целей). `[file:line]` — конкретное место.

### 1. Нет универсального ExchangeAdapter — root cause incident'а

`Executor` Protocol [exchanges/base.py:117-125] — низкоуровневые примитивы
(`submit`, `transfer_*`, `round_qty`, `get_position`, `reconcile`).
`LiveHLExecutor` дополнительно имеет `fetch_account_state`
[hyperliquid_live.py:292] и `fetch_wallet_state` [hyperliquid_live.py:335]
вне Protocol'а — `server.py` и `api/routes/wallet.py` используют их
через duck-typing ([wallet.py:100]).

**Следствия:**
- dry_run guard невозможно поставить на адаптер — каждый callsite сам
  заботится. Inсident'ный watchdog [strategy_a.py:478,482] вызывает
  `_close_position` и `transfer_perp_to_spot` без guard'а.
- Strategy знает HL-специфику: ей надо отдельно делать `transfer_spot_to_perp`
  перед открытием, потом `open_paired`, потом учитывать `perp_margin_locked`.
  На Drift это будет другая последовательность — придётся переписывать
  Strategy.

### 2. hyperliquid.py vs hyperliquid_live.py — два параллельных HL-адаптера

Token-mapping дублируется:
- [hyperliquid.py:21-28] `_SPOT_TOKEN_INVERSE = {"UBTC": "BTC", ...}`
- [hyperliquid_live.py:324] `_inverse_spot_token_map()`

Pair-name построение зеркальное:
- [hyperliquid.py:203] `_normalize_hl_coin` — для входящих fills
- [hyperliquid_live.py:108] `_make_name` — для исходящих ордеров

Asymmetry имён:
- `HLMarketData` имеет `fetch_user_fills`/`fetch_user_funding` (user-state).
- `LiveHLExecutor` имеет `fetch_wallet_state`/`fetch_account_state`/`get_position`.

Должен быть **один `HyperliquidAdapter`** с единым token-cache и
szDecimals-cache (сейчас он ещё и третий раз — два разных `_sz_decimals_cache`
живут в разных файлах).

### 3. server.py делает работу домена (660 строк, target ~150)

| Текущее место | Куда переехать |
|---|---|
| [server.py:68-72] `MAINNET_SPOT_TOKEN_MAP` | внутрь `HyperliquidAdapter` |
| [server.py:86-124] `_validate_spot_pairs` | `HyperliquidAdapter.validate_at_startup()` |
| [server.py:144-167] `_compute_auto_sizes` | `Strategy.compute_sizing(specs, budget)` |
| [server.py:170-215] `_build_margin_manager` | `MarginPolicyFactory.from_settings` |
| [server.py:271-296] `_build_wallet_snapshotter` | `WalletSnapshotter` class |
| [server.py:319-377] `_ensure_strategy`/`_mark_stopped_if_owner` | `StrategyOwnership` сервис |
| [server.py:421-481] `_rehydrate_strategy_from_db` | через Portfolio service |

### 4. Нет `Position` / `Portfolio` value objects

Капитал одной монеты на HL размазан:

| Кусочек | Где |
|---|---|
| `spot_qty`, `entry_*`, `perp_qty` | `_PositionRecord` [strategy_a.py:58-65] |
| `spot_cash` агрегат | `strategy._cash` |
| `perp_cash` агрегат | `strategy._perp_cash` |
| `required_margin` per pos | on-fly через `compute_required_margin_for_open` |
| `OpenPosition` snapshot | [margin_manager.py:26-33] |
| `(spot_cost, perp_margin)` | `compute_pair_footprint` |

Нет `Position` aggregate. Нет `Portfolio`, который сам считает
`equity`/`committed_budget`/`margin_ratio`.

### 5. Strategy смешивает 4 обязанности

`StrategyA._open_position` [strategy_a.py:382-427] делает:
1. **Sizing** (`qty = size / mark`).
2. **Order construction** (`OrderRequest`).
3. **Execution** (`executor.open_paired`).
4. **Accounting** (`_cash -= ...`, `_positions[coin] = ...`).

Принимает `executor: AtomicExecutor` концретным типом [strategy_a.py:103].
Та же картина в [two_phase_dynamic.py:449-494].

### 6. Strategy A vs Two-Phase Dynamic — ~80% дубль кода

Идентично между двумя стратегиями: `__init__`, все 6 properties,
`set_fees_cum`/`set_funding_cum`, `open_positions`, `warmup_from_history`,
`_position_size_for`, `_open_position_snapshots_for_manager`,
`on_minute_tick`, `_close_position`, `_select_weakest_open`,
`_watchdog_force_close`, `margin_watchdog`, `compute_equity`.

Различается: `on_hour_tick` (decision logic), params dataclass, position
record (TwoPhase хранит `min_hold`/`consec_negative`), `update_hot_params`
signature.

Такой же дубль в `strategies/registry.py` — `_StrategyASpec` [89-149]
и `_TwoPhaseDynamicSpec` [152-272] с одинаковой структурой и разной
`_HOT_SCHEMA` таблицей.

### 7. TickReport polymorphic, Position таблица — то же

[strategies/base.py:42-43]:
```python
opened_min_holds: tuple[tuple[str, int], ...] = ()       # для TwoPhase
consec_negative_updates: tuple[tuple[str, int], ...] = ()  # для TwoPhase
```

[db/models.py:138-139]:
```python
position_min_hold_hours: Mapped[int] = mapped_column(default=0)
consec_negative_hours: Mapped[int] = mapped_column(default=0)
```

Для двух стратегий — два per-strategy поля в общей таблице. Должен быть
`state: Mapped[dict] = mapped_column(JSON)`.

### 8. Position sizing размазан в трёх местах

- [strategy_a.py:44] `StrategyAParams.position_size_usdc` (default 1000).
- [margin_manager.py:110-112] `MarginManager.position_size_for(coin)`.
- [server.py:144-167] `_compute_auto_sizes`.

### 9. Layering violations / приватный доступ

| Где | Что трогает |
|---|---|
| [strategy_a.py:244,253] | `self._margin_manager._params` |
| [strategy_a.py:31] | импорт `frab.engine.margin_manager` (engine→strategies) |
| [api/routes/margin.py:72-89] | `strategy._margin_manager`, `_last_quotes`, `_positions`, `_params`, `_open_position_snapshots_for_manager()` |
| [cli.py:23] | `from frab.server import _hl_info_url, _select_spot_token_map` |
| [cli.py:307,314,340,350,358] | `executor._info.spot_meta`/`all_mids`/`l2_snapshot`/`user_state` — лезет в SDK напрямую (нет соответствующих методов в adapter'е) |
| [db/models.py:8] | `from frab.engine.signals import Decision` (DB → engine) |

### 10. Strategy ↔ DB ↔ HL цикл (fee/funding reconcilers)

`fee_reconciler.run_once` [engine/fee_reconciler.py:222] и
`funding_reconciler.run_once` [funding_reconciler.py:149] вызывают
`strategy.set_fees_cum`/`set_funding_cum` — мутируют strategy authoritative
суммой из HL. Strategy не source-of-truth для своих accumulators.
Правильно: убрать `_fees_cum`/`_funding_cum` из strategy, держать в
Portfolio service, reconcilers пишут в Portfolio.

### 11. Reconcile = три разных вещи под одним именем

- `engine/reconcile.py` — startup scanner FAILED/OPENING positions (observe).
- `engine/fee_reconciler.py` — periodic HL fills → DB → strategy.
- `engine/funding_reconciler.py` — periodic HL funding → DB → strategy.
- `executor.reconcile()` — no-op метод [exchanges/base.py:121].

Переименовать: `StartupHealthScan`, `FeeSync`, `FundingSync`. Executor.reconcile() убрать.

### 12. Settings — HL-flat

[settings.py:39-48] — `hl_*` префикс. Multi-exchange требует
`exchanges: dict[str, ExchangeSettings]` namespace.

### 13. exchanges/registry.py асимметричен

Только `MarketDataSource`, не `Executor`. Hardcoded единственный реестр —
HL. После рефакторинга — реестр `Adapter` factory.

### 14. AtomicExecutor типизирован концретно у стратегий

`executor: AtomicExecutor` в strategies блокирует подмену в тестах
и DryRunGuard wrapping.

---

## Целевая архитектура

### Принципы

1. **Universal ExchangeAdapter** — одна абстракция per exchange, высокоуровневая.
   Все exchange-quirks внутри.
2. **Strategy венчур-aware** — конструируется для конкретной биржи, на старте
   запрашивает `ExchangeProfile` (funding_interval_hours, default fees) и
   `MarketSpec` per coin (leverage, maint_ratio, per-coin fee overrides) у
   адаптера. Использует их для annualization сигналов, breakeven математики,
   sizing. Strategy не венчур-agnostic (на разных биржах разная экономика),
   но **infrastructure-agnostic** — работает с любым ExchangeAdapter,
   реализующим Protocol.
3. **Strategy задаёт notional + margin_reserve явно** — она знает свой
   бюджет и market_specs, считает сама.
4. **Portfolio service** — single source of truth для cash/accumulators/positions.
5. **Tick pipeline** — компоненты (risk_manager, strategy, rebalancer)
   шарят Portfolio и Adapter, исполняются последовательно в одном тике.
6. **DryRunGuard** оборачивает мутирующие методы Adapter'а на уровне инфраструктуры.
7. **Domain — pure** (Position, Portfolio, MarketSpec, ExchangeProfile, Snapshot). No I/O.

### Диаграмма

```
┌──────────────────────────────────────────────────────────────────────┐
│  Composition Root (server.py — ТОЛЬКО wiring, ~150 строк)           │
│  Lifespan, app.state, background tasks                              │
├──────────────────────────────────────────────────────────────────────┤
│  Application                                                         │
│   • Engine (tick pipeline)                                          │
│     каждый тик:                                                     │
│       snap = market_data.snapshot(); portfolio = portfolio.current()│
│       for component in [risk, strategy, rebalancer]:                │
│           await component.run(snap, portfolio, adapter)             │
│   • TickComponent (Protocol): RiskManager, Strategy, Rebalancer     │
│   • PortfolioService (owns: cash, fees_cum, funding_cum, positions) │
│   • Reconcilers (FeeSync, FundingSync → пишут в PortfolioService)   │
│   • StartupHealthScan, StrategyOwnership                            │
├──────────────────────────────────────────────────────────────────────┤
│  Domain (pure, no I/O)                                              │
│   • Exchange (HL, Drift, …)                                            │
│   • ExchangeProfile { exchange, funding_interval_hours,                   │
│                    default_spot_taker_bps, default_perp_taker_bps } │
│   • Position { exchange, coin, notional_usd, margin_reserve_usd,       │
│                entry_*, opened_at, state: dict }                    │
│   • Portfolio (immutable aggregate snapshot)                        │
│   • WalletInfo { available_usdc, reserved_usdc, total_value }       │
│   • MarketSpec (per coin: leverage_max, maint_ratio, fees overrides)│
│   • Quote, FundingTick, EquitySnapshot (есть)                       │
├──────────────────────────────────────────────────────────────────────┤
│  Infrastructure: ExchangeAdapter Protocol (один per exchange)          │
│                                                                      │
│   class ExchangeAdapter(Protocol):                                  │
│      exchange: Exchange                                                   │
│                                                                      │
│      # Reads (safe в dry-run — НЕ оборачиваются)                    │
│      async def get_exchange_profile() -> ExchangeProfile                  │
│      async def get_wallet() -> WalletInfo                           │
│      async def get_open_positions() -> list[Position]               │
│      async def get_market_specs() -> dict[str, MarketSpec]          │
│      async def fetch_quote(coin) -> Quote                           │
│      async def fetch_funding(coin) -> FundingTick                   │
│      async def fetch_user_fills(since) -> list[UserFill]            │
│      async def fetch_user_funding(since) -> list[FundingPayment]    │
│                                                                      │
│      # Writes (MUTATING — DryRunGuard оборачивает)                  │
│      async def open_position(coin, *, notional_usd,                 │
│                              margin_reserve_usd) -> Position        │
│      async def close_position(coin) -> ClosedPosition               │
│      async def adjust_margin(coin, delta_usd) -> None               │
│                                                                      │
│   ── DryRunAdapterGuard wraps ТОЛЬКО write-методы ──                │
│   ── Никакой код выше не видит raw adapter, только wrapped ──       │
│                                                                      │
│   HyperliquidAdapter имплементация прячет:                          │
│     - MAINNET_SPOT_TOKEN_MAP, szDecimals cache, retry/transient     │
│     - usdClassTransfer choreography (open: transfer → spot → perp)  │
│     - spot-first paired open/close                                  │
│     - margin reserve accounting (perp wallet vs spot wallet)        │
│                                                                      │
│   DriftAdapter, BinanceAdapter — будущие, тот же Protocol           │
│                                                                      │
│   DB (низкоуровневое persistence, не "Adapter"):                    │
│     PositionStore, EventStore, EquityStore, FundingStore            │
│     (вместо одного DbRecorder god-class)                            │
└──────────────────────────────────────────────────────────────────────┘
```

### Ключевые типы

#### Domain

```python
@dataclass(frozen=True, slots=True)
class Position:
    exchange: Exchange
    coin: str
    notional_usd: float          # market value of the delta-neutral pair
    margin_reserve_usd: float    # USDC locked at exchange for margin buffer
    entry_spot_price: float
    entry_perp_price: float
    opened_at: datetime
    funding_collected: float
    fees_paid: float
    state: dict                  # per-strategy state (e.g. min_hold)

@dataclass(frozen=True, slots=True)
class WalletInfo:
    exchange: Exchange
    available_usdc: float        # free для использования стратегией
    reserved_usdc: float         # уже под margin / locked
    total_value_usd: float       # incl. spot tokens at mark

@dataclass(frozen=True, slots=True)
class ExchangeProfile:
    """Per-exchange static facts. Strategy queries on init для annualization
    сигналов, breakeven математики, default fees."""
    exchange: Exchange
    funding_interval_hours: float       # HL: 1.0  Binance: 8.0  Drift: 1.0
    periods_per_year: float             # 24*365/funding_interval_hours
    default_spot_taker_bps: float
    default_perp_taker_bps: float


@dataclass(frozen=True, slots=True)
class MarketSpec:
    coin: str
    has_spot: bool
    has_perp: bool
    max_leverage: int                       # for perp
    maint_ratio: float
    min_size: float
    tick_size: float
    spot_taker_bps: float | None = None     # None → use ExchangeProfile default
    perp_taker_bps: float | None = None

@dataclass(frozen=True, slots=True)
class Portfolio:
    """Immutable snapshot used within one tick."""
    positions: tuple[Position, ...]
    wallet_per_exchange: dict[Exchange, WalletInfo]
    cash_available_per_exchange: dict[Exchange, float]  # = wallet.available_usdc
    fees_cum: float
    funding_cum: float
    realized_pnl_cum: float
```

#### Application

```python
class PortfolioService:
    """Owns mutable portfolio state. Single source of truth."""
    async def current(self) -> Portfolio: ...
    async def apply_open(self, pos: Position) -> None: ...
    async def apply_close(self, exchange, coin, pnl, fees) -> None: ...
    async def sync_accumulators_from_db(self) -> None: ...   # called by reconcilers

class TickComponent(Protocol):
    """Runs once per engine tick. May call adapter (write) or just observe."""
    async def run(self, snap: MarketSnapshot, portfolio: Portfolio,
                  adapter: ExchangeAdapter) -> None: ...

class Strategy(TickComponent):
    """Main strategy: decides entry/exit. Computes notional + margin_reserve
    per coin from budget allocation + market_specs."""

class RiskManager(TickComponent):
    """Margin watchdog: tops up or forces close on margin breach.
    Runs BEFORE Strategy in tick pipeline."""

class Rebalancer(TickComponent):
    """Optional: redistributes capital across coins/exchanges."""

class Engine:
    """Tick pipeline."""
    components: list[TickComponent]   # ordered: [risk, strategy, rebalancer]
    async def tick_once(self, now): ...
```

#### Infrastructure

```python
class ExchangeAdapter(Protocol):
    exchange: Exchange
    # Reads
    async def get_wallet(self) -> WalletInfo: ...
    async def get_open_positions(self) -> list[Position]: ...
    async def get_market_specs(self) -> dict[str, MarketSpec]: ...
    async def fetch_quote(self, coin: str) -> Quote: ...
    async def fetch_funding(self, coin: str) -> FundingTick: ...
    # ... другие reads
    # Writes (wrapped)
    async def open_position(self, coin: str, *,
                            notional_usd: float,
                            margin_reserve_usd: float) -> Position: ...
    async def close_position(self, coin: str) -> ClosedPosition: ...
    async def adjust_margin(self, coin: str, delta_usd: float) -> None: ...

class DryRunAdapterGuard:
    """Forwards reads to underlying. Synthesises paper-fills for writes
    using current adapter quotes; never calls exchange."""
    def __init__(self, underlying: ExchangeAdapter, *, slippage_bps: float):
        ...
    async def open_position(self, coin, *, notional_usd, margin_reserve_usd):
        # use underlying.fetch_quote(coin) to synth a fill, return paper Position
        ...
```

### Где живёт dry_run

В `server.build_app`:
```python
adapter = HyperliquidAdapter.from_settings(s)
if s.dry_run:
    adapter = DryRunAdapterGuard(adapter, slippage_bps=s.dry_run_slippage_bps)
```

После этого **никакой код** (включая будущие watchdog'и, rebalancer'ы,
manual API endpoints) не может случайно мутировать биржу. Guard на самом
низком уровне.

---

## Migration plan (поэтапный)

Принцип: каждый этап оставляет систему рабочей, каждый этап = PR с тестами.

### Phase 0 — Safety net (БЛОКЕР, делать первым)

**Цель:** dry_run-safe before prod restart.

Ввести минимальный `ExchangeAdapter` Protocol с 3 write-методами
(`open_position`, `close_position`, `adjust_margin`), реализовать
`DryRunAdapterGuard`. На данном этапе оборачивать только эти 3 операции,
остальной код пока использует старый Executor.

Поскольку Strategy сейчас вызывает `_close_position`/`transfer_*` напрямую —
**компромисс на Phase 0**: в Strategy сохранить `if self._dry_run: return`
проверки во ВСЕХ вызовах executor'а (в т.ч. watchdog'е), даже если выглядит
duplicative. После Phase 1+2 эти проверки убираются вместе с миграцией
Strategy на Adapter.

**Файлы изменений:**
- `src/frab/strategies/strategy_a.py`: добавить `if self._dry_run: return`
  в `_watchdog_force_close`, `margin_watchdog` (для transfer_spot_to_perp).
- `src/frab/strategies/two_phase_dynamic.py`: то же.
- `src/frab/strategies/tests/test_dry_run_invariant.py`: новый тест,
  проверяет что во всех методах с `dry_run=True` executor не вызван.

**Размер:** ~50 строк + тест. Тривиально, разблокирует prod restart.

---

### Phase 1 — PortfolioService + Position/WalletInfo

**Цель:** материализовать domain объекты, забрать accumulators у Strategy.

1. Создать `frab/domain/` пакет: `position.py`, `wallet.py`, `portfolio.py`,
   `market_spec.py`.
2. Создать `frab/application/portfolio_service.py` — owns mutable state,
   API: `current()`, `apply_open()`, `apply_close()`, `sync_*_from_db()`.
3. Reconcilers (fee/funding) пишут в PortfolioService вместо
   `strategy.set_fees_cum`/`set_funding_cum`. Strategy property — read-only
   через `portfolio.current().fees_cum`.
4. `MarginManager.OpenPosition` → `Position` value object.
5. `Strategy.compute_equity` → `Portfolio.equity()`.
6. DB migration: добавить `Position.exchange` (default "hyperliquid"),
   `Position.state` JSON. Удалить `position_min_hold_hours` /
   `consec_negative_hours` — переехать в `state`.
7. Тесты: `test_portfolio_service.py`, `test_position.py`.

**Размер:** ~500 строк + миграция + тесты. Strategy внутренне меняется,
но публичный контракт `on_hour_tick → TickReport` остаётся для совместимости
с Engine.

---

### Phase 2 — HyperliquidAdapter consolidation

**Цель:** один HL-адаптер, реальный Protocol для всех exchange-операций.

1. Создать `frab/exchanges/hyperliquid/adapter.py` с `HyperliquidAdapter`
   имплементацией `ExchangeAdapter` Protocol.
2. Внутри: единый token-cache, единый szDecimals-cache, retry/transient
   policy.
3. Имплементировать высокоуровневые операции:
   - `open_position(coin, notional, margin_reserve)`:
     internally: `transfer_spot_to_perp(margin_reserve)` →
     `open_paired(spot_first)` → возвращает `Position`.
   - `close_position(coin)`: `close_paired` → `transfer_perp_to_spot` →
     возвращает `ClosedPosition`.
   - `adjust_margin(coin, delta)`: `transfer_spot_to_perp(+delta)` или
     обратно.
4. Старые `hyperliquid.py`/`hyperliquid_live.py`/`atomic.py` → внутренние
   `_market.py`/`_orders.py`/`_paired.py`/`_transfers.py`. Удалить
   `MAINNET_SPOT_TOKEN_MAP` из `server.py`.
5. `DryRunAdapterGuard` wraps ВСЕ writes — теперь все 3 операции защищены.
6. `exchanges/registry.py` → реестр `Adapter` factory: `name → Adapter`.
7. Тесты: `test_hyperliquid_adapter.py` (интеграционные с respx).

**Размер:** ~400 строк перекомпоновки + ~200 новых.

---

### Phase 3 — Strategy → ExchangeAdapter

**Цель:** Strategy больше не знает про executor/atomic/transfers.

1. `Strategy.__init__` принимает `adapter: ExchangeAdapter` (Protocol)
   вместо `AtomicExecutor`.
2. `Strategy._open_position` — теперь:
   ```python
   pos = await adapter.open_position(
       coin,
       notional_usd=self._compute_notional(coin, portfolio.cash_available),
       margin_reserve_usd=self._compute_margin_reserve(coin),
   )
   await portfolio_service.apply_open(pos)
   ```
3. `Strategy._compute_notional` / `_compute_margin_reserve` — стратегия сама
   считает по budget / market_specs (специфики leverage уходят из server.py).
4. Watchdog → `RiskManager` (отдельный `TickComponent`). Вызывает
   `adapter.adjust_margin(coin, +X)` или `adapter.close_position(coin)`.
5. Убрать все `if self._dry_run` проверки из Strategy — теперь guard внизу.
6. Удалить `MarginManager` после полной миграции его методов в Strategy
   или Domain (`Portfolio.margin_ratio()`).

**Размер:** ~300 строк refactor. После этого Strategy полностью
infrastructure-agnostic.

---

### Phase 4 — Engine pipeline (TickComponent)

**Цель:** Engine не знает про watchdog/strategy separately — просто
последовательно вызывает компоненты.

1. `TickComponent` Protocol.
2. Engine принимает `components: list[TickComponent]`.
3. `Strategy`, `RiskManager` имплементируют TickComponent.
4. Engine.tick_once: snap → portfolio = service.current() →
   for component in components: await component.run(snap, portfolio, adapter).
5. Server: `engine = Engine([RiskManager(...), MyStrategy(...)])` (порядок
   фиксирован).

**Размер:** ~200 строк refactor.

---

### Phase 5 — Server slim-down

**Цель:** server.py <200 строк.

1. Вынести: `_compute_auto_sizes`, `_validate_spot_pairs`,
   `_build_wallet_snapshotter`, `_ensure_strategy`,
   `_rehydrate_strategy_from_db` (большинство уже переехало в
   Phases 1-4 — финальная зачистка).
2. `cli.py`: удалить импорты из `server.py` privates.

---

### Phase 6 — Strategy dedup (DecisionPolicy)

**Цель:** одна `FundingHarvestStrategy` + 2 DecisionPolicy.

1. Создать `FundingHarvestStrategy` — общий код двух стратегий.
2. Извлечь `EntryExitThresholdPolicy` (A) и `TwoPhasePolicy` (TwoPhase).
3. `TickReport` — убрать TwoPhase-specific поля; generic
   `position_state_updates`.
4. `strategies/registry.py` — общий `StrategySpec` базис.

**Размер:** -500 строк (удаление дубля) + 200 (база) = net -300.

---

### Phase 7 — DB recorder breakup

`DbRecorder` god-class → разбить по агрегатам (`PositionStore`,
`FillStore`, `EquityStore`, ...). Может объединиться с `PortfolioService`
(persistence-layer для портфолио).

---

### Phase 8 — Multi-exchange (опционально)

Добавить `DriftAdapter`. Settings: `hl_*` → `exchanges.hyperliquid.*`
namespace. PortfolioService уже multi-exchange (Phase 1 заложил exchange в Position).

---

## Открытые вопросы

1. **DB migration для Position.state JSON**: оставить
   `position_min_hold_hours`/`consec_negative_hours` columns как есть
   (с deprecation) или сразу удалить?
2. **PortfolioService — persistence стратегия**: hold in-memory + flush on
   change, или каждый `apply_*` сразу пишет в DB?
3. **MarketSpec.max_leverage** — для HL это max разрешённый бирже плечо;
   стратегия использует своё ниже. Это поле в spec или в `StrategyConfig.leverage_per_coin`?
   (Сейчас в `PER_COIN_PARAMS_JSON.leverage`.)
4. **Какие фазы начинаем сейчас?** Минимум — Phase 0 для prod-restart.
   Дальше — Phase 1 (Portfolio) разблокирует много чего, но это самая
   большая фаза.
5. **Отдельные `.ai/task-*.md` для каждой фазы?** Phase 0 можно расписать
   как `task-f0-dry-run-guard.md` за ~30 минут.

---

## Связанные документы

- [feedback-dry-run-invariant memory](../../.claude/projects/-Users-d-prj-funding-rate-arbitrage/memory/feedback_dry_run_invariant.md)
- [project-margin-policy memory](../../.claude/projects/-Users-d-prj-funding-rate-arbitrage/memory/project_margin_policy.md)
- [Phase 4 atomic execution memory](../../.claude/projects/-Users-d-prj-funding-rate-arbitrage/memory/project_phase4_atomic_execution.md)
