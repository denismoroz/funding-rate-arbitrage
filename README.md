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
