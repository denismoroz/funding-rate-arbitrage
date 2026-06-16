# PLAN — Cross-exchange funding-spread carry as a candidate return stream

## Context & goal

FRAB (carry) = HL **spot-vs-perp basis**. XSMOM (momentum) = cross-sectional perp.
Trend прогнан и отложен (коррелирован с XSMOM). Открытая ось — **cross-exchange
funding-spread carry**: тот же perp-контракт (BTC, ETH, …) имеет РАЗНЫЙ funding на
разных биржах (HL vs Binance vs Bybit). Идея: **delta-neutral по цене, не-нейтрально
по funding** — short-perp на бирже с высоким funding (лонги платят тебе), long-perp на
бирже с низким/отрицательным funding (тебе платят/мало платишь). Net carry за период =
`funding_rich − funding_cheap = |spread|` (до костов). Цена BTC захеджирована между
двумя perp-ногами.

**Почему это НЕ дубль прошлой cross-venue работы:** `CROSS_VENUE_SYNTHESIS.md` делал
**best-venue-per-coin routing** (выбрать ОДНУ биржу с макс. funding на монету). Здесь —
**spread carry** (одновременно short rich + long cheap, собрать РАЗНИЦУ). Другой механизм.

**Главные вопросы (как в trend PLAN):**
1. Standalone: есть ли реальный эдж после ДВОЙНЫХ костов (4 taker-ноги на round-trip,
   биржевые fees выше HL), переживает ли OOS (DSR), какой turnover.
2. **РЕШАЮЩИЙ — декорреляция с FRAB (HL basis carry) и XSMOM.** perp-vs-perp funding
   differential структурно ОТЛИЧЕН от HL spot-vs-perp basis → может быть настоящим
   некоррелированным диверсификатором. Это приз, важнее standalone DSR.

Трезвая планка: ищем тонкий некоррелированный эдж в корзину carry+momentum, не мотор.

## Данные (есть локально, переиспользуем)

Funding CSV `time, fundingRate` по биржам: `research/data` (HL, 1h), `data_binance`
(8h), `data_bybit` (8h), `data_drift` (1h), `data_backpack` (8h, с 2025-01).
- **Ядро = HL ∩ Binance ∩ Bybit**, ~3 года (с 2023-06). Пересечение монет:
  BTC ETH SOL AVAX LINK AAVE DOGE ARB OP MATIC ADA DOT LTC XRP — взять те, что есть
  во всех трёх (проверить в Task A; HL-универс уже).
- **Drift/Backpack** — вторичные (короткая история / иной механизм funding) → опционально
  в characterize как доп. пары, НЕ в committed-ядро.

### Критичные дата-нюансы (зафиксированы, чтобы агенты не разошлись)
- **Cadence mismatch:** HL/Drift = 1h funding, Binance/Bybit/Backpack = 8h. Привести к
  ОБЩЕЙ сетке **8h**: для 1h-бирж — СУММИРОВАТЬ 8 часовых fundings в 8h-бакет (00/08/16
  UTC). Спред считать на выровненной 8h-сетке (inner join по времени).
- **Знак funding:** `fundingRate>0` ⇒ лонги платят шортам. Short-perp на rich venue
  ПОЛУЧАЕТ funding_rich; long-perp на cheap venue ПЛАТИТ funding_cheap. Net/период =
  `funding_rich − funding_cheap`. Позиция = `sign(spread)`; carry = `position·spread`.
- **НЕТ модели цены/базиса/ликвидации:** у нас нет синхронных perp-mark'ов между биржами
  → моделируем ТОЛЬКО funding-spread pnl (delta-neutral ⇒ ценовой pnl двух ног взаимно
  гасится В МОДЕЛИ). Реальный basis-риск (расхождение цен между биржами на входе/выходе,
  неатомарность) — НЕ моделируется, помечать как НЕустранённый caveat. Это потолок честности.

## Косты (честно, двойные)

Round-trip cross-venue = **4 taker-ноги** (вход 2 + выход 2), по одной на каждой бирже.
Per-venue perp taker (из `multi_exchange.py`, актуальные): HL **3.5bps**, Binance
**5.0bps**, Bybit **5.5bps**. Слиппедж на майорах ~**0.2bps** (из execution-аудита,
см. memory `project_execution_costs`). Cost на смену позиции (flip) = сумма taker обеих
ног × 2 (закрыть старую пару + открыть новую). Turnover мерить честно по сменам знака.
⚠ В отличие от HL-only стратегий, здесь fee выше и ног вдвое — это главный killer тонкого
спреда. Не использовать research-овые 8.5; считать per-venue реальные.

## Reuse vs new

**Reuse:** `validation_harness/` (CPCV n_groups=6 k=2 purge≥lookback embargo=7, DSR, PBO);
`cross_sectional/crypto/metrics_daily.daily_metrics` (sqrt365, честные дневные уровни);
паттерн «книга = одна синтетическая дневная pnl-серия → harness» из
`trend_following/trend_validation.py` / `event_driven_validation.py`; для Task D —
`survivorship.run_book(panel)` (XSMOM-книга) + `survivorship.build_pt_panel`.

**New:** `research/cross_exchange/spread.py` — движок spread-carry. Не трогать xsec.py /
trend.py / прод src/frab.

### Annualization caveat (как везде)
harness `compute_metrics` аннуализирует hourly (HOURS_PER_YEAR=8760), наша pnl
агрегируется к ДНЕВНОЙ → pooled-OOS sharpe/annual раздуты. АБСОЛЮТНЫЕ уровни — только
через `metrics_daily` (sqrt365). DSR/PBO period-agnostic, валидны.

---

## Таски (каждый = отдельный Sonnet-агент; Opus ревьюит + коммитит/пушит per task)

Последовательность: B,C,D зависят от A; D зависит ещё и от C. Research only, прод не трогаем.

### Task A — Spread-движок + выравнивание данных + self-test
Создать `research/cross_exchange/spread.py`:
- `load_funding(venue_dir, coin) -> pd.Series` (indexed by UTC time, fundingRate).
- `resample_8h(funding_1h_or_8h, native_interval_h) -> pd.Series` — привести к 8h-сетке
  (1h → сумма 8 баров в бакет 00/08/16; 8h → как есть, выровнять на сетку).
- `build_spread_panel(venue_a, venue_b, coins) -> dict` → {"coins", "spread"(DataFrame,
  8h index × coins = f_a − f_b), "f_a", "f_b"}; inner join по времени, NaN до листинга.
- `spread_signal(spread, threshold=0.0, hysteresis=0.0) -> DataFrame` позиций {-1,0,+1} =
  входим в `sign(spread)` когда `|spread|>threshold`, держим пока `|spread|>hysteresis`
  (гистерезис против чаттера; threshold≥hysteresis). NaN→0.
- `portfolio_returns_spread(positions, spread, taker_a_bps, taker_b_bps, slip_bps=0.2)`
  → дневная pnl-серия книги: per-период carry = `position·spread`, минус turnover-кост
  на смену позиции = `Δposition·(taker_a+taker_b+2·slip)/1e4` (flip=закрыть+открыть обе
  ноги). Equal-weight по монетам. Агрегировать 8h→daily (сумма pnl в сутки) на выходе.
  ⚠ funding-only, БЕЗ ценовой ноги (см. caveat).
- `__main__` toy (2 монеты, несколько 8h-баров) с асертами: знак позиции = sign(spread),
  carry=|spread| при удержании, кост списывается ТОЛЬКО на flip, гистерезис не чаттерит.
**Deliverable:** модуль + проходящий self-test. Коммит+пуш.

### Task B — Характеризация спредов (sanity, без стенда)
`research/cross_exchange/characterize.py`: на 3 парах (HL-Binance, HL-Bybit, Binance-Bybit)
по ядру монет:
- per-coin spread-статы: mean/vol спреда (annualized %), **sign-persistence** (доля
  периодов, где знак спреда = знаку прошлого; структурно-устойчив ли «кто богаче»),
  % времени `|spread|>band`.
- книга по паре: честные daily-метрики (Sharpe/ann/maxDD/Calmar/hit via metrics_daily),
  **gross vs net** (с двойными костами), turnover/год.
- выбрать ОДИН committed (пара+threshold) по net-Sharpe/устойчивости — зафиксировать заранее
  для Task C. Опц.: добавить HL-Backpack/HL-Drift как доп.пары (отметить короткую историю).
- JSON `characterize.json` + краткий итог.
**Deliverable:** characterize.py + .json. Коммит+пуш.

### Task C — Прогон через validation_harness (CPCV+DSR+PBO)
`research/cross_exchange/spread_validation.py` по образцу `trend_validation.py`:
- Menu = {пары × threshold-варианты} (напр. HL-Binance/HL-Bybit/Binance-Bybit × thr∈{0,band}).
  committed = выбранный в B. Провенанс: committed-книга bit-exact (diff 0.0) vs characterize.
- DSR(committed) + DSR(N=1 на каждый) + PBO across menu. CPCV n_groups=6 k=2
  purge=lookback embargo=7. Honest daily metrics. Turnover/год на каждый.
- Вердикт: проходит ли committed DSR>0.95, переживает ли OOS, PBO. JSON.
**Deliverable:** скрипт + JSON + SUMMARY TABLE + VERDICT. Коммит+пуш.

### Task D — РЕШАЮЩИЙ: декорреляция с FRAB (HL carry) + XSMOM
`research/cross_exchange/blend_vs_book.py`:
- committed spread-книга (из C), выровнять на общем окне с:
  - **XSMOM**: `survivorship.run_book(build_pt_panel(core_coins))` (та же машинерия, что у
    trend Task D).
  - **FRAB-прокси (HL basis carry):** простая HL-only funding-harvest книга — equal-weight
    long-funding на HL (per-coin `max(funding,0)`-harvest или held HL funding accrual),
    как стенд-ин под FRAB. Явно пометить: это ПРОКСИ; реальный FRAB⟂ замер — на ЖИВЫХ
    данных (live-чекпоинт ~2026-07-16, memory `project_riskparity_checkpoint`).
- **Корреляция spread⟂XSMOM и spread⟂FRAB-proxy** (главные числа; Pearson + rolling-90д).
- **Risk-parity бленд** (inv-vol) spread+FRAB-proxy и spread+XSMOM → Sharpe/maxDD бленда
  против каждой ноги. Цель: spread должен быть НИЗКО коррелирован с HL-carry (разный
  funding-источник).
- Вердикт: стоит ли spread строить live КАК ДИВЕРСИФИКАТОР, независимо от standalone DSR.
- JSON `blend_vs_book.json`.
**Deliverable:** скрипт + JSON + вердикт по диверсификации. Коммит+пуш.

### Task E — Сводка + рамка (Opus сам, по итогам A–D)
`research/cross_exchange/FINDINGS.md`: standalone (DSR/OOS/turnover/двойные косты),
декорреляция с FRAB-proxy и XSMOM, путь к carry-диверсификации. Честные caveats
(нет модели базиса/ликвидации/контрагента, cadence-выравнивание, 3 года растущий рынок,
FRAB только прокси). Кандидат на memory-запись.

---

## Verification (общее)
- Self-test spread.py обязателен (Task A assert): знак, carry, кост-на-flip, гистерезис.
- No look-ahead: сигнал в период t использует spread≤t, зарабатывает следующий период.
  purge≥lookback (для сигнала с гистерезисом lookback мал, но purge≥1 период минимум;
  если threshold на rolling-стате — purge≥окно).
- Honest daily levels только через metrics_daily (sqrt365); pooled-OOS harness-числа
  помечать как раздутые.
- Косты — per-venue реальные (HL 3.5 / Binance 5.0 / Bybit 5.5 bps + slip 0.2), НЕ 8.5.
- Каждый таск — отдельный коммит+пуш (CLAUDE.md), без Co-Authored-By.
- venv python: `/Users/d/prj/funding-rate-arbitrage/.venv/bin/python` (в системном нет numpy).
  PYTHONPATH: `research:research/validation_harness:research/cross_sectional:research/cross_sectional/crypto:research/cross_exchange`.

## Что НЕ делать
- Не трогать `src/frab` (прод), `xsec.py`, `trend.py`.
- Не моделировать ложный ценовой/базисный pnl, которого нет в данных — спред-carry
  funding-only, basis-риск честно помечен как неустранённый.
- Не цитировать research-овые 8.5bps; не аннуализировать hourly как абсолют.
