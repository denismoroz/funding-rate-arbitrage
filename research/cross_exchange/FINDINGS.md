# FINDINGS — Cross-exchange funding-spread carry as a candidate return stream

Итог по Tasks A–E плана `PLAN.md`. Research-only, прод не тронут. Данные: funding
HL (1h) / Binance (8h) / Bybit (8h), ~2023-06 → 2026-05, ядро 10 монет
(BTC ETH SOL AVAX LINK AAVE DOGE ARB OP MATIC), приведены к общей 8h-сетке.

## Идея

Тот же perp-контракт имеет РАЗНЫЙ funding на разных биржах. Delta-neutral по цене,
не-нейтрально по funding: **short-perp на бирже с высоким funding** (лонги платят
тебе), **long-perp на бирже с низким** → net carry/период = `funding_rich −
funding_cheap = spread`. Цена захеджирована между двумя perp-ногами. Это НЕ дубль
прошлой cross-venue работы (`CROSS_VENUE_SYNTHESIS.md` делал best-venue-per-coin
*routing*; здесь — *spread carry*, собираем РАЗНИЦУ).

## Что построено

- `spread.py` — движок: `load_funding` / `resample_8h` (1h→сумма в 8h-бакет) /
  `build_spread_panel` / `spread_signal` (stateful hysteresis) /
  `portfolio_returns_spread`. **Лаговый carry, no look-ahead**: `carry[t] =
  position[t-1]·spread[t]`, кост двойной (4 taker-ноги на round-trip, per-venue:
  HL 3.5 / Binance 5.0 / Bybit 5.5 bps + slip 0.2). 29 self-test асертов.
- `characterize.py` — статы спредов + два семейства сигналов + sweep.
- `spread_validation.py` — прогон committed через `validation_harness` (CPCV+DSR+PBO).
- `blend_vs_book.py` — **решающий**: декорреляция vs XSMOM и FRAB-proxy + risk-parity.
- Провенанс: committed-книга bit-exact (diff ~1e-16) во всех скриптах.

Committed = **HL-Binance, trailing-direction lb90/rb21** (causal): направление =
`sign(trailing-30д mean spread)`, ребаланс ~раз в неделю, держим.

## Результаты

### 1. Gross-эдж РЕАЛЕН, но наивная версия убита костами
- Gross funding-spread **+7…10%/год на монету, ВСЕ положительные** (HL структурно
  богаче CEX). Sign-persistence **0.77–0.96** (HL vs CEX) — «кто богаче» устойчиво.
- Binance-Bybit спред ≈0 (два CEX тикают funding почти одинаково) — нет HL-ноги,
  нечего собирать.
- **Наивный always-flip (threshold=0) = −27%/год NET**: спред шумит вокруг
  положительного среднего, посимвольный sign-chase даёт ~1170 флипов/год × двойной
  кост → costs съедают gross. Churn — весь убыток.
- **Trailing-direction lb90/rb21 → NET +9.26%/год, turnover 62/год** (vs 1170).
  Низкий turnover причинно восстанавливает структурный наклон. Эдж — в УДЕРЖАНИИ
  направления, не в ловле каждого бара.

### 2. Standalone метрики ИНФЛИРОВАНЫ артефактом гладкости (Task C)
- DSR(committed, N=18) = **1.0 PASS**, PBO = **0.21** (умеренный), OOS-знак выживает
  (**100%** сегментов положительны), 15/18 меню OOS-положительны.
- НО абсолютный **Sharpe ~13.6 / maxDD 0.35% — ФАНТОМ**: модель funding-only, БЕЗ
  cross-venue basis/MTM риска (две perp-ноги считаются идеально delta-neutral
  по-тиково). pnl — гладкое начисление funding: **lag-1 autocorr 0.80**, vol 0.68%,
  **n_eff ≈ 119** (не 1078). DSR/Sharpe period-инфлированы гладкостью.
- Честный читаемый сигнал: **ЗНАК эджа реален и OOS-устойчив**; абсолютный
  risk-adjusted уровень — нет (доминирующий риск не смоделирован). Тот же паттерн,
  что у FRAB: funding-only бэктест льстит, live скромнее.

### 3. РЕШАЮЩИЙ — декорреляция (Task D): НЕ carry-диверсификатор
| пара | Pearson | rolling-90д | %\|corr\|<0.3 |
|------|---------|-------------|---------------|
| SPREAD ⟂ XSMOM       | **+0.01** | +0.01 | 100%  |
| SPREAD ⟂ FRAB-proxy  | **+0.86** | +0.70 | 4.6%  |
| XSMOM  ⟂ FRAB-proxy  | +0.04     | +0.05 | 99.9% |

- **SPREAD ⟂ FRAB-proxy (HL carry) = +0.86** — высоко коррелирован. Механически
  ожидаемо: HL funding — общий драйвер и HL-ноги спреда, и HL-carry-прокси.
  Cross-exchange spread — это **leveraged переэкспрессия той же экспозиции, что уже
  собирает FRAB**, а не новый funding-источник.
- SPREAD ⟂ XSMOM ≈ 0 — да, некоррелирован с momentum. НО **FRAB тоже** (+0.04). На
  оси momentum spread не даёт ничего, чего бы уже не давал FRAB.
- Crisis: SPREAD держался в 5/5 худших просадок XSMOM, но лишь 2/5 FRAB-proxy
  (со-кровоточит с HL-carry — согласуется с +0.86).
- Корреляция scale-инвариантна → этот вывод НЕ зависит от артефакта гладкости (в
  отличие от Sharpe). risk-parity inv-vol дал бы spread вес 0.64–0.99 (раздут крошечной
  фантомной vol) — поэтому blend-Sharpe только suggestive, декорреляция — robust.

## Решение: НЕ строить live

Cross-exchange funding-spread carry — **реальный gross-эдж, но НЕ то, что нам нужно**:
1. **Не carry-диверсификатор** (corr +0.86 с HL funding) — приз был именно
   FRAB-декорреляция, и он не взят. FRAB уже покрывает HL-funding экспозицию.
2. **Риск мисмерян**: абсолютные метрики раздуты гладкостью funding-only; реальный
   доминирующий риск (basis между HL/Binance perp, неатомарный выход между биржами,
   контрагент второй биржи) НЕ смоделирован и вероятно материален.
3. **Операционная сложность** ради экспозиции, которую уже держим: капитал на 2
   биржах, две площадки маржи, контрагентский риск — за тот же HL-carry.

Эдж положить как **validated-но-отвергнутый**: код и валидация готовы; пересмотреть
ТОЛЬКО если появится площадка с funding-источником, структурно НЕзависимым от HL
(другой класс трейдеров/механизм), что дало бы реальную декорреляцию.

## Caveats
- **funding-only, нет модели basis/ликвидации/контрагента** — потолок честности этих
  данных (нет синхронных perp-mark'ов между биржами). Sharpe/maxDD/DSR оптимистичны.
- 3 года, преимущественно растущий рынок; нет затяжной медвежки in-sample.
- **FRAB — ПРОКСИ** (HL funding-harvest), не живой FRAB. Реальный FRAB⟂spread замер —
  на ЖИВЫХ данных (live-чекпоинт ~2026-07-16, memory `project_riskparity_checkpoint`).
- Серийная гладкость (autocorr 0.80) → n_eff≈119; эффективная выборка мала.
- Secondary venues (Drift/Backpack) исключены (короткая/однорежимная история).

## Связи
Ось carry-диверсификации, которую стоит мерить, — **FRAB ⟂ XSMOM на ЖИВЫХ данных**
(не perp-spread, который дублирует FRAB). Дисциплина claims — `feedback_backtest_claims`;
реальные косты — `project_execution_costs`; trend-параллель (тоже коррелированный
кузен, не диверсификатор) — `project_trend_following`.
