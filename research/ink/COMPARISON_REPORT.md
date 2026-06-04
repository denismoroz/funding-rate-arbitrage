# Nado (Ink L2) — Сравнительный отчёт для funding-арбитража

**Дата отчёта**: 2026-06-04  
**Автор**: research agent  
**Контекст**: Оценка Nado DEX как кандидата на venue №2 для диверсификации от Hyperliquid

---

## 1. Статус venue: ALIVE (Production, Open Beta Season 1)

### Хронология
| Дата | Событие |
|------|---------|
| 2025-07-08 | Ink Foundation поглощает команду Vertex Protocol; VRTX sunset |
| 2025-08-14 14:00 UTC | Vertex окончательно закрывается на всех EVM chains |
| 2025-11-20 | **Nado Private Alpha** запускается на Ink L2 |
| 2026-01-15 | Конец private alpha, переход в Open Beta |
| 2026-01 → сейчас | **Nado Open Beta Season 1** (публичный доступ, points program) |

**Nado = Vertex Protocol reborn**. Та же команда (Kraken engineers + Vertex team), та же архитектура (synchronous CLOB + off-chain sequencer + on-chain settlement), новый бренд и новая chain (Ink L2, chain id 57073, OP Stack).

---

## 2. Spot + Perp на одной venue — ЧАСТИЧНО

Это критический фактор для Strategy A. Оценка:

| Аспект | Статус |
|--------|--------|
| Perp markets | ✅ BTC, ETH, SOL и ещё ~26-30 пар |
| Spot markets | ⚠️ kBTC/USDT0, wETH/USDT0 — wrapped tokens только |
| SOL spot | ❓ Неизвестно (не подтверждён явно) |
| Unified margin (spot + perp hedge) | ✅ Автоматически признаётся hedge, снижает margin |
| HYPE spot/perp | ❌ Маловероятен (конкурирующий протокол) |

**Ключевое ограничение**: spot assets на Nado — wrapped tokens (`kBTC`, `wETH`), требующие bridge через `inkonchain.com`. Это не native BTC/ETH, а chain-specific wrapped representations. Для Strategy A (long spot + short perp) это работает в рамках Nado, но несёт дополнительный bridge/counterparty risk по сравнению с HL, где UBTC/UETH — нативные spot токены.

**Unified margin — реальное преимущество**: Nado's risk engine автоматически распознаёт kBTC spot long + BTC-PERP short как hedge и снижает совокупные margin требования. Это архитектурно идентично тому, что предоставляет HL.

---

## 3. Universe Coverage (наши 7 коинов)

| Монета | Perp на Nado | Spot на Nado | Confidence |
|--------|-------------|-------------|------------|
| BTC | ✅ | ✅ (kBTC) | Высокая |
| ETH | ✅ | ✅ (wETH) | Высокая |
| SOL | ✅ | ❓ | Perp подтверждён, spot неизвестен |
| DOGE | ⚠️ вероятно | ❌ нет | Perp вероятен (~26-30 markets), spot не ожидается |
| AVAX | ⚠️ вероятно | ❌ нет | Аналогично DOGE |
| LINK | ⚠️ вероятно | ❌ нет | Аналогично DOGE |
| HYPE | ❌ маловероятен | ❌ нет | HYPE — токен конкурента (Hyperliquid) |

**Для Strategy A full coverage нужен spot+perp для всех 7 монет** — это недостижимо на Nado. Реалистично: BTC + ETH (возможно SOL) для полноценной basis trade strategy.

---

## 4. Funding Data (исторические данные)

### Доступность по окнам

| Окно | Статус |
|------|--------|
| Hot (2023-2024) | ❌ UNAVAILABLE — Nado физически не существовал |
| Warm 2025-09 → 2025-11-20 | ❌ UNAVAILABLE — gap: Vertex умер 14 авг, Nado запустился 20 ноя |
| Warm 2025-11-20 → 2026-04-01 | ✅ AVAILABLE — ~4.5 месяца данных |
| Current (2026-04 → now) | ✅ AVAILABLE |

**Честная оценка warm window**: данных за 2025-09 → 2026-04 примерно 4.5 месяца (не 7). Пробел сентябрь-ноябрь 2025 — неизбежен, это период между смертью Vertex и рождением Nado.

### Funding rate механизм
- Пересчёт: каждые ~20 секунд
- Выплата: **hourly** (каждый час)
- Направление: positive rate → longs платят shorts (perp выше spot)
- Settlement currency: **USDT0**
- API: `https://archive.prod.nado.xyz/v1` с endpoint `/funding-rates`
- Python SDK: `pip install nado-python-sdk`

### Числовые данные
**Конкретных funding rate цифр получить не удалось** (WebFetch недоступен, прямой API-вызов не выполнялся). Из косвенных источников:
- BTC/ETH funding на Nado в целом конкурентоспособен с другими топ-perp DEX
- Nado находится на позиции ~#12 по объёму среди perp DEX (январь 2026: $828M/день)
- Для сравнения: HL обрабатывает ~$7B/день (в ~8-9 раз больше)

---

## 5. Ликвидность / TVL

| Метрика | Значение | Источник |
|---------|----------|---------|
| TVL (Nado perp) | ~$53M | DefiLlama (ранние 2026) |
| Spot TVL | отдельно трекается | DefiLlama /protocol/nado-spot |
| 24h volume (Jan 2026) | ~$828M | Публикации |
| Cumulative volume (4 мес) | ~$42B | Обзоры 2026 |
| Annual revenue estimate | $14.9M — $2.4B | Разброс в источниках; $14.9M вероятнее |
| Ranking среди perp DEX | ~#12 (Jan 2026) | PANews |
| Maker fee | -0.8 bps (rebate) → 0 bps |  |
| Taker fee | +1.5 bps → +3.5 bps |  |

**Сравнение с HL**: HL доминирует с 44% рынка perp DEX (2026), дневной объём ~$7B+. Nado — примерно в 8-10 раз меньше по объёму, что означает значительно меньшую глубину стакана для крупных позиций.

---

## 6. Operational Overhead для интеграции

### Что нужно для Strategy A на Nado

1. **New wallet / keys**: Стандартный EVM wallet (совместим с существующей EVM инфраструктурой)
2. **Bridging USDC на Ink**:
   - `inkonchain.com/bridge` или Rhino Bridge
   - Bridge ETH (для gas) + USDC/USDT0 (collateral)
   - Потенциальные задержки bridging: минуты-часы
3. **Новый executor**: 
   - REST API схожа с Vertex Edge (та же команда!) — адаптация относительно проста
   - Python SDK: `pip install nado-python-sdk` (nadohq)
   - EIP-712 signature для ордеров — стандарт для EVM DEX
4. **Spot assets**: 
   - kBTC ≠ BTC (требует bridge)
   - wETH ≠ ETH (требует bridge или swap)
   - SOL spot недоступен → Strategy A для SOL невозможна на Nado
5. **Margin management**: 
   - Единый collateral pool (unified margin) — проще чем split-venue
   - Auto top-up логика аналогична HL
6. **Chain risk**:
   - Ink L2 — относительно новая сеть (запущена декабрь 2024)
   - OP Stack / Optimism stack — audited, production-grade
   - Kraken backing — снижает риск abandonment
   - Централизованный sequencer (как большинство L2) — риск MEV/censorship

---

## 7. Вердикт по диверсификации от HL

### Плюсы

- ✅ **Spot + perp на одной venue** (для BTC + ETH): критическое требование Strategy A выполнено для мажорных монет
- ✅ **Unified margin**: автоматический hedge-recognition, capital efficiency аналогичен HL
- ✅ **API compatible**: та же команда, что Vertex — архитектура API схожа, Python SDK есть
- ✅ **Production-ready**: не alpha, Open Beta с публичным доступом
- ✅ **Kraken backing**: institutional legitimacy, снижает chain abandonment risk
- ✅ **Low fees**: maker rebate up to -0.8 bps — выгоднее HL для maker-based strategies
- ✅ **Different chain risk**: Ink L2 ≠ HL L1 — цель диверсификации достигается

### Минусы

- ❌ **Universe ограничен**: только BTC + ETH (возможно SOL) имеют spot pairs. DOGE/AVAX/LINK/HYPE — только perp, Strategy A для них невозможна
- ❌ **Короткая история**: ~6 месяцев данных vs ~3 года на HL. Backtesting невозможен
- ❌ **Низкая ликвидность**: ~$828M/день vs ~$7B+ на HL. Slippage выше, impact крупных позиций сильнее
- ❌ **Bridge overhead**: kBTC/wETH требуют bridge, добавляют задержку и counterparty риск
- ❌ **SOL spot неизвестен**: если SOL spot отсутствует, Strategy A покрывает только 2 из 6 перспективных монет
- ❌ **Нет HYPE**: токен, дающий высокую funding доходность на HL, не доступен на Nado
- ⚠️ **Новая цепь**: Ink запущена декабрь 2024 — <2 лет security track record

### Рекомендация

**Целесообразно как venue №2 при следующих условиях:**

| Сценарий | Вердикт |
|----------|---------|
| Portfolio < $10K | Нет — overhead (bridging, новый executor) не окупается |
| Portfolio $10K–$50K | Условно да — только BTC + ETH legs на Nado; осторожно |
| Portfolio > $50K | Да — диверсификация chain risk оправдана; BTC + ETH basis trade |

**Приоритет интеграции**: НИЗКИЙ → СРЕДНИЙ.

**Рекомендуемый первый шаг**: запустить `fetch_one_coin.py` с CONTRACTS утилитой, получить реальный список продуктов и funding history за ноябрь 2025 → апрель 2026. Сравнить средние funding rates BTC/ETH с HL за тот же период. Если Nado funding BTC/ETH стабильно > 5% годовых — интеграция оправдана при $20K+ капитале.

---

## 8. Технические ссылки

| Ресурс | URL |
|--------|-----|
| Приложение | https://app.nado.xyz/perpetuals |
| Документация | https://docs.nado.xyz/ |
| Archive API | https://archive.prod.nado.xyz/v1 |
| Gateway API | https://gateway.prod.nado.xyz/v1 |
| Python SDK | https://nadohq.github.io/nado-python-sdk/ |
| Stats | https://stats.nado.xyz/ |
| DefiLlama (perp) | https://defillama.com/protocol/nado |
| DefiLlama (spot) | https://defillama.com/protocol/nado-spot |
| Status page | https://nado-xyz.betteruptime.com/ |
| Bridge | https://inkonchain.com (bridge tab) |
