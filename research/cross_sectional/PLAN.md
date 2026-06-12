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
- Питон: `research/.venv/bin/python` (numpy/scipy есть). Запуск из папки фазы с
  `PYTHONPATH=..:../..:../../validation_harness`.
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

- [ ] **C1. Общий xsec-движок (Sonnet, data-independent).** `xsec.py`:
      `rank_to_weights(scores: DataFrame[date×inst], tercile_frac=1/3) -> weights`
      (dollar-neutral, Σлонг=Σшорт=1); `portfolio_returns(weights, fwd_ret, costs)`
      нетто turnover×spread. **Acceptance:** assert нейтральность; игрушка из 4
      инструментов считается руками и сходится; rebal-частота параметризуема (дни).
      *Запускается параллельно с C0.*

- [ ] **C0. Крипто-универс + данные (Sonnet).** Инвентаризовать `research/data/`
      (есть ~18 коинов 1h). Найти существующий HL-загрузчик в репо и **расширить
      универс до ≥30 ликвидных HL-перпов** (публичный HL API, без креденшелов),
      ≥1.5 года часовой истории, фильтр по объёму/возрасту листинга. `cryptodata.py:
      universe() -> list[str]`, `load_panel() -> DataFrame[date×coin]` (price, ret,
      funding). **Acceptance (Opus):** ≥30 коинов, нет дыр/NaN, даты ровные, funding
      присутствует; список универса воспроизводим (фильтр в коде, не захардкожен).

- [ ] **C2. Крипто-сигналы (Sonnet).** `crypto/signals.py`: `momentum(panel, lb)`
      (трейлинг-ret, варианты lb=30/60/90/180 дней = trials для DSR);
      `carry(panel)` (накопленный/сглаженный funding). Cross-sectional z-score
      каждый. **Acceptance:** формулы ревьюит Opus; знаки осмысленны; seam-safe
      (считать на полном panel).

- [ ] **C3. Адаптер crypto под стенд (Sonnet, ≤30 строк).** `crypto_pkg.py` —
      протокол `harness.Package`: menu={mom30/60/90/180, carry, blend},
      selected="blend"; `run_crypto.py`. Переиспользует `xsec`+`signals`+стенд.
      **Acceptance:** end-to-end прогон, печать отчёта, JSON; purge ≥ макс. lookback
      (в днях); контракт seam-safe.

- [ ] **C4. Прогон + вердикт crypto (Opus).** DSR/PBO/OOS по mom-вариантам /carry
      /blend. Интерпретация, коммит, заметка в memory.

## Фазы — FX (второй блок, после C4)

- [ ] **F0. FX-данные (Sonnet).** G10 дневной спот (Stooq CSV), короткие ставки
      (FRED CSV) для carry, REER (BIS) для value. `fx/fxdata.py: load_panel()`.
      **Acceptance:** диапазоны/sanity; знак carry (AUD/NZD high, JPY/CHF low); URL+дата.

- [ ] **F1. FX-сигналы (Sonnet).** `carry(rate diff)`, `momentum(12-1)`,
      `value(REER-отклонение)`; переиспользуют общий `xsec`. **Acceptance:** формулы
      по литературе, ревью Opus.

- [ ] **F2. Адаптер FX + AQR кросс-чек (Sonnet) — ГЕЙТ ДОВЕРИЯ.** `fx_pkg.py` +
      `run_fx.py`; `aqr_crosscheck.py` качает бесплатные месячные AQR FX-факторы
      (Carry, Value&Momentum Everywhere), корреляция с нашими. **Acceptance:** corr
      > 0.7 → конструкция верна; иначе чинить ДО вердикта (аналог Ф6 стенда).

- [ ] **F3. Прогон + вердикт FX (Opus).** DSR/PBO/OOS по carry/momentum/value/blend.
      H1-H4 (carry +но crashy; momentum слабый/диверсифицирует; blend доминирует;
      переживает ли DSR/PBO). Коммит, memory.

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
