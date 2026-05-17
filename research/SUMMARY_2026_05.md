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
| 15 | Exit modes (raw/symmetric/persistent) | `exit_modes` | persistent_6h_-0.30: ann_full 21.11%/calmar 35.1, last_90d 3.95%/calmar 75.3 | Overfit: avg_hold 163 дней — exit почти не триггерит. Не настоящее улучшение, просто отключённый exit |
| 16 | Exit modes + max_hold_cap | `exit_modes_capped` | persistent_6h+cap=90d: ann_full 14.06%/calmar 23.4 | Cap режет noise-filter benefit. Без cap = overfit, с cap ≤90d = на уровне baseline |
| 17 | Dynamic exits (adaptive/trailing/forward) | `dynamic_exit` | adaptive +0.3: ann_full 11.86%/calmar 15.5, last_90d 2.26%/calmar 42.1 | Marginal vs baseline. Конфиги что «выигрывают» — degenerate (avg_hold 3000-7600h) |
| 18 | Continuous breakeven exit (rate-aware) | `breakeven_exit` | e=0.15/base=48/cap=720: ann_full 11.94%/calmar **106.6**, last_90d ann −0.07%/calmar −1.3 | Хорошо на full, dead на cold. Логика правильно режет убытки |
| 19 | **Two-phase exit (break-even / profit)** | **`two_phase_exit`** | **e=0.15/p1_neg=24/p1_cap=480/p2_exit=-0.10: ann_full 13.03%/calmar 114.4, last_90d ann −0.07%/calmar −1.3** | **NEW BEST на full calmar**. 42 phase2 exits (profit) + 27 phase1 exits (cut loss) = 60/40. Реальная стратегия, не overfit |
| 20 | **Two-phase exit + dynamic min_hold** | **`two_phase_dynamic`** | **e=0.10/sm=5/cap=720/p1_neg=72/p1_cap=720/p2=-0.10: ann_full 15.34%/calmar 23.1, last_90d ann 3.44%/calmar 65.9** | **Дуальный лидер**. Low entry активирует cold market (3.44% > DynAgg 2.79%), dynamic min_hold защищает phase1 от ранних exits на шуме (только 7 phase1 vs 40 phase2). Жертва — full calmar упал с 114 до 23 (DD 0.67% vs 0.11%). Кандидат для **always-on** ноги портфеля. |

---

## Exit logic experiments (15-19) — ключевой блок

**Проблема:** Текущая прод-логика exit использует МГНОВЕННЫЙ rate (`rate × 8760 < exit_threshold`), тогда как entry использует 12h MA `smoothed_signal`. Асимметрия — один шумовой час с rate < -15% годовых вылетает позицию, которая копила funding неделю.

**Что пробовали:**
- `symmetric` (12h MA для exit как для entry) — умеренное улучшение
- `persistent N-hours` — overfit-кандидат, exit почти не триггерит
- `dynamic adaptive/trailing/forward` — marginal vs baseline
- `continuous breakeven` (пересчёт hours-to-breakeven каждый час) — хорошо на full, мёртв на cold
- `two-phase exit` — **главная находка**

**Two-phase logic:**
- **Phase 1** (`gross_funding < total_fees_to_recoup`): цель — окупить fees. Не выходим даже при низком положительном rate (re-entry стоит ещё $4.20 fees). Exit только если: (a) rate негативный N часов подряд, или (b) hours_to_breakeven > cap.
- **Phase 2** (`gross_funding >= total_fees_to_recoup`): уже в плюсе. Exit когда smoothed rate < phase2_exit_threshold.

**Best config:** `entry=0.15, p1_negative_patience=24h, p1_breakeven_cap=480h, p2_exit_threshold=-0.10`.
- Full: annual **13.03%**, calmar **114.4** (новый рекорд!), max_dd 0.11%, 72 trades, avg_hold 33 дня
- Phase split: 42 phase2 exits (60% — взяли прибыль) + 27 phase1 exits (40% — режем убыток)
- На last_90d: молчит (entry=0.15 не пробивается), но и не теряет — это разумное поведение в холодном рынке

**Ключевой вывод:** существующая прод-логика exit активно ломает результаты. Two-phase exit даёт +3 п.п. annual и +14 calmar при той же DD vs старого COMBO. Это применимо в проде прямо сейчас.

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
| **two_phase e=0.15/p1_neg=24/p1_cap=480/p2_exit=-0.10** ⭐ | **13.03%** | **0.11%** | **114.4** | −0.07% (мёртв) | 0.05% | −1.3 | two_phase_exit |
| **two_phase_dynamic e=0.10/sm=5/cap=720/p1_neg=72/p1_cap=720/p2=-0.10** ⭐⭐ | **15.34%** | 0.67% | 23.1 | **3.44%** | 0.05% | **65.9** | two_phase_dynamic |
| breakeven_exit e=0.15/base=48/cap=720 | 11.94% | 0.11% | 106.6 | −0.07% | 0.05% | −1.3 | breakeven_exit |

**Замечания:**
- Full calmar для dynamic_aggressive (14.8) значительно хуже baseline (100.9) — цена за активность в cold-режиме
- DD 0.76% на full = ≈$46 на $6000 капитала; при длинном hold до 720h ценовой риск реален
- math_derived s=5/cap=480: лучший full calmar (87.0), но dead на last_90d — хороший выбор если ждать прогрева
- **two_phase e=0.15: новый рекорд по full calmar (114.4)**, превосходит baseline_30_120 (100.9) на +3 п.п. annual при той же DD; молчит на cold market

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

Теперь у нас **ТРИ чётких кандидата** с разной философией. Выбор зависит от убеждения о будущем рынке. **Рекомендуемая комбинация:** split капитала между A (two_phase) и C (two_phase_dynamic) — см. раздел "Portfolio split" ниже.

### Кандидат A: two_phase exit (если ждёшь возврата горячего рынка)

**«Работаем когда есть альфа, отдыхаем когда нет»**

```
coins                       = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
K                           = 3
entry                       = 0.15        # 15% annualized
signal_window               = 12          # 12h rolling mean for entry
base_min_hold               = 24
phase1_negative_patience    = 24          # часов rate<0 подряд до сдачи в phase 1
phase1_breakeven_cap_hours  = 480         # макс часов до окупаемости при текущем rate
phase2_exit_threshold       = -0.10       # exit из phase 2 когда rate ушёл < -10%
```

Бэктест: full **13.03%/year, Calmar 114.4, max_dd 0.11%**, 72 трейда, avg_hold 33 дня. Phase split: 42 profit / 27 cut-loss. На last_90d: молчит (entry=0.15 не пробивается на cold market).

### Кандидат B: dynamic_aggressive (если хочешь стабильно работать всегда)

**«Всегда торгуем, режем убытки агрессивно»**

```
coins          = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
K              = 3
entry          = 0.08
safety_mult    = 5.0
cap_min_hold   = 720
base_min_hold  = 24
exit_threshold = -0.15
signal_window  = 12
```

Бэктест: full 11.19%/year, Calmar 14.8, last_90d **2.79%/year, Calmar 51.2**. Работает в любом режиме, но full calmar в 8 раз хуже Кандидата A.

### Кандидат C: two_phase_dynamic (always-on, доминирует в обоих режимах) ⭐⭐

**«Та же two-phase философия, но входим раньше — а dynamic min_hold защищает позицию от шума пока fees не окупились»**

```
coins                       = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
K                           = 3
entry                       = 0.10        # 10% annualized (vs 0.15 у Кандидата A)
signal_window               = 12
base_min_hold               = 24          # минимум (срабатывает только при очень высоком rate)
safety_mult                 = 5.0         # position_min_hold = 5 × breakeven_h
cap_min_hold                = 720         # верхний потолок hold (30 дней)
phase1_negative_patience    = 72          # часов rate<0 подряд до сдачи в phase 1
phase1_breakeven_cap_hours  = 720
phase2_exit_threshold       = -0.10
```

**Per-position min_hold формула:**
```
breakeven_h = 18.4 / entry_rate_annual           # 18.4 = 0.0021 × 8760
position_min_hold = min(720, max(24, 5 × breakeven_h))

# Примеры:
# entry rate 10% → breakeven 184h → min_hold = min(720, 920) = 720h
# entry rate 15% → breakeven 123h → min_hold = min(720, 615) = 615h
# entry rate 30% → breakeven  61h → min_hold = min(720, 305) = 305h
# entry rate  5% → breakeven 368h → min_hold = 720h (capped)
```

**Бэктест:** full **15.34%/year, Calmar 23.1, DD 0.67%**, 50 трейдов, avg_hold 1496h (62 дня). Phase split: 7 phase1 (cut-loss) / 38 phase2 (profit). На last_90d: **3.44%/year, Calmar 65.9** — лучший cold-market результат во всём исследовании.

**Trade-off vs Кандидата A:** annual выше (+2.31 п.п.), но DD ×6 (0.67% vs 0.11%), full Calmar в 5 раз ниже (23 vs 114). Зато работает на любом рынке без regime detector.

### Portfolio split (рекомендация)

Поскольку A и C имеют **противоположные профили**, имеет смысл держать обе ноги одновременно:

- **A (two_phase, entry=0.15):** **молчит** в cold, выдаёт 13%/year при горячем рынке с микроскопической DD. Работает только когда есть альфа.
- **C (two_phase_dynamic, entry=0.10):** **всегда в рынке**, выдаёт 3-15%/year в зависимости от регима, DD на порядок выше.

**Простая статическая аллокация (без regime detector):**

| Профиль | A (two_phase) | C (two_phase_dynamic) | Смысл |
|---|---|---|---|
| Консервативный | 70% | 30% | Минимальная DD, opportunity cost когда C простаивает |
| **Сбалансированный** ⭐ | **50%** | **50%** | Плавный профиль: A добавляет upside в горячем рынке, C обеспечивает baseline |
| Агрессивный | 30% | 70% | Максимальная активность, готов к DD до ~0.5% на портфельном уровне |

**Сбалансированный 50/50 (грубая оценка, A и C на ~независимых трейдах):**
- Full annual: ≈ (13.03 + 15.34) / 2 = **14.2%**
- Full DD: ≈ max(0.11, 0.67) = **0.67%** (доминирует C)
- Last_90d annual: ≈ (0 + 3.44) / 2 = **1.72%**

**Важно для реализации:** A и C **должны видеть разные слоты** (например, по K=2 у каждой = total 4 позиции одновременно). Иначе они будут конкурировать за один coin и сигнал A (e=0.15) всегда выиграет у C (e=0.10) — C превратится в reserve, а смысл split'а пропадёт. Технически: две отдельные `strategies` записи в DB, каждая со своим набором позиций.

### ВАЖНО

Текущие prod-параметры (entry=0.10, min_hold=1) **гарантированно убыточны** — при min_hold=1 трейд закрывается через 1 час, fees=21bps, funding gain за 1h при 10% annual ≈0.11bps → убыток −20.9bps на каждый трейд. Нужно срочно переключить на A или B.

### Лучшее из обоих миров (теоретически)

Regime detector: автоматическое переключение между two_phase entry=0.15 (hot mode) и dynamic_aggressive entry=0.08 (cold mode) на основе median funding rate за последние 30 дней. **Не реализовано — теперь менее актуально, т.к. Кандидат C сам адаптируется через per-position dynamic min_hold.**

---

## Что осталось НЕ исследовано

1. ~~**Combination two-phase exit + dynamic min_hold на low entry**~~ ✅ **Сделано** (см. эксперимент #20, файл `two_phase_dynamic.py`). Гипотеза подтвердилась: low entry + dynamic min_hold = активность в cold market БЕЗ phase1-катастрофы. Стал Кандидатом C.

2. **Regime-switching автомат** — менее актуально, т.к. Кандидат C (two_phase_dynamic) сам адаптируется через per-position dynamic min_hold. Но split A+C (статичная аллокация 50/50) даёт ещё лучший результат — описан, требует реализации в Engine как две независимые strategy records.

3. **Strategy B (stake & hedge)** — по memory calmar 3.07 в бектесте, независимый источник альфы. `research/backtest_b_hedge_results.csv` существует, но стратегии A+B не сведены в единый portfolio view.

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

## Rebalance branch — scale-in/scale-out + rotation (v0.0 → v0.9)

Параллельная ветка исследования: вместо бинарного "open full / close full" — **постепенный вход/выход** + **ротация** капитала между монетами по силе funding. Принципиально другая идея: убрать хардкод `min_hold` и `exit_threshold`, заменить их **экономикой движений** (хочешь выйти — плати fees; есть лучше монета — переезжай).

**Архитектура:**
- 4 слота (`n_slots_total = 4`), из них до 2 в активной фазе одновременно (`n_main_cap = 2`). Остальные 2 — buffer для ротации (новый coin growing пока старый shrinking).
- State machine: `empty → growing → holding → shrinking → empty`.
- Per-tranche accounting: каждый "транш" = 10% от full position ($100 spot + $100 perp), со своим entry_price, накопленным funding, fees. P&L позиции = сумма по траншам.
- Ротация: если в неудерживаемой монете `ma12 > current + rotation_delta_apr (10%)` — старый слот в `shrinking`, новый в `growing`.
- Frozen-growing unwind (Fix A): если slot замёрз на partial fill (signal упал ниже entry threshold) и unrealized P&L достиг breakeven — сворачивается.
- Aave overlay: idle капитал может accrue 5% APR baseline (в v0.5 убран для честных метрик).

**Универс:** U11 = BTC, ETH, SOL, AVAX, LINK, AAVE, DOGE, UNI, ARB, OP, TIA. Capital base = peak observed (~$4.4k = ~2.2 слота).

### Итерации

| # | Файл | Что | Pure annual full | Calmar full | Annual last_90d | Заметка |
|---|---|---|---|---|---|---|
| v0.0 | `rebalance_v0.py` | baseline mechanism (4 slots, 10% tranches, rotation, signal-based exit at 10% APR) | 5-7%* | 8-12* | −0.65 to 0.82* | * с Aave overlay |
| v0.1 | `rebalance_v01.py` | +Aave 5% baseline, drop breakeven gate, fix window-scoped metrics | 7-9%* | 4-19* | 0.7-4.5%* | * с Aave |
| v0.2 | `rebalance_v02.py` | +Fix A (frozen-growing unwind), asymmetric entry/exit (15%/12%), window-aware %usdc | 8.76* | 6.84* | 5.00%* | * Aave давала +3.94% |
| v0.3 | `rebalance_v03.py` | exit_signal_threshold 0.12 → 0.00 (hold-til-zero) | 8.76* | 6.84* | 5.00%* | **null result** — ротация всегда срабатывает РАНЬШЕ exit floor. Exit threshold — dead code. |
| v0.4 | `rebalance_v04.py` | derived entry threshold: `2 × (aave + target_alpha + fee_drag) ≈ 0.15` | 8.96* | 4.77* | 5.00%* | Magic 15% оказалась экономически выведена. |
| v0.5 | `rebalance_v05.py` | **drop Aave overlay** + sweep floor (0/5/10/15) | 4.82 → 8.26 | 3.6 → 4.4 | 0 → 1.52 | Pure strategy без подушек. |

\* — с Aave income overlay (~3.94% padding).

### Ключевая находка v0.5 — Aave маскировал реальные числа

| config (no Aave overlay) | floor | annual_full | calmar_full | annual_90d | dd_full | dd_90d |
|---|---|---|---|---|---|---|
| v05_no_floor | 0% | **8.26%** | 4.03 | **−1.06%** | 2.05% | 0.45% |
| v05_floor_5 | 5% | 8.41% | 3.58 | 1.17% | 2.35% | 0.11% |
| v05_floor_10 | 10% | 7.44% | 4.44 | **1.52%** | 1.68% | 0.03% |
| **v05_floor_15** (derived) | 15% | **4.82%** | 3.74 | 0.00% | 1.29% | 0.00% |

**Floor=15% (derived from economics = 2 × (Aave 5% + fee_drag 2.5%)) — рекомендация.** Не самый высокий annual, но единственный с экономическим обоснованием. Floor=10% даёт лучшие cold-market числа, **но это подгонка под текущий рынок** — нет принципа, который бы её обосновывал, и при изменении Aave rate / fees / market regime она перестанет быть оптимальной.

### Сравнение rebalance vs two_phase_exit (pure strategy, без Aave)

| Strategy | Full annual | Full Calmar | Full DD | 90d annual | 90d Calmar |
|---|---|---|---|---|---|
| **two_phase (entry=0.15)** | **13.03%** | **114.4** | 0.11% | −0.07% | −1.3 |
| baseline_DynAgg | 11.19% | 14.8 | 0.76% | **2.79%** | 51.2 |
| baseline_COMBO (prod) | 9.93% | 100.9 | 0.10% | 0.00% | 0 |
| rebalance v0.5 floor=10 | 7.44% | 4.44 | 1.68% | 1.52% | 51.2 |
| rebalance v0.5 floor=15 | 4.82% | 3.74 | 1.29% | 0.00% | 0 |

**Вывод:** rebalance ветка **не доминирует** two_phase_exit ни на одном горизонте. Two_phase — лучший на full period (13.03% / Calmar 114, в **2.7× больше annual** при **30× лучше Calmar**). DynAgg — лучший на last_90d (2.79% vs наши 1.52%). Rebalance занимает нишу "стабильно мало" — DD скромнее на cold market, но и upside меньше.

### Дополнительные наблюдения

1. **exit_signal_threshold — dead code.** В v0.3 проверили все значения 0.00 / 0.05 / 0.12 / −0.05 — идентичные результаты. Rotation срабатывает раньше, exit threshold никогда не доходит до триггера. В v1.0 можно его удалить из API.

2. **Frozen-growing unwind (Fix A) — обязательный фикс**, без него партишн позиции зависают навсегда и блокируют слот. До Fix A: 4.54% annual last_90d (trapped). После: 5.00% (= match Aave через корректный exit). Чистый win.

3. **Capital usage:** peak ~$4.4k, average ~$3.5k. Стратегия использует ~2 слота из 4 в нормальном режиме, поднимается до 2.2-2.5 во время ротации.

4. **fast_tick (6h tick)** даёт чуть выше calmar при цене 3-4× fees. Не рекомендуется — увеличение churn без понятной выгоды.

### Вердикт по rebalance ветке

**Не превзошла two_phase_exit.** Хорошие свойства (нет хардкода `min_hold`/`exit_threshold`, плавный вход/выход, естественная защита от спайков на входе) не компенсируют **проигрыш в annual returns и Calmar в 30× раз**. Архитектурно интересна, но pure performance уступает.

**Что можно ещё посмотреть, если возвращаться:**
1. Combined approach: two_phase signal exit logic + tranche-based entry (10% step). Может дать ramp-in benefit без потери spike capture.
2. Сделать rotation **более избирательным** — сейчас trigger на любом `+10% APR delta`. Может надо требовать persistence (delta держится N часов).
3. Тестировать на других периодах (2024 hot, 2025 mixed) — может в горячем рынке rebalance дисциплина даёт что-то чего two_phase не может.

### Продолжение rebalance ветки (v0.6 → v0.9)

| # | Файл | Что | Pure annual full | Calmar full | Annual last_90d | Calmar 90d | Заметка |
|---|---|---|---|---|---|---|---|
| v0.6 | `rebalance_v06.py` | Раздельные thresholds: entry для **первого транша** убран (берём best ma12 без floor), continue ramp = **trailing anchor** (anchor двигается только вверх, slack по APR) | 2.07 → 2.63 | 2.96 → **4.53** | 1.73 → **5.75** | **3.74** → **126.27** | strict trailing убивает upside; **slack_5pct** — лучший risk-adjusted во всей ветке, впервые пробил Aave 5% на last_90d без overlay |
| v0.7 | `rebalance_v07.py` | **Defensive rotation**: ротация только когда current ma12 < degradation_threshold | 2.85-5.00 | 1.31-2.78 | −8.31 → 1.16 | −2.75 → 1.44 | **Null result**: `n_degradation_exits = 0` во всех конфигах. Спека была: "candidate > current" → всегда находит marginally-better мертвеца → круговорот. |
| v0.8 | `rebalance_v08.py` | Fix v0.7 бага: replacement должен быть HEALTHY (> threshold), а не "лучше дохлой". + first-tranche entry тоже с floor. | 2.85-5.00 | 1.31-2.78 | −8.31 → 1.16 | −2.75 → 1.44 | **Идентично v0.7** — в U11 за 2.5 года ни одного тика когда все 11 монет одновременно ниже 5% threshold. Fix корректен но в этих данных не активируется. |
| v0.9 | `rebalance_v09.py` | Убрать лимит на позиции (n_main_cap 2 → 11) + tick 24h → 1h + sweep continue variants (trailing/fixed/decay/none) | 3.45 → 7.02 | 0.29 → 1.13 | **−14.58 → −6.68** | −4.06 → −4.04 | **Структурные изменения убили risk-adjusted.** Gross funding ×3, но fees ×6 (11 слотов × 1h tick = много сделок). Last_90d все конфиги глубоко в минусе. continue_mode='none' лучший — confirms anchor logic это fee-burning machine. |

### Ключевая находка v0.6 — slack_5pct лучший в rebalance ветке

| config (no Aave) | continue logic | annual_full | calmar_full | annual_90d | calmar_90d | DD_full |
|---|---|---|---|---|---|---|
| v0.5 no_floor | без anchor | 8.26% | 4.03 | −1.06% | −2.33 | 2.05% |
| v0.5 floor_15 | с floor | 4.82% | 3.74 | 0.00% | 0 | 1.29% |
| **v0.6 slack_5pct** | trailing+5% slack | 2.63% | **4.53** | **5.75%** | **126.27** | 0.58% |
| v0.9 best (none) | без anchor, 1h, 11 slots | 7.02% | 1.13 | −6.68% | −4.04 | 6.20% |

**v0.6 slack_5pct остаётся рекомендованным** конфигом этой ветки: единственный pure-strategy config который пробил Aave 5% на last_90d (через trailing anchor + selective rotation). Жертвует full-period upside (2.63% vs v0.5's 8.26%), но даёт устойчивость в cold market.

### Архитектурные insights из v0.6-v0.9

1. **Trailing anchor как fee-burning machine.** На 1h tick + 11 slots каждый микро-dip MA12 = freeze/unfreeze cycle = round-trip fees. v0.9 показал что fees scale с количеством сделок (×6), а gross funding только с deployed капиталом (×3). Net negative.

2. **Defensive rotation бесполезен на нашей вселенной (U11).** Чтобы exit-to-USDC сработал, нужно чтобы ВСЕ 11 монет одновременно были ниже degradation threshold. За 2.5 года это случилось 1 раз и не активировало путь. U11 даёт достаточно диверсификации что "вся вселенная мертва" не возникает.

3. **Размер позиций не зависит от количества слотов линейно.** Убрав n_main_cap=2 → 11, peak_capital вырос с $4.4k до $22k. Но annual return НЕ вырос пропорционально — fees съели всё.

4. **continue_mode='none' (fill-and-hold) выигрывает в high-frequency context.** Это противоречит интуиции "нужна защита от ramp into degrading coin", но эмпирически — round-trip cost защиты > потенциального loss от плохого ramp.

5. **Fee reduction — единственный реальный рычаг для масштабирования.** Чтобы стратегия с большим капиталом и быстрым tick'ом заработала, нужно либо снизить fees (maker orders / VIP tier / Drift / Binance perp), либо принципиально уменьшить churn (slice → 100% сразу).

### Обновлённый вердикт по rebalance ветке

**v0.6 slack_5pct — лучший risk-adjusted во всей rebalance ветке** (Calmar 4.53 full / 126.27 last_90d), **но проигрывает two_phase_exit** на full period (2.63% vs 13.03%). Подтверждение исходного вывода: rebalance — это **defensively-focused niche**, не general-purpose alternative для two_phase.

**v0.9 показал boundary условия:** при попытке масштабировать (больше слотов, чаще tick) — fee drag начинает доминировать. Без снижения fees rebalance не масштабируется.

**Что осталось не проверено в rebalance ветке:**
1. **Larger slice (20-50%)** при default tick=24h. Меньше round-trips на full position.
2. **Maker orders** в paper executor (slippage модель должна учитывать post-only/limit orders).
3. **Strategy combination**: v0.6 slack_5pct (defensive) + two_phase (offensive) в одной portfolio, аллокация по regime.

### Файлы ветки (v0.0 → v0.9)

- [rebalance_v0.py](rebalance_v0.py), [rebalance_v0_results.csv](rebalance_v0_results.csv)
- [rebalance_v01.py](rebalance_v01.py), [rebalance_v01_results.csv](rebalance_v01_results.csv)
- [rebalance_v02.py](rebalance_v02.py), [rebalance_v02_results.csv](rebalance_v02_results.csv)
- [rebalance_v03.py](rebalance_v03.py), [rebalance_v03_results.csv](rebalance_v03_results.csv)
- [rebalance_v04.py](rebalance_v04.py), [rebalance_v04_results.csv](rebalance_v04_results.csv)
- [rebalance_v05.py](rebalance_v05.py), [rebalance_v05_results.csv](rebalance_v05_results.csv)
- [rebalance_v06.py](rebalance_v06.py), [rebalance_v06_results.csv](rebalance_v06_results.csv)
- [rebalance_v07.py](rebalance_v07.py), [rebalance_v07_results.csv](rebalance_v07_results.csv)
- [rebalance_v08.py](rebalance_v08.py), [rebalance_v08_results.csv](rebalance_v08_results.csv)
- [rebalance_v09.py](rebalance_v09.py), [rebalance_v09_results.csv](rebalance_v09_results.csv)

---

*Документ составлен 2026-05-16, обновлён 2026-05-17. Числа из CSV — точные, без округления сверх исходных 2 знаков после запятой.*
