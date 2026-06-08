# Monte-Carlo Validation Harness for `two_phase_margin`

**Цель.** Ответить на вопрос, который backtest на одной истории (n=1) ответить не
может: **эдж `two_phase` — реальный или артефакт подгонки?** Backtest выдал
Calmar 114 при max_dd 0.11% — это одна реализация одного пути. MC прогоняет
стратегию на тысячах правдоподобных *альтернативных* историй и возвращает
**распределение** метрик (медиана, хвосты, P(убыточный год)), а не одну цифру.

Связано: `research/SYNTHETIC_VALIDATION.md` (проверил только арифметику на 1
вырожденном пути — цена/фандинг const), `research/TWOPHASE_MARGIN_REPORT.md`
(анкер-числа single-path), memory `feedback_backtest_claims`,
`feedback_apr_denominator`, `project_two_phase_exit_gap`.

---

## Архитектурное решение (читать ДО любого таска)

0. **Движок синхронизирован с продом (2026-06-08).** `decide_two_phase` в
   `two_phase_margin.py` приведён в соответствие с `two_phase_signals.py` commit
   `9e17c8a` — добавлен **Phase-1 negative hard-stop `CLOSE_PHASE1_NEGSTOP`**
   (сигнал < `neg_stop_threshold_apr=-0.15` И `consec_negative ≥
   neg_stop_patience_hours=6`, в обход min_hold). MC обязан валидировать ТЕКУЩИЙ
   набор выходов, включая NEGSTOP. Эффект фикса на реальных данных: max_dd
   U-prod buf=3 упал вдвое (0.102%→0.043%) — см. свежий
   `research/TWOPHASE_MARGIN_aggregate.csv`.

1. **НЕ переписывать движок.** `research/two_phase_margin.py::simulate()` —
   прод-точное ядро (грузит leverage/maint/fees из `src/frab/constants.py`,
   params из БД, зеркалит `two_phase_signals.py`). Оно потребляет
   `dfs: dict[str, pd.DataFrame]`, где каждый df индексирован почасово и имеет
   колонки `close`, `fundingRate` (+ сигналы от `add_signals`). **MC = подменяем
   источник `dfs` на синтетический генератор, движок зовём как есть.** Любая
   реимплементация логики выхода/маржи = расхождение с продом = весь смысл MC
   потерян.

2. **Подводный камень импорта.** Папка `research/two_phase_margin/` имеет тот же
   stem, что файл `research/two_phase_margin.py` → `import two_phase_margin`
   двусмыслен (пакет затенит модуль). **Грузить движок строго по пути файла**
   через `importlib.util.spec_from_file_location("tpm_engine",
   REPO/"research/two_phase_margin.py")`, НЕ по имени. (См. T1.)

3. **Границы (scope discipline, memory).** Весь код живёт ТОЛЬКО в
   `research/two_phase_margin/monte_carlo/`. НЕ трогать `src/frab/`. Params/БД —
   read-only, ровно как уже делает `two_phase_margin.py`. Прод не трогать.

4. **Anti-garbage-in — обязательный гейт.** MC ровно настолько хорош, насколько
   генератор воспроизводит реальность. Если генератор не умеет в отрицательный
   фандинг и переключение hot↔cold — он соврёт красивым Calmar (а живая книга в
   июне-2026 ест минус именно из cold/negative режима). T3 содержит
   round-trip-гейт: статистики синтетики обязаны совпасть с реальными (T2).
   Без зелёного T3 результаты T5/T6 не имеют силы.

5. **Метрики — на занятый капитал, не на budget** (memory
   `feedback_apr_denominator`). max_dd — на equity-кривой занятого капитала.
   **⚠️ ИЗВЕСТНЫЙ GAP (выявлено в T1):** `RunResult.equity` от адаптера — это
   equity ВСЕГО портфеля (budget ~$1000 + позиции, включая праздно лежащий кэш),
   т.е. движковый `eq`. Поэтому `metrics.summarize` сейчас даёт APR на ПОЛНЫЙ
   бюджет (те самые ~2.5%), а не на занятый капитал. max_dd на этой кривой
   корректен (просадка всего счёта). Для честного APR в отчёте (T5/T6/T7) нужно
   отдельно вывести occupied-capital знаменатель — либо трекать развёрнутый
   ноционал по часам, либо нормировать на средний занятый капитал. НЕ хоронить
   этот множитель: ~2.5% на бюджет ≠ ~6-8% на занятый.

---

## Граф задач (DAG)

```
T0 scaffolding
      │
T1 engine adapter ──────────────┐
      │                         │
T2 calibration (stylized facts) │
      ├──────────────┐          │
T3 parametric gen   T4 bootstrap gen
      └──────┬───────┘          │
             └─────► T5 MC runner ◄──── (T1)
                          │
                     T6 aggregation + report
                          │
                     T7 verdict (Opus, не Sonnet)
```

Зависимости строгие: задача не стартует, пока её предки не приняты (acceptance ✅).

---

## Делегирование

Каждый таск ниже самодостаточен — копируется в промпт Sonnet-агента как есть.
Реализует Sonnet, **ревью + коммит — Opus** (memory `feedback_delegation`).
Шаблон промпта в конце файла. После каждого принятого таска — коммит+пуш
(CLAUDE.md), без `Co-Authored-By`.

Прогресс отмечать здесь же галочками.

- [x] **T0** — scaffolding ✅ (metrics + stubs, 29 tests green, commit d3405c7)
- [x] **T1** — engine adapter ✅ (run_on_dfs via _dfs_override; anchor U-prod buf=3
      annual 2.50/max_dd 0.078/negstop 2 воспроизведён; 42 теста; см. GAP в правиле 5)
- [x] **T2** — calibration / stylized facts ✅ (5 coins + cross-corr json; anchors
      SOL cold 2.52% / BTC 8.63% pass; neg-hours SOL 24%/PURR 2%; 58 tests green)
- [x] **T3** — parametric generator (+ round-trip gate) ✅ (log-level AR(1) funding; GBM+jumps price; 1000-path round-trip gate all OK; 50 new tests; 108 total green)
- [x] **T4** — bootstrap generator ✅ (synchronous circular block bootstrap; preserves
      marginals/ACF/cross-corr; cold-only 5-coin window by construction; 59 tests, 167 total)
- [x] **T5** — MC runner ✅ (run() + multiprocessing Pool; seed=base+i per path;
      CSV out w/ raw fields for occupied-APR reconstruction; 31 tests, 198 total)
- [x] **T6** — aggregation + report ✅ (distribution_stats + write_report; percentiles/
      P(loss)/CVaR/exit-mix; parametric vs bootstrap side-by-side; single-path anchor;
      occupied-capital reframe deferred to T7; matplotlib best-effort; 39 tests, 237 total)
- [ ] **T7** — verdict (Opus)

---

## T0 — Scaffolding

**Goal.** Каркас пакета, чтобы остальные таски клали файлы на готовое место.

**Do.**
- Создать `research/two_phase_margin/monte_carlo/` структуру:
  ```
  monte_carlo/
    __init__.py
    metrics.py            # T0: метрики из equity-кривой
    engine_adapter.py     # T1
    calibrate_stats.py    # T2
    generators/
      __init__.py
      parametric.py       # T3
      bootstrap.py        # T4
    run_mc.py             # T5
    report.py             # T6
    calibration/          # выход T2 (json per coin) — .gitkeep
    results/              # выход T5 (parquet/csv) — .gitkeep
    tests/
      __init__.py
      test_metrics.py
  ```
  (пустые/stub-файлы для T1–T6 — только сигнатуры + `raise NotImplementedError`.)
- `metrics.py`: чистые функции на вход `equity: pd.Series` (индекс — время,
  значение — equity занятого капитала):
  - `annualized_return(equity) -> float`
  - `max_drawdown(equity) -> float` (доля, как 0.11% = 0.0011)
  - `calmar(equity) -> float`
  - `sharpe(equity, periods_per_year=8760) -> float`
  - `summarize(equity) -> dict` (все сразу)
- НЕ ставить новых зависимостей сверх numpy/pandas (что уже у движка).

**Acceptance.**
- `tests/test_metrics.py` зелёный: линейный рост → известный APR, max_dd==0;
  V-образная просадка с известной глубиной → max_dd совпадает; Calmar = APR/dd.
- `python -c "import ..."` структуры импортится без ошибок.

**Out:** распределённые расчёты, генераторы — не здесь.

---

## T1 — Engine adapter

**Goal.** Тонкая обёртка, которая зовёт существующий `simulate()` БЕЗ изменений и
возвращает нормализованный результат (equity-кривая занятого капитала + метрики
из T0). Это единственная точка связи MC с прод-движком.

**Do.**
- В `engine_adapter.py` загрузить движок ПО ПУТИ (см. камень №2):
  `spec_from_file_location("tpm_engine", REPO_ROOT/"research/two_phase_margin.py")`.
- Изучить и задокументировать в докстринге **точный** контракт `simulate()`:
  какие аргументы, что возвращает, как из возврата достать почасовую equity
  занятого капитала. (Прочитать тело `simulate`, `_compute_equity`, `Position`.)
- Реализовать:
  `run_on_dfs(dfs: dict[str, pd.DataFrame], params, mbuf, coins) -> RunResult`,
  где `RunResult` = dataclass(equity: pd.Series, metrics: dict, raw: Any).
  Внутри: прогнать `add_signals` (как в проде), вызвать `simulate`, собрать equity,
  прогнать через `metrics.summarize`.

**Acceptance (регрессионный анкер — критично).**
- Прогнать адаптер на РЕАЛЬНЫХ данных (`load_coin_df` для прод-сета монет) и
  воспроизвести числа из `research/TWOPHASE_MARGIN_aggregate.csv` (annual, max_dd,
  calmar) с точностью ≥ 6 знач. цифр. Это доказывает, что обёртка зовёт движок
  идентично, ничего не переопределяя. **Анкер — СВЕЖИЙ файл (после синка NEGSTOP,
  2026-06-08): U-prod buf=3 → annual 2.95%, max_dd 0.0432%, n_phase1_negstop=2.**
  Если adapter не бьёт эти числа — обёртка переопределяет логику, чинить.
- Тест в `tests/` фиксирует этот анкер.

**Out:** генерация синтетики — не здесь; адаптер агностичен к источнику `dfs`.

---

## T2 — Calibration / stylized facts

**Goal.** Извлечь из РЕАЛЬНОЙ истории (`research/data/{coin}.csv` +
`{coin}_1h.csv`) ровно те статистики, которые генератор обязан воспроизвести.
Это фундамент anti-garbage-in.

**Do.** `calibrate_stats.py` per coin считает и пишет
`calibration/{coin}.json`:
- **Цена:** часовой log-return μ, σ; эксцесс/частота скачков (доля |r| > k·σ);
- **Фандинг:** среднее, σ; коэффициент AR(1) φ (скорость возврата к среднему);
- **Доля отрицательных часов** фандинга (ОБЯЗАТЕЛЬНО — это то, что генератор
  чаще всего «теряет»);
- **Режимы hot/cold:** среднее фандинга в каждом + частота переходов
  (можно по порогу/скользящему среднему; cold-окно 2025-01→2026-04 как якорь);
- **Корреляция** price-return ↔ funding-rate;
- кросс-корреляция фандинга между монетами (для совместной генерации в T3/T5).

**Acceptance.**
- Числа бьются с уже известными якорями в пределах допуска: SOL cold ≈ 2.71%,
  hot ≈ 23.09% (`research/drift/regime_comparison.csv`); общая доля
  negative-hours правдоподобна. Несовпадение > допуска = баг калибровки.
- `calibration/*.json` для всего прод-сета монет в репозитории.

**Out:** сама генерация — T3.

---

## T3 — Parametric generator (+ round-trip gate)

> **РЕШЕНИЕ ПОЛЬЗОВАТЕЛЯ (garbage-in развилка):** у HYPE/PURR нет горячей
> истории ЦЕНЫ (price с 2025-11, cold-only). Горячую vol для них генерим
> ЗАИМСТВОВАНИЕМ у майоров: hot_σ(HYPE) = cold_σ(HYPE) × mean(hot_σ/cold_σ по
> BTC/ETH/SOL). НЕ держать их на плоской cold-vol (занижает hot-риск) и НЕ
> выкидывать из книги.


**Goal.** По калибровке (T2) + горизонт + seed эмитить синтетический `dfs` В ТОЙ
ЖЕ ФОРМЕ, что потребляет адаптер (T1).

**Do.** `generators/parametric.py::generate(calib, horizon_h, seed, coins) -> dfs`:
- **Цена:** GBM с калиброванными μ/σ + редкие скачки (jump-diffusion) для
  толстых хвостов/крахов;
- **Фандинг:** AR(1)/OU вокруг режимного среднего, **умеющий уходить в минус**,
  с **переключением режимов hot↔cold** (Марков по частоте переходов из T2);
- учесть price↔funding корреляцию и кросс-корреляцию монет (совместные крахи —
  как 02→05.06.2026);
- выход: `dict[coin → DataFrame[close, fundingRate]]`, индекс почасовой.

**Acceptance — ROUND-TRIP GATE (без него таск не принят).**
- Сгенерить ≥ 1000 путей, прогнать на них T2-экстрактор, и подтвердить, что
  статистики синтетики совпадают с входной калибровкой в пределах допуска:
  negative-hours share, funding mean/σ/φ, режимные средние, return σ и хвосты.
- Детерминизм при фиксированном seed.
- Тест в `tests/` проверяет round-trip хотя бы по negative-hours и funding mean.

**Out:** оркестрация прогонов — T5.

---

## T4 — Bootstrap generator (block resampling)

**Goal.** Непараметрическая альтернатива: пересэмплить реальную историю блоками,
сохранив автокорреляцию и НАСТОЯЩИЕ толстые хвосты, без допущений о распределении.

**Do.** `generators/bootstrap.py::generate(real_dfs, horizon_h, seed, coins) -> dfs`:
- stationary / circular block bootstrap по СОВМЕСТНЫМ (цена+фандинг, все монеты
  синхронно) часовым блокам — чтобы не порвать корреляции;
- тот же выходной контракт, что T3.

**Acceptance.**
- Сохранение маргинального распределения и автокорреляции реального ряда
  (проверить ACF фандинга на лагах 1..24);
- совместность: блоки берутся синхронно по всем монетам;
- детерминизм по seed; тест в `tests/`.

**Out:** —

---

## T5 — MC runner

**Goal.** Оркестратор: N итераций × генератор → adapter → метрики → строки в
`results/`.

**Do.** `run_mc.py` (CLI):
- аргументы: `--n`, `--horizon-days`, `--seed`, `--generator {parametric,bootstrap}`,
  `--coins`, `--mbuf`, `--params {prod,defaults}`;
- params грузить read-only как `two_phase_margin.py` (прод-параметры — основной
  кейс);
- цикл: `generate → engine_adapter.run_on_dfs → metrics`; копить per-path строки
  (seed, annual, max_dd, calmar, sharpe, P&L-разбивка если доступна);
- писать `results/mc_{generator}_{timestamp}.parquet` (+ csv-зеркало).

**Acceptance.**
- Детерминизм: один `--seed` → идентичные строки;
- `--n 50` отрабатывает на прод-сете без ошибок и пишет 50 строк;
- разумная скорость (если медленно — векторизация/мультипроцесс, но БЕЗ
  изменения движка).

**Out:** агрегация/графики — T6.

---

## T6 — Aggregation + report

**Goal.** Из строк T5 — распределение и человекочитаемый вывод.

**Do.** `report.py`:
- считать по каждой метрике: медиана, 5/25/75/95-й перцентили, min/max;
- `P(annual < 0)`, `P(max_dd > X)`, худшие 5% хвоста (CVaR-стиль);
- сгенерить `MONTE_CARLO_REPORT.md` с таблицей распределения + (matplotlib, если
  допустимо) гистограммы APR/max_dd/Calmar и fan-chart equity;
- В отчёте ЯВНО рядом поставить single-path анкер (из СВЕЖЕГО
  `research/TWOPHASE_MARGIN_aggregate.csv` — текущий U-prod, не устаревшие
  числа) и распределение MC — чтобы виден был контраст.

**Acceptance.**
- Отчёт рендерится на выходе ≥ 500 путей обоих генераторов;
- таблица перцентилей + P(neg year) присутствуют.

**Out:** интерпретация/вердикт — T7.

---

## T7 — Verdict (Opus, НЕ Sonnet)

**Goal.** Аналитическое суждение: эдж реальный или артефакт.

**Do (Opus сам).**
- Сопоставить распределение MC с single-path: насколько Calmar 114 — выброс
  правого хвоста против медианы MC? Где 5-й перцентиль APR (минус?)? Каков
  реалистичный «плохой год» (5% max_dd)?
- Сверить parametric vs bootstrap (расходятся → модельная хрупкость);
- Явно отметить остаточные допущения генератора, которые MC всё ещё не покрывает
  (например, structural break типа смены режима фандинга индустрией);
- Вердикт + рекомендация по размеру слива capital под `two_phase` (связать с
  `project_strategy_a_final`, `feedback_apr_denominator`);
- Дописать раздел «Verdict» в `MONTE_CARLO_REPORT.md`, обновить memory если
  вывод меняет тезис.

**Acceptance.** Раздел «Verdict» с числами из распределения, не из single-path.

---

## Сиблинг-трек (вне этого плана, упомянуть и отложить)

**Walk-forward** (train params на одном куске истории, test на невидимом) —
отдельная нога harness'а, отвечает на «не переподогнаны ли *параметры*» (MC
отвечает на «робастен ли *эдж* к альтернативным путям»). Завести отдельным
планом после T7, если MC покажет, что тема жива.

---

## Шаблон промпта для Sonnet-агента

```
Контекст: research-харнесс Monte-Carlo для two_phase. Читай ВЕСЬ файл
research/two_phase_margin/monte_carlo/PLAN.md — особенно «Архитектурное решение»
(4 правила) — затем выполни ТОЛЬКО задачу <Tn>, скопированную ниже.

Жёсткие границы:
- Код только в research/two_phase_margin/monte_carlo/. НЕ трогать src/frab.
- Движок two_phase_margin.py НЕ переписывать; грузить по пути файла (камень №2).
- Params/БД — read-only. Прод не трогать.
- Только numpy/pandas (+ matplotlib для T6), без новых зависимостей.
- Выполнить Acceptance задачи и показать, что критерии зелёные (прогнать тесты).
- Предки задачи уже приняты — опираться на их выход, не переделывать.

<сюда вставить полный блок задачи Tn из PLAN.md>

Не коммить — ревью и коммит сделает Opus.
```
