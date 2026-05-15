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
uv run frab serve --host 127.0.0.1 --port 8765 \
  --coins BTC,ETH,SOL,AVAX,LINK,AAVE,DOGE

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
