# Signal Window × Phase1 Negative Patience — 2D Sweep

**Date:** 2026-06-03
**Strategy:** two_phase_dynamic (live prod)
**Universe:** BTC, ETH, SOL, HYPE, PURR (live config)
**Period:** full available (HL data) + last_90d

## Motivation

Текущий прод использует `signal_window=12h` и `phase1_negative_patience=72h`. Оба параметра захардкожены с самого начала — ни разу не были предметом sweep'а. Цель — проверить совместное влияние и найти оптимум для текущего universe.

`phase1_negative_patience` — число часов подряд с отрицательным smoothed signal до триггера `CLOSE_PHASE1_NEG` (cut-loss в Phase 1). `signal_window` — окно rolling mean по funding rate, который сглаживает шум.

Эти два параметра связаны: длинное MA медленнее уходит в минус, короткое — быстрее. Поэтому tune'ить их отдельно бессмысленно.

## Sweep grid

- `signal_window ∈ {4, 6, 8, 12, 24}` часов
- `phase1_negative_patience ∈ {24, 48, 72, 120, 240}` часов
- 25 точек × 2 периода (full + last_90d) = 50 строк

**Зафиксированные параметры (из live `params_json`):**
```
entry_threshold = 0.10
safety_mult = 5
base_min_hold = 24
cap_min_hold = 720
phase1_breakeven_cap_hours = 720
phase2_exit_threshold = -0.10
K = 3
fee_multiplier = 1.0
```

## Результаты

### Annual % — full period
```
  sw\pat       24      48      72     120     240
       4    9.99    9.99    9.98    9.98    9.96
       6   10.81   10.80   10.80   10.79   10.79
       8   10.44   10.43   10.42   10.42   10.42
      12   10.38   10.37   10.36*  10.35   10.35     ← baseline
      24   11.16   11.15   11.14   11.14   11.14
```

### Calmar — full period
```
  sw\pat       24      48      72     120     240
       4    17.2    16.8    16.5    16.0    14.5
       6    19.8    18.8    18.1    18.0    18.0
       8    21.6    20.3    19.3    18.8    18.8
      12    21.7    20.3    19.5*   19.1    19.1     ← baseline
      24    33.0    30.5    29.8    29.2    29.2
```

### Annual % — last_90d (cold market)
```
  sw\pat       24      48      72     120     240
       4    3.38    3.36    3.36    3.36    3.36
       6    5.83    5.83    5.83    5.83    5.83
       8    6.69    6.69    6.69    6.69    6.69     ← best на 90d
      12    5.02    5.02    5.02*   5.02    5.02     ← baseline
      24    5.62    5.62    5.62    5.62    5.62
```

На last_90d значения `patience` не различаются ни в одной строке — за последние 90 дней не было затяжных серий negative signal даже для 24h, и cut-loss не триггерил.

## Топ-3 по annual (full)

| # | sw | patience | annual% | Calmar | max_dd% |
|---|----|----------|---------|--------|---------|
| 1 | 24 | 24 | 11.16 | 33.0 | 0.34 |
| 2 | 24 | 48 | 11.15 | 30.5 | 0.37 |
| 3 | 24 | 72 | 11.14 | 29.8 | 0.37 |

## Топ-3 по Calmar (full)

| # | sw | patience | Calmar | annual% | max_dd% |
|---|----|----------|--------|---------|---------|
| 1 | 24 | 24 | 33.0 | 11.16 | 0.34 |
| 2 | 24 | 48 | 30.5 | 11.15 | 0.37 |
| 3 | 24 | 72 | 29.8 | 11.14 | 0.37 |

## Топ-3 по annual (last_90d)

| # | sw | patience | annual% | Calmar | max_dd% |
|---|----|----------|---------|--------|---------|
| 1 | 8 | 24 | 6.69 | 73.5 | 0.09 |
| 2 | 6 | 24 | 5.83 | 52.7 | 0.11 |
| 3 | 24 | 24 | 5.62 | 81.2 | 0.07 |

## Выводы

1. **sw=24 доминирует на full периоде** (+0.8 п.п. vs baseline, Calmar 33 vs 19.5).
2. **sw=8 доминирует на last_90d** (+1.7 п.п. vs baseline). На холодном рынке более короткое окно ловит транзиентные funding-окна, которые sw=24 успевает сгладить в ноль.
3. **patience оказывает слабое влияние на annual** во всех окнах (~0.03 п.п. между крайними значениями при фиксированном sw), но заметно сокращает Calmar при коротких значениях: переход с patience=72 на 24 даёт **+1.5–2 п.п. Calmar** и снижает max_dd на ~0.02–0.04 п.п.
4. **patience и sw связаны:** короткое окно делает signal чувствительнее к шуму → меньше patience нужно, чтобы вовремя резать; длинное окно само фильтрует, длинная patience избыточна.

## Рекомендация для live

**Конфликт sw=24 (full) vs sw=8 (90d)** разрешается верой в режим рынка:

- если ждём возврата горячего funding → sw=24, patience=24 (annual 11.16% / Calmar 33)
- если текущий cold-режим продолжится → sw=8, patience=24 (last_90d 6.69% / Calmar 73)
- компромисс — sw=8, patience=24: full 10.44% (всего на 0.7 п.п. хуже sw=24) и +1.7 п.п. на 90d vs baseline

**Решение:** перейти на `signal_window_hours=8`, `phase1_negative_patience=24`. Patch live через UI; reload без рестарта (Force Tick).

## Caveat

Числа из old-sweep'а (`two_phase_dynamic_stability_results.csv`) показывали sw=12/patience=72 = **15.34%** annual на U7 = [BTC, ETH, SOL, AVAX, LINK, AAVE, DOGE]. На live universe (5 коинов без bridge tokens) тот же конфиг даёт **10.36%** — разрыв в **5 п.п. от состава universe, не от параметров**. Числа из U7-sweep не переносятся на live, поэтому этот sweep заново под live config.

## Артефакты

- [research/signal_window_patience_sweep.py](signal_window_patience_sweep.py) — скрипт
- [research/signal_window_patience_sweep_results.csv](signal_window_patience_sweep_results.csv) — сырые результаты (50 строк)
