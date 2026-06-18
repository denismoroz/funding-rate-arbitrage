# PLAN — Coin Registry: один источник правды в БД, редактируемый через настройки FRAB

## Цель и контекст

Добавить монету в FRAB сейчас = правка кода в **5-7 местах** (verified ниже) + ломается
брит­тл-тест. Параметры монеты размазаны по константам, env-оверрайдам и хардкод-картам,
которые ОБЯЗАНЫ быть синхронны (footgun корректности). Консолидируем в **ОДИН источник —
таблицу в БД**, редактируемую через настройки стратегии FRAB (UI), как уже сделано у XSMOM
(`strategies.params_json` + settings-API). Добавление монеты = строка в UI, **ноль правок кода**.

Это **прод-рефактор** (`src/frab`), явно авторизован пользователем. Поведение для текущих
живых монет НЕ должно измениться (provenance-эквивалентность обязательна).

**Workflow (от пользователя): local-first, прод трогаем последним.** Вся разработка и
проверка — ЛОКАЛЬНО, на свежей копии прод-БД, со **ВСЕМИ стратегиями выключенными**
(`strategies.status='paused'`, никакого авто-трейда на буте). Прогоняем весь рефактор +
e2e локально. Только КОГДА всё взлетело локально (тесты зелёные, миграция чистая на копии
прод-схемы, e2e-чеклист пройден) — **выкладываем на прод** отдельным контролируемым шагом
(Фаза H). Это ровно тот же паттерн, что использовали для XSMOM.

### Зафиксированные решения (от пользователя — НЕ пересматривать)
1. **ОДИН источник = таблица в БД.** Никаких env-оверрайдов, никаких код-констант как
   второго слоя. Слоёный конфиг = путаница «какое значение победило».
2. **Сид один раз → константы УДАЛИТЬ.** Миграция засевает таблицу сегодняшними значениями
   (прод не дёргается), после чего код-константы стираются целиком. seed-and-**delete**, не keep.
3. **Нет fallback-дефолта.** Есть строка в таблице → монета tradeable; нет строки → НЕ
   торгуется, точка. `FALLBACK_LEVERAGE` (молчаливый дефолт) удаляется.
4. **Разделить две природы полей:**
   - **Риск-параметры** (`leverage`, `maint_ratio`, `position_size_usd`, `active`) — свободно
     редактируются юзером через settings.
   - **Рыночные факты** (`spot_token`, `sz_decimals`, bridge-safe) — НЕ печатаются руками;
     **дискаверятся + валидируются из HL** по тикеру и пишутся в ТУ ЖЕ строку (write-path,
     не конкурирующий источник). Иначе UI вернёт bridge-token footgun
     (кто-то впишет `AVAX→AVAX0` → торгуем независимо-ценимый bridge-токен).
5. **Гейт валидации:** монета не tradeable, пока `validate_spot_pairs` не подтвердил её
   спот-пару живьём из HL `spotMeta`; bridge-blacklist enforced.
6. **Защита открытых позиций:** нельзя удалить монету или сменить ей `leverage`/`maint_ratio`,
   пока на ней живая (открытая) `farb_position`.

## Текущая россыпь (verified — что схлопывается в одну таблицу)

| Место | Что там сейчас |
|-------|----------------|
| `src/frab/constants.py:9-18` | `RESEARCH_LEVERAGE`, `RESEARCH_MAINT_RATIO`, `FALLBACK_LEVERAGE`, `FALLBACK_MAINT_RATIO`, `CoinMarginSpec` |
| `src/frab/exchanges/hyperliquid/tokens.py:28,30` | `BRIDGE_TOKEN_BLACKLIST`, `MAINNET_SPOT_TOKEN_MAP`, `select_spot_token_map()`, `validate_spot_pairs()` |
| `src/frab/exchanges/hyperliquid/symbols.py:20` | `SPOT_TOKEN_INVERSE` (хардкод-инверсия; читается в backfill_fees, _fees, equity-route, reader, symbols — 6+ мест) |
| `src/frab/server.py:40` | `DEFAULT_COINS` |
| `src/frab/settings.py:44,54,111,192,209` | `hl_universe` env, `per_coin_params_json` env, `per_coin_params()`, `get_coin_spec()`, `universe_tuple()` |
| `src/frab/tests/test_settings_coin_spec.py:53` | брит­тл keyset-ассерт |
| `web/src/components/SignalsStrip.tsx:122`, `web/src/pages/Funding.tsx:22` | хардкод `["BTC","ETH","SOL"]` фолбэки |

Существующий паттерн для зеркала: `strategies.params_json` (JSON-колонка) + XSMOM
settings-API (`src/frab/api/routes/xsmom.py:458` get/patch params). Таблица `markets`
(`models.py:35`) уже есть под per-coin факты (её `has_spot` бажный — заодно вычистить).

## Целевой дизайн

**Новая таблица `coin_registry`** (рекомендация; Phase A финализирует table-vs-extend-`markets`
после чтения models.py — НО дефолт = отдельная таблица, чище для CRUD и guard'ов):

| колонка | тип | природа | заметка |
|---------|-----|---------|---------|
| `coin` | str PK | — | канонический перп-тикер (uppercase) |
| `leverage` | int | риск (юзер) | 1..50 |
| `maint_ratio` | float | риск (юзер) | (0, 0.5) |
| `position_size_usd` | float? | риск (юзер) | nullable = авто из budget/K/buffer |
| `active` | bool | риск (юзер) | в юниверсе/торгуется |
| `spot_token` | str? | факт (HL) | напр. `UBTC`; null = нет спот-ноги |
| `sz_decimals` | int? | факт (HL) | из perp_meta |
| `bridge_safe` | bool | факт (HL) | spot 1:1 с перпом, не EVM-bridge |
| `validated_at` | int(ms)? | факт (HL) | null = не валидирована → НЕ tradeable |

**Один сервис `CoinRegistry`** (загрузка из БД) отдаёт всё, что код берёт сейчас из
констант/env/карт — деривируя:
- `get_coin_spec(coin)` → `CoinMarginSpec(leverage, maint_ratio)` (из строки; нет строки → ошибка, не fallback).
- `universe()` → активные валидированные монеты.
- `spot_token_map()` → `{coin: spot_token}` где `spot_token` задан.
- `spot_token_inverse()` → инверсия, считается ОДИН раз при загрузке (рассинхрон невозможен).
- `bridge_blacklist` / `bridge_safe` → из флага.
- `sz_decimals(coin)`.

---

## Фазы (каждая = отдельный Sonnet-агент; Opus ревьюит + коммитит/пушит per фазу)

Прод-аккуратность во ВСЕХ фазах:
- venv: `/Users/d/prj/funding-rate-arbitrage/.venv/bin/python`; тесты: `uv run pytest`.
- Прод-доступ (если нужен): **`ssh dis@10.8.0.5`** (macOS, НЕ root, НЕ `d@`), repo
  `/Users/dis/prj/funding-rate-arbitrage`, БД `data/frab.db`, `uv` НЕ в ssh PATH → `.venv/bin/`.
- **Прод НЕ мигрировать/деплоить в фазах 0–G** — только read-only копия прод-БД для теста
  миграции. Всё локально, стратегии OFF. Деплой на прод = отдельная **Фаза H** под контролем
  пользователя, после того как всё взлетело локально.
- Каждая фаза — отдельный коммит+пуш (CLAUDE.md), **без `Co-Authored-By`**.
- **Provenance-инвариант:** после рефактора coin-spec/universe/spot-карты для ТЕКУЩИХ живых
  монет обязаны быть bit-exact равны до-рефакторным (доказать тестом). Никакого изменения
  торгового поведения.

### Фаза 0 — свежая прод-БД локально (read-only) + стратегии OFF
`ssh dis@10.8.0.5`: бэкап локальной `data/frab.db` → read-only scp свежей прод-`data/frab.db`
→ `.venv/bin/alembic upgrade head` для проверки, что локальная схема == прод. Чтобы новая
миграция легла поверх реальной прод-схемы без конфликтов. (Паттерн как в XSMOM Phase 0.)
**На локальной копии перед любым прогоном движка выставить `strategies.status='paused'` для
ВСЕХ строк** — никакого авто-трейда с прод-кредами на локальной машине. Включение стратегий
локально — только ручным toggle для e2e, и только если осознанно нужно.

### Фаза A — схема + модель + миграция (seed) + repo
- `models.py`: класс `CoinRegistry` (`__tablename__="coin_registry"`, колонки выше). Решить
  table-vs-extend-`markets` прочитав models.py; дефолт — отдельная таблица.
- Alembic `revision --autogenerate` (script_location `src/frab/db/migrations`): создать таблицу
  + **засеять из текущих констант**: `RESEARCH_LEVERAGE`/`RESEARCH_MAINT_RATIO` →
  leverage/maint; `MAINNET_SPOT_TOKEN_MAP` → spot_token (+ bridge_safe=true для них);
  `DEFAULT_COINS` ∪ research-монеты → строки; `active=true` для текущего живого юниверса;
  `validated_at` = now для засеянных (они уже живут в проде, валидны де-факто).
- `repo/coin_registry_repo.py`: CRUD (list/get/upsert/set_active/delete) + guard-хелперы.
- Тесты репо + теста миграции на КОПИИ прод-БД (alembic upgrade head чисто, строки засеяны верно).
**Deliverable:** модель + миграция + repo + тесты. Коммит+пуш.

### Фаза B — сервис деривации + замена call-sites (БЕЗ изменения поведения)
- `CoinRegistry` сервис (загрузка из БД, кеш в памяти, методы выше).
- Заменить ВСЕ читатели на сервис: `settings.get_coin_spec`→registry; `tokens` spot-карты→
  registry; `symbols.SPOT_TOKEN_INVERSE` (6+ мест)→`registry.spot_token_inverse()`;
  `server.DEFAULT_COINS`/`_select_coins`/`universe_tuple`→`registry.universe()`.
  Константы/env ПОКА оставить как источник сида, но НЕ читать их в рантайме.
- **Provenance-тест:** для текущих 7 живых монет `registry`-derived spec/universe/spot-map ==
  старые constants-derived (bit-exact). Доказать эквивалентность.
- **No-fallback:** монета вне registry → не в universe, не торгуется (assert).
- `uv run pytest` зелёный (существующее покрытие test_symbols/test_tokens/test_reader/
  test_settings_coin_spec — страховочная сеть; адаптировать, не ослаблять).
**Deliverable:** сервис + заменённые call-sites + provenance-тест. Коммит+пуш. (Самая аккуратная фаза.)

### Фаза C — HL-дискавери + валидация как write-path
- Поток «добавить монету по тикеру»: запрос HL `spotMeta`/`perp_meta` → разрешить
  `spot_token` (или null если спота нет), `sz_decimals`, проверить bridge-blacklist
  (`bridge_safe`), прогнать `validate_spot_pairs` → записать факты в строку + `validated_at`.
  Переиспользовать `tokens.validate_spot_pairs`. Перенести `validate_spot_pairs` на чтение
  spot-карты ИЗ registry.
- Монета без `validated_at` или с `active=false` → не tradeable. Гейт enforced на старте движка.
- Тесты дискавери/валидации (фикстура spotMeta; bridge-токен → reject; нет спота → spot_token=null).
**Deliverable:** discovery+validation write-path + тесты. Коммит+пуш.

### Фаза D — settings-API (CRUD реестра)
`api/routes/` (новый роутер или в составе FRAB-настроек):
- `GET /coins` — список строк реестра (риск-поля + факты + active + validated_at).
- `POST /coins` — добавить по тикеру → дискавери+валидация (Фаза C) → строка (active=false до подтверждения).
- `PATCH /coins/{coin}` — править риск-поля; **guard:** запрет менять `leverage`/`maint_ratio`
  при открытой `farb_position` на этой монете.
- `POST /coins/{coin}/active` — вкл/выкл в юниверс (с валидацией перед вкл).
- `DELETE /coins/{coin}` — **guard:** запрет при открытой позиции.
- **Reload-семантика:** определить, когда движок перечитывает реестр (на save И/ИЛИ hour-tick).
  ИЗБЕЖАТЬ XSMOM-подвоха «ручное действие не перечитывает params → стейл» ([[project_xsmom_params_reload]]):
  явно инвалидировать кеш `CoinRegistry` на каждое мутирующее API-действие.
- Smoke-тесты API + guard-тесты.
**Deliverable:** роутер + guards + reload + тесты. Коммит+пуш.

### Фаза E — UI (настройки FRAB: таблица реестра)
`web/src/` (дробить на компоненты, как XSMOM-настройки — НЕ простыня):
- Страница/секция настроек FRAB: таблица монет (строки = монеты; колонки = риск-поля
  редактируемые + факты read-only + `active` toggle + validated-бейдж).
- Форма «добавить по тикеру» → POST → показать дискаверенные факты → подтвердить active.
- Edit риск-полей inline; remove с guard-предупреждением (если открыта позиция — запретить с пояснением).
- `lib/` react-query хуки отдельно от разметки. Переиспользовать `components/ui/*`, паттерн `XsmomSettings`.
**Deliverable:** UI-компоненты + хуки. Коммит+пуш.

### Фаза F — cleanup: УДАЛИТЬ второй источник (это и есть «один источник»)
- Удалить: `constants.py` (`RESEARCH_LEVERAGE`/`RESEARCH_MAINT_RATIO`/`FALLBACK_*`),
  `per_coin_params_json` + `per_coin_params()` + `hl_universe` + `universe_tuple()` +
  `get_coin_spec()` из settings, `DEFAULT_COINS`, `MAINNET_SPOT_TOKEN_MAP`/`select_spot_token_map`,
  хардкод `SPOT_TOKEN_INVERSE` константу. `CoinMarginSpec` оставить как dataclass (тип), но
  данные — только из registry.
- Переписать `test_settings_coin_spec.py` на проверку РЕЕСТРА (без хардкод-keyset).
- **grep-доказательство:** ноль оставшихся рантайм-читателей удалённых констант/env.
- `uv run pytest` зелёный.
**Deliverable:** удаление констант/env + переписанный тест + grep-чек. Коммит+пуш.

### Фаза G — фронт-фолбэки + финальная e2e-проверка
- Убрать хардкод coin-списки `SignalsStrip.tsx:122`, `Funding.tsx:22` (брать из API/registry).
- Локальный e2e: `frab serve` → обе стратегии paused (local_mode) → открыть FRAB-настройки →
  добавить монету по тикеру (дискавери+валидация работает) → edit риск-полей → remove-guard
  при открытой позиции срабатывает → движок видит только активные валидированные монеты.
**Deliverable:** фронт-cleanup + e2e-чеклист пройден. Коммит+пуш.

### Фаза H — деплой на прод (ТОЛЬКО после зелёного локального e2e; под контролем пользователя)
Гейт входа: фазы 0–G готовы, `uv run pytest` зелёный, локальный e2e-чеклист (Фаза G) пройден.
- `ssh dis@10.8.0.5`, repo `/Users/dis/prj/funding-rate-arbitrage`: `git pull`.
- **Перед миграцией — бэкап прод-БД** (`data/frab.db` → копия с таймстампом).
- Остановить агенты (`launchctl` `com.frab.{engine,web}`), `.venv/bin/alembic upgrade head`
  (seed-миграции `294489218bcb`+`f1a2b3c4d5e6` засеют `coin_registry` 38 монет → те же живые 5).
- **БЛОКЕР — поправить prod plist:** `~/Library/LaunchAgents/com.frab.engine.plist` сейчас
  запускает `frab serve ... --coins BTC,ETH,SOL,HYPE,PURR ...`. Арг `--coins` УДАЛЁН из кода →
  после деплоя рестарт упадёт «no such option --coins». Убрать `--coins BTC,ETH,SOL,HYPE,PURR`
  из `ProgramArguments` (бэкап .plist, `plutil`/правка, `launchctl unload`+`load`).
- **Почистить prod .env:** удалить мёртвую `FRAB_HL_UNIVERSE` (settings её больше не читает;
  не блокер, но гигиена единого источника).
- **Sanity на проде после миграции, движок ещё OFF:** проверить, что `coin_registry` засеян
  (38 строк, 5 active = текущий живой юниверс) и spec/spot-карта совпадают (provenance) — *до* рестарта.
- Рестарт агентов → убедиться, что движок поднялся на тех же монетах, открытые позиции целы,
  торговое поведение не изменилось. Rollback-план: восстановить бэкап БД + откатить git.
**Deliverable:** прод мигрирован, живой юниверс не изменился, реестр редактируется через UI.

---

## Verification (общая)
- **Local-first:** фазы 0–G полностью на локальной копии прод-БД со стратегиями OFF; прод
  не трогается до Фазы H, и только после зелёного локального e2e.
- `uv run pytest` зелёный после КАЖДОЙ фазы (особенно B и F).
- **Provenance-эквивалентность** (Фаза B): для текущих живых монет registry == constants bit-exact.
- Миграция протестирована на КОПИИ прод-БД (Фаза 0/A): `alembic upgrade head` чисто, строки засеяны.
- **No-fallback** (Фаза B/F): монета вне registry → не торгуется (assert).
- **Guard'ы** (Фаза D): нельзя удалить/перенастроить монету с открытой `farb_position`.
- **grep** (Фаза F): ноль рантайм-читателей удалённых констант/env.
- Bridge-footgun закрыт: spot-факты только из HL-дискавери+валидации, не из UI-текста.

## Что НЕ делать
- НЕ оставлять константы как дремлющий второй источник (seed → **delete**).
- НЕ давать UI свободно печатать spot-маппинг (рыночные факты — только HL-дискавери+валидация).
- НЕ разрешать удаление/перенастройку монеты с открытой позицией.
- НЕ трогать XSMOM registry/params-семантику (отдельная стратегия).
- НЕ менять торговое поведение текущих монет (provenance обязан совпасть).
- НЕ мигрировать/деплоить прод в фазах 0–G (только read-only копия БД; деплой = Фаза H,
  отдельно, после зелёного локального e2e). НЕ запускать движок с прод-кредами локально без OFF.
- Не мапить bridge-токены AVAX0/LINK0/AAVE0 на canonical perp ([[feedback_hl_bridge_tokens]]).
