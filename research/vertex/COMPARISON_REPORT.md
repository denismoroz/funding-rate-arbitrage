# Vertex Protocol Edge — Research Report

**Дата исследования:** 2026-06-04  
**Статус:** Negative result — данные не получены, протокол мёртв

---

## TL;DR

**Vertex Protocol прекратил работу ~19 июля 2025 года.**  
TVL рухнул с $58M до $0 за 48 часов. Текущий TVL = $49 (не ошибка: именно $49, не $49M).  
API недоступен с нашего IP (Vercel geo-blocking).  
**Интеграция не имеет смысла: venue не существует.**

---

## 1. Какие коины торговались на Vertex

На основе анализа SDK и публичных источников (не live API):

| Монета | Статус на Vertex | Подтверждено | product_id |
|--------|-----------------|--------------|------------|
| BTC    | Присутствовал  | ✓ (SDK tests) | 2 |
| ETH    | Присутствовал  | ✓ (SDK tests) | 4 |
| SOL    | Вероятно присутствовал | Частично | неизвестен |
| AVAX   | Вероятно присутствовал | Частично | неизвестен |
| LINK   | Вероятно присутствовал | Частично | неизвестен |
| DOGE   | Вероятно присутствовал | Частично | неизвестен |
| **HYPE** | **НЕ ЛИСТОВАН** | ✓ (HL-native) | N/A |

BTC и ETH подтверждены через SDK tests и начальные on-chain данные.  
SOL/AVAX/LINK/DOGE — предположительно присутствовали (Vertex имел ~50+ perp markets на пике), но без live API product_id'ы не верифицированы.  
HYPE — нативный токен Hyperliquid, на Vertex не листовался.

---

## 2. Spot+Perp Parity Check

Vertex — вертикально-интегрированная DEX: spot и perp на одной venue, единый cross-margin счёт.

| Коин | Spot pair | Perp | Cross-margin |
|------|-----------|------|--------------|
| BTC  | ✓ BTC spot (product_id=1) | ✓ BTC-PERP (product_id=2) | ✓ |
| ETH  | ✓ ETH spot (product_id=3) | ✓ ETH-PERP (product_id=4) | ✓ |
| SOL  | вероятно ✓ | вероятно ✓ | ✓ |
| AVAX | вероятно ✓ | вероятно ✓ | ✓ |
| LINK | вероятно ✓ | вероятно ✓ | ✓ |
| DOGE | вероятно ✓ | вероятно ✓ | ✓ |
| HYPE | ✗ | ✗ | N/A |

**Вывод по spot+perp:** Структура аналогична HL — это было потенциально интересно для funding-арбитража без cross-venue execution risk. Но протокол мёртв.

---

## 3. Funding Rate Comparison (Hot/Cold)

**Данные не получены** — API заблокирован, протокол прекратил работу.

| Монета | HL Hot | Vertex Hot | HL Cold | Vertex Cold |
|--------|--------|------------|---------|-------------|
| BTC    | 21.35% | N/A        | 9.20%   | N/A         |
| ETH    | 22.82% | N/A        | 7.66%   | N/A         |
| SOL    | 23.09% | N/A        | 2.71%   | N/A         |
| HYPE   | 118.8% | NOT LISTED | 19.38%  | NOT LISTED  |
| AVAX   | 11.84% | N/A        | 5.17%   | N/A         |
| LINK   | 21.35% | N/A        | 11.21%  | N/A         |
| DOGE   | 28.23% | N/A        | 7.31%   | N/A         |

*HL Hot = 2023-06-01 → 2024-12-31, HL Cold = 2025-01-01 → 2026-04-01*  
*Vertex данные за hot-период (2023-2024) теоретически существовали, но API заблокирован*

---

## 4. API Доступность: Технический анализ

**Результат:** Полная блокировка. Данные недоступны через public API.

### Почему недоступно

Все Vertex endpoints (`*.prod.vertexprotocol.com`) хостятся на **Vercel**.  
Vercel позволяет деплоеру настраивать geo-блокировки через Firewall rules.  
При TLS-хандшейке с правильным SNI-именем сервер немедленно закрывает соединение (`SSL_ERROR_SYSCALL / UNEXPECTED_EOF_WHILE_READING`).

Проверено:
- Наш публичный IP: `134.209.83.56` (DigitalOcean Frankfurt)
- Все `*.prod.vertexprotocol.com` — BLOCKED  
- Другие Vercel-сайты (vercel.com, nextjs.org) — РАБОТАЮТ  
- TLS без SNI — коннектится, но Vercel возвращает 404 (не знает deployment)

Исключение: `prod.vertexprotocol.com`, `api.vertexprotocol.com` — возвращают 404 через HTTPS (разные Vercel deployments, не заблокированы, но и API не имеют).

### Альтернативные попытки

| Подход | Результат |
|--------|-----------|
| Python urllib (stdlib) | BLOCKED |
| Python requests | BLOCKED |
| Python httpx | BLOCKED |
| curl | BLOCKED |
| openssl s_client с SNI | BLOCKED |
| openssl s_client без SNI | Connected, HTTP 404 |
| The Graph hosted service | DEPRECATED (redirect to error.thegraph.com) |
| The Graph decentralized network | Требует API ключ |
| Goldsky subgraph | 404 (subgraph не найден) |
| Arbitrum RPC (on-chain) | РАБОТАЕТ, но только текущий state, не история |
| CoinGecko API | Vertex не индексирован |
| DefiLlama derivatives API | 402 (требует платный план) |

---

## 5. Liquidty Assessment (на пике, 2024)

На основе TVL истории из DefiLlama:

| Период | TVL | Оценка ликвидности |
|--------|-----|-------------------|
| 2023-06-01 (Hot начало) | $3.5M | LOW |
| 2024-01-01 | $43.7M | MEDIUM |
| 2024-05-27 (пик) | **$102.2M** | HIGH |
| 2024-12-31 (Hot конец) | $81.4M | HIGH |
| 2025-01-01 (Cold начало) | $81.4M | HIGH |
| 2025-07-09 | $50.9M | MEDIUM |
| 2025-07-17 | $400K | CRITICAL-LOW |
| **2025-07-19** | **$0** | **SHUTDOWN** |
| 2026-06-04 (сейчас) | $49 | DEAD |

**Важно:** TVL обрушился c $50.9M до $0 менее чем за 10 дней (7-17 июля 2025).  
Это не органическое снижение — возможно плановое закрытие, exploit, или rebranding (средства могли быть мигрированы).

---

## 6. Vertex vs HL: Качественное сравнение

| Параметр | Hyperliquid | Vertex (пик 2024) |
|----------|-------------|-------------------|
| Chain | HL L1 | Arbitrum |
| TVL пик | $500M+ | $102M |
| Spot+Perp | ✓ | ✓ |
| Cross-margin | ✓ | ✓ |
| HYPE listing | ✓ | ✗ |
| API доступность | ✓ open | Geo-blocked |
| Status 2026 | Active | DEAD |
| Funding данные | Доступны | Недоступны |

---

## 7. Вывод по Integration Feasibility

**Vertex Protocol НЕ подходит для интеграции по следующим причинам:**

1. **Протокол мёртв** — TVL $0, операции прекращены с июля 2025. Нет смысла интегрировать мёртвую venue.

2. **API geo-blocked** — даже если бы протокол работал, доступ с нашего IP (DigitalOcean Frankfurt) невозможен без VPN/proxy из-за Vercel geo-blocking.

3. **HYPE отсутствует** — наш наиболее прибыльный коин (hot: 118.8% годовых) не листован на Vertex.

4. **Cold-window недоступна** — даже если бы API работал, данные за cold-period (2025-01-01 → 2026-04-01) доступны только до ~2025-07-17 (половина окна).

5. **Исторические данные не получены** — negative result для research task.

**Рекомендация:** Исключить Vertex из списка кандидатов. Приоритизировать другие venues (dYdX уже исследован, рассмотреть GMX, Gains Network, или Drift если RPC-доступ восстановится).

---

## 8. Appendix: Vertex API Schema (для архива)

Если в будущем Vertex Protocol перезапустится или аналогичный протокол использует ту же API структуру:

### Market Snapshots (исторические funding rates)
```
POST https://archive.prod.vertexprotocol.com/v1/query
{
  "type": "market_snapshots",
  "interval": {
    "count": 500,
    "granularity": 3600,
    "max_time": 1717200000
  },
  "product_ids": [2, 4]
}
```

### Ответ
```json
{
  "snapshots": [
    {
      "timestamp": 1717200000,
      "funding_rates": {
        "2": "12345678901234567",
        "4": "-5678901234567890"
      },
      "open_interests": {"2": "1234567890000000000000", "4": "..."},
      "cumulative_volumes": {...},
      ...
    }
  ]
}
```

### Конвертация funding rate
```python
raw_x18 = int("12345678901234567")   # из API ответа
rate_per_hour = raw_x18 / 1e18       # = 0.0000123456... fraction
annualized_pct = rate_per_hour * 24 * 365 * 100  # = X.XX%
```
