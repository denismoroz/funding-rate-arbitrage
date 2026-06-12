# Cross-Sectional Multi-Factor — план (crypto → FX)

Первый реальный потребитель `research/validation_harness/`. Гоним
**cross-sectional long-short** через дисциплину CPCV + DSR + PBO и получаем
честный вердикт — реальный это премиум или decayed/crowded мираж — **до** любой
production/брокер-инфры.

Направление (решено с пользователем): **сначала crypto** (данные и инфра уже
есть → дёшево валидируем подход И стенд на нашем домене), **затем FX** (другой
класс активов, экономический риск-премиум, диверсификация). Endgame —
risk-parity книга crypto+FX (позже, вне текущего скоупа).

## Общая конструкция (одна на оба класса)

Корзина N инструментов. Каждый период: скор по инструменту → ранг → лонг верхняя
треть / шорт нижняя → **dollar-neutral** long-short (Σлонг = Σшорт ноционал).
Меню сигналов единообразно по всем инструментам (без per-asset cherry-pick =
артефакта). Это переиспользуемый движок `xsec.py`; различаются лишь данные и
сигналы per asset class.

**Важно (поправка по ходу):** размер крипто-универса для ранжирования НЕ ограничен
7 монетами frab — те 7 это фильтр funding-arb (нужна спот-нога). Для направленной
long-short книги нужен только ликвидный перп → на HL это десятки инструментов.

## Делегирование

Opus пишет план + ревьюит + коммитит. **Каждая фаза — задача Sonnet** с чётким
Deliverable и Acceptance. Между фазами гейт: Opus проверяет Acceptance, при провале
возвращает на доработку, при успехе коммитит+пушит (CLAUDE.md: коммит+пуш после
каждого таска; без Co-Authored-By).

Guardrails для всех Sonnet-задач:
- Работать ТОЛЬКО в `research/cross_sectional/`. **НЕ трогать `src/frab`**, не
  править `research/validation_harness/` (переиспользовать импортом).
- Питон: репо-рутовый `.venv/bin/python` (numpy 2.4.4/pandas 3.0.3/scipy есть;
  `research/.venv` НЕ существует). Запуск из папки фазы с PYTHONPATH до `research/`
  и `research/validation_harness/`.
- FX-данные — бесплатные, без API-ключей; в коде URL + дата загрузки. Данные в
  `data/` соответствующего под-пакета.
- НЕ доверять числам из `research/quant/` (в т.ч. `quant/crypto_xsec_momentum`) —
  это ненадёжная agent-солянка; можно глянуть как идею, но не цитировать.
- Никакого live, брокера, оптимизатора. Стенд только судит.

## Структура папки
```
cross_sectional/
  xsec.py            # ОБЩИЙ движок: rank → dollar-neutral weights → pnl нетто costs
  crypto/            # HL perps
    cryptodata.py    # универс + loader (reuse research/engine.load_data)
    signals.py       # momentum(lb), carry(funding)
    crypto_pkg.py    # адаптер под harness.Package
    run_crypto.py
  fx/                # G10
    fxdata.py        # Stooq/FRED/BIS loaders
    signals.py       # carry(rate diff), momentum(12-1), value(PPP/REER)
    fx_pkg.py
    run_fx.py
    aqr_crosscheck.py
  README.md
```

## Фазы — Crypto (первый блок)

- [x] **C1. Общий xsec-движок (Sonnet, data-independent).** `xsec.py`:
      `rank_to_weights(scores: DataFrame[date×inst], tercile_frac=1/3) -> weights`
      (dollar-neutral, Σлонг=Σшорт=1); `portfolio_returns(weights, fwd_ret, costs)`
      нетто turnover×spread. **Acceptance:** assert нейтральность; игрушка из 4
      инструментов считается руками и сходится; rebal-частота параметризуема (дни).
      *Запускается параллельно с C0.*

- [x] **C0. Крипто-универс + данные (Sonnet).** Инвентаризовать `research/data/`
      (есть ~18 коинов 1h). Найти существующий HL-загрузчик в репо и **расширить
      универс до ≥30 ликвидных HL-перпов** (публичный HL API, без креденшелов),
      ≥1.5 года часовой истории, фильтр по объёму/возрасту листинга. `cryptodata.py:
      universe() -> list[str]`, `load_panel() -> DataFrame[date×coin]` (price, ret,
      funding). **Acceptance (Opus):** ≥30 коинов, нет дыр/NaN, даты ровные, funding
      присутствует; список универса воспроизводим (фильтр в коде, не захардкожен).

- [x] **C2. Крипто-сигналы (Sonnet).** `crypto/signals.py`: `momentum(panel, lb)`
      (трейлинг-ret, варианты lb=30/60/90/180 дней = trials для DSR);
      `carry(panel)` (накопленный/сглаженный funding). Cross-sectional z-score
      каждый. **Acceptance:** формулы ревьюит Opus; знаки осмысленны; seam-safe
      (считать на полном panel).

- [x] **C3. Адаптер crypto под стенд (Sonnet, ≤30 строк).** `crypto_pkg.py` —
      протокол `harness.Package`: menu={mom30/60/90/180, carry, blend},
      selected="blend"; `run_crypto.py`. Переиспользует `xsec`+`signals`+стенд.
      **Acceptance:** end-to-end прогон, печать отчёта, JSON; purge ≥ макс. lookback
      (в днях); контракт seam-safe.

- [x] **C4. Прогон + вердикт crypto (Opus).** Вердикт: эдж = чистый momentum
      (mom30 Sharpe~1.0/+43%, дневная годовизация), но carry МЁРТВ (−0.04),
      blend ВРЕДИТ (разбавляет). PBO=0.83 ❌ (выбор lookback не переносится),
      DSR=0.64 ⚠️ (на blend). maxDD 40-60% (factor-crash). Многофакторный тезис
      в крипте провалился; чистый momentum реален-но-fragile, не robust-альфа.
      NB: стенд годовит √8760 (часовая модель) — OOS levels раздуты ×~5.9; честные
      дневные числа в analyze_c4.py. Для FX нужна корректная дневная годовизация.

## Крипто v2 (C5-C6) — робастность + ансамбль — ЗАВЕРШЕНО

- [x] **C5. Карта робастности (Sonnet).** `sweep.py` + `metrics_daily.py` (честный
      daily √365) + сигналы reversal/vol-adj. Вывод: momentum = ПЛАТО (не магическое
      число), но reversal/vol-adj/carry провалились; эдж выцветает.
- [x] **C6. Ансамбль vs adaptive-select (Sonnet+Opus).** `momentum_ensemble` (среднее
      по плато, без выбора). Решающий OOS-тест: ансамбль БЬЁТ adaptive-select (100% vs
      80% сегментов+). Честный daily: Sharpe 1.34/Calmar 2.11/maxDD 26%, DSR 0.974 ✅.
      Fade: 2-я пол. Sharpe 1.0 (слабеет, не умирает). Оговорка: НЕ учтён ongoing
      perp funding на удержании — вероятный встречный ветер, проверить до веры в уровень.

## Фазы — FX (второй блок) — ЗАВЕРШЕНО

- [x] **F0. FX-данные (Sonnet).** G10 (9 валют vs USD), 2006→2026, 5240 bdays.
      Спот Stooq→Yahoo (XXXUSD), ставки FRED→OECD 3M (carry), REER BIS broad (value).
      Egress в песочнице капризный → авто-fallback'и. `fx/fxdata.py: load_panel()` →
      price/fwd_ret/short_rate/usd_rate/reer. Carry-sanity ок (JPY/CHF−, AUD/NZD/NOK+).

- [x] **F1. FX-сигналы (Sonnet).** `carry`=ставка foreign−USD (НЕ негейтим, textbook
      знак), `momentum`=12-1 (скип последнего месяца), `value`=−Δlog REER 5y (AQR Value
      Everywhere). z-score/blend подняты в общий `xsec`. Seam-safe, hand-checked.

- [x] **F2. Адаптер FX + AQR-гейт (Sonnet).** `fx_pkg.py`(зеркало crypto)+`run_fx.py`+
      `aqr_crosscheck.py` (stdlib OOXML-ридер, без openpyxl). Гейт: momentum corr 0.769
      PASS, value 0.554 borderline (верный знак) → конструкция валидна. carry: AQR-файл
      недоступен (404), не сверен. ВЫЯВЛЕНО: pnl ~0 — движок не начислял held-carry.

- [x] **F2.5 (доп). Начисление held-carry (Opus+Sonnet).** `xsec.portfolio_returns`
      получил опц. `accrual` (default None → crypto байт-в-байт). carry-yield =
      (short_rate−usd)/100/252 на ВСЕ факторы. Закрыл модельный пробел (= давний todo
      «ongoing funding»). Эффект: carry −0.09→+0.26, blend +0.07→+0.32 Sharpe.

- [x] **F3. Вердикт FX (Opus).** blend_fx: честный daily Sharpe **0.32**, ann +3.4%,
      maxDD **15.7%**, DSR **0.72**, PBO 0.44, +OOS-сегментов 83%, IS-best=blend.
      **Многофакторный тезис ПОДТВЕРДИЛСЯ** (в отличие от крипты): 2008 carry-crash
      blend −1.8% vs carry −23.2% — momentum/value гасят крах carry. НО: размер скромен
      (×4 слабее крипты), выцветает (Sharpe 0.40→0.23), буксует с 2021 (2022 −7.7%);
      value лишь погранично сверен с AQR, carry не сверен.

## Фаза — Doc
- [ ] **D. README (Sonnet).** Таблица факторов crypto+FX, источники, запуск,
      вердикты. Проставить чекбоксы PLAN.

## Definition of Done
- Crypto и FX cross-sectional прогнаны через стенд; DSR/PBO/OOS по каждой ноге+blend.
- FX-факторы кросс-чекнуты против AQR (F2 гейт).
- Вердикты записаны, memory обновлена (`project-cross-sectional`, линк на
  [[project-validation-harness]]).
- `src/frab/` и `validation_harness/` не тронуты.

## Вне скоупа
- EM-валюты; ML/learning-to-rank; live/брокер/оптимизатор.
- Объединённая risk-parity crypto+FX книга — endgame, отдельная фаза после двух вердиктов.
