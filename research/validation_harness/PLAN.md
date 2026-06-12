# Validation Harness — план

Переиспользуемый «прогонный стенд»: подаёшь стратегию + данные, получаешь
**честные, дефлейтнутые** метрики и диагностику оверфита. Цель — один раз
написать дисциплину валидации (вместо отдельного скрипта под каждую гипотезу),
чтобы любая будущая идея проходила через единый фильтр.

Источники методологии:
- CPCV / purge+embargo / PBO — Bailey & López de Prado (CSCV, "Probability of
  Backtest Overfitting"); Wikipedia "Purged cross-validation".
- Deflated Sharpe Ratio — Bailey & López de Prado (2014).
- Cross-sectional / learning-to-rank — Poh et al. (на будущее, не в этом харнессе).

## Принципы (чему стенд защищает)
1. **Purge + embargo** — никакой утечки между train и test (наш `signal_lag=1` —
   наивная версия; здесь делаем правильно).
2. **CPCV** — не один walk-forward путь, а *распределение* OOS-метрик по многим
   комбинациям окон → устойчивость к одному режиму.
3. **DSR** — поправка Sharpe на число перебранных конфигов (мы прогнали десятки
   сигналов → наш «честный» Sharpe ещё должен просесть).
4. **PBO** — вероятность, что in-sample лучший проигрывает медиане OOS.
5. **Ансамбль ТФ, не выбор «лучшего» ТФ** — выбор лучшего = selection bias.

## Интерфейс стратегии (контракт)
Стратегия — это callable, чтобы стенд не знал её внутренностей:

```python
# fit на train, оценка на test. Возвращает почасовой pnl-массив на test-срезе.
def strategy(df_train: pd.DataFrame, df_test: pd.DataFrame, *, costs) -> np.ndarray: ...
```
- Для «выбора из меню» (как в B) — стратегия сама выбирает сигнал на `df_train`,
  применяет на `df_test`. Это и есть честный OOS.
- Для PBO нужен **набор** конфигов → отдельный контракт: `menu -> {name: pnl_matrix}`.

## Файловая структура (research/validation_harness/)
- `splitter.py` — Purged K-Fold + CPCV генератор путей (purge gap + embargo).
- `metrics.py` — Sharpe/CAGR/maxDD/Calmar + **DSR** + хелперы (skew/kurt).
- `pbo.py` — CSCV → PBO + IS→OOS деградация (logit-распределение).
- `harness.py` — оркестратор: берёт стратегию/меню + данные, гоняет CPCV,
  собирает распределение OOS, зовёт DSR/PBO, печатает отчёт + пишет JSON/CSV.
- `strategies/` — адаптеры существующих стратегий под контракт (первый — B).
- `report.py` — форматирование отчёта (медиана/IQR, % путей Calmar>0, DSR, PBO).
- Переиспользуем `research/engine.py` (load_data, compute_metrics, костанты
  PERP_TAKER/SPOT_TAKER, STAKING_YIELD) и реальные косты из B (maker/taker+slip).

## Фазы (чек-лист)
- [ ] **Ф0. Скелет.** Папка, контракт стратегии, заглушка `harness.py`,
      подключение `engine.py`. Конфиг костов (maker 2bps / taker 5bps / slip).
- [x] **Ф1. Splitter.** `splitter.py` — Purged K-Fold + CPCV (все C(N,k)
      комбинаций), purge симметричный + embargo односторонний. Инвариант
      train∩test=∅ ассертится на каждом пути; self-test проходит (игрушка +
      деградация бюджета train: purge=720h → ~50% ряда).
- [x] **Ф2. Single-strategy runner.** `runner.py` + `contract.py` (mask-based,
      seam-safe) + `costs.py` + `strategies/baselines.py`. CPCV-прогон → OOS
      распределение (медиана/IQR/доля Calmar>0). Smoke BTC buy&hold: median
      Calmar 1.55, IQR [-0.98, 5.46]; AlwaysFlat≡0 ассерт проходит.
      Per-coin разбивка — через цикл run_cpcv по монетам в оркестраторе (Ф5).
- [x] **Ф3. DSR.** `metrics.py` — PSR (поправка skew/kurt) + expected-max-Sharpe
      под нуллём + DSR. Все Sharpe поперодные (одна частота). Self-test на
      известном ответе: noise best-of-N → DSR≈0.50 (PSR-vs-0 при этом 0.99);
      реальный edge N=1 → DSR=1.0; тот же edge в 200 пустышках → 0.81.
- [x] **Ф4. PBO (CSCV).** `pbo.py` — матрица (T×N конфигов), S смежных кусков,
      все C(S,S/2) IS/OOS-сплита, ранг IS-лучшего в OOS → logit → PBO. Self-test
      проверяет НАПРАВЛЕНИЕ (на iid-шуме PBO<0.5 из-за персистентной «глоб.
      удачи»): edge 0.008 < noise 0.195 < overfit 1.000.
- [ ] **Ф5. Report.** Единый отчёт (print + JSON/CSV): OOS-распределение, DSR,
      PBO, IS→OOS деградация, per-coin.
- [x] **Ф6. Валидация САМОГО стенда — ПРОЙДЕНА.** `harness.py`+`report.py`+
      `strategies/baselines_pkg.py`+`validate_harness.py`. Эталоны (известный ответ):
      - noise-меню → DSR=0.000, PBO=0.741 (выбор не переносится: IS-лучший noise5,
        OOS-ранг 0.33). На iid PBO выходит высокий, не 0.5 (см. Ф4);
      - look-ahead cheat → DSR=1.000, PBO=0.000 (выигрывает все 12870 сплитов);
      - buy&hold → total симулятора = прямой расчёт, rel.err<1e-6.
      Ассерты cheat≫noise проходят → стенд воспроизводит известные ответы.
- [x] **Ф7. Strategy B прогнана.** `strategies/b_pkg.py`+`run_b.py`. Меню — 10
      моментум-сигналов ЕДИНООБРАЗНО по монетам (без per-coin cherry-pick =
      артефакта). Вердикт: **DSR=0.931 ⚠️** (in-sample-aggregate Sharpe реален,
      не флук 10 trials) + **PBO=0.488 ⚠️** (выбор лучшего сигнала — coin-flip OOS,
      ранг 0.55, выбор размазан). OOS медиана Calmar 0.98, IQR [0.11,3.14],
      TIA/INJ слабые. Численно = «эдж реален, но не стационарен», B остаётся
      закрытой. (maker не воспроизводим — simulate_constdollar хардкодит taker.)
- [x] **Ф8. Doc.** `README.md` — таблица DSR/PBO/OOS, запуск, доверие к стенду
      (Ф6), шаблон подключения новой гипотезы ≤30 строк, вердикт B.

## Definition of Done
- Новую гипотезу можно подключить ≤30 строк адаптера и получить отчёт одной
  командой.
- Эталоны Ф6 проходят (стенд не врёт на известных случаях).
- B прогнана, PBO/DSR записаны в `project-strategy-b-final` memory.

## Явно вне скоупа (чтобы не расползлось)
- Не трогаем прод `src/frab/`.
- Не строим cross-sectional/ранкинг-стратегию здесь — это *потребитель* стенда,
  следующий шаг. Сначала инструмент.
- Не делаем live-исполнение/оптимизатор параметров. Стенд только *судит*.

## После compact — стартовая точка
Начать с **Ф1 (splitter.py)** — это фундамент; остальное вешается на него.
