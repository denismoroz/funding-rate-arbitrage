# Drift Protocol v2 — заметки по API

**Дата скаутинга:** 2026-06-03  
**Источники:** [docs.drift.trade](https://docs.drift.trade/), [v2-teacher GitHub](https://github.com/drift-labs/v2-teacher), OpenAPI spec на `https://data.api.drift.trade/playground/json`

---

## Base URL

```
https://data.api.drift.trade
```

Интерактивная документация (Swagger UI):
```
https://data.api.drift.trade/playground
```

OpenAPI JSON spec (публично, без ключа):
```
https://data.api.drift.trade/playground/json
```

---

## Аутентификация

**Критически важный нюанс:** сам OpenAPI spec объявляет `securitySchemes: None`, но прямые `curl` запросы без заголовков возвращают `{"message":"Forbidden"}`. Нужны заголовки `Referer` и `Origin` как у браузера:

```bash
curl -s "https://data.api.drift.trade/market/SOL-PERP/fundingRates?limit=5" \
  -H "Referer: https://app.drift.trade" \
  -H "Origin: https://app.drift.trade"
```

Без этих заголовков — 403 Forbidden. Отдельного API-ключа нет, CORS-whitelist браузерная.

---

## Funding rates endpoint

### `GET /market/{symbol}/fundingRates`

Возвращает последние ~31 день записей о funding для perp-рынка. Сортировка: новые → старые.

**Параметры:**

| Параметр | Тип    | Обязателен | Описание                           |
|----------|--------|------------|------------------------------------|
| `symbol` | string | Да (path)  | Символ рынка, например `SOL-PERP`  |
| `page`   | string | Нет        | Пагинация                          |
| `limit`  | int    | Нет        | 1–750, default 20                  |

**Пример:**
```bash
curl -s "https://data.api.drift.trade/market/SOL-PERP/fundingRates?limit=3" \
  -H "Referer: https://app.drift.trade" \
  -H "Origin: https://app.drift.trade"
```

**Реальный ответ (сокращённый):**
```json
{
  "success": true,
  "records": [
    {
      "ts": 1775066400,
      "txSig": "P7PJz4E6Adxj...",
      "slot": 410361551,
      "recordId": "29779",
      "marketIndex": 0,
      "symbol": "SOL-PERP",
      "fundingRate": "-0.001024958",
      "fundingRateLong": "-0.001024958",
      "fundingRateShort": "-0.001024958",
      "cumulativeFundingRateLong": "44.373045417",
      "cumulativeFundingRateShort": "44.166845742",
      "oraclePriceTwap": "84.693889",
      "markPriceTwap": "84.652352",
      "periodRevenue": "-2063.302451",
      "baseAssetAmountWithAmm": "0.000000000",
      "baseAssetAmountWithUnsettledLp": "0.000000000"
    }
  ],
  "meta": {"nextPage": null}
}
```

**Конвертация в процент:**
```python
funding_rate_pct_per_hour = float(fundingRate) / float(oraclePriceTwap)
annualized_pct = funding_rate_pct_per_hour * 24 * 365 * 100
```

**Пример расчёта для записи выше:**
- `fundingRate = -0.001024958`, `oraclePriceTwap = 84.693889`
- `pct/hr = -0.001024958 / 84.693889 = -0.001210%/час`
- `APR = -0.001210% * 24 * 365 = -10.60%`

### Датированный endpoint: `GET /market/{symbol}/fundingRates/{year}/{month}/{day}`

Возвращает записи за конкретную дату UTC.

```bash
curl -s "https://data.api.drift.trade/market/SOL-PERP/fundingRates/2026/04/01" \
  -H "Referer: https://app.drift.trade" \
  -H "Origin: https://app.drift.trade"
# → 19 записей за 2026-04-01
```

---

## Сколько истории доступно

**ВАЖНЫЙ BLOCКЕР:** В ходе тестирования (2026-06-03) API возвращает данные только до **~2026-04-01**. Данные за май и июнь 2026 — пустые ответы:

```bash
curl "https://data.api.drift.trade/market/SOL-PERP/fundingRates/2026/05/31" ...
# → {"success":true,"records":[],"meta":{"records":0,"totalRecords":0,...}}
```

Причина неизвестна — возможно, индексер отстаёт или API заморожен. S3-хранилище тоже не помогает: **S3 flat files официально deprecated с января 2025** и также не обновлялись до нужных дат.

Документация обещает "31 дней", но что именно считается "сейчас" — неясно.

---

## Markets endpoint

Списка рынков как отдельного REST endpoint не существует. Для получения полного списка нужно использовать:

1. **TypeScript SDK константы** (актуальный источник):
   ```
   https://github.com/drift-labs/protocol-v2/blob/master/sdk/src/constants/perpMarkets.ts
   ```
   Там `MainnetPerpMarkets` — массив с `{symbol, baseAssetSymbol, marketIndex, marketStatus}`.

2. **Endpoint `/market/{symbol}/...`** работает с любым символом из этого списка.

Всего на mainnet зарегистрировано **86 perp-рынков** (marketIndex 0–85), из которых ~25 помечены как `DELISTED`, ~20 — prediction markets (не perp). Реальных торгуемых perp-рынков около **40**.

---

## Volume / Open Interest

В REST API **нет доступного endpoint для получения 24h volume и OI** без авторизации. Протестированные варианты:

```bash
# Вернул 403:
curl "https://data.api.drift.trade/amm/openInterest?marketName=SOL-PERP&start=...&end=..." \
  -H "Referer: https://app.drift.trade"
# → {"message":"Forbidden"}

# contracts endpoint:
curl "https://data.api.drift.trade/contracts" \
  -H "Referer: https://app.drift.trade"
# → {"message":"Forbidden"}
```

OpenAPI spec содержит endpoint `/amm/openInterest`, но он требует временного диапазона (`start`, `end` в секундах), и возвращает 403 даже с браузерным Referer.

Для получения OI и volume нужно либо:
- Использовать TypeScript/Python SDK (он читает on-chain аккаунты)
- Парсить данные с `app.drift.trade` через WebSocket `/ws`

---

## Mechanics — funding на Drift v2

**Интервал:** Каждый час (не каждые 8 часов, как на CEX).

**Формула:**
```
hourly_rate = (1/24) * (mark_twap - oracle_twap) / oracle_twap
```

`mark_twap` = midpoint of bid_twap and ask_twap (EMA с span 1 час).

**Нормализация:** Ставка capped по tier:
- Tier A (BTC): cap ±0.125%/час
- Tier B (SOL, ETH): cap ±0.0417%/час  
- Tier C и ниже: ±0.25–0.4167%/час

**Settlement:** Lazy — обновляется при открытии/закрытии позиций. Гарантированный flush если долго нет трейдов.

**Асимметрия (Rebate Pool):** Если OI longs ≠ OI shorts, одна сторона платит в "Rebate Pool", а не другой стороне. Это значит `fundingRateLong` может ≠ `fundingRateShort` при дисбалансе.

**Единицы в API:** `fundingRate` возвращается как `quote/base` (числитель в USDC, знаменатель в base-asset). Нужно делить на `oraclePriceTwap` для получения процента.

**Сравнение с HL:**
- HL: фандинг каждые 1 час, возвращается уже в `%` (умноженный на 8 для сравнения с биткоинскими биржами). Читается напрямую.
- Drift: 1 час, сырой `quote/base`, требует деления на price. Numerically сопоставимо после конвертации — оба hourly.

---

## Rate Limits

Из документации: "rate limiting is implemented, but specific limits depend on the endpoint and system load. If exceeded — 429 Too Many Requests. Recommended: exponential backoff."

Конкретных цифр нет. В заголовках ответов rate-limit не видно.

---

## Известные проблемы / gotchas

1. **Данные устарели:** REST API последний раз обновлялся ~2026-04-01 (2 месяца без новых данных на момент скаутинга). Причина неизвестна. **Это критический blocкер для использования как live-источника.**

2. **403 без browser-headers:** Нужны `Referer: https://app.drift.trade` и `Origin: https://app.drift.trade`. Это неофициальный workaround — может сломаться в любой момент.

3. **S3 deprecated:** `drift-historical-data-v2.s3.eu-west-1.amazonaws.com/...` — официально deprecated с января 2025, не работает.

4. **OI/Volume недоступны:** `/amm/openInterest` и `/contracts` возвращают 403 даже с browser-headers.

5. **`fundingRate` в API — не процент:** Это `quote/base` raw value. Деление на `oraclePriceTwap` обязательно. Ошибка в этом дает значения, отличающиеся в ~70–85x (цена SOL/BTC).

6. **Prediction markets в списке:** В `MainnetPerpMarkets` SDK содержатся prediction markets (выборы, спорт) с marketIndex в той же последовательности. Они delist'нуты, но занимают индексы (не contiguous для perp).

7. **`fundingRateLong` vs `fundingRateShort`:** При дисбалансе OI они расходятся. Для funding-arb нужно использовать ту сторону, которую занимаете (для LST+short стратегии — `fundingRateLong` получает шортовый коллег, т.е. смотреть `fundingRateShort`).

8. **Для live исполнения нужен SDK:** Drift Gateway (self-hosted Go-proxy) или driftpy (Python). REST API только для чтения исторических данных, не для торговли.

---

## Полный список endpoints (из OpenAPI spec)

```
GET /amm/bidAskPrice
GET /amm/openInterest       ← volume/OI, но 403 без спец. авторизации
GET /amm/oraclePrice
GET /amm/position
GET /amm/spreads
GET /market/{symbol}/fundingRates        ← РАБОТАЕТ (с browser headers)
GET /market/{symbol}/fundingRates/{year}/{month}/{day}  ← РАБОТАЕТ
GET /market/{symbol}/trades
GET /market/{symbol}/trades/{year}/{month}/{day}
GET /user/{accountId}/positions
GET /user/{accountId}/trades
GET /user/{accountId}/fundingPayments
GET /params                              ← РАБОТАЕТ но "DASHBOARD_TABLE not configured"
GET /ws                                  ← WebSocket
```
