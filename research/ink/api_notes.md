# Nado DEX (Ink L2) — API Notes

## Статус (по состоянию на 2026-06-04)

**Протокол живой, в production (Open Beta Season 1).** Vertex Protocol мигрировал на Ink L2, команда вошла в состав Ink Foundation и запустила **Nado** — новый бренд, built на той же технологии (synchronous CLOB + off-chain sequencer + on-chain settlement).

---

## История

- **2025-07-08**: Ink Foundation объявляет о поглощении команды и технологии Vertex Protocol. VRTX token sunset.
- **2025-08-14 14:00 UTC**: Vertex окончательно прекращает торги на всех EVM-деплойментах (Arbitrum, Blast, Avalanche, Sonic, и др.)
- **2025-11-20**: Nado Private Alpha запускается
- **2026-01-15**: Конец private alpha
- **2026-01 → сейчас**: Nado Open Beta Season 1 (Points farming, публичный доступ)

**Вывод по "warm window" 2025-09 → 2026-04**: данные за сентябрь-ноябрь 2025 отсутствуют (Vertex уже умер, Nado ещё не запустился). Практически реальные данные начинаются с ~2025-11-20.

---

## Endpoints

### Production

| Назначение | URL |
|---|---|
| Gateway REST | `https://gateway.prod.nado.xyz/v1` |
| Gateway REST v2 | `https://gateway.prod.nado.xyz/v2` |
| Gateway WebSocket | `wss://gateway.prod.nado.xyz/v1/ws` |
| Subscriptions WS | `wss://gateway.prod.nado.xyz/v1/subscribe` |
| Archive (Indexer) | `https://archive.prod.nado.xyz/v1` |
| Archive v2 | `https://archive.prod.nado.xyz/v2` |

### Документация

- Docs: https://docs.nado.xyz/
- API reference: https://docs.nado.xyz/developer-resources/api
- Endpoints list: https://docs.nado.xyz/developer-resources/api/endpoints
- Archive (indexer): https://docs.nado.xyz/developer-resources/api/archive-indexer
- Funding rates docs: https://docs.nado.xyz/funding-rates
- Python SDK: https://nadohq.github.io/nado-python-sdk/index.html

---

## API Структура

API разделён на три части:

### 1. Gateway (REST + WS)
- **Writes (executes)**: place/cancel orders, deposit/withdraw
- **Queries**: contracts list, fee rates, account state, positions, balances
- Требует подписи транзакций через Ethereum wallet

### 2. Subscriptions (WebSocket)
- Live feeds: funding rate updates (пересчёт ~каждые 20 секунд), orderbook, trades
- Не требует аутентификации для публичных данных

### 3. Archive / Indexer (REST, read-only, публичный)
Это основной endpoint для нашего fetch_one_coin.py:
- `/v1/funding-rates` — исторические funding rates (endpoint найден в docs)
- `/v1/matches` — история сделок
- `/v1/orders` — ордера
- `/v1/candlesticks` — OHLCV
- `/v1/events` — события (liquidations, etc.)
- `/v1/market-snapshots` — снепшоты рынка
- `/v1/product-snapshots` — снепшоты продуктов

**Схема ответа funding rate (предположительно, по аналогии с Vertex):**
```json
{
  "funding_rates": [
    {
      "product_id": 2,
      "timestamp": 1700000000,
      "funding_rate_x18": "123456789000000000"
    }
  ]
}
```

Конкретная схема требует прямого обращения к `docs.nado.xyz/developer-resources/api/archive-indexer`.

---

## Funding Rate Semantics

- **Механизм**: Continuous (пересчёт ~каждые 20 секунд), выплата **hourly**
- **Направление**: Positive rate → longs платят shorts (perp > spot)
- **Валюта расчёта**: USDT0 (аналог USDC на Ink chain)
- **База**: Процент от notional value позиции
- **Формула annualization**: `rate_per_hour * 24 * 365 * 100` = `annualized_pct`

---

## Collateral / Products

### Spot assets (подтверждено):
- `kBTC` (wrapped BTC на Ink)
- `wETH` (wrapped ETH на Ink)
- `USDT0` (bridged USDT на Ink)
- `USDC` (native или bridged)

### Perp markets (подтверждено для наших 7 коинов):
- BTC-PERP ✅
- ETH-PERP ✅
- SOL-PERP ✅
- BNB-PERP ✅
- XRP-PERP ✅
- DOGE-PERP — вероятно (из ~26-30 perp markets), не подтверждён явно
- AVAX-PERP — вероятно, не подтверждён явно
- LINK-PERP — вероятно, не подтверждён явно
- HYPE-PERP — неизвестно (конкурирующий протокол, сомнительно)

**Итого**: 26-30+ perp markets согласно некоторым источникам. Точный список — через `GET /v1/contracts` на gateway.

---

## Rate Limits

Явных данных нет. По аналогии с Vertex: archive API не имеет жёстких лимитов для чтения. Рекомендуется добавить задержку 0.3-0.5 сек между запросами.

---

## Bridging / Onboarding

Чтобы торговать на Nado нужно:
1. Bridge ETH или USDC на Ink chain через `inkonchain.com/bridge` или Rhino Bridge
2. На Ink нативный gas token — ETH (как на всех OP Stack chains)
3. USDT0 — стандартный collateral на Nado
4. Wallet — любой EVM wallet (MetaMask, etc.) или programmatic через EIP-712 signatures

---

## SDKs

- **Python SDK**: `pip install nado-python-sdk` (пакет `nadohq`)
  - Docs: https://nadohq.github.io/nado-python-sdk/index.html
- **TypeScript SDK**: доступен (упоминается в docs)
- **Rust SDK**: доступен (упоминается в docs)

---

## Ограничения для нашего use-case

1. **Funding history availability**: данные начинаются с ~2025-11-20. "Warm window" 2025-09 → 2025-11-20 — **пустая** (Vertex умер, Nado не запустился).
2. **Spot assets**: kBTC и wETH — это wrapped tokens, не native. Требуется мэппинг и проверка ценовой конвергенции с оригинальными активами.
3. **No HYPE perp**: HYPE — это нативный токен конкурента (Hyperliquid). Листинг на Nado крайне маловероятен.
4. **Chain risk**: Ink — новый L2, запущен декабрь 2024. Меньше $100M TVL на момент запуска Nado.
