# PLAN — Trend-following (TSMOM + Donchian breakout) as a third return stream

## Context & goal

XSMOM (cross-sectional momentum) внутри-книжные ручки исчерпаны: lookback-ансамбль,
каденция R=7, event-driven дрейф, inverse-vol веса — все через validation_harness →
**keep incumbent**. FX — слабый диверсификатор (Sharpe 0.32), отдельный движок, не стоит.
Единственный непроверенный кандидат с реальной литературой и СТРУКТУРНО ДРУГИМ профилем —
**trend-following** (TSMOM / Donchian breakout). В broad study он в топ-3 (Donchian/carry/trend),
и там же помечен следующий шаг — **trend+carry бленд**.

**Цель:** прогнать TSMOM + Donchian на HL-крипто-универсе через validation_harness
(CPCV+DSR+PBO) и ответить на ДВА вопроса:
1. Standalone: есть ли реальный эдж (DSR), переживает ли OOS, какой turnover/costs.
2. **РЕШАЮЩИЙ — декорреляция с XSMOM.** Третий поток ценен ТОЛЬКО если некоррелирован
   с уже живой momentum-книгой. Корреляция trend⟂XSMOM и risk-parity бленд важнее, чем
   standalone DSR. Trend по конструкции имеет time-varying beta (long в бычьем / short в
   медвежьем) → ожидаем crisis-alpha (растёт там, где cross-sec momentum и carry проседают).

Трезвая планка: broad study показал — робастных 25% CAGR в крипте нет. Ищем тонкий
некоррелированный эдж в корзину **carry (FRAB) + momentum (XSMOM) + trend**, не мотор.

## Почему trend структурно другой (не очередной cross-sectional фактор)

- **Directional**, не dollar-neutral: позиция per-asset = +1/−1/flat по СОБСТВЕННОМУ тренду,
  не по cross-sectional рангу. Это не PBO-ловушка «ещё один крипто-фактор» — другой механизм.
- **Положительный skew + crisis-alpha** vs отрицательный/нейтральный skew у momentum-спреда.
- Зарабатывает на затяжных движениях, проигрывает в чопе (whipsaw) → cost-sensitive,
  turnover надо мерить честно.

## Reuse vs new

**Reuse (через PYTHONPATH, как в cross_sectional/crypto):**
- Данные: `survivorship.build_pt_panel(coins)` → PT-панель (survivorship-debiased), даёт
  `price`, `fwd_ret`, `funding`. ТА ЖЕ панель, что у XSMOM-книги → корреляция апельсин-в-апельсин.
- Косты: `COSTS_BPS = survivorship.COSTS_BPS` (8.5 bps/leg).
- Стенд: `validation_harness/` (CPCV n_groups=6 k=2 purge≥max_lookback embargo=7, DSR, PBO).
- Честные дневные метрики: `metrics_daily.daily_metrics` (sqrt(365)).
- Паттерн «один синтетический коин XSEC» из `event_driven_validation.py` /
  `rebal_validation.py` — книга = одна дневная pnl-серия, скармливается в harness.

**New:** движок тренда `research/trend_following/trend.py` — DIRECTIONAL, vol-targeted,
отдельный от `xsec.py` (тот cross-sectional). Не трогать xsec.py / прод src/frab.

## Сквозные дизайн-решения (зафиксированы, чтобы агенты не расходились)

- **TSMOM сигнал:** `pos_i[t] = sign(trailing_return_i over L)`; vol-scaled вариант
  `pos_i[t] = ret_L_i / vol_i` с клипом. Lookbacks для тренда ДЛИННЕЕ cross-sec:
  menu **L ∈ {30, 60, 90, 120}д**, плюс committed = ансамбль (равновес z-scored) — как
  FixedEnsemble у cross-sec, чтобы не оверфитить выбор lookback.
- **Donchian breakout:** long если `close > rolling_max(high, N)` прошлого окна; short если
  `close < rolling_min(low, N)`; держим до противоположного пробоя (STATEFUL, не ежедневный
  пересчёт). Channels **N ∈ {20, 55, 100}д** (классика Turtle 20/55). HL даёт OHLC дневные.
- **Vol-targeting:** масштабировать позицию каждого ассета к постоянной per-asset vol
  (realized vol окно 30д, причинно), затем усреднить по универсу → книга. Cap на leverage
  (напр. суммарный gross ≤ некий предел) — записать явно.
- **Funding:** directional perp-позиции платят/получают funding → ВКЛЮЧИТЬ accrual честно,
  как cross-sec книга (`accrual` в портфельной pnl; знак следует позиции).
- **Seam-safety / no look-ahead:** сигнал в t использует цены/доходности ≤ t, зарабатывает
  `fwd_ret[t]` (t→t+1). purge ≥ max lookback (для menu = 120д → purge=120). Двойная проверка:
  НЕ использовать fwd_ret для построения сигнала/vol.
- **Annualization caveat:** harness compute_metrics аннуализирует по HOURS_PER_YEAR=8760
  (часовая модель), наша pnl ДНЕВНАЯ → pooled-OOS annual_pct/sharpe раздуты (~×35 / ×5.9).
  Для АБСОЛЮТНЫХ уровней — только `metrics_daily` (sqrt(365)). DSR/PBO period-agnostic.

---

## Таски (каждый = отдельный Sonnet-агент; Opus ревьюит + коммитит/пушит per task)

Последовательность: B,C,D зависят от A; D зависит ещё и от C. Research only, прод не трогаем.

### Task A — Движок тренда + self-test
Создать `research/trend_following/trend.py`:
- `tsmom_signal(panel, lookback, vol_window=30) -> pd.DataFrame` позиций (+/−/0, vol-scaled).
- `tsmom_ensemble(panel, lookbacks, ...)` — равновес z-scored ансамбль по lookbacks.
- `donchian_signal(panel, channel) -> pd.DataFrame` stateful breakout позиций.
- `portfolio_returns_directional(positions, fwd_ret, costs_bps, accrual, vol_target, leverage_cap)`
  → дневная pnl-серия книги (turnover-косты на смене позиции, funding accrual, vol-target,
  cap). Зеркалит экономику `xsec.portfolio_returns`, но DIRECTIONAL (не нормируем Σ=±1).
- `__main__` hand-checkable toy (2-3 ассета, несколько дней), асерты на знак/turnover/cost/
  vol-target — как в `xsec.py`.
**Deliverable:** модуль + проходящий self-test. Коммит.

### Task B — Характеризация сигналов (sanity, без стенда)
`research/trend_following/characterize.py`: на PT-панели построить TSMOM (по lookback'ам +
ансамбль) и Donchian (по каналам); вывести честные daily-метрики (Sharpe/ann/maxDD/Calmar/
hit/vol через metrics_daily), turnover/год, и **crisis-alpha чек**: net-exposure книги во
времени + поведение в крупных просадках рынка (BTC). Цель — убедиться, что книга идёт
net-long в бычьем / net-short в медвежьем и не разваливается на чопе. JSON.
**Deliverable:** characterize.py + characterize.json + краткий итог. Коммит.

### Task C — Прогон через validation_harness (CPCV+DSR+PBO)
`research/trend_following/trend_validation.py` по образцу `event_driven_validation.py`:
- Menu = {TSMOM L30, L60, L90, L120, TSMOM-ensemble, Donchian N20, N55, N100}; committed =
  TSMOM-ensemble (или лучший по характеризации — зафиксировать ОДИН committed заранее).
- DSR(committed) + DSR(N=1 на каждый) + PBO across menu. CPCV n_groups=6 k=2 purge=120
  embargo=7. Honest daily metrics. Turnover/год на каждый.
- Вердикт в тоне предыдущих: проходит ли committed DSR>0.95, переживает ли OOS, PBO.
- JSON `trend_validation.json`.
**Deliverable:** скрипт + JSON + SUMMARY TABLE + VERDICT. Коммит.

### Task D — РЕШАЮЩИЙ: декорреляция с XSMOM + risk-parity бленд
`research/trend_following/blend_vs_xsmom.py`:
- Взять committed trend-книгу (из C) и cross-sec XSMOM-книгу (`survivorship.run_book(panel)`),
  выровнять на общем окне.
- **Корреляция trend⟂XSMOM** (главное число). Плюс beta/корреляция к рынку (BTC) — показать
  time-varying beta / crisis-alpha.
- **Risk-parity бленд** (обратно-vol веса двух книг) → Sharpe/maxDD бленда против каждой по
  отдельности. Crisis-alpha: как trend ведёт себя в худших просадочных окнах XSMOM-книги.
- Вердикт: стоит ли trend строить live КАК ДИВЕРСИФИКАТОР, независимо от standalone DSR.
- JSON `blend_vs_xsmom.json`.
**Deliverable:** скрипт + JSON + вердикт по диверсификации. Коммит. Это вход для будущего
checkpoint'а risk-parity (см. memory project_riskparity_checkpoint) — но на SIM, не live.

### Task E — Сводка + trend+carry рамка (по итогам A–D)
Короткий write-up: standalone (DSR/OOS/turnover), декорреляция с momentum, путь к
risk-parity **carry+momentum+trend**. Честные caveats (3 года, нет настоящей медвежки,
PT-панель, harness unchanged, directional beta). Кандидат на memory-запись.

---

## Verification (общее)
- `uv run pytest` не нужен (research), но self-test trend.py обязателен (Task A assert).
- No look-ahead: явно проверить, что сигнал/vol не используют fwd_ret.
- Honest daily levels только через metrics_daily; pooled-OOS harness-числа помечать как раздутые.
- Каждый таск — отдельный коммит+пуш (CLAUDE.md), без Co-Authored-By.
- venv python: `/Users/d/prj/funding-rate-arbitrage/.venv/bin/python` (в системном нет numpy).
  PYTHONPATH: `research:research/validation_harness:research/cross_sectional:research/cross_sectional/crypto:research/trend_following`.

## Что НЕ делать
- Не трогать `src/frab` (прод) и `xsec.py`. Не майнить новые cross-sectional крипто-факторы.
- Не строить live до прохождения стенда И подтверждения декорреляции (Task D).
