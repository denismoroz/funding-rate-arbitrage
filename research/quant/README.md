# research/quant — Systematic Strategy Research

This folder holds a **broad** algorithmic-strategy research effort (trend, mean-reversion,
breakout, momentum, carry, volatility, stat-arb, intraday) across **crypto and FX**, distinct
from the existing funding-rate-carry work in `research/` (the live CarryMesh project).

Goal: identify the 3 most promising systematic strategies, validated by backtest, after
realistic costs. Aspirational target: **25% APR/CAGR net of costs** — not a hard requirement.

## Status: Stage 0–2 complete (inventory, candidates, shortlist). Awaiting approval to backtest.

Each strategy that we backtest gets its own folder:
`research/quant/<strategy_name>/` containing `README.md`, `strategy_description.md`,
`sources.md`, `backtest.py`, configs, `results.csv`, `trades.csv`, `metrics.json`, plots,
`final_assessment.md`.

## Reusable data (already in repo — see Stage 0 inventory below)

| Dataset | Path | Coverage | Use |
|---|---|---|---|
| Crypto 1h OHLCV (Hyperliquid) | `research/data/<COIN>_1h.csv` | 19 coins, 2023-06-01 → 2026-06-01 | trend / MR / breakout / statarb |
| Crypto funding (HL) | `research/data/<COIN>.csv` | per-coin funding+premium | carry / basis |
| Crypto funding (multi-venue) | `research/data_{binance,bybit,drift,backpack}/` | cross-venue funding | funding-spread arb |
| Staking yields | `research/staking/` | per-coin staking APR | carry net-yield |

**No FX data is present** — must be downloaded (Dukascopy / HistData / yfinance) for any FX strategy.

## Cost assumptions (baseline, Hyperliquid-class venue)
- Perp taker 3.5 bps, spot taker 7 bps (from `research/engine.py`).
- Add slippage per trade; funding paid/received where a position is held across funding stamps.
- FX: spread-based cost (e.g. 0.2–0.8 bps majors) + commission; specified per-strategy.

See chat deliverable / git history for the full candidate table and shortlist rationale.
