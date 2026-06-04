# dYdX v4 Indexer — заметки по API

**Дата скаутинга:** 2026-06-04  
**Источники:** публичный indexer `https://indexer.dydx.trade/v4`, прямые curl-тесты

---

## Base URL

```
https://indexer.dydx.trade/v4
```

Аутентификация не требуется. Нет необходимости в браузерных заголовках (в отличие от Drift).

---

## Ключевые endpoints

### `GET /perpetualMarkets`

Список всех perp-рынков. Возвращает объект `markets` с ключами-тикерами.

```bash
curl "https://indexer.dydx.trade/v4/perpetualMarkets"
```

Поля на момент скаутинга: `ticker`, `status`, `marketType`, `oraclePrice`, `nextFundingRate`,
`initialMarginFraction`, `maintenanceMarginFraction`, `openInterest`, `volume24H`, и др.

Всего 296 рынков. Все 7 наших монет присутствуют:
`BTC-USD`, `ETH-USD`, `SOL-USD`, `HYPE-USD`, `AVAX-USD`, `LINK-USD`, `DOGE-USD` — статус ACTIVE.

### `GET /historicalFunding/{ticker}`

История funding rates для тикера. Возвращает массив `historicalFunding`, отсортированный от новых к старым.

**Параметры:**

| Параметр              | Тип    | Описание                                        |
|-----------------------|--------|-------------------------------------------------|
| `limit`               | int    | Макс. записей за запрос. Макс. = 1000           |
| `effectiveBeforeOrAt` | string | ISO8601 cursor для пагинации (≤ этого времени)  |

```bash
curl "https://indexer.dydx.trade/v4/historicalFunding/BTC-USD?limit=5"
curl "https://indexer.dydx.trade/v4/historicalFunding/BTC-USD?limit=1000&effectiveBeforeOrAt=2025-01-01T00:00:00.000Z"
```

**Пример ответа:**
```json
{
  "historicalFunding": [
    {
      "ticker": "BTC-USD",
      "rate": "-0.000065875",
      "price": "64340.38381",
      "effectiveAtHeight": "91932161",
      "effectiveAt": "2026-06-04T05:00:00.070Z"
    }
  ]
}
```

Поля:
- `rate` — funding rate уже нормализованный (dimensionless fraction), не quote/base как у Drift
- `price` — oracle price на момент settlement
- `effectiveAt` — ISO8601 UTC timestamp
- `effectiveAtHeight` — block height на Cosmos chain

---

## Семантика funding и формула annualized

**Интервал:** 1 час (проверено: данные идут с шагом ровно 1 час)

**`rate`** — это уже нормализованный hourly funding rate (fraction of notional, dimensionless).  
Например, `rate = 0.0001075` означает 0.01075% за час для лонга.

**Формула annualized_pct:**
```python
annualized_pct = rate * 24 * 365 * 100   # умножаем на 8760 часов и на 100 для %
```

Пример:
- `rate = 0.0001075`
- `annualized = 0.0001075 * 8760 * 100 = 9.42%`

**Важно:** В отличие от Drift, у dYdX `rate` возвращается уже как fraction, не как quote/base.  
Делить на цену НЕ нужно. Это упрощает обработку.

**Полярность:** Положительный rate → лонги платят шортам (backwardation отрицательный).  
Отрицательный rate → шорты платят лонгам.

---

## Доступность исторических данных

| Монета       | Начало данных на dYdX |
|--------------|----------------------|
| BTC, ETH, SOL, AVAX, LINK, DOGE | ~2023-10-27 |
| HYPE         | ~2026-02-26          |

**Важный gap:** dYdX v4 запустился в октябре 2023.  
Данных за июнь–октябрь 2023 (начало hot-window) **нет**. Hot-окно фактически начинается с 2023-10-27.

Cold-window (2025-01-01 → 2026-04-01) покрыта полностью.

---

## Rate Limits

- Явных заголовков с rate-limit (X-RateLimit-*) в ответах нет
- Limit=1000 работает стабильно; limit=10000 возвращает ошибку
- При параллельных запросах (3-5 монет одновременно) с задержкой 0.3s между страницами — 429 не наблюдался
- Рекомендую: 0.3s между страницами, экспоненциальный backoff при 429

---

## Pagination pattern

Ключ для пагинации — `effectiveBeforeOrAt`. Алгоритм:
1. Начать с `effectiveBeforeOrAt = end_date`
2. Получить страницу, взять `effectiveAt` последней записи
3. Следующий запрос: `effectiveBeforeOrAt = last_effectiveAt - 1 секунда`
4. Стоп: если пришло < limit записей (достигли начала) или cursor < start_date

---

## Сравнение с Drift

| Свойство          | dYdX v4           | Drift v2               |
|-------------------|-------------------|------------------------|
| Аутентификация    | Нет (открытый)    | Нужны browser headers  |
| Rate format       | Нормализованный %  | quote/base (raw)       |
| Интервал          | 1 час             | 1 час                  |
| История (начало)  | Окт 2023          | Есть до 2023-06        |
| Обновляемость     | Активный (июнь 2026)| Заморожен с апр 2026  |
| HYPE              | Есть (с фев 2026) | Нет                    |
| Pagination        | `effectiveBeforeOrAt` | date-based URL      |

---

## Gotchas

1. **HYPE листнулся поздно:** dYdX добавил HYPE только в феврале 2026, hot-окно (до дек 2024) для него отсутствует.
2. **Нет данных до 2023-10-27** — часть hot-окна (июнь–октябрь 2023) на dYdX недоступна.
3. **Limit=1000 максимальный** — limit=10000 возвращает ошибку.
4. **Нет `nextPage` cursor** — пагинация только через `effectiveBeforeOrAt`.
5. **API живой** — в отличие от Drift REST, dYdX indexer обновляется в реальном времени.
