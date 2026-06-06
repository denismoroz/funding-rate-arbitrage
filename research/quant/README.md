# research/quant — Systematic Strategy Research

This folder holds a **broad** algorithmic-strategy research effort (trend, mean-reversion,
breakout, momentum, carry, volatility, stat-arb, intraday) across **crypto and FX**, distinct
from the existing funding-rate-carry work in `research/` (the live CarryMesh project).

Goal: identify the 3 most promising systematic strategies, validated by backtest, after
realistic costs. Aspirational target: **25% APR/CAGR net of costs** — not a hard requirement.

## Status: COMPLETE. All 7 strategies backtested. See **[FINAL_REPORT.md](FINAL_REPORT.md)**.

**Result in one line:** no single strategy is a robust 25%-CAGR machine net of costs. Two real
edges — crypto **trend/breakout** (Donchian 28.5% CAGR but 2023-dependent) and delta-neutral
**funding carry** (6-7% CAGR, Sharpe ≫, MaxDD <1%, but compressing) — are regime-orthogonal; the
defensible play is a **blend**. Cross-sectional momentum, short-term reversal, pairs/cointegration,
and FX trend were all **rejected** (failed OOS or net of costs; several negative even at zero cost).

| Strategy | CAGR (net) | Sharpe | MaxDD | Calmar | Verdict |
|---|---|---|---|---|---|
| Donchian breakout | 28.5% | 0.96 | −28.6% | 1.00 | ⚠️ meets target, regime-dependent |
| Funding carry (delta-neutral) | 6-7% | 30+* | <1% | 7-11 | ✅ best risk-adj, low return |
| TS-momentum trend | 9.2% | 0.46 | −27.7% | 0.33 | ⚠️ below target, drawdown-reducer |
| Pairs / cointegration | 4.0% OOS | 0.31 | −15% | 0.27 | ❌ decayed 2025 |
| Cross-sectional momentum | −10%/+8% | 0.25/0.42 | −81/−50% | neg | ❌ reject |
| Short-term reversal/MR | −23 to −30% | <0 | <−60% | neg | ❌ reject |
| FX trend (G10) | ~0% | −0.01 | −22.5% | neg | ❌ reject |

\* funding-carry Sharpe is model-optimistic (omits basis/slippage/depeg); trust the ~6-7% return.

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
