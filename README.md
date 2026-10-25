# frab — funding-rate arbitrage paper trading

Async paper-trading platform for Hyperliquid funding-harvest strategy (Strategy A).
Architecture: Python 3.13 + FastAPI + SQLAlchemy 2.0 (async) + aiosqlite + Alembic;
React + Vite frontend.

See `SUMMARY.md` for research history and `.claude/plans/` for the production MVP plan.

## Setup

```bash
uv sync                          # install Python deps
(cd web && npm install)          # install frontend deps

uv run frab init-db              # create SQLite schema (data/frab.db)
uv run frab seed                 # insert Hyperliquid + 7 markets
uv run frab backfill --hours 24  # populate 24h of funding history
```

## Run

```bash
# engine + API (uvicorn) on :8765
# Universe is sourced from the coin_registry table (configured via UI Settings)
uv run frab serve --host 127.0.0.1 --port 8765

# web dashboard (vite dev server) on :5173 — proxies /api and /ws to :8765
cd web && npm run dev
```

Open <http://localhost:5173>.

## Tests

```bash
uv run pytest                              # full suite
uv run pytest --cov=src/frab --cov-report=term-missing
```

## Deployment

Two launchd services run on the always-on prod host:

- `com.frab.engine` — uvicorn on `127.0.0.1:8765`
- `com.frab.web` — vite dev server on `0.0.0.0:5173`

Install with `deploy/launchd/install.sh`. See `AGENTS.md` for the prod host address.

## Trend с недельной перенастройкой (`trend_retune*`, paper)

Вариант стратегии `trend`, в котором монеты и периоды выбирает **код по прошлому**, а не список,
написанный руками (решение владельца 2026-10-25; в q-lab проверен как `trend-retune-weekly`). Только
бумага: ключа подписи у движка нет, ордеров он не ставит.

Как работает: каждый день движок считает 10 вариантов (правило выбора монет × набор периодов).
Правила — 25 крупнейших по рыночной стоимости без стейблкоинов и 25 самых торгуемых на Hyperliquid;
наборы — 30/60/90/120 вместе и каждый период по отдельности. В начале каждой недели выбирается вариант
с лучшим Шарпом после комиссий **по доходности, которая уже состоялась до этого дня**, и бумажная книга
торгует им до следующей перенастройки. Вариант допускается к выбору, только проторговав (в симуляции)
целый период. Код: `src/frab/strategy/trend/retune*.py`, `coin_selector.py`, `cap_history.py`.

### Подгрузка истории (backfill, T52b)

Без неё первая перенастройка с выбором наступила бы через 2–3 недели после запуска (владелец, 2026-10-25:
«да ладно 2–3 — недели держать 10к… а историю загрузить слабо?»). Поэтому у новой книги (строка есть, состояния
ещё нет) движок сначала **восстанавливает прошлое кандидатов** с 2025-01-01 (начало окна q-lab) и проигрывает
недельные перенастройки, так что текущий выбор — тот, который даёт история. Это честно, потому что каждый день
прошлого видит только то, что существовало до него — как стенд q-lab:

- цены и обороты — дневные свечи Hyperliquid, закрытые к этому дню; фандинг — выплаты до этого дня;
- рыночная стоимость — **недельные срезы CoinMarketCap** из q-lab (`deploy/trend_retune_cap_history.json`,
  делает q-lab: `scripts/export_cap_history.py`); срез, датированный днём D, виден со следующего дня после D;
- те же правила, что у стенда: символ → перп Hyperliquid (включая `kPEPE` и т. п.), стейблкоины по тегу
  CoinMarketCap исключены, для ранжирования нужны 151 дневных закрытий.

Сами позиции книги **начинаются в момент запуска**: прошлых сделок в книге нет, она сразу открывает позиции по
текущему выбору. С первого живого дня рейтинг берётся из CoinGecko (как раньше); в состоянии
(`retune.cap_sources` в сводке: неделя → `cmc_history` | `coingecko`) и в событиях (`cap_source`) записано,
откуда взят рейтинг каждой недели. История перенастроек попадает в `/api/trend/retunes` с `phase: "history"`.

**Срок бумажного теста считается от первого выбора** (владелец: «отсчёт от первого выбора»): событие `retune`
с `first_choice: true` — в момент запуска, если история уже даёт выбор (`phase: "launch"`), иначе в первую
живую перенастройку с выбором. В сводке: `first_choice_at` (мс) и `retune.days_since_first_choice`.

Проверка на данных стенда (2026-10-25, те же свечи, 662 дня): из 94 недельных выборов подгрузка и q-lab
совпали во всех 94, если у q-lab не отсекать монеты с дырами в истории фандинга; с их отсечением — в 84
(все 2025 год и текущий выбор совпадают, расходятся `cap/ens` и `cap/l120` в 2026-02…08: q-lab не держит монету,
у которой нет фандинга за день, например XMR, а здесь недостающий фандинг считается нулём и событие
записывается в `funding_missing`).

### Две книги и как их включить

Движок стартует **по одному на каждую строку стратегии, имя которой начинается с `trend_retune`**
(`trend_retune_3k`, `trend_retune_10k`, …): у каждой свои параметры (капитал), состояние и события, API —
`?variant=<имя>`. Загруженная история общая (кэш на диске), поэтому вторая книга её не качает заново.
Файлы параметров отличаются только капиталом: `deploy/trend_retune_3k_params.json` (3 000 $) и
`deploy/trend_retune_10k_params.json` (10 000 $). Старая одиночная строка `trend_retune` с
`deploy/trend_retune_params.json` работает по-прежнему, но без подгрузки (в файле нет `"backfill": true`).

Сервер сам строк не создаёт — пока их нет, движок выключен. Как включить на проде (macOS `sqlite3`, JSON
вставлен прямо в запрос, без `readfile`):

```bash
cd /Users/dis/prj/funding-rate-arbitrage
# 1. код и файл deploy/trend_retune_cap_history.json на прод (стандартный deploy из AGENTS.md; миграций нет)
# 2. бэкап БД
cp data/frab.db data/frab.db.bak-$(date +%Y%m%d-%H%M%S)
# 3. две строки стратегий
sqlite3 data/frab.db <<'SQL'
INSERT INTO strategies (name, version, params_json, status) VALUES
('trend_retune_3k', 'v1-paper', '{"capital_usd":3000.0,"vol_window":30,"vol_target_daily":0.02,"leverage_cap":3.0,"risk_scale":0.2,"book_vol_target_ann":0.14,"book_vol_window_days":60,"book_vol_min_days":20,"book_vol_scale_min":0.25,"book_vol_scale_max":2.0,"book_vol_prior_ann":0.3,"min_history_days":151,"slippage":0.0005,"min_order_usd":10.0,"rebalance_hour_utc":0,"maint_margin_rate":{"BTC":0.0125,"ETH":0.02,"SOL":0.025},"default_maint_margin_rate":0.05,"mode":"paper","top_k_by_market_cap":25,"top_k_by_volume":25,"top_k_requires_history":true,"lookback_sets":{"ens":[30,60,90,120],"l30":[30],"l60":[60],"l90":[90],"l120":[120]},"universe_rules":["cap","volume"],"score_taker_fee_bps":3.5,"score_slippage_bps":0.9,"objective":"sharpe_net","market_cap_rank_limit":200,"selection_max_age_hours":12.0,"coingecko_base_url":"https://api.coingecko.com/api/v3","daily_fetch_pause_s":0.6,"backfill":true,"backfill_start":"2025-01-01","cap_history_path":"deploy/trend_retune_cap_history.json","backfill_cache_dir":"data/trend_retune_cache","backfill_candle_pause_s":2.0,"backfill_funding_page_pause_s":2.7,"cap_history_max_age_days":21.0}', 'active'),
('trend_retune_10k', 'v1-paper', '{"capital_usd":10000.0,"vol_window":30,"vol_target_daily":0.02,"leverage_cap":3.0,"risk_scale":0.2,"book_vol_target_ann":0.14,"book_vol_window_days":60,"book_vol_min_days":20,"book_vol_scale_min":0.25,"book_vol_scale_max":2.0,"book_vol_prior_ann":0.3,"min_history_days":151,"slippage":0.0005,"min_order_usd":10.0,"rebalance_hour_utc":0,"maint_margin_rate":{"BTC":0.0125,"ETH":0.02,"SOL":0.025},"default_maint_margin_rate":0.05,"mode":"paper","top_k_by_market_cap":25,"top_k_by_volume":25,"top_k_requires_history":true,"lookback_sets":{"ens":[30,60,90,120],"l30":[30],"l60":[60],"l90":[90],"l120":[120]},"universe_rules":["cap","volume"],"score_taker_fee_bps":3.5,"score_slippage_bps":0.9,"objective":"sharpe_net","market_cap_rank_limit":200,"selection_max_age_hours":12.0,"coingecko_base_url":"https://api.coingecko.com/api/v3","daily_fetch_pause_s":0.6,"backfill":true,"backfill_start":"2025-01-01","cap_history_path":"deploy/trend_retune_cap_history.json","backfill_cache_dir":"data/trend_retune_cache","backfill_candle_pause_s":2.0,"backfill_funding_page_pause_s":2.7,"cap_history_max_age_days":21.0}', 'active');
SQL
sqlite3 data/frab.db "SELECT id, name, status, json_extract(params_json, '$.capital_usd') FROM strategies WHERE name LIKE 'trend_retune%';"
# 4. перезапуск движка (первая подгрузка начнётся сама)
launchctl kickstart -k gui/$(id -u)/com.frab.engine
```

Что будет: первый тик каждой книги качает историю — **около 2 200 запросов к Hyperliquid, порядка полутора
часов** (234 перпа с дневными свечами, ~1 900 страниц фандинга для ~160 монет, которые когда-либо попадали в
выбор; паузы 2,0 с и 2,7 с держат нагрузку около 1 000 из 1 200 «весов» в минуту). Пока идёт загрузка, книга не
создана (`started: false`, `last_error` пуст; в `logs/engine.out.log` строки `trend retune backfill: …`).
Вторая книга берёт всё из кэша. Прерывание безопасно: каждая монета пишется на диск в
`data/trend_retune_cache/` сразу, следующий тик (или перезапуск) продолжает с места остановки; когда обе
книги запущены, каталог можно удалить. Сам проигрыш 660 дней занимает секунды. Рестарт после подгрузки
историю заново не качает (книга уже есть). Если файла `trend_retune_cap_history.json` нет, а `backfill` включён,
книга **не стартует пустой**, а пишет причину в `last_error`.

Проверка:

```bash
curl -s 'localhost:8765/api/trend/summary?variant=trend_retune_3k'  | jq '{started, capital, equity, first_choice_at, retune: .retune | {chosen, refits, backfill, cap_sources, days_since_first_choice}}'
curl -s 'localhost:8765/api/trend/summary?variant=trend_retune_10k' | jq '.capital, .equity, .legs'
curl -s 'localhost:8765/api/trend/retunes?variant=trend_retune_10k' | jq '.[0:3]'    # все перенастройки: победитель и оценки
curl -s 'localhost:8765/api/trend/events?variant=trend_retune_10k'
```

Пауза: `status='paused'` в строке стратегии. Полный сброс: `TrendRepo.reset(strategy_id)` — следующий тик
подгрузит историю заново (из кэша).

Откуда данные (бесплатно, без ключей): цены, обороты, фандинг — Hyperliquid; рыночная стоимость живых дней —
CoinGecko `/coins/markets` (раз в неделю), прошлого — срезы CoinMarketCap из q-lab. **Отличие от q-lab:** у
CoinGecko ранги и тикеры могут не совпадать со срезами CoinMarketCap; поэтому первая живая неделя берётся уже
из другого источника. Нагрузка в обычном режиме: раз в сутки около 230 запросов дневных свечей (пауза 0,6 с,
~2,5 минуты) и по 2 запроса на монету из книги в остальные часы. Капитал на 3 000 $ — проверка того, мешает ли
минимальный ордер биржи мелким ногам (в q-lab отмечено, что на 3 000 $ часть ног может не пройти).

## Bv2 с месячной перенастройкой (`b2_retune`, paper)

Бумажная книга Bv2, в которой **код раз в месяц выбирает по прошлому** одну из двух настроек, уже работающих на проде:
`b2` (запас маржевого пула `hedge_margin_headroom = 1.0`) или `b2_cold` (1.5). Решение владельца 2026-10-25;
в q-lab это выживший эксперимент `bv2-research-headroom-retune` (на стенде вперёд с 2026-06-01 по 2026-10-24:
+22,26 % годовых, Шарп 1,48, максимальная просадка −5,19 %, 5 перенастроек с выбором, 1 смена выбора). Только
бумага: ключа подписи у движка нет, ордеров он не ставит. Код: `src/frab/strategy/b2/retune*.py`.

Как работает (подробно — в докстринге `retune.py`):

- **Два кандидата идут всё время.** Каждый — полный набор книг по монетам (`CoinBook`, BTC ETH SOL AVAX,
  `B2Params(capital_usd=257, carry_enabled=False)`) и отличается только запасом пула. Их двигает каждый закрытый
  часовой бар независимо от того, кто выбран; старт монет и разогрев — как у стенда q-lab (BTC/ETH/SOL с
  2026-04-09 22:00, AVAX с 2026-05-06 14:00, когда появился спот UAVAX).
- **Перенастройка — в первый час каждого календарного месяца UTC.** Счёт кандидата — Шарп (среднее / стандартное
  отклонение, ddof=1) его часовых доходностей по собственному капиталу книги, уже состоявшихся к этому моменту,
  по растущему окну от первого активного часа кандидата. К выбору допускается тот, кто стартовал раньше
  **предыдущей** перенастройки (проторговал целый период). Нет допущенных — книга **стоит в деньгах** до следующей
  перенастройки, «любимчика» по умолчанию нет. Первый выбор — 2026-06-01.
- **Сдвиг на час, как у стенда.** Решение принимается в момент закрытия первой свечи месяца (00:00–01:00): в счёт
  входит доходность этой свечи, а новый выбор зарабатывает со следующей. У q-lab то же самое: его «строка t» —
  доходность свечи t+1.
- **Публикуемая книга — составная.** Её капитал начинается с 257 $ в момент первого выбора и каждый час меняется
  на часовую доходность выбранного кандидата; позиции на экране — позиции выбранного, умноженные на (капитал
  книги / капитал кандидата). Это аналог того, что стенд берёт веса выбранного кандидата.
- **Смена выбора стоит денег.** Собственные комиссии frab (`SPOT_TAKER`, `PERP_TAKER`) плюс `slippage` на
  **модуль разности** масштабированных позиций двух кандидатов (по монете и ноге), событиями `retune_switch`.
  Первый выбор — это покупка позиций «из денег» (≈0,27 $ на 257 $), переход «в деньги» — их закрытие. Опубликованная
  кривая начинается строкой на 257 $ (метка = начало первой свечи выбора), а стоимость открытия — её первый шаг.

### История и «живая» часть

При первом тике (строки состояния нет) движок за секунды проигрывает **всю историю** с 2026-03-08 02:00 UTC —
это первый бар панели стенда, поэтому книги стартуют в те же часы, что и у стенда, — и публикует строки с первого
выбора. Всё, что создал этот первый тик, — **подгруженное прошлое**; правило владельца «4–6 недель бумаги»
считается только по живому:

- `launch_ms` (час первого тика) лежит в состоянии книги; строки `b2_equity` с `ts_ms <= launch_ms` — история,
  позже — живые. В API: `retune.launch_ms`, `retune.live_hours`, `retune.live_pnl`; на странице — пунктир «launch».
- у каждого события `b2_events` в `details_json.phase`: `backfill` или `live`; итог проигрыша — событие
  `backfill` (число баров, перенастроек, секунды, запросы).
- Рестарт **не проигрывает историю заново**: состояние (книги обоих кандидатов, накопленные суммы для Шарпа, масштаб)
  — одна строка `b2_books` с монетой `__retune__`, в той же транзакции, что и публикуемые строки; следующий тик
  продолжает с бара после `last_bar_ms`. Если параметры строки стратегии изменились после сохранения состояния,
  движок откажется продолжать (`last_error: … params changed`) — сброс см. ниже.

Почему нужен файл `deploy/b2_retune_history_seed.json`: `candleSnapshot` Hyperliquid отдаёт только последние
~5000 часовых свечей (сейчас с 2026-03-31 05:00), а проигрыш начинается 2026-03-08. Файл — замороженные часы
(закрытия, максимумы свечей для проверки ликвидации, фандинг BTC ETH SOL AVAX) из снимка q-lab, на котором
считался стенд (`scripts/export_b2_retune_seed.py`, запускается окружением q-lab). Сверка с самой биржей по
перекрытию: 4 964 часа × 4 монеты, закрытия, максимумы и фандинг совпали полностью. Дальше движок докачивает
историю сам и **хранит её в `data/b2_retune_cache/hourly/<монета>.json`** — этот кэш **не чистится по возрасту**
(в отличие от кэша trend): старые часы биржа больше не отдаст, и растущее окно перестало бы воспроизводиться.
Каталог нельзя удалять и после; если он потерян, файл-семя восстановит часы до 2026-10-24, а часы после семени
будут докачаны с биржи, пока она их ещё отдаёт (около 5000 часов от «сейчас»).

### Как включить на проде

Сервер сам строку не создаёт — пока её нет, движок выключен. Нужен код с этим коммитом (стандартный deploy из
AGENTS.md; миграций нет) и файл `deploy/b2_retune_history_seed.json` из репозитория:

```bash
cd /Users/dis/prj/funding-rate-arbitrage
# 1. код уже на проде (стандартный deploy из AGENTS.md, миграций нет); бэкап БД
cp data/frab.db data/frab.db.bak-$(date +%Y%m%d-%H%M%S)
# 2. строка стратегии (имя именно b2_retune)
sqlite3 data/frab.db <<'SQL'
INSERT INTO strategies (name, version, params_json, status) VALUES
('b2_retune', 'v2-paper-retune', '{"coins":["BTC","ETH","SOL","AVAX"],"capital_usd":257.0,"spot_share":0.5,"hedge_threshold":0.0,"sticky_exit_hours":12,"ratchet_threshold":0.5,"carry_enabled":false,"carry_fraction":0.6,"carry_entry_apr":0.1,"carry_exit_hours":24,"slippage":0.0005,"min_order_usd":10.0,"margin_enabled":true,"short_leverage":{"BTC":3.0,"ETH":2.0,"SOL":1.5,"AVAX":1.5},"default_leverage":1.0,"maint_margin_rate":{"BTC":0.0125,"ETH":0.02,"SOL":0.025,"AVAX":0.05},"default_maint_margin_rate":0.05,"margin_buffer":0.1,"hedge_margin_headroom":1.0,"rebalance_at_im_share":0.5,"mode":"paper","retune_candidates":{"b2":{"hedge_margin_headroom":1.0},"b2_cold":{"hedge_margin_headroom":1.5}},"retune_history_start":"2026-03-08T02:00:00Z","retune_coin_starts":{"BTC":"2026-04-09T22:00:00Z","ETH":"2026-04-09T22:00:00Z","SOL":"2026-04-09T22:00:00Z","AVAX":"2026-05-06T14:00:00Z"},"retune_refit_every":"M","retune_history_seed_path":"deploy/b2_retune_history_seed.json","retune_history_cache_dir":"data/b2_retune_cache","retune_candle_pause_s":2.0,"retune_funding_page_pause_s":2.7}', 'active');
SQL
sqlite3 data/frab.db "SELECT id, name, status FROM strategies WHERE name LIKE 'b2%';"
# 3. перезапуск движка (история проигрывается в первом же тике: ~30 секунд, из них секунд 5 — сам проигрыш)
launchctl kickstart -k gui/$(id -u)/com.frab.engine
```

Проверка:

```bash
curl -s 'localhost:8765/api/b2/summary?test=b2_retune' | jq '{equity, pnl_pct, hours, started_ms, retune: .retune | {chosen, last_refit_ms, next_refit_ms, refits, choice_changes, launch_ms, live_hours, live_pnl, candidates}}'
curl -s 'localhost:8765/api/b2/retunes' | jq '.[0:3]'                 # каждая перенастройка: выбор и оценки обоих кандидатов
curl -s 'localhost:8765/api/b2/events?test=b2_retune&limit=20'
sqlite3 data/frab.db "SELECT MIN(ts_ms), MAX(ts_ms), COUNT(DISTINCT ts_ms) FROM b2_equity WHERE strategy_id=(SELECT id FROM strategies WHERE name='b2_retune');"
```

Страница: `http://10.8.0.5:5173/#/b2/retune` (вкладка «Re-tuned monthly» в разделе B v2) — текущий выбор, дата последней
и следующей перенастройки, Шарп кандидатов, граница «история | живое».

Пауза: `status='paused'` в строке стратегии (кэш при этом не пополняется; после возобновления движок докачает
пропущенные часы, пока они в пределах ~5000 часов). Полный сброс (заново проиграть историю, например после смены
параметров): остановить движок, затем
`DELETE FROM b2_equity WHERE strategy_id=<id>; DELETE FROM b2_events WHERE strategy_id=<id>; DELETE FROM b2_books WHERE strategy_id=<id>;`
(то же делает `B2Repo.reset`), кэш `data/b2_retune_cache/` не трогать.
