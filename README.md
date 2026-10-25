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

## Trend с недельной перенастройкой (`trend_retune`, paper)

Вариант стратегии `trend`, в котором монеты и периоды выбирает **код по прошлому**, а не список,
написанный руками (решение владельца 2026-10-25; в q-lab проверен как `trend-retune-weekly`). Только
бумага: ключа подписи у движка нет, ордеров он не ставит.

Как работает: каждый день движок считает 10 вариантов (правило выбора монет × набор периодов).
Правила — 25 крупнейших по рыночной стоимости без стейблкоинов и 25 самых торгуемых на Hyperliquid;
наборы — 30/60/90/120 вместе и каждый период по отдельности. В начале каждой недели выбирается вариант
с лучшим Шарпом после комиссий **по доходности, которая уже состоялась до этого дня**, и бумажная книга
торгует им до следующей перенастройки. Первые 2–3 недели позиций нет (первая перенастройка ничего не выбирает, вторая — первая с выбором): вариант допускается к выбору,
только проторговав (в симуляции) целый период. Код: `src/frab/strategy/trend/retune*.py`.

Откуда данные (бесплатно, без ключей): цены, обороты, фандинг — Hyperliquid; рыночная стоимость —
CoinGecko `/coins/markets` (раз в неделю). **Отличие от q-lab:** там стоимость брали из недельных срезов
CoinMarketCap, у CoinGecko ранги и тикеры могут не совпадать; прошлое ранжирование по стоимости
восстановить нельзя, поэтому оно копится только с момента запуска и записывается в состояние.

Как включить на проде (сам сервер строку не создаёт, пока её нет — движок выключен):

```bash
# 1. код на прод (стандартный deploy из AGENTS.md); миграций нет, новых таблиц нет
# 2. бэкап БД и строка стратегии с параметрами из deploy/trend_retune_params.json
cp data/frab.db data/frab.db.bak-$(date +%Y%m%d-%H%M%S)
sqlite3 data/frab.db "INSERT INTO strategies (name, version, params_json, status)
  VALUES ('trend_retune', 'v1-paper', json(readfile('deploy/trend_retune_params.json')), 'active');"
# (если в sqlite3 нет readfile — вставить JSON из файла вручную в кавычках)
# 3. launchctl kickstart -k gui/$(id -u)/com.frab.engine
```

Проверка: `curl -s 'localhost:8765/api/trend/summary?variant=trend_retune' | jq .retune`,
`/api/trend/retunes` — все перенастройки (победитель и оценки всех вариантов), `/api/trend/events?variant=trend_retune`.
Сброс: поставить `status='paused'` в строке стратегии; полный сброс — `TrendRepo.reset(strategy_id)`.

Нагрузка: раз в сутки около 230 запросов дневных свечей Hyperliquid (с паузой 0.6 с, ~2.5 минуты) и по
2 запроса на монету из книги в остальные часы. Капитал в файле параметров — 10 000 $ (на 3 000 $ мелкие
ноги не проходят минимальный ордер биржи, как отмечено в q-lab).
