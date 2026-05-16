# Research Summary: Funding-Harvest Strategy Adaptation — May 2026

**Аудитория:** автор, возвращающийся через неделю/месяц. Документ позволяет восстановить контекст за 5 минут.

---

## Контекст

Funding-harvest стратегия (Strategy A) на 7 монетах Hyperliquid (BTC/ETH/SOL/AVAX/LINK/AAVE/DOGE) в конфигурации `entry=0.30, min_hold=120, K=3` генерировала ~10% годовых с Calmar ~100 на полной истории, но полностью остановилась последние 90 дней: рынок перпетуальных фьючерсов «похолодел» с 2024 г., funding rates обвалились до уровня HL floor. Цель исследования — найти конфигурацию, которая восстанавливает активность в холодном рынке, не разрушая риск-метрики на горячей истории.

Капитал модели: K=3 × $2000 = $6000.

---

## Картина рынка (диагностика last_90d)

**Динамика funding rates по периодам** (annualized %, 12h MA signal, источник: `diagnose_cold_period.py`):

| Период | Характеристика |
|--------|----------------|
| 2023   | mean 15–30% по монетам, горячий рынок |
| 2024   | mean 10–20%, рынок тёплый |
| 2025   | mean 5–10%, охлаждение |
| 2026 (last_90d) | mean ≈3–5%, медиана ≈ floor, **полный коллапс** |

Ключевые факты по last_90d (raw funding, все U7-монеты суммарно):
- **49% часов** — funding на HL floor (≈10.95% annualized = 0.0000125 × 8760)
- **27% часов** — negative funding (лонги платят шортам)
- P95 по всем монетам ≈ floor; редкие спайки не достигают 30%

**Vol↔funding correlation** (`vol_funding_corr.py`):
- Full period: Pearson corr concurrent = **+0.19** (слабая положительная)
- Last_90d: Pearson corr concurrent = **−0.12** (инвертирована)
- Вывод: vol-based селекция монет не работает в cold-режиме

**Break-even по комиссиям** (`dynamic_min_hold.py`):
- HL fees per cycle = 21 bps (PERP_TAKER + SPOT_TAKER, открытие + закрытие)
- `hours_breakeven = 18.4 / annual_rate` (где annual_rate — в долях)
- При entry rate 8% → breakeven = 230h; при 15% → 123h; при 30% → 61h

---

## Что пробовали

| # | Идея | Файл | Лучший результат (full / last_90d) | Вывод |
|---|------|------|-------------------------------------|-------|
| 1 | Fallback-позиция при empty (relaxed threshold) | `concurrency_cap_fallback` | calmar_full 78.8 (fb_ratio=0.5, fb_mh=120) | Нейтрально: +0.1% annual, DD растёт незначительно. last_90d не измерялся |
| 2 | Wait-time distribution stats | `wait_time_stats` | — (диагностика) | Prod COMBO (entry=0.30): last_90d = 100% времени пусто. entry=0.10 (dev): last_90d = 100% занято, но убыточно |
| 3 | Entry threshold sweep 2D (entry × min_hold) | `entry_threshold_sweep` | e=0.30/mh=72: calmar_full 117.3; e=0.15/mh=120: calmar_full 83.5 | Sweet spot — entry=0.15–0.30, min_hold=120+. На low entry (0.05–0.10) calmar падает до 3–13 из-за DD |
| 4 | Adaptive percentile-based entry | `adaptive_entry` | adaptive top_x=10/lb=30/floor=0.15: calmar_full 86.0; last_90d calmar −1.3 | Не помогает cold-рынку: floor=0.15 → мёртв, floor=0.08 → убыточен на last_90d |
| 5 | **Dynamic min_hold (по entry rate)** | **`dynamic_min_hold`** | e=0.15/sm=3: calmar_full 77.1, last_90d мёртв; **e=0.08/sm=5: calmar_full 14.8, last_90d calmar 51.2** | **Главная находка**: агрессивная конфигурация оживляет cold-рынок. min_hold = min(cap, max(base, safety×18.4/rate)) |
| 6 | Combined adaptive + dynamic hold | `combined_adaptive` | best: top_x=30/lb=60/floor=0.08/sm=5: calmar_full 9.8, last_90d calmar 44.1 | Не доминирует над dynamic-only: full calmar хуже (9.8 vs 14.8), last_90d сопоставим |
| 7 | Universe expansion (U7→U8→U11→U13) | `universe_sweep` | U11 dynamic_aggressive: ann_full 11.68%, calmar_full 14.6, last_90d ann 3.05% / calmar 46.8 | Малая прибавка (+0.26pp annual_90d U11 vs U7). U13 хуже из-за мем-монет |
| 8 | Staged thresholds (slot-by-slot) | `staged_entry` | staged [auto_30d,auto_30d,0.30]/sm=5: ann_full 11.0%, calmar_full 36.7, last_90d ann 2.17% / calmar 39.3 | Не доминирует над dynamic_aggressive. Умеренный компромисс |
| 9 | Position rotation (ротация на лучший сигнал) | `rotation` | rot_f1.5_m0.20/sm=3: ann_full 8.45%, calmar_full 13.0 | Fees съедают: rotation_trades до 169–197, full calmar 9–13 (хуже no_rotation 18.4). Не работает |
| 10 | Math-derived entry (floor из физики комиссий) | `math_derived_entry` | s=5/cap=480 U7: ann_full 10.63%, calmar_full **87.0**, last_90d ann −0.12% / calmar −1.9 | Устраняет хардкод: floor = safety×18.4/cap. Лучший full calmar в sweep, но dead на last_90d |
| 11 | Z-score based entry | `zscore_entry` | z=0.5/lb=14/sm=3/cap=480: calmar_full 65.4, last_90d calmar 11.4 | Стабильный, но не доминирует ни на full, ни на last_90d |
| 12 | Binance backtest | `binance_backtest` | Binance dynamic_aggressive: ann_full 7.43%, last_90d −0.75%; HL: ann_full 10.33% | Binance хуже HL на 28–47% по annual. Диверсификация не даст альфы |
| 13 | Cold-period P&L diagnosis | `diagnose_cold_period` | dynamic_aggressive last_90d: gross funding $59 / fees_total $18 / net $41 ≈ 0.69% / 90d | Верно: 100% time-in-market, gross ≈4% annual, после fees ≈2.79% net |
| 14 | Vol↔funding correlation | `vol_funding_corr` | corr_concurrent full=+0.19, last_90d=−0.12 | Vol не является надёжным предиктором funding. Фиксированный universe + прямой фильтр лучше |

---

## Ключевые количественные находки

**Break-even структура:**
- Fees per cycle: `PERP_TAKER + SPOT_TAKER = 0.00105 + 0.00105 = 0.0021` (21 bps)
- Break-even hold = `18.4 / annual_rate` часов
- При prod params (entry=0.10, min_hold=1): hold=1h vs breakeven=184h → убыток −20.9bps на трейд

**Dynamic min_hold механика:**
- `min_hold = min(cap_min_hold, max(base_min_hold, safety_mult × 18.4 / entry_rate))`
- safety_mult=5, cap=720, base=24 → при entry 8%: min_hold = min(720, max(24, 5×230)) = 720h
- safety_mult=5, cap=720, base=24 → при entry 15%: min_hold = min(720, max(24, 5×123)) = 615h

**Cold period P&L breakdown (dynamic_aggressive U7, last_90d, из `diagnose_cold_period.py`):**

| Компонент | $ | % от капитала |
|-----------|---|---------------|
| gross_funding | +59.xx | +0.99% / 90d |
| fees_total | −18.xx | −0.31% / 90d |
| fees как % от gross | — | 30.8% |
| net_pnl | +41.xx | +0.69% / 90d → **2.79% annual** |

Теоретический максимум при floor 10.95% annualized и 100% time-in = ≈3% annual net — цифра совпадает.

**49% часов last_90d на HL floor** → стратегия физически не может зарабатывать больше ~3% annual в текущем режиме, независимо от параметров входа.

---

## Реальные кандидаты для прода

Числа строго из CSV-файлов:

| Стратегия | Full annual | Full DD | Full calmar | 90d annual | 90d DD | 90d calmar | Источник CSV |
|-----------|-------------|---------|-------------|------------|--------|------------|--------------|
| baseline_30_120 U7 (старый прод) | 9.93% | 0.10% | 100.9 | 0.00% (мёртв) | 0.00% | 0.0 | dynamic_min_hold |
| baseline_15_120 U7 | 9.79% | 0.12% | 83.5 | −0.07% | 0.05% | −1.3 | dynamic_min_hold |
| dynamic_balanced U7 (e=0.15/sm=3/cap=720) | 10.34% | 0.13% | 77.1 | −0.07% | 0.05% | −1.3 | dynamic_min_hold |
| math_derived s=5/cap=480 U7 (floor=19.17%) | 10.63% | 0.12% | **87.0** | −0.12% | 0.07% | −1.9 | math_derived |
| **dynamic_aggressive U7 (e=0.08/sm=5/cap=720)** | 11.19% | 0.76% | 14.8 | **2.79%** | 0.05% | **51.2** | dynamic_min_hold |
| dynamic_aggressive U11 (e=0.08/sm=5/cap=720) | 11.68% | 0.80% | 14.6 | **3.05%** | 0.07% | **46.8** | universe_sweep |
| math_derived s=3/cap=1080 U7 (floor=5.11%) | 10.79% | 0.98% | 11.0 | 2.79% | 0.07% | 42.2 | math_derived |
| math_derived s=5/cap=1080 U7 (floor=8.52%) | 11.14% | 0.98% | 11.4 | 2.68% | 0.06% | 47.6 | math_derived |

**Замечания:**
- Full calmar для dynamic_aggressive (14.8) значительно хуже baseline (100.9) — цена за активность в cold-режиме
- DD 0.76% на full = ≈$46 на $6000 капитала; при длинном hold до 720h ценовой риск реален
- math_derived s=5/cap=480: лучший full calmar (87.0), но dead на last_90d — хороший выбор если ждать прогрева

---

## Стратегический вывод

**Opportunity cost:** Morpho/Aave/Pendle дают ~5% annual на USDC без операционного риска. На last_90d наша стратегия даёт max 2.79–3.05% annual — **ниже baseline lending**.

Funding-harvest имеет смысл только при горячем рынке (median funding > 12% annualized по U7 за последние 30 дней). В cold-режиме (median ≈ floor 10.95%) стратегия проигрывает DeFi lending по доходности и добавляет операционный риск.

**Диагностическая метрика для переключения:**
```
median(signal_ma12 по U7 за последние 30 дней) > 0.12   →  funding-harvest включён
median(signal_ma12 по U7 за последние 30 дней) < 0.08   →  parking в DeFi lending
```

---

## Рекомендация для прода

### Если запускать сейчас (warm-up / тест с малым капиталом):

**Конфигурация: dynamic_aggressive U7**

```
coins          = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
K              = 3          # max concurrent positions
entry          = 0.08       # 8% annualized signal threshold
safety_mult    = 5.0        # min_hold = min(cap, max(base, 5 × breakeven))
cap_min_hold   = 720        # max hold = 720 hours (30 days)
base_min_hold  = 24         # floor hold = 24 hours
exit_threshold = -0.15      # exit if signal < -15% annualized
signal_window  = 12         # 12h rolling mean for entry signal
```

Бэктест: full 11.19%/year, Calmar 14.8, last_90d 2.79%/year, Calmar 51.2.

**ВАЖНО:** Текущие prod-параметры (entry=0.10, min_hold=1) **гарантированно убыточны** — при min_hold=1 трейд закрывается через 1 час, fees=21bps, funding gain за 1h при 10% annual ≈0.11bps → убыток −20.9bps на каждый трейд.

### Если ждать прогрева рынка:

Активировать baseline_30_120 (calmar_full 100.9, annual 9.93%) когда median funding по U7 стабильно > 12% за последние 30 дней.

### Если нужен максимальный full calmar при умеренной cold-активности:

**math_derived: safety=5, cap=480, U7 (floor=19.17%)**
- full: 10.63% / 0.12% DD / calmar 87.0
- last_90d: −0.12% (практически мёртв, убыток минимален)

---

## Что осталось НЕ исследовано

1. **Regime-switching автомат** — код детектора режима (hot/cold), hysteresis, cooldown перед переключением. Концепция описана, реализация отсутствует.

2. **Strategy B (stake & hedge)** — по memory calmar 3.07 в бектесте, независимый источник альфы. `research/backtest_b_hedge_results.csv` существует, но стратегии A+B не сведены в единый portfolio view.

3. **Live PnL validation** — ни одна из конфигураций не запускалась в реальном prod с dynamic_aggressive. Трейды на entry=0.10/min_hold=1 (убыточная конфигурация) не показательны.

4. **Fee tier optimization** — HL снижает тейкер для high-volume. Рассмотреть достижение VIP тира.

5. **Alternative perp DEXes** — Drift Protocol и Backpack Exchange: adapters готовы, но не тестировались live. Binance показал −28–47% к HL по annual.

6. **Tail risk при длинном hold** — dynamic_aggressive держит до 720h (30 дней). Стресс-тест на flash crash / funding spike не проводился. DD 0.76% в бэктесте — на обычных данных, без экстремальных событий.

7. **Capital scaling** — все бэктесты на $6000. Поведение при $20k–100k (slippage, глубина HL spot) не анализировалось.

---

## Текущее состояние прода

- **Хост:** 10.8.0.5 (mbp2.local), always-on Mac
- **Prod параметры:** entry=0.10, min_hold=1, K=3 — **тестовая конфигурация, убыточна на cold market**
- **Проверка позиций:** `curl localhost:8765/api/positions?status=open | jq .`
- **Данные:** HL 1h OHLCV + fundingRate в `research/data/` (U7 + расширение до U11)

---

*Документ составлен 2026-05-16. Числа из CSV — точные, без округления сверх исходных 2 знаков после запятой.*
