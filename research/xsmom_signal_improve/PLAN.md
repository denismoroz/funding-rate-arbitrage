# XSMOM Signal-Improvement Validation Plan

**Запрос юзера:** 5 вариантов улучшения XSMOM, прогнать через стенд.

**Дедупликация (НЕ перепроверять — уже NO-GO в `cross_sectional/crypto`):**
inverse-vol веса (invvol_validation), covariance веса (run_covweight), частота
ребаланса 7/14/21 (rebal_validation, держим R=7), value-тилты drawdown/MA-dist/
LT-reversal (run_value), beta-neutral / residual momentum (run_mr, Sharpe −0.46/
−2.10), mean-reversion (run_mr), exit-оверлеи (xsmom_risk_overlay, все NO-GO).
Косты намерены (~4.4bps, [[project_execution_costs]]) — не лезем.

Research-only. Прод `src/frab/` не трогаем. Incumbent = ванильный XSMOM
(momentum-ensemble z-score lookbacks 14/21/30/45/60, top/bottom терциль, EW,
weekly, dollar-neutral, 1×). Судья = [[project_validation_harness]] (OOS CPCV +
PBO; DSR информационно). Сравнение = head-to-head против incumbent в ОДНОМ меню,
median_oos_rank + PBO (см. метод сравнения: PBO относителен к null/incumbent).

## 5 пре-зарегистрированных рук (сетки фиксированы — не расширять)

1. **Risk-adjusted momentum (R):** скор = mean(ret)/std(ret) по lookback вместо
   сырого ret, потом тот же кросс-секц z + ensemble. Сетка: применить к тому же
   набору lookbacks; вариант t-stat (mean/std·√n) как 2-я ячейка. Прайор: средний.
2. **Skip-recent gap (G):** момент по окну [t−lb, t−gap], `gap ∈ {3,5,7}` дней —
   убрать загрязнение свежим краткосрочным разворотом. Прайор: средний.
3. **Rank-based сигнал (K):** кросс-секц ПЕРЦЕНТИЛЬ-ранг вместо z-score (робастно к
   выбросам — монета в 10× не искажает cross-section). Одна ячейка. Прайор: слабо-средний.
4. **TS×XS-гейт (T):** XS-лонг держим только если собств. тренд монеты вверх
   (ret_lb>0), XS-шорт — только если вниз; иначе нога в кэш. `trend_lb ∈ {30,60}`.
   Прайор: слабый (trend standalone мёртв [[project_trend_following]]).
5. **Breadth-свип (B):** размер ноги top/bottom `frac ∈ {1/5, 1/3, 1/2}`
   (концентрация vs ширина отбора). Прайор: слабый (см. invvol breadth-аргумент).

## Реализация
Каждая рука = модификация scores→weights поверх `cross_sectional/crypto/signals.py`
+ `xsec.py` (rank_to_weights/portfolio_returns). Универс = живой 32-коин ∩ data.
Кост 4.4bps/нога. Все руки + baseline в ОДНО меню стенда (PBO на полном меню =
честный мультитест-штраф). NO look-ahead: сигнал на полном df один раз, маски лишь
отбирают строки; purge ≥ макс lookback (60+gap). selftest: each-arm-degenerate ≈
baseline (gap=0; frac=1/3; trend_lb→∞ off; rank≈z на нормальном распределении).

## Критерии (пре-зафиксированы)
GO только если vs baseline: (1) улучшает OOS-медиану Calmar/снижает maxDD, (2) НЕ
ухудшает OOS-медиану Sharpe, (3) PBO низкий (выбор переносится). DSR информационно.
Если выигрыш только у везучей ячейки при высоком PBO → оверфит → NO-GO.

## Честный априор
Прайоры в основном слабые: соседние идеи (inverse-vol, value, beta-neutral, rebal)
уже легли → внутренний тюнинг XSMOM выжат. R и G — лучший шанс (лит-поддержка).
Не зарубаем рассуждением — прогоняем; PBO решает. Самый большой рычаг (риск-парити
бленд ортогональных рукавов) — ВНЕ этого плана, к чекпоинту ~07-16.

## Артефакт
`research/xsmom_signal_improve/{PLAN.md, signals_plus.py, improve_pkg.py,
run_improve.py, run_improve.json, selftest.py, README.md}` + таблица вердикта.
