# AGENTS.md

Заметки по запуску и эксплуатации frab — funding-rate arbitrage shadow-trading platform.

Целевая среда: macOS, Python 3.13, Node 26, uv для Python-зависимостей, npm для веб.

---

## 0. Делегирование работы (Opus → Sonnet)

**Правило:** Opus делегирует реализацию Sonnet-агентам максимально часто. Сам Opus занимается планированием, дизайном интерфейсов, ревью diff'ов суб-агентов, интеграционными вызовами, решениями на codebase-wide контексте. **Не пиши код руками, если задача укладывается в одного Sonnet-агента.**

Спека для Sonnet должна быть self-contained:
- точные пути к файлам (read / edit / create)
- публичные сигнатуры / поля DTO / DB-схема
- какие тесты добавить (путь + минимальный набор кейсов)
- явные «не делай»: не запускай `git`, не коммить, не добавляй лишних абстракций

Опус оставляет за собой:
- ревью diff'ов до коммита
- запуск `pytest`-suite и `npm run build`
- `git add` / `git commit` / `git push` (Sonnet-агенты НЕ запускают git)
- рестарт сервисов (`launchctl kickstart`) и верификацию live-поведения

Если sub-agent вернул что-то неполное — Opus формулирует точный fix-prompt для следующей итерации, а не правит сам (кроме тривиальных правок).

Грануляция: одна Sonnet-таска ≈ один класс/модуль/feature + тесты к нему.

---

## 1. Что это

- **Backend** (`src/frab/`) — FastAPI + asyncio engine, торгует Strategy A (funding-harvest) в **paper mode** на live Hyperliquid feed. Пишет в SQLite (`data/frab.db`).
- **Frontend** (`web/`) — Vite + React 18 + TS Dashboard. Читает `/api/*` через dev-proxy.
- **Research** (`research/`) — оффлайн backtest engine и эксперименты, не трогаются prod-кодом.

### Hosts

| Host | Role | Где |
|------|------|-----|
| local mac | **dev** — здесь идёт разработка, новые фичи, тесты | `/Users/d/prj/funding-rate-arbitrage` |
| `10.8.0.5` (mbp2.local) | **prod** — always-on paper-trading | `ssh dis@10.8.0.5`, `/Users/dis/prj/funding-rate-arbitrage` |

Prod-инстанс крутится 24/7 (mac не уходит в sleep), source-of-truth для оценки live pace стратегии. Web UI: `http://10.8.0.5:5173/`. Deploy: `git push` на main → SSH → `git pull && uv sync && cd web && npm install && cd .. && launchctl kickstart -k gui/$(id -u)/com.frab.engine`.

---

## 2. Где что лежит

| Путь | Что |
|------|-----|
| `data/frab.db` | SQLite база, gitignored. Все timeseries + позиции + события. |
| `logs/` | stdout/stderr launchd-сервисов, gitignored. |
| `src/frab/` | Backend Python пакет. |
| `web/` | Frontend Vite-проект. |
| `research/` | Оффлайн research, не используется в проде. |
| `deploy/launchd/` | LaunchAgent plists + install/uninstall скрипты. |
| `~/Library/LaunchAgents/com.frab.{engine,web}.plist` | Рендеренные plists (создаются `install.sh`). |

---

## 3. Первый запуск (с нуля)

```bash
# 1. Python deps
uv sync

# 2. Frontend deps
cd web && npm install && cd ..

# 3. Создать SQLite + Alembic schema
uv run frab init-db

# 4. Заполнить exchange + 7 markets (HL, идемпотентно)
uv run frab seed

# 5. Подкачать 24h funding history из HL (идемпотентно)
uv run frab backfill --hours 24

# 6. Поднять оба сервиса через launchd
deploy/launchd/install.sh
```

После шага 6:
- Backend: `http://127.0.0.1:8765/healthz`
- Web UI: `http://127.0.0.1:5173/`
- Auto-start при логине, auto-restart при крэше (KeepAlive on Crashed).

---

## 4. Управление сервисами

```bash
# Включить оба
deploy/launchd/install.sh

# Или только один
deploy/launchd/install.sh engine
deploy/launchd/install.sh web

# Снести оба
deploy/launchd/uninstall.sh

# Статус
launchctl print gui/$(id -u)/com.frab.engine | grep -E "state|last exit"
launchctl print gui/$(id -u)/com.frab.web    | grep -E "state|last exit"

# Хард-рестарт (например после изменения кода)
launchctl kickstart -k gui/$(id -u)/com.frab.engine
launchctl kickstart -k gui/$(id -u)/com.frab.web

# Остановить временно
launchctl bootout gui/$(id -u)/com.frab.engine

# Поднять обратно
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.frab.engine.plist
```

---

## 5. Логи

```bash
# Engine
tail -f logs/engine.out.log
tail -f logs/engine.err.log

# Web (Vite)
tail -f logs/web.out.log
tail -f logs/web.err.log
```

Логирование backend: `structlog`-style, формат `%(asctime)s %(levelname)-7s %(name)s: %(message)s`. Все timestamp'ы в **локальной TZ**, не UTC.

---

## 6. Полезные API-запросы

```bash
# Healthcheck
curl localhost:8765/healthz

# Последние signals (engine activity)
curl -s "localhost:8765/api/signals?strategy_id=1&limit=10" | jq .

# Свежие events (engine lifecycle, position open/close)
curl -s "localhost:8765/api/events?limit=20" | jq .

# Открытые позиции
curl -s "localhost:8765/api/positions?strategy_id=1&status=open" | jq .

# Equity timeseries
curl -s "localhost:8765/api/equity?strategy_id=1&limit=2000" | jq '.[-1]'

# Funding history per coin
curl -s "localhost:8765/api/funding/BTC?limit=24" | jq .
```

---

## 7. CLI команды (`frab ...`)

| Команда | Назначение |
|---------|-----------|
| `frab init-db` | Применить Alembic миграции к head |
| `frab seed` | Вставить HL + 7 markets (idempotent) |
| `frab backfill --hours 24` | Подкачать funding history из HL в DB |
| `frab serve --port 8765` | Запустить FastAPI + engine + event sink |

---

## 8. Quick checks

```bash
# Engine жив? (должны быть свежие 'engine.started' events после каждого старта)
sqlite3 data/frab.db "SELECT ts,kind,message FROM events ORDER BY id DESC LIMIT 5;"

# Сколько funding rows в DB?
sqlite3 data/frab.db "SELECT m.coin, COUNT(*) FROM funding_rates fr JOIN markets m ON m.id=fr.market_id GROUP BY m.coin;"

# Последние signals — должны идти каждый час
sqlite3 data/frab.db "SELECT MAX(ts) FROM signals;"

# Equity snapshot — должен обновляться каждую минуту
sqlite3 data/frab.db "SELECT MAX(ts), total_equity FROM equity_snapshots;"
```

---

## 9. Разработка

```bash
# Python tests + coverage
uv run pytest src/frab -q
uv run pytest src/frab --cov=src/frab --cov-report=term-missing

# Frontend type-check + build
cd web && npm run build

# Frontend dev-сервер вручную (если выключил launchd сервис)
cd web && npm run dev
```

**Правило коммитов:** после выполнения таска — commit + push. Без `Co-Authored-By:`.

---

## 10. Troubleshooting

| Симптом | Что проверить |
|---------|---------------|
| Dashboard пуст, signals 0.0000 | DB не подкачана: `frab backfill --hours 24`, потом kickstart engine |
| Время на дашборде смещено | API должен отдавать ts с `Z` — проверь `curl … /api/signals` |
| Engine не пишет signals | `tail logs/engine.err.log`, ищи `Background task engine failed` |
| Vite не отвечает на 5173 | `launchctl print gui/$(id -u)/com.frab.web | grep state` |
| Service сразу перезапускается | `ThrottleInterval=10s` + `KeepAlive on Crashed`. Смотри err.log |
| `frab` не найден | `uv sync` в репе |

---

## 11. Что НЕ настроено (явно отложено)

- LiveExecutor (real orders на HL) — пока только PaperExecutor.
- Strategy B (stake & hedge) — заходит после стабильной A.
- Drift как 2-я биржа — adapter готов, но не интегрирован.
- Telegram/email alerts — event bus готов, sink'и добавятся позже.
- Auth/multi-user — local single-user.
