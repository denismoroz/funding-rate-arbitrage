# Funding-Rate Arbitrage — Research Summary

Дата: 2026-05-14
Цель проекта: построить production-систему для funding-rate-арбитража на Hyperliquid (с возможной диверсификацией на Drift / Bybit / Binance / Backpack).

Капитал в бэктестах: **$2000** ($1000 spot + $1000 perp margin или USDC buffer).
Данные: HL funding+OHLCV 2023-06 → 2026-05 (~2.9 года).

---

## Стратегия А — delta-neutral funding harvest

### Идея

Купить спот + одновременно зашортить такой же объём перпа на той же монете. Позиция нейтральна к цене (если spot вырос на $X, perp short потерял $X). Доход — **funding payments**, которые платят лонгеры шортерам на бирже когда perp price > spot price (бычий рынок, FOMO).

### Механика

- **Состав позиции**: $1000 spot + $1000 perp short, плечо 1:1 (delta-neutral).
- **Вход**: когда funding > 30% годовых (12h MA сигнал).
- **Выход**: когда funding < −15% годовых.
- **Min hold**: 120 часов (защита от whipsaw).
- **Universe**: 7 коинов (BTC, ETH, SOL, AVAX, LINK, AAVE, DOGE).
- **Concurrency cap**: K=3 одновременных позиций (top-K по signal strength).
- **Биржа**: Hyperliquid (spot taker 0.07%, perp taker 0.035%).

### Результаты (backtest)

| Метрика | Значение |
|---------|----------|
| Annual return | ~10% |
| Max DD | <1% |
| Calmar ratio | 100+ |
| Sharpe | 3+ |
| Trades/year | ~14 |
| Time in position | ~54% |

### Realistic live performance

Бэктест переоценивает на ~3-7%/год из-за дрэгов:

| Дрэг | Цена в год |
|------|------------|
| Slippage (4 ноги × 14 циклов) | 0.5-1.5% |
| Запас маржи для perp (30-50%) | 1-2% |
| Execution lag | 0.3-0.7% |
| API глитчи | 0.5-1% |
| Funding compression (forward) | 0-3% |

**Реалистичный диапазон:** 4-8% годовых в нормальных условиях; 2-4% в "тихий" год; до 12% в активный bull market.

### Multi-exchange сравнение (А)

| Биржа | Annual % | Funding interval | Время в позиции |
|-------|----------|-----------------|----------------|
| **Drift** | **17.8%** | 1h | 73% |
| **Hyperliquid** | 10.4% | 1h | 54% |
| **Backpack** | ~10% (короткий window) | 1h | 50% |
| Binance | 4.7% | 8h | 63% |
| Bybit | 4.5% | 8h | 47% |

**Почему HL > Binance/Bybit:**
1. Hourly funding (granular exit) vs 8h commitment.
2. Институциональный арбитраж компрессит rates на CEX до softcap ~11% APR.
3. На HL retail flow преобладает → больше перегрева → выше funding peaks.
4. HL fees 30-50% ниже.

### План диверсификации

| Капитал | Аллокация |
|---------|-----------|
| $1k-$6k | 100% HL |
| $10k-$50k | 50% Drift + 50% HL (~14% APR) |
| $50k+ | 40% Drift + 40% HL + 20% Bybit/Binance passive (~10% APR) |
| $200k+ | active + passive + USDC/T-bills (~8% APR) |

---

## Стратегия Б — stake & hedge с constant-dollar ratchet

### Идея

Держим спот в монете для **стейкинга** (ETH 3.5%, SOL 8.5%, AVAX 6.5%, TIA 14%, INJ 18%). Шортим перп **только когда сигнал говорит "будет просадка"**. Когда цена растёт — продаём излишек в кэш (lock profit). Кэш работает в Стратегии А.

### Механика (финальная версия v3)

**Spot leg — constant-dollar ratchet:**
- Старт: купить $1000 spot (units = $1000/P0)
- Стейкинг компаундится в монетах: `units_spot *= (1 + staking/8760)` каждый час
- Если `spot_value > $1050` → продать излишек, USDC → cash buffer
- После закрытия хеджа → долить spot обратно до $1000 **только когда mom14d > 0** (тренд подтвердился)

**Hedge leg — selective short:**
- `mom14d < 0` → открыть short на текущий dollar-value спота
- `mom14d > 0` → закрыть short, realize PnL
- **BTC и INJ дополнительно требуют LT-фильтр** (`mom90d < 0`) — у них есть выраженные циклы, без фильтра ловим bull-market whipsaws

**Cash buffer:**
- Лежит в Стратегии А (funding harvest на HL), даёт ~6-9% APR live
- В бэктесте моделировал как фиксированный 10% APR

### Результаты (backtest, per-coin)

Сигнал хеджа в скобках:

| Coin | Annual % | Max DD % | Calmar | Buy & Hold annual |
|------|----------|----------|--------|-------------------|
| BTC (mom14d_lt90d) | 38.0 | 9.1 | **4.16** | 70.8 |
| ETH (mom14d) | 34.6 | 8.8 | **3.94** | 13.4 |
| SOL (mom14d) | 58.6 | 13.8 | **4.26** | 191.6 |
| AVAX (mom14d) | 43.9 | 15.5 | **2.84** | −4.6 |
| TIA (mom14d) | 45.5 | 22.8 | **2.00** | −28.1 |
| INJ (mom14d_lt90d) | 43.7 | 16.3 | **2.69** | 6.0 |

**Portfolio (равновзвешенно по 6 коинам):**

| Метрика | v3 (финал) | Buy & Hold |
|---------|-----------|------------|
| Annual | **44.1%** | 41.5% |
| Max DD | **14.4%** | 77.0% |
| Calmar | **3.07** | 0.54 |

### Эволюция стратегии Б (что попробовали)

| Версия | Описание | Portfolio Calmar |
|--------|----------|------------------|
| B oldsignal (regime-based) | Funding-condition + MA200/momentum filter | 0.4 |
| B_hedge mom14d (фикс $1000 хедж) | Чистый mom14d, под-хеджённая позиция | 0.52 |
| B_hedge mom14d (full hedge) | Хедж = текущий dollar value спота | 1.91 |
| const_v1 (ratchet) | + constant-dollar rebalancing на росте | 2.36 |
| const_v2 (trend-confirm refill) | + не доливать в падающий нож | 2.37 |
| **const_v3** | + cash в Стратегии А (10% APR) | **3.07** |

### Декомпозиция доходности (per-coin)

Откуда реально приходят %:

| Coin | Total | Funding | Hedge PnL | Spot price | Staking | Fees |
|------|-------|---------|-----------|-----------|---------|------|
| BTC | 38% | +0.6% | +5.0% | +35.5% | 0% | −1.6% |
| ETH | 34.6% | +1.8% | +12.6% | +4.4% | +2.3% | −3.7% |
| SOL | 58.6% | +0.9% | −0.6% | +71.0% | +24.9% | −3.6% |
| AVAX | 43.9% | +0.1% | +17.9% | −4.9% | +2.6% | −3.5% |
| TIA | 45.5% | +1.1% | +29.5% | −15.7% | +1.7% | −3.6% |
| INJ | 43.7% | +0.1% | +9.1% | −5.2% | +8.2% | −2.4% |

**Ключевые выводы:**

1. **Funding (источник дохода А) — почти НОЛЬ в Б** (0-2%). Хедж не зарабатывает на funding, он зарабатывает на **price protection** (hedge PnL).

2. **Hedge работает там, где монета падает или стагнирует** — AVAX, TIA, INJ, частично ETH. На сильных бычьих BTC/SOL хедж нейтрален.

3. **Стейкинг материален на SOL** (+24.9%) и INJ (+8.2%). На TIA с 14% APY получили только +1.7% — потому что цена обвалилась, лишние монеты обесценились.

4. **Fees ~3% дрэг** — постоянная вкл/выкл хеджа стоит дорого.

### Realistic live performance Б

| Дрэг для Б | Цена в год |
|------------|------------|
| Slippage хеджей (mom14d даёт ~250 trades/yr × 4 нога × 0.05%) | 0.5% |
| Slippage spot rebalances (~280 ребалов/yr) | 0.2% |
| Cash yield ниже backtest (А даёт 6-9%, не 10%) | −1 to −2% |
| Funding compression на хедже | 0-1% |
| Whipsaw на mom14d false signals | 1-2% |

**Реалистично:** 20-25% annual / DD 18-22% / Calmar 1.0-1.5.

User сказал: *"если получится хотя бы половину — буду счастлив"* → таргет ~22% annual / Calmar 1.5 для live версии Б.

---

## Архитектурные решения

1. **V1 запуск:** только Hyperliquid. Стратегия А отдельно, потом Б.
2. **V2:** добавить Drift как 2-й слой (composite A_HL + A_Drift).
3. **V3:** Б_hedge поверх A — единый счёт на HL.
4. Capital management — авто-пополнение, dashboard, kill switch.

## Риски

- **Биржевой**: HL может лечь / быть взломанной. Mitigation: лимит на $ на HL, V2 диверсификация в Drift.
- **Smart contract**: Drift, Aave — DeFi риск.
- **Funding compression**: рост числа конкурентов сжимает rates. Это уже произошло на Binance/Bybit (yield упал с 10%+ до 4-5%). На HL может произойти за 1-2 года.
- **Регуляторный**: запрет CeFi/DeFi в юрисдикции пользователя.

## Каденс пересмотра параметров А

- **Ежедневно**: мониторинг бота и slippage.
- **Еженедельно**: PnL pace, trade count.
- **Ежемесячно**: universe (мёртвые монеты), fee changes.
- **Ежеквартально**: полный backtest, тюнинг entry/exit/min_hold с out-of-sample.
- **Полугодовое**: архитектура, новые биржи.
- **Триггерно**: 2 месяца ниже 50% от ожидания → стоп и разбор.

## Ключевые файлы

### Engine + общие
- `research/engine.py` — equity-based симулятор A_cycle / A_spot_keep / B / B_hedge с декомпозицией доходности.

### Стратегия А
- `research/backtest_a.py` — параметрический sweep.
- `research/optimize_a.py` — cross-margin + momentum filter.
- `research/concurrency_cap.py` — K=3 multi-coin sim.
- `research/test_stoploss.py` — stop-loss test (не нужен).

### Multi-exchange
- `research/multi_exchange.py` — сравнение А на HL/Drift/Binance/Bybit/Backpack.
- `research/fetch_*_funding.py` — скачиватели данных.

### Стратегия Б (от старой к новой)
- `research/backtest_b.py` — старая Б (regime-based, устарела).
- `research/backtest_b_hedge.py` — Б_hedge с per-coin signal search + decomp.
- `research/backtest_b_voltgt.py` — volatility targeting (не помогло, но даёт исправленный hedge sizing).
- `research/backtest_b_combined.py` — сетка сигналов (mom + LT + DD triggers).
- `research/backtest_b_constdollar.py` — **финальная v3** (ratchet + trend-confirm + per-coin LT + cash в А).

### CSV результаты
- `research/backtest_a_results.csv`
- `research/concurrency_cap_results.csv`
- `research/multi_exchange_results.csv`
- `research/backtest_b_constdollar_results.csv` — **финальный для Б**

---

## Следующие шаги (план для prod)

1. **Прототип А-бота на HL** ($1-2k тестовый капитал).
2. Отладка 2-3 месяца, сравнение live PnL с backtest pace.
3. При успехе — добавить Drift как 2-й слой (single binary, multi-exchange).
4. Б_hedge запускать **после** того как А стабильно работает 6 месяцев.
5. Dashboard + alerts (drawdown, missed entries, fee drift).

**Целевые цифры live:**
- А: 5-8% APR на $1-50k капитала.
- А+Б композит: 15-25% APR при DD 15-20% на $10k+ капитала.
