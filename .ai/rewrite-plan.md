# Rewrite Plan: FarbPosition Domain + DB-as-Bus

## Context (2026-05-28)

После 18 коммитов F1.4 рефакторинга (на ветке `main`, head=`4dfbd03`) принято решение остановить incremental cleanup и **переписать с чистого листа**. Token-burn / working-feature rate стал плохой, архитектура накопила confusion (live.py vs adapter.py, cash semantics gap, position state в двух местах).

**Новый подход:** tools-first. Удалить всю strategy logic + bookkeeping mid-tier, привести инструменты к норме, написать стратегию с нуля на чистых tools. DB — шина между компонентами; in-memory только текущая операция.

Все предыдущие commits (F1.1-F1.4f-i, F2.x) остаются в `main` как archive. Новая работа поверх.

## Domain model

```python
# Single-leg primitive
class Instrument(Enum):
    SPOT = "spot"
    PERP = "perp"
    COLLATERAL = "collateral"   # margin reservation на perp wallet

class Side(Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"   # для COLLATERAL

@dataclass(frozen=True)
class Position:
    id: int | None
    exchange: Exchange
    coin: str                   # BTC, USDC, USDT
    instrument: Instrument
    side: Side
    qty: float
    entry_price: float
    opened_at: datetime
    closed_at: datetime | None
    status: PositionStatus      # OPEN, CLOSED
    farb_position_id: int | None

# Composite — strategy-level (funding arb on one coin)
class FarbState(Enum):
    CHECK_MARGIN = "check_margin"
    OPENING_MARGIN = "opening_margin"
    MARGIN_RESERVED = "margin_reserved"
    OPENING_LONG = "opening_long"
    LONG_OPENED = "long_opened"
    OPENING_SHORT = "opening_short"
    OPEN = "open"
    CLOSING_SHORT = "closing_short"
    SHORT_CLOSED = "short_closed"
    CLOSING_LONG = "closing_long"
    LONG_CLOSED = "long_closed"
    RELEASING_MARGIN = "releasing_margin"
    CLOSED = "closed"
    FAILED = "failed"

@dataclass(frozen=True)
class FarbPosition:
    id: int | None
    strategy_id: int
    coin: str
    state: FarbState
    state_data: dict            # TwoPhaseDynamic-specific (min_hold_hours, phase, consec_negative)
    spot_position_id: int | None
    perp_position_id: int | None
    margin_position_id: int | None
    opened_at: datetime
    closed_at: datetime | None
```

State machine = source of truth для recovery. Restart → SELECT FarbPositions WHERE state NOT IN (OPEN, CLOSED, FAILED) → resume.

## Component contracts

**Exchange (stateless tool):**
```python
class Exchange(Protocol):
    name: str
    async def get_quote(coin: str) -> Quote
    async def get_funding_rate(coin: str) -> FundingTick
    async def get_meta() -> list[MarketSpec]
    async def open_position(req: OpenRequest) -> Position   # API → write DB → return
    async def close_position(pos: Position) -> ClosedPosition
    async def get_open_positions() -> list[Position]        # fresh fetch + reconcile
    async def get_accrued_funding(pos: Position) -> float
    async def get_wallet(coin: str) -> float                # free balance
    async def transfer(from_wallet, to_wallet, amount) -> None
```

Каждый вызов: API call → DB write → return. Никакого in-memory кэша позиций.

**FarbRepo (тонкий DAO):**
- INSERT/UPDATE/SELECT FarbPosition rows
- Atomic state transitions (UPDATE WHERE state=expected)
- No business logic

**Ledger (stateless aggregator):**
```python
async def compute_equity(strategy_id, exchange_marks) -> EquitySnapshot:
    # SELECT open FarbPositions → spot_value, perp_unrealized, margin
    # SELECT SUM(fees), SUM(funding), SUM(realized)
    # SELECT cash per exchange from wallet_snapshots
    # return formula
```

Никакого state. Каждый вызов — fresh SELECT.

**Strategy (TwoPhaseDynamic v2):**
- on_tick: SELECT open FarbPositions → для каждого не-в-steady-state advance state machine → compute signals → check exits → check new candidates → создать FarbPosition(state=CHECK_MARGIN)
- Rollback: Strategy сама закрывает успешные ноги если последующая упала (Q4=a)
- Никакого in-memory accumulator state

**Engine loop:**
- Простой asyncio scheduler: minute tick / hour tick → strategy.on_*
- Никакого equity computation, никакого bookkeeping

## Decisions (записаны для исторической памяти)

- **Q1: margin = Position (instrument=COLLATERAL)** — единый примитив; явно видно "столько USDC на perp wallet"
- **Q2: Exchange stateless** — read → write DB → return; in-memory минимум
- **Q3: FarbPosition explicit state machine в DB** — crash recovery бесплатно
- **Q4: Strategy сама rollback'ит** atomicity (не AtomicExecutor)
- **Q5: Ledger отдельный stateless сервис** — сканирует DB, считает
- **Wallet (b)**: wallet balance — отдельный концепт (`wallet_snapshots` table), не Position. Меняется любой операцией, не только open/close lifecycle.

## DB schema (single new initial migration в Step 3)

```
exchanges        (id, name UNIQUE, funding_interval_h, spot_taker_bps, perp_taker_bps)
markets          (id, exchange_id, coin, has_spot, has_perp, min_size, tick_size, UNIQUE(exchange_id, coin))

funding_rates    (id, exchange_id, coin, ts_ms, rate, premium, annualized_pct,
                  UNIQUE(exchange_id, coin, ts_ms))
prices           (id, exchange_id, coin, ts_ms, mark, spot, bid, ask,
                  UNIQUE(exchange_id, coin, ts_ms))

strategies       (id, name, version, params_json, status, started_at_ms, stopped_at_ms)

positions        (id, exchange_id, coin, instrument, side, qty, entry_price,
                  opened_at, closed_at, status, farb_position_id FK nullable)
farb_positions   (id, strategy_id, coin, state, state_data JSON,
                  spot_position_id FK, perp_position_id FK, margin_position_id FK,
                  opened_at, closed_at)

fills            (id, position_id FK, ts_ms, side, qty, price, fee, slippage_bps, is_paper)
funding_accruals (id, position_id FK, ts_ms, amount)

wallet_snapshots (id, exchange_id, coin, ts_ms, balance, source)

equity_snapshots (id, strategy_id, ts_ms, total_equity, cash, spot_value,
                  perp_unrealized, perp_realized_cum, funding_cum, fees_cum)

events           (id, ts_ms, level, source, kind, message, payload_json)
```

## 9-Step Plan

1. **Hard delete** (Sonnet): strategies, application/portfolio_service.py, domain/*, engine/loop.py, engine/state.py, exchanges/dry_run.py, exchanges/base.py (старый Protocol), exchanges/hyperliquid/adapter.py, все migrations, data/frab.db, integration replay tests. Narrow: server.py / cli.py / api routes / test fixtures.
2. **Tools cleanup** (Sonnet): bridge tokens whitelist в exchanges/hyperliquid/tokens.py + live.py vs reader.py merge в один HLExchange.
3. **Новый domain + migration** (Sonnet): Position v2, FarbPosition, FarbState enum, одна new initial Alembic migration под полную новую схему.
4. **Exchange Protocol + реализации** (Sonnet): HLExchange — единственная реализация, stateless. PaperExchange отброшен per user decision (shadow mode придёт позже через другой механизм).
5. **FarbRepo** (Sonnet): DAO + atomic state transitions.
6. **Ledger** (Sonnet): stateless aggregator.
7. **TwoPhaseDynamic v2** (Sonnet): decision tree + state machine driver, на чистых tools.
8. **Engine loop v2** (Sonnet): простой scheduler.
9. **Final test pass** (Sonnet): минимальный набор тестов (db, exchange, ledger, strategy decision tree).

После каждого шага: коммит + push (project rule). Без `Co-Authored-By`.

## Step 1 spec (delegate to Sonnet first thing after compact)

**Goal:** clean slate. Удалить strategy + bookkeeping + migrations + DB. Tree compiles, surviving tests pass.

**Hard delete (`git rm`):**
- `src/frab/strategies/` (полностью, включая tests/)
- `src/frab/application/portfolio_service.py` + `src/frab/application/tests/`
- `src/frab/domain/` (полностью)
- `src/frab/engine/loop.py` + его тесты (`src/frab/engine/tests/test_loop*.py`)
- `src/frab/engine/state.py` + его тесты
- `src/frab/exchanges/dry_run.py` + тесты
- `src/frab/exchanges/base.py` (старый ExchangeAdapter Protocol, paired DTO) — НЕ путать с `exchanges/hyperliquid/`
- `src/frab/exchanges/hyperliquid/adapter.py` + его тесты (Adapter wrapping pattern уйдёт; live.py/reader.py остаются, в Step 2 сольются)
- `src/frab/db/migrations/versions/*.py` (все 9, оставить `.keep`)
- `data/frab.db` (бэкапы `.bak` НЕ трогать)
- `tests/integration/test_replay_*.py` (все)

**Narrow (поправить, не удалять):**
- `src/frab/server.py`: убрать strategy/engine/portfolio_service из lifespan. Оставить FastAPI + DB session. `app.state.exchange = None` placeholder.
- `src/frab/__main__.py` / `src/frab/cli.py`: убрать `serve` engine bootstrap. Оставить `init-db` (alembic upgrade head — после Step 3 будет работать; пока что просто Base.metadata.create_all через fallback), `seed`. Команды `start-strategy` / `stop-strategy` — удалить.
- `src/frab/api/routes/`: routes которые читают `strategy.*` или `app.state.portfolio_service` — заглушить (return 503 "engine not configured") ИЛИ переключить на прямой DB SELECT где тривиально. Decide per route, priority — компиляция + green tests, не функциональность.
- `tests/conftest.py` + любые fixtures: `alembic upgrade head` → `Base.metadata.create_all(bind=sync_engine)` (модели в `db/models.py` ещё там).

**Survives:**
- `src/frab/db/models.py` (модели остаются, заменим в Step 3)
- `src/frab/db/recorder.py` (узким сделаем в Step 5)
- `src/frab/db/session.py`
- `src/frab/exchanges/hyperliquid/{reader,live,tokens}.py` (cleanup в Step 2, replace в Step 4)
- `src/frab/engine/signals.py` (pure)
- `src/frab/events/bus.py`
- `src/frab/api/routes/` (большинство выживает после narrow)

**Acceptance gates:**
1. `uv run python -c "import frab"` — exit 0
2. `uv run python -m frab --help` — exit 0
3. `uv run pytest` — exit 0 (тестов осталось мало, но все green)
4. `git grep -n "PortfolioService\|StrategyA\|TwoPhaseDynamic\|class Strategy\b" src/frab/` — empty (за исключением possible test stubs)
5. `ls src/frab/db/migrations/versions/*.py` — empty (только .keep)
6. `ls data/frab.db` — file not found

**Commit:** `refactor: hard delete strategies/portfolio/engine + migrations — fresh slate for FarbPosition redesign`. Без `Co-Authored-By`. Push.

**Report:** список удалённых директорий (категории), список narrow'нутых файлов, итоговый `pytest` summary (`N passed, M deleted`), commit hash.

## Что после Step 1

Дерево минималистичное:
- DB layer (models + session + recorder, схема старая но скоро rewrite)
- Exchange layer (HL reader/live/tokens + paper + events bus, до cleanup)
- API routes (заглушенные / DB-only)
- signals.py (pure)

Готово к Step 2 (tools cleanup) → Step 3 (new schema).
