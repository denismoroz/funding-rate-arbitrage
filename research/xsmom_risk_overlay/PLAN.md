# XSMOM Risk-Overlay Validation Plan

**Вопрос:** можно ли выходить из позиций XSMOM раньше, чем они «жудко подешевели»,
снизив просадку — БЕЗ убийства эджа? Судит [[validation_harness]] (CPCV+PBO), а не
глаз по backtest-DD. Инкумбент, которого надо побить = ванильный недельный XSMOM.

Это research-only. Прод `src/frab/` НЕ трогаем.

## Переиспользование (без изобретения заново)

- **Сигналы:** `research/cross_sectional/crypto/signals.py::momentum_ensemble`
  — БИТ-В-БИТ совпадает с live `src/frab/strategy/xsmom/evaluators/signal.py::compute_scores`
  (z-score momentum по lookbacks (14,21,30,45,60), среднее). → backtest = live.
- **Движок:** `research/cross_sectional/xsec.py` (`rank_to_weights` dollar-neutral
  терцили + `portfolio_returns` turnover-кост).
- **Данные:** `research/data/<COIN>_1h.csv` → дневной ресэмпл. Вселенная = ЖИВОЙ
  список 32 коинов (`strategies.params_json` у xsmom) ∩ доступные _1h.csv → ЗАМОРОЗИТЬ.
- **Косты:** 4.4 bps/нога (валидированный реальный HL-кост, [[project_execution_costs]]),
  обе ноги. НЕ research-овые 8.5.
- **Судья:** `research/validation_harness/` — OOS CPCV-распределение + PBO (DSR
  ИНФОРМАЦИОННО, не gate — см. калибровку carry).

## Руки (СЕТКИ ПРЕ-ЗАРЕГИСТРИРОВАНЫ — не расширять по ходу)

- **Baseline (инкумбент):** недельный ребаланс, equal-weight терцили, 1×, без оверлея.
- **Рука A — vol-target (на уровне книги):** масштаб gross = `target_vol /
  trailing_realized_vol` (КАУЗАЛЬНО, EWMA-окно W). Сетка: `target_vol ∈ {0.10,0.15,0.20}`
  годовых; `vol_window ∈ {20,40}` дней. (Один смысловой рычаг + окно.) Литературно
  устойчивый класс (Barroso–Santa-Clara, Daniel–Moskowitz «momentum crashes»).
- **Рука B — парный стоп + перевход (идея юзера):** трекаем cumulative PnL каждой
  позиции ВНУТРИ окна удержания; когда нога ушла в минус ≥ S, режем её И парную
  противоположную ногу (правило пары R), чтобы сохранить dollar-neutral; правило
  перевхода E. Сетка: `S ∈ {−8%,−12%,−20%}`; `R ∈ {worst-opposite, symmetric-rank}`;
  `E ∈ {next-rebalance, none}`.

## Расширение движка (нужно только для руки B)

`xsec.py` держит веса постоянными между ребалансами (carry-forward). Рука B требует
ВНУТРИ-оконной симуляции пути: дневной running-PnL по каждой позиции, зануление
сработавших ног (+ парной), опц. перевход. Реализовать как path-aware вариант;
baseline и рука A остаются на текущем движке (рука A лишь пере-масштабирует gross).

## Seam-safety / NO look-ahead (ОБЯЗАТЕЛЬНО)

- `scores[t]` используют инфу ≤ t; вес `w[t]` зарабатывает `fwd_ret[t]` (shift снаружи) —
  контракт `xsec.py`.
- Оценка волы в t использует доходности ≤ t−1. Триггер стопа в день d — цены ≤ d.
- `purge ≥ макс. lookback (60d)` в CPCV.
- Сигналы считать на ПОЛНОМ df один раз, маски лишь отбирают строки (инвариант стенда).

## Критерии вердикта (ПРЕ-ЗАФИКСИРОВАНЫ)

Оверлей = GO только если против baseline он: (1) улучшает OOS-медиану Calmar /
снижает OOS maxDD, (2) НЕ ухудшает OOS-медиану Sharpe, (3) PBO остаётся низким
(выбор переносится; высокий PBO = «улучшение» это режимная удача/оверфит). DSR
информационно. Если результат достижим ТОЛЬКО расширением сетки → это оверфит,
reject (урок token_unlock squeeze-фильтра).

## Честные априоры

- **Рука A:** реальный шанс улучшить Calmar/maxDD при нейтральном Sharpe.
- **Рука B:** парная поправка юзера сохраняет нейтральность (это плюс снимает мою
  претензию «ломает хедж»); НО whipsaw на крипто-шуме + большая поверхность
  параметров обычно НЕ переносится OOS. Честный тест, решает PBO. Прямой прецедент:
  squeeze-фильтр token_unlock ОТВЕРГНУТ данными.

## Артефакт

`research/xsmom_risk_overlay/{PLAN.md, overlay.py, overlay_pkg.py, run_overlay.py,
run_overlay.json, README.md}` + таблица вердикта стенда baseline vs каждая рука.
