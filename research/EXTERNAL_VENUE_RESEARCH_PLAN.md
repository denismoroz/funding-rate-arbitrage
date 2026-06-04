# External Venue Funding Research Plan

**Дата:** 2026-06-04
**Контекст сессии:** scout альтернативных perp DEX'ов для funding-арбитража после того, как Drift скаут показал отрицательный edge в cold market.

## Цель

Получить **реальные** funding-rate numbers (не оценки!) по dYdX v4 и Vertex Edge на той же универсе и тех же окнах, что мы уже посчитали для HL и Drift. Решить — есть ли смысл интегрировать эти venue'ы.

## Universe (overlap с HL backtest)

```
["BTC", "ETH", "SOL", "HYPE", "AVAX", "LINK", "DOGE"]
```

## Окна анализа

- **Hot:** 2023-06-01 → 2024-12-31
- **Cold:** 2025-01-01 → 2026-04-01

## Reference frame (уже собрано)

| Файл | Что |
|---|---|
| `research/drift/funding_history_hl/<COIN>.csv` | HL funding (полный, 7 коинов) |
| `research/drift/funding_history/<COIN>.csv` | Drift funding (SOL/BTC/HYPE полные, ETH в работе, AVAX/LINK/DOGE в очереди) |
| `research/drift/regime_comparison.csv` | HL vs Drift regime split (полный для HL, частичный для Drift) |
| `research/drift/compare_hl_drift.py` | Шаблон comparison-скрипта |
| `research/drift/api_notes.md` | Drift API notes |
| `research/drift/COMPARISON_REPORT.md` | (не написан, отложен) |

## Output per venue (`research/<venue>/`)

1. `api_notes.md` — endpoints, rate limits, schema, funding semantics (RU)
2. `markets.json` — все perp markets с metadata
3. `funding_history/<COIN>.csv` — schema `ts_ms, coin, funding_rate_normalized, annualized_pct`
4. `regime_comparison.csv` — schema `coin, hl_hot, <venue>_hot, delta_hot, hl_cold, <venue>_cold, delta_cold, freq_hot, freq_cold`
5. `COMPARISON_REPORT.md` (RU)

## Venue specs

### dYdX v4

- Chain: Cosmos / dYdX Chain
- Public indexer: `https://indexer.dydx.trade/v4`
- Эндпоинты (verify): `/perpetualMarkets`, `/historicalFunding/{ticker}`
- Ticker convention: `{COIN}-USD`
- Funding interval: 1h (verify)
- Без CORS-троттлинга — должен качаться быстро

### Vertex Edge

- Chain: Arbitrum (primary), multi-chain
- API base: `https://archive.prod.vertexprotocol.com/` (verify)
- Имеет spot+perp на одной venue (структурный аналог HL) — важно отметить
- Funding mechanism: mark - oracle premium (как HL)

## Делегирование

Per memory [feedback-delegation](../../.claude/projects/-Users-d-prj-funding-rate-arbitrage/memory/feedback_delegation.md):
- Opus делегирует Sonnet'ам по одному per venue (параллельно — разные API, нет rate-limit конфликта)
- Бюджет каждому ~45 мин
- **NO commit, NO push** — Opus ревьюит и коммитит после
- Если Sonnet застрял на 20 мин без прогресса — отчитаться partial data, не блокировать

## Статус на момент сохранения

| Что | Статус | Детали |
|---|---|---|
| dYdX Sonnet | 🟢 запущен (фон) | agentId `a5a9024b43f03a6ea`, стартовал перед /compact |
| Vertex Sonnet | ⏸️ НЕ запущен | прервано пользователем перед launch |
| Drift fetch chain (старый фон) | 🟡 может ещё качать | PID 9354 (ETH) + 9538 (chain AVAX→LINK→DOGE), маркер `/tmp/drift_chain_done` |

## Next steps после /compact

1. Проверить статус dYdX Sonnet'а (`SendMessage` к `a5a9024b43f03a6ea` или через нотификацию)
2. Запустить Vertex Sonnet с аналогичным промптом (структура та же, поменять venue specifics)
3. Проверить Drift fetch chain (`ls /tmp/drift_chain_done`, `ls -la research/drift/funding_history/`)
4. После завершения обоих new agent'ов — собрать сводный отчёт **4 venue'ов** (HL, Drift, dYdX, Vertex) в одной таблице, дать решение по next action на капитале $1k

## Что не вошло в этот рисёрч (отложено)

- **Aevo, Paradex, Lighter, Apex Pro** — мелкие venue'ы, если HL/Drift/dYdX/Vertex покрывают картину, отдельно не нужны
- **GMX-family** — другая модель funding (borrow rate из GLP), не bilateral. Не подходит под нашу funding-арб структуру.
- **Cross-venue integration code** — research only, без реальной интеграции до решения о smysl'е

## Решение текущего масштаба ($1k capital)

Per договорённости в сессии — никакой cross-venue работы прямо сейчас, потому что gas + slippage > expected edge. Этот research — для **roadmap'а** на момент scaling до $10-50k+.
