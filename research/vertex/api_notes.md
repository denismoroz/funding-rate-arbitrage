# Vertex Protocol Edge — заметки по API

**Дата скаутинга:** 2026-06-04  
**Источники:** Python SDK GitHub, публичные RPC-вызовы, DefiLlama TVL, прямые тесты endpoints

---

## КРИТИЧЕСКИЙ ВЫВОД ВВЕРХУ

**Vertex Protocol прекратил работу ~17-19 июля 2025 года.**  
TVL обрушился с $58M до $0 за двое суток. Текущий TVL = $49.  
Протокол де-факто мёртв. Интеграция нецелесообразна.

---

## Базовые URL (из Python SDK)

```python
# Mainnet (Arbitrum)
GATEWAY  = "https://gateway.prod.vertexprotocol.com/v1"
INDEXER  = "https://archive.prod.vertexprotocol.com/v1"
TRIGGER  = "https://trigger.prod.vertexprotocol.com/v1"

# Другие chain-деплойменты (Mantle, Sei, Base, Sonic, Abstract, Avalanche) — аналогичная структура
# Например:
# MANTLE_INDEXER = "https://archive.mantle-prod.vertexprotocol.com/v1"
```

---

## Проблема доступности: Geo-blocking через Vercel

**Все `*.prod.vertexprotocol.com` заблокированы для нашего IP (134.209.83.56, DigitalOcean Frankfurt).**

Механизм блокировки: TLS/SNI-level через Vercel Infrastructure.  
При попытке TLS-хандшейка с SNI-именем сервер немедленно закрывает соединение (`SSL_ERROR_SYSCALL`).  
Без SNI TLS устанавливается, но Vercel возвращает 404 (не знает деплоймент).

Проверено:
- `gateway.prod.vertexprotocol.com` — BLOCKED (SSL EOF)  
- `archive.prod.vertexprotocol.com` — BLOCKED (SSL EOF)  
- `archive.mantle-prod.vertexprotocol.com` — BLOCKED  
- `archive.sei-prod.vertexprotocol.com` — BLOCKED  
- `archive.base-prod.vertexprotocol.com` — BLOCKED  
- `archive.avax-prod.vertexprotocol.com` — BLOCKED  
- `archive.sonic-prod.vertexprotocol.com` — BLOCKED  
- `gateway.sepolia-test.vertexprotocol.com` — BLOCKED (даже тестнет)

Для сравнения: другие Vercel-сайты (vercel.com, nextjs.org) работают нормально.  
Значит, блокировка специфична для Vertex-деплоймента, а не для всего Vercel.

---

## API структура (из SDK, не из живого ответа)

### Gateway (Engine) — synchronous queries

```
POST https://gateway.prod.vertexprotocol.com/v1/query
Body: {"type": "all_products"}         # список продуктов
Body: {"type": "market_price", "product_id": 2}  # цена для продукта
```

### Archive (Indexer) — исторические данные

```
POST https://archive.prod.vertexprotocol.com/v1/query
```

**Funding rates (текущие):**
```json
{"type": "funding_rates", "product_ids": [2, 4]}
```
Ответ:
```json
{
  "2": {"product_id": 2, "funding_rate_x18": "12345678901234567", "update_time": "1717200000"},
  "4": {"product_id": 4, "funding_rate_x18": "-5678901234567890", "update_time": "1717200000"}
}
```

**Market snapshots (исторические funding rates через время):**
```json
{
  "type": "market_snapshots",
  "interval": {"count": 100, "granularity": 3600},
  "product_ids": [2, 4, 6, 8, 10, 12, 14]
}
```
Ответ: массив `snapshots`, каждый содержит:
- `timestamp` — unix timestamp
- `funding_rates` — dict `product_id -> cumulative_rate_x18`
- `open_interests` — dict `product_id -> OI`
- `cumulative_volumes` — dict `product_id -> volume`

---

## Семантика funding rate

**Формат:** `funding_rate_x18` — целое число, fraction × 10^18  
Пример: `"12345678901234567"` → `12345678901234567 / 1e18 = 0.0000123...`

**Конвертация:**
```python
raw_x18 = int(funding_rate_x18_str)
rate_fraction = raw_x18 / 1e18  # normalized fraction (dimensionless)
```

**Интервал:** Vertex использует **hourly** funding rate (1 час).  
Подтверждается: `IndexerMarketSnapshotsParams` с `granularity=3600`.

**Формула annualized_pct:**
```python
annualized_pct = rate_fraction * 24 * 365 * 100  # = rate * 8760 * 100
```

---

## Product ID mapping (Arbitrum mainnet)

На основе анализа on-chain данных (StartData.json с timestamp=2023-04-26):

| product_id | Тип     | Coin  |
|------------|---------|-------|
| 0          | spot    | USDC (quote) |
| 1          | spot    | BTC   |
| 2          | perp    | BTC   |
| 3          | spot    | ETH   |
| 4          | perp    | ETH   |
| 5          | spot    | ARB   |
| 6          | perp    | ARB   |
| 7          | spot    | BNB   |
| 8          | perp    | BNB   |
| 9          | spot    | XRP   |
| 10         | perp    | XRP   |
| ...        | ...     | ...   |

**Важно:** Четные product_id = perp, нечетные = spot (после USDC).  
`BTC perp = 2`, `ETH perp = 4`. Другие коины добавлялись позднее.

**Из Universe (BTC, ETH, SOL, HYPE, AVAX, LINK, DOGE):**
- BTC perp: product_id = **2** ✓ (подтверждено из sanity тестов SDK)
- ETH perp: product_id = **4** ✓ (подтверждено)
- SOL perp: product_id ≈ **12-16** (добавлен позже)
- AVAX perp: возможно присутствует
- LINK perp: возможно присутствует
- DOGE perp: возможно присутствует
- **HYPE:** НЕТ (листинг на Vertex не подтверждён)

---

## On-chain альтернативы

FQuerier контракт на Arbitrum: `0x1693273B443699bee277eCbc60e2C8027E91995d`  
Вызов `getAllProducts()` (selector: `0x02ee3a52`) через публичный RPC (`https://arb1.arbitrum.io/rpc`) работает и возвращает ~48KB данных.  
Но это текущий state, не история. Для исторических данных нужен archive node.

---

## Rate Limits (из документации, не проверено)

- Gateway: очень низкий rate limit (предназначен для приложений с SDK)
- Archive/Indexer: до 10 requests/second по публичной документации
- Subgraph: rate-limited, требует API ключ через The Graph Network

---

## Subgraph

Vertex имеет три subgraph на The Graph (hosted service, deprecated):
```
vertex-prod-core:         https://api.thegraph.com/subgraphs/name/vertex-protocol/vertex-prod-core
vertex-prod-markets:      https://api.thegraph.com/subgraphs/name/vertex-protocol/vertex-prod-markets
vertex-prod-candlesticks: https://api.thegraph.com/subgraphs/name/vertex-protocol/vertex-prod-candlesticks
```

**Статус:** hosted service был deprecated The Graph, все три субграфа редиректятся на `error.thegraph.com`.  
Decentralized network требует API ключ.

---

## Spot + Perp структура

Vertex — spot+perp на одной venue. Каждый токен имеет:
- spot market (product_id нечётный)
- perp market (product_id чётный)  
- Единый cross-margin счёт (spot collateral может использоваться как margin для perp)

Это структурно аналогично Hyperliquid.

---

## Выводы

1. **API недоступен** с DigitalOcean Frankfurt IP из-за Vercel geo-blocking
2. **Протокол фактически умер** в июле 2025 (TVL $58M → $0 за 2 дня)
3. **Данных за cold-window (2025-01-01 → 2026-04-01)** практически нет — протокол работал до июля 2025
4. **Исторические funding rates не получены** — negative result
5. **Интеграция нецелесообразна** — live venue не существует
