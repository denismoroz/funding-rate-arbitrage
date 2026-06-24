# Pairs / Cointegration Mean-Reversion — план проверки гипотезы

Статус: ДИЗАЙН (код не написан). Прогон строго через `research/validation_harness`
(CPCV + DSR + PBO), как все прежние гипотезы (B, cross-sectional, trend, spread).

## 0. Гипотеза (фальсифицируемая)

Mean-reversion на коинтегрированных **крипто-парах** даёт OOS-эдж, который
одновременно:
1. переживает **DSR > 0.95** (Sharpe не флук перебора пар),
2. переживает **PBO < 0.2** (выбор лучшей-по-бэктесту пары переносится forward),
3. **некоррелирован** (|corr| < ~0.3) c BTC buy&hold, FRAB-carry и XSMOM-momentum.

**Null (что почти наверняка случится в крипте):** «коинтеграция» пары — это
замаскированная общая BTC-beta; после нейтрализации к BTC спред перестаёт быть
стационарным, либо эдж выцветает OOS, либо PnL коррелирует с уже имеющимися
sleeve'ами. Тогда — задокументировать и закрыть (как SPREAD/trend), и только
ТОГДА разворачивать FX-плечо (Приложение A).

Приоритет — крипта (данные доступнее, см. `cross_sectional/crypto/data`, ~60
коинов дневные+1h, цена+funding, воспроизводимый `cryptodata.load_panel`). FX —
fallback, не выгорит крипта.

## 1. Почему стенд подходит для пар «из коробки»

Стенд asset-agnostic: итерирует по «юнитам» (`coins`), на каждый `load()→df` +
pnl-серия; CPCV/DSR/PBO работают поверх. Для пар **юнит = пара** (не монета):
`load(pair)` отдаёт df с двумя ногами, спред считается внутри стратегии.

Главное: **меню для PBO/DSR = множество пар-кандидатов.** Это идеально ложится на
пар-трейд —
- **PBO** отвечает на центральный вопрос пар-трейда: «переносится ли вперёд выбор
  лучшей-по-бэктесту пары?»;
- **DSR** дефлейтит Sharpe ровно на число перебранных пар = настоящий
  multiple-testing surface пар-трейда.

Поэтому широкий пул пар (запрос юзера) допустим ИМЕННО потому, что стенд штрафует
перебор. Без DSR/PBO широкий пул = чистый p-hacking.

## 2. BTC-beta defense (критично, без этого вся затея — самообман)

В крипте почти всё коинтегрировано с BTC просто из-за общей market-beta. Меры:

**2a. Residualize before testing.** Коинтеграцию проверять НЕ на сырых ценах ноги,
а на **BTC-нейтральных остатках**: для каждой ноги regress log-price на log(BTC)
(rolling, train-only) → остаток. Пара тестируется/торгуется на остатках. Если
пара коинтегрирована только из-за общего BTC-фактора — после residualize спред
перестаёт быть стационарным и отсеивается на Ф5.

**2b. Hard orthogonality gate (Ф7).** PnL книги мерить на корреляцию к ТРЁМ
бенчмаркам: BTC buy&hold, FRAB-carry-прокси, XSMOM-momentum-прокси. Провал любого
(|corr| ≥ ~0.3) = пара не новая ось, а переодетая старая → fail.

## 3. Пул кандидатов — по экономической логике, НЕ all-pairs

All-pairs по 60 коинам = ~1770 пар = p-hacking даже с DSR. Ограничиваемся
секторами с реальным общим драйвером сверх market-beta (~30–60 пар):
- **L1-конкуренты:** SOL / AVAX / NEAR / ADA / DOT / ATOM / APT / SUI
- **L2:** ARB / OP
- **DeFi:** AAVE / UNI / CRV / ...
- **LST/restaking:** ETHFI / EIGEN / ETH
- (опц.) **memes:** DOGE / ... — слабый структурный якорь, отдельной группой

Пул ФИКСИРУЕТСЯ заранее (freeze), как `crypto/freeze_universe.py`. Никакого
«докидывания пар по ходу» — это ещё одна точка переобучения.

## 4. Адаптер пары под контракт стенда (Ф2)

Стратегия считает на ПОЛНОМ df (lookback цел), маски только отбирают строки
(seam-safe, см. `contract.py` / `fx_pkg.py`). На пару:
- **β — Kalman time-varying** (state = β [+ intercept]); process/obs noise — НЕ
  фитить по PnL, задать априори или фитить только на `train_idx` через `fit()`;
- спред `S_t = resid_a − β_t · resid_b` (на BTC-нейтральных остатках, см. §2a);
- z-score на rolling-окне; входы ±2σ, выход 0;
- **time-stop = 2–3 × half-life** (OU): не вернулось — равновесие сломано, режем;
- dollar-neutral pnl; косты — **perp TAKER ~4.4 bps/нога** (живой HL-кост из
  `project_execution_costs`, не research-овые 8.5);
- **held-funding accrual** обеих ног (удерживаемая perp-позиция платит/получает
  funding — крипто-аналог FX held-carry из `fx_pkg._carry_rate_panel`).

## 5. Seam-safety / purge (Ф3)

`purge ≥ max lookback` = Kalman warm-up + BTC-residual окно + z-окно + горизонт
half-life. Для mean-reversion критично: иначе train-бар дотянется до test-бара
(утечка в `fit`). embargo как в стенде (24).

## 6. Коинтеграционный гейт (Ф5)

Engle-Granger / Johansen + ADF на **rolling train-окнах** (на BTC-остатках).
Назначение — **отключать развалившиеся** пары, а не искать новые. Репорт:
- p-value стационарности спреда по подпериодам,
- стабильность β (дрейф Kalman-state),
- half-life и число mean-crossings (proxy торгуемости).

## 7. Self-test адаптера (Ф6, как `validate_harness.py`)

Эталоны с известным ответом — расходятся → баг в адаптере, не в гипотезе:
- β-cheat (Kalman подглядывает на бар вперёд) → DSR≈1, PBO≈0;
- random pairs (случайные «пары») → DSR≈0, высокий PBO;
- buy&hold спреда → total симулятора = прямой расчёт (rel.err < 1e-6).

## 8. Вердикт (Ф8)

`run_harness(PairsPackage())` → OOS-распределение Calmar/Sharpe + DSR + PBO →
затем orthogonality gate (§2b). Светофор:
- **GO (рассмотреть live):** DSR>0.95 И PBO<0.2 И все три |corr|<~0.3;
- **NO-GO:** иначе — задокументировать в README, закрыть, перейти к FX (Прил. A).

Никакого live-кода до прохождения всех трёх гейтов.

## Приложение A — FX fallback

Если крипта = NO-GO (ожидаемо из-за BTC-beta): те же Ф1–Ф8 на FX commodity-блоке
(AUD/CAD/NZD), Scandi (NOK/SEK), euro-сателлиты — данные уже в
`cross_sectional/fx/data` (spot+rate+reer daily). Косты 2bps/нога spot,
held-carry accrual rate-diff (`fx_pkg` переиспользуется напрямую). FX —
исторически более «настоящий» дом коинтеграции (equities > FX > crypto), но в
портфеле ценен ещё и тем, что некоррелирован с крипто-sleeve'ами.

## Файлы (план)

```
research/pairs_cointegration/
  PLAN.md              # этот файл
  pairs_data.py        # пул пар (freeze) + BTC-residual панель (reuse cryptodata)
  kalman.py            # time-varying β (Kalman), seam-safe
  pairs_strategy.py    # спред/z/входы/time-stop/pnl + funding accrual
  pairs_pkg.py         # адаптер под harness.Package (юнит = пара)
  coint_gate.py        # Ф5: Engle-Granger/Johansen/ADF rolling, half-life
  selftest.py          # Ф6: cheat/random/buy&hold эталоны
  run_pairs.py         # Ф8: вердикт + orthogonality gate → run_pairs.json
  README.md            # вердикт (заполняется после прогона)
```
