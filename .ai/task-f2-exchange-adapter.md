# Task F2: ExchangeAdapter Protocol + HyperliquidAdapter consolidation

Phase 2 рефакторинга по `.ai/architecture-review.md`. **Зависит от F1
(Portfolio domain).**

## Goal

Ввести универсальный `ExchangeAdapter` Protocol с высокоуровневыми
операциями (`open_position`, `close_position`, `adjust_margin`,
`get_open_positions`, `get_wallet`, `get_market_specs`, market data reads).
Реализовать `HyperliquidAdapter` который консолидирует существующие
`hyperliquid.py` + `hyperliquid_live.py` + `atomic.py` в один agрегат,
прячущий все exchange-quirks (token map, transfers, spot-first paired open,
margin reserve choreography).

Реализовать `DryRunAdapterGuard` — обёртка над `ExchangeAdapter` которая
forwards reads и synthesises paper-fills для writes. После F2 dry_run
guard живёт на одном уровне infrastructure: невозможно обойти.

**Не делаем в этой фазе** (отложено в F3):
- Migration `Strategy` на `ExchangeAdapter` — strategies продолжают
  использовать `AtomicExecutor` напрямую. Adapter существует параллельно,
  используется только server'ом для wallet snapshot, smoke CLI, и future
  RiskManager.
- Удаление `MarginManager` — позже.

## Pre-condition

- F1 закоммичен и протестирован: `PortfolioService` существует,
  `Position`/`WalletInfo`/`MarketSpec` domain объекты есть.

## Files

### Add

- `src/frab/exchanges/adapter.py` — `ExchangeAdapter` Protocol +
  shared DTOs (`OpenPositionResult` = `Position` from domain;
  `ClosedPosition` from domain).
- `src/frab/exchanges/dry_run.py` — `DryRunAdapterGuard`.
- `src/frab/exchanges/hyperliquid/__init__.py`
- `src/frab/exchanges/hyperliquid/adapter.py` — `HyperliquidAdapter`
  (главный класс, имплементирует `ExchangeAdapter`).
- `src/frab/exchanges/hyperliquid/_market.py` — приватный: rest /info reads.
- `src/frab/exchanges/hyperliquid/_account.py` — приватный: wallet/positions reads.
- `src/frab/exchanges/hyperliquid/_orders.py` — приватный: order submit + transfers.
- `src/frab/exchanges/hyperliquid/_paired.py` — приватный: spot-first open/close (был AtomicExecutor).
- `src/frab/exchanges/hyperliquid/_tokens.py` — единый token map + sz_decimals cache.
- `src/frab/exchanges/tests/test_adapter_protocol.py` — runtime_checkable check на оба implementations.
- `src/frab/exchanges/tests/test_dry_run_guard.py` — invariant: no underlying writes called.
- `src/frab/exchanges/hyperliquid/tests/__init__.py`
- `src/frab/exchanges/hyperliquid/tests/test_adapter.py` — интеграционный с respx.
- `src/frab/exchanges/hyperliquid/tests/test_paired.py` — был test_atomic.
- `src/frab/exchanges/hyperliquid/tests/test_tokens.py` — token resolution.

### Move (rename, content переезжает)

- `src/frab/exchanges/hyperliquid.py` → удалить после переноса логики
  в `hyperliquid/_market.py` + `hyperliquid/_account.py`.
- `src/frab/exchanges/hyperliquid_live.py` → удалить после переноса в
  `hyperliquid/_orders.py` + `hyperliquid/_account.py`.
- `src/frab/exchanges/atomic.py` → переехать в `hyperliquid/_paired.py`
  (но: `PairedOpenResult`/`PairedCloseResult` DTOs оставить в общем
  `exchanges/_paired_results.py`, т.к. они exchange-agnostic).
- `src/frab/exchanges/tests/test_hyperliquid.py` → разнесено по
  новым test файлам.
- `src/frab/exchanges/tests/test_hyperliquid_live.py` → разнесено.
- `src/frab/exchanges/tests/test_atomic.py` → `hyperliquid/tests/test_paired.py`
  (поскольку spot-first логика exchange-specific — на Drift другая).

### Modify

- `src/frab/exchanges/base.py` — оставить старые `Executor` Protocol +
  DTOs (Quote, FundingTick, ...) **на время transition** (F3 удалит
  `Executor`). Добавить `Direction` enum для `TransferRequest`.
- `src/frab/exchanges/registry.py` — теперь реестр **Adapter** factories
  (`name → Callable[..., ExchangeAdapter]`). Старый MarketData реестр
  удалить.
- `src/frab/server.py`:
  - Использовать `HyperliquidAdapter.from_settings(s, portfolio_service)`
    вместо ручной сборки `HLMarketData` + `LiveHLExecutor` + `AtomicExecutor`.
  - Оборачивать в `DryRunAdapterGuard` если `settings.dry_run`.
  - `executor = adapter._paired_router` для backwards compat со
    strategies (они продолжают принимать AtomicExecutor-shaped). Это
    временный hack до F3.
  - Удалить `_select_spot_token_map`, `_validate_spot_pairs`,
    `MAINNET_SPOT_TOKEN_MAP`, `_hl_info_url`, `_build_executor`,
    `_build_wallet_snapshotter`.
  - Adapter.startup_validate() заменяет _validate_spot_pairs.
- `src/frab/cli.py`:
  - Удалить импорты `_hl_info_url`, `_select_spot_token_map` из server.
  - `_build_smoke_clients` → возвращает `HyperliquidAdapter`.
  - `live-smoke` команды используют `adapter.fetch_quote`, `adapter.get_wallet`,
    etc. Никакого `executor._info.spot_meta` прямого доступа — выставить
    нужные методы на adapter (`adapter.fetch_spot_meta()`).
- `src/frab/api/routes/wallet.py` — duck-typing убрать; читать
  `app.state.adapter.get_wallet()` (live) или из portfolio_service (paper-mode
  detected via `isinstance(adapter, DryRunAdapterGuard)`).
- Все engine/reconcilers — продолжают принимать `market_data` параметр;
  теперь это `adapter` (Protocol совместим: `fetch_funding`, `fetch_quote`,
  `fetch_user_fills`, `fetch_user_funding` все на adapter).

## Public surface

### Protocol

`src/frab/exchanges/adapter.py`:

```python
from typing import Protocol, runtime_checkable
from frab.domain.position import Position, ClosedPosition, Exchange
from frab.domain.wallet import WalletInfo
from frab.domain.market_spec import MarketSpec
from frab.exchanges.base import (
    Quote, FundingTick, FundingPayment, UserFill, OrderRequest, FillReport,
)
from frab.exchanges._paired_results import PairedOpenResult, PairedCloseResult


@runtime_checkable
class ExchangeAdapter(Protocol):
    """Universal per-exchange trading interface.

    Strategy talks to this and ONLY this. All exchange quirks (separate
    wallets, transfer choreography, spot-first ordering, token name
    mapping, szDecimals rounding) live inside concrete implementations.

    Reads are always safe in dry-run.
    Writes (open_position, close_position, adjust_margin) are MUTATING
    and MUST be wrapped by DryRunAdapterGuard when dry_run=True.
    """
    exchange: Exchange

    # ── reads (safe in dry-run) ──────────────────────────────────────

    async def get_exchange_profile(self) -> ExchangeProfile:
        """Per-exchange static facts: funding_interval_hours, periods_per_year,
        default fees. Strategy queries on init для annualization сигналов,
        breakeven математики, sizing."""

    async def get_wallet(self) -> WalletInfo: ...
    async def get_open_positions(self) -> list[Position]: ...
    async def get_market_specs(self) -> dict[str, MarketSpec]: ...

    async def fetch_quote(self, coin: str) -> Quote: ...
    async def fetch_funding(self, coin: str) -> FundingTick: ...
    async def fetch_funding_history(
        self, coin: str, since_ms: int,
    ) -> list[FundingTick]: ...

    async def fetch_user_fills(self, since_ms: int) -> list[UserFill]: ...
    async def fetch_user_funding(self, since_ms: int) -> list[FundingPayment]: ...

    async def startup_validate(self, coins: tuple[str, ...]) -> None:
        """Fail-fast: ensure every coin has the required markets and
        meta on the exchange. Called once at server startup."""

    # ── writes (MUTATING — DryRunGuard wraps) ────────────────────────

    async def open_position(
        self, coin: str, *,
        notional_usd: float,
        margin_reserve_usd: float,
        client_ref: str | None = None,
    ) -> Position:
        """Open delta-neutral spot+perp pair sized to `notional_usd`,
        with `margin_reserve_usd` locked at the exchange's perp wallet
        beyond the initial margin requirement.

        Implementation handles all exchange-specific choreography
        (transfer USDC to perp wallet, spot buy first, perp short sized
        to actual spot delta, etc.). Raises if any leg fails.
        """

    async def close_position(self, coin: str) -> ClosedPosition:
        """Close both legs and release margin. Returns realized PnL +
        released_margin_usd for portfolio update."""

    async def adjust_margin(
        self, coin: str, delta_usd: float,
    ) -> None:
        """Top-up (delta > 0) or release (delta < 0) margin reserve.
        Positive: transfer cash → perp wallet. Negative: transfer back."""

    # ── lifecycle ────────────────────────────────────────────────────

    async def close(self) -> None:
        """Release HTTP client + any other resources."""
```

### DryRunGuard

`src/frab/exchanges/dry_run.py`:

```python
class DryRunAdapterGuard:
    """Wraps an ExchangeAdapter. Forwards reads. Synthesises paper fills
    for writes — NEVER calls the underlying writer.

    Fill price: derived from current quote (BUY: ask × (1 + slip),
    SELL: bid × (1 - slip), PERP: mark + half-spread + slip).
    Fees: from exchange fee table (or override).

    Position returned from open_position is fully populated and can be
    fed into PortfolioService.apply_open identically to a real fill.
    """
    def __init__(
        self,
        underlying: ExchangeAdapter,
        *,
        slippage_bps: float = 2.0,
        fee_table: dict[str, float] | None = None,
        clock_fn: Callable[[], datetime] | None = None,
    ) -> None: ...

    @property
    def exchange(self) -> Exchange:
        return self._underlying.exchange

    # ── reads: forward as-is ──
    async def get_exchange_profile(self): return await self._underlying.get_exchange_profile()
    async def get_wallet(self): return await self._underlying.get_wallet()
    async def get_open_positions(self): return await self._underlying.get_open_positions()
    # ... etc forward

    # ── writes: synthesise ──
    async def open_position(self, coin, *, notional_usd, margin_reserve_usd, client_ref=None):
        quote = await self._underlying.fetch_quote(coin)
        spec = (await self._underlying.get_market_specs())[coin]
        # synth fill price + fees, return Position
        ...

    async def close_position(self, coin):
        # synth realized PnL from current quote
        ...

    async def adjust_margin(self, coin, delta_usd):
        # no-op (paper mode)
        return None
```

### HyperliquidAdapter

`src/frab/exchanges/hyperliquid/adapter.py` (skeleton):

```python
class HyperliquidAdapter:
    """Universal adapter for Hyperliquid. Hides:
      - separate spot/perp wallets (uses usdClassTransfer for moves)
      - wrapped token names (UBTC for BTC on mainnet spot)
      - spot-first paired open/close (avoids fee-in-asset accounting issues)
      - margin reserve accounting (debits/credits perp wallet)
      - szDecimals rounding and HL meta cache
    """
    exchange = Exchange.HYPERLIQUID

    def __init__(
        self, *,
        private_key: str | None,
        account_address: str | None,
        network: Literal["testnet", "mainnet"],
        portfolio_service: PortfolioService,
        slippage: float = 0.01,
        bus: EventBus | None = None,
    ) -> None:
        self._market = _HLMarketReader(network=network, ...)        # внутр.
        self._account = _HLAccountReader(network=network, address=account_address, ...)
        self._orders = _HLOrderWriter(network=network, key=private_key, address=account_address, ...)
        self._paired = _HLPairedRouter(self._orders, self._account, bus)
        self._tokens = _TokenMap.for_network(network)
        self._portfolio = portfolio_service
        self._slippage = slippage

    @classmethod
    def from_settings(cls, s: Settings, portfolio_service: PortfolioService, bus: EventBus) -> "HyperliquidAdapter":
        return cls(
            private_key=s.hl_private_key.get_secret_value() if s.hl_private_key else None,
            account_address=s.hl_account_address,
            network=s.hl_network,
            portfolio_service=portfolio_service,
            slippage=s.hl_live_slippage,
            bus=bus,
        )

    _PROFILE = ExchangeProfile(
        exchange=Exchange.HYPERLIQUID,
        funding_interval_hours=1.0,
        periods_per_year=24 * 365,         # 8760
        default_spot_taker_bps=7.0,        # current HL retail
        default_perp_taker_bps=3.5,
    )

    # reads
    async def get_exchange_profile(self) -> ExchangeProfile:
        return self._PROFILE   # const for HL — может стать dynamic если HL поменяет fees

    async def get_wallet(self) -> WalletInfo:
        raw = await self._account.fetch_account_state()
        marks = await self._latest_marks_for_open_positions()
        return _normalize_to_wallet_info(raw, marks)

    async def get_open_positions(self) -> list[Position]:
        # Read from HL directly, normalize spot coin names, derive
        # notional/margin_reserve from wallet state.
        # Note: this returns VENUE truth, not portfolio truth.
        ...

    async def get_market_specs(self) -> dict[str, MarketSpec]:
        meta = await self._market.fetch_meta()
        spec_overrides = await self._market.fetch_funding_intervals()
        return {s.coin: _to_market_spec(s, ...) for s in meta}

    async def fetch_quote(self, coin): return await self._market.fetch_quote(coin)
    async def fetch_funding(self, coin): return await self._market.fetch_funding(coin)
    # ... etc

    async def startup_validate(self, coins: tuple[str, ...]) -> None:
        if self._network == "mainnet":
            await _validate_mainnet_spot_pairs(self._market, self._tokens, coins)

    # writes
    async def open_position(self, coin, *, notional_usd, margin_reserve_usd, client_ref=None):
        # 1. Transfer margin_reserve_usd from spot → perp wallet.
        # 2. Compute spot qty = notional_usd / quote.ask (rounded down).
        # 3. open_paired: spot buy first, then perp short sized to actual spot delta.
        # 4. Build Position DTO and return (caller passes to portfolio_service.apply_open).
        ...

    async def close_position(self, coin):
        # 1. close_paired: spot sell first, then perp cover sized to actual spot delta.
        # 2. Transfer released margin from perp → spot wallet.
        # 3. Build ClosedPosition DTO.
        ...

    async def adjust_margin(self, coin, delta_usd):
        if delta_usd > 0:
            await self._orders.transfer_spot_to_perp(delta_usd)
        elif delta_usd < 0:
            await self._orders.transfer_perp_to_spot(-delta_usd)

    async def close(self):
        await self._market.aclose()
```

## Wiring in server.py

```python
async def lifespan(app):
    # ... settings, db, portfolio_service rehydrate ...

    adapter: ExchangeAdapter = HyperliquidAdapter.from_settings(
        settings, portfolio_service, bus,
    )
    if settings.dry_run:
        adapter = DryRunAdapterGuard(adapter, slippage_bps=2.0)

    await adapter.startup_validate(resolved_coins)

    # Strategies still take AtomicExecutor — provide wrapper from adapter:
    paired_router = adapter._paired if hasattr(adapter, "_paired") \
                    else adapter._underlying._paired  # peel DryRunGuard
    # NOTE: this is a temporary hack until F3. In F3 strategies take ExchangeAdapter.

    strategy = StrategyA(
        params, executor=paired_router,
        portfolio_service=portfolio_service,
        dry_run=settings.dry_run,
        margin_manager=margin_manager,
    )

    engine = Engine(
        market_data=adapter,                   # adapter satisfies MarketData reads
        strategy=strategy,
        portfolio_service=portfolio_service,
        ...
    )

    app.state.adapter = adapter
    # ...
```

## Out of scope

- Refactor Strategy to use ExchangeAdapter directly (F3).
- Removal of `AtomicExecutor` / old `Executor` Protocol (F3).
- Drift adapter (F8).
- Sub-strategy composition (TickComponent pipeline) — F4.

## Constraints

1. **Single source of HL knowledge** — все token maps, sz_decimals, retry
   policy ровно в `frab/exchanges/hyperliquid/_tokens.py` + `_market.py`.
   После F2: `git grep "UBTC\|UETH\|USOL\|MAINNET_SPOT_TOKEN_MAP" src/frab/`
   возвращает только результаты внутри `hyperliquid/` директории.
2. **DryRunAdapterGuard invariant** — тест проверяет что в dry_run mode
   подсчёт вызовов `underlying.open_position`/`close_position`/`adjust_margin`
   равен 0 после серии вызовов на guard.
3. **ExchangeAdapter runtime_checkable** — `isinstance(adapter, ExchangeAdapter)`
   возвращает True для `HyperliquidAdapter` и `DryRunAdapterGuard`.
4. **Strategy не меняется в F2** — публичный конструктор остаётся
   `(params, executor, portfolio_service, ...)`. Strategies продолжают
   использовать paired_router тем же образом что и сейчас AtomicExecutor.
5. **pytest-mock** только.
6. **No new dependencies**.
7. **≤2000 lines added, ≤1500 removed** (большая часть — перенос hyperliquid.py + hyperliquid_live.py + atomic.py).
8. **Никакие emojis, никакие TODO**.

## Acceptance criteria

1. `uv run pytest` exits 0. Все существующие тесты адаптированы
   (большинство переехало в `hyperliquid/tests/`).
2. ≥15 новых тестов:
   - `test_adapter_protocol.py`: isinstance check на оба implementations.
   - `test_dry_run_guard.py`: ≥5 тестов на forward-reads и no-op writes.
   - `test_adapter.py`: open_position/close_position end-to-end через respx mock HL.
   - `test_tokens.py`: token resolution для mainnet и testnet.
3. `git grep "from frab.exchanges.hyperliquid import\|from frab.exchanges.hyperliquid_live import\|from frab.exchanges.atomic import" src/frab/` — пусто (все импорты через `frab.exchanges.hyperliquid.adapter` или `frab.exchanges.adapter`).
4. `git grep "executor\._info\." src/frab/` — пусто (cli использует adapter методы).
5. `git grep "_select_spot_token_map\|_validate_spot_pairs\|MAINNET_SPOT_TOKEN_MAP\|_hl_info_url" src/frab/server.py` — пусто.
6. `git diff --stat` — основные изменения в `exchanges/hyperliquid/`.
7. Lifespan smoke test: dry_run=True, watchdog triggers force_close →
   подтверждение через mock что underlying.close_position не вызван
   (DryRunGuard остановил).

## Tests to run

```bash
uv run pytest src/frab/exchanges/ -v
uv run pytest src/frab/exchanges/hyperliquid/tests/ -v
uv run pytest -x  # full suite
```

## Risks

- **Strategy продолжает звать executor методы через paired_router hack** —
  это работает потому что _paired экспонирует `.open_paired`/`.close_paired`/
  `.transfer_*` методы как раньше. Если что-то отвалится — F2 спека
  предполагает что paired_router сохраняет API совместимость с AtomicExecutor.
- **DryRunAdapterGuard и cash accounting** — paper-fill всё ещё дебитит
  cash в portfolio_service (через apply_open в стратегии). Это OK: paper
  mode имеет synthetic cash; нет реального HL movement.
- **HL meta endpoint rate limit** — `get_market_specs` теперь вызывается
  каждый раз когда стратегия спрашивает leverage. Кешировать в адаптере
  с TTL ~1 hour (специфика HL не меняется чаще).
- **fetch_user_fills/funding теперь на adapter** — старый FeeReconciler
  принимает `market_data` параметр. Передавать adapter (он Duck-type
  совместим).
- **Lifespan ordering** — adapter должен быть создан ДО startup_validate
  (которому нужны HL вызовы) и ДО strategy/engine.

## Progress reporting

Append START/DONE to `/tmp/F2.progress`:
```
<TS>Z START — F2 spec read
<TS>Z DONE adapter.py — ExchangeAdapter Protocol + DTOs
<TS>Z DONE dry_run.py — DryRunAdapterGuard
<TS>Z DONE hyperliquid/_tokens.py — единый token map
<TS>Z DONE hyperliquid/_market.py — переезд HLMarketData reads
<TS>Z DONE hyperliquid/_account.py — переезд account state reads
<TS>Z DONE hyperliquid/_orders.py — переезд submit/transfers
<TS>Z DONE hyperliquid/_paired.py — переезд AtomicExecutor
<TS>Z DONE hyperliquid/adapter.py — HyperliquidAdapter aggregate
<TS>Z DONE registry.py — adapter factory registry
<TS>Z DONE server.py — wiring через from_settings + DryRunGuard
<TS>Z DONE cli.py — live-smoke через adapter, удалены private imports
<TS>Z DONE api/routes/wallet.py — get_wallet через adapter
<TS>Z DONE удаление старых файлов (hyperliquid.py, hyperliquid_live.py, atomic.py)
<TS>Z DONE all tests — N pass
```
