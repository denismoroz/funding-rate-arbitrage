# FINDINGS — Trend-following (TSMOM + Donchian) as a third return stream

Итог по Tasks A–E плана `PLAN.md`. Research-only, прод не тронут. PT-панель
survivorship-debiased, ~2023-06-08 → 2026-06-13 (1102 дня, 62 коина), та же
панель, что у cross-sec книги → корреляции апельсин-в-апельсин.

## Что построено

- `trend.py` — directional, vol-targeted движок: TSMOM (per-lookback + ансамбль),
  Donchian breakout (stateful), `portfolio_returns_directional` (дневной ребаланс,
  funding-accrual, vol-target, leverage cap). 24 self-test асерта. Причинный, без
  look-ahead. Структурно ДРУГОЙ, чем `xsec.py` (не нормируем к Σ=±1).
- `characterize.py` — характеризация на PT-панели. Committed = **TSMOM-ENSEMBLE**
  (lookbacks 30/60/90/120). Константы: VOL_TARGET=0.02/день, LEVERAGE_CAP=3.0,
  COSTS_BPS=8.5, accrual=−funding.shift(−1).
- `trend_validation.py` — прогон через validation_harness (CPCV n_groups=6 k=2
  purge=120 embargo=7, DSR, PBO).
- `blend_vs_xsmom.py` — **решающий**: декорреляция с cross-sec momentum + risk-parity.

Провенанс: committed-книга bit-exact (diff 0.0) во всех трёх скриптах.

## Результаты

### Standalone эдж — MARGINAL
- DSR(committed, N=8 меню) = **0.81** → WARN (не PASS >0.95). DSR(N=1)=0.89.
- Pooled-OOS median Sharpe (честный, sqrt365) = **+0.94**, 84% сегментов положительны
  → OOS переживает.
- **PBO = 0.63 → ВЫСОКИЙ риск оверфита.** Меню коррелировано, выбор IS-победителя
  ненадёжен. Главный красный флаг.
- IS Sharpe ансамбля 0.72, Calmar 1.19, skew −0.05 (наименее отрицательный в меню,
  vs cross-sec спред −0.20 — как и предсказывает trend-теория).
- ⚠ Абсолютные Ann/Vol/MaxDD (111%/156%/94%) — **артефакт** VOL_TARGET=2%/день ×
  gross~2.9, НЕ деплой-конфиг. Sharpe/Calmar/skew vol-инвариантны и являются
  честными метриками. Для DSR/PBO/корреляции масштаб безразличен.

### Декорреляция с XSMOM (cross-sec momentum) — НЕ прошла чистую планку
- **Pearson +0.40**, Spearman +0.39. Rolling-90д: mean +0.40, диапазон [−0.09, +0.76],
  |corr|<0.3 лишь в **32%** окон → **не робастно низкая**.
- Структурная причина: TSMOM и XSMOM — **оба momentum**, делят общий риск-фактор.
  Тезис «третий некоррелированный поток» оказался слишком оптимистичным.
- Risk-parity бленд (inv-vol, w≈0.20 trend / 0.80 xsmom): Sharpe **+0.88** (бьёт
  лучшую ногу XSMOM +0.76, Δ+0.12), НО maxDD **43.5%** глубже, чем у XSMOM (27.8%).
  → диверсификация по Sharpe, но НЕ по просадке.
- Time-varying beta подтверждён: rolling-90д beta тренда к BTC качается −1.04…+0.76
  (отрицательна 52% дней); XSMOM market-neutral (~0). Crisis-alpha сигнатура есть.
- Crisis-alpha смешанный: trend flat-to-positive лишь в **2 из 5** худших просадок
  XSMOM (+132%, +121% в резких сбросах), но тёк вместе с XSMOM в медленном чопе
  2024-05 / 2025-02 / гринде 2025-26.

**Вердикт Task D: NEEDS-LIVE-CONFIRMATION** — ни чистый BUILD, ни DON'T.

## Главный вывод и рамка trend+carry

Trend — **НЕ чистый третий некоррелированный поток**, а умеренно-коррелированный
(+0.40) кузен XSMOM: оба эксплуатируют momentum. Standalone маргинален (DSR WARN,
PBO высокий). Бленд с XSMOM улучшает Sharpe, но углубляет просадку → как пара к
cross-sec momentum trend оплату за live-сложность не отрабатывает.

**Где декорреляция реально может быть — это trend ⟂ carry (FRAB), не trend ⟂ XSMOM.**
Carry (FRAB funding-арбитраж) структурно противоположен trend: short-vol /
mean-reverting кэрри-сбор vs long-vol / convex trend. Ось диверсификации, которую
стоит мерить, — momentum (XSMOM) ⟂ carry (FRAB) ⟂ trend, и решающий read — на
ЖИВЫХ данных, не sim:

- Сначала закрыть live-чекпоинт FRAB⟂XSMOM (~2026-07-16, см. memory
  `project_riskparity_checkpoint`) — это первая реальная ось диверсификации.
- Trend держать как validated-но-отложенный кандидат: код готов и провалидирован,
  но строить live ТОЛЬКО если live-данные покажут, что он добавляет к корзине
  carry+momentum (в частности — некоррелирован с FRAB), чего sim показать не может
  (нет настоящей затяжной медвежки в выборке).

## Caveats
- ~3 года, преимущественно растущий рынок, НЕТ устойчивой медвежки in-sample →
  crisis-alpha **suggested, не proven**. Реальный read — live.
- Trend-книга directional (time-varying beta) — не market-neutral, в отличие от XSMOM.
- Honest absolute levels только через `metrics_daily` (sqrt365); harness-аннуализация
  hourly (×4.9 Sharpe / ×35 ann) помечена как раздутая везде.
- PT-панель, survivorship-debiased; меню коррелировано (отсюда высокий PBO).

## Решение
**НЕ строить trend live сейчас.** Код и валидация готовы и зафиксированы. Следующий
шаг диверсификации — live-чекпоинт FRAB⟂XSMOM; trend пересмотреть как trend⟂carry
кандидат уже на живых данных.
