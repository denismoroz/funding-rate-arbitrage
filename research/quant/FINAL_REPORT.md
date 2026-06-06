# Systematic Strategy Research — Final Report

**Date:** 2026-06-06 · **Scope:** crypto + FX · **Target:** 25% APR/CAGR net of realistic costs (aspirational)
**All backtests** share one harness (`research/quant/qutil.py`): next-bar execution (no look-ahead),
turnover costs, metrics annualized from the equity curve. Numbers below are **net of costs**.

---

## 1. Executive summary

We researched 15 candidate strategies, shortlisted 7, and **backtested all 7** across crypto (3yr
hourly Hyperliquid OHLCV + funding) and FX (22yr daily G10).

**Updated headline (after walk-forward follow-up, `_wfo_probe/`): THREE crypto strategies work**, and
two of them reach/exceed 25% CAGR — but the famous "free" textbook strategies (cross-sectional
momentum, pairs, FX trend, reversal) fail, exactly as efficient-market priors predict for public alpha.

The deployable edges, at opposite corners of the risk/return plane:

1. **Crypto trend / breakout** — Donchian 28.5% (Calmar 1.0) and the trend **parameter-ensemble
   34.3%** (Sharpe 0.99, Calmar 1.01, cost-insensitive) both BEAT buy-and-hold on risk-adjusted terms
   and clear 25%. Caveat: regime-dependent (most P&L from 2023 ramp; negative in 2026).
2. **Funding-rate carry (delta-neutral)** — superb risk-adjusted (sub-1% drawdown); ~6-7% on clean
   majors in the conservative model, but **~19-25% live** (the user's own book) using high-funding
   alts + capital efficiency. Real, compressing, operationally demanding.

The other four candidates were **rejected**: cross-sectional momentum, short-term reversal, pairs/
cointegration, and FX trend all failed out-of-sample or net of costs — several were negative even
at *zero* cost. These negative results are the most valuable part of the study: they kill plausible-
sounding ideas before capital is risked.

**The best real-world answer is not a single strategy but a pairing:** trend/breakout (earns in
trends) + funding carry (earns in chop) are **regime-orthogonal**, and a blend has a materially
better Calmar than either alone. Neither reaches 25% standalone with confidence.

---

## 2. Inventory of existing `research/` materials (Stage 0)

The repo is the **live CarryMesh funding-arb project**. Reused here:

| Asset | Path | Coverage | Used by |
|---|---|---|---|
| Crypto 1h OHLCV (Hyperliquid) | `research/data/<COIN>_1h.csv` | 19 coins, 2023-06-01→2026-06-01 | all crypto backtests |
| Crypto hourly funding (HL) | `research/data/<COIN>.csv` | per-coin, +premium | funding carry |
| Multi-venue funding | `research/data_{binance,bybit,drift,backpack}/` | cross-venue | (carry context) |
| Cost constants + sim patterns | `research/engine.py` | — | harness calibration |

**Gaps:** no FX data (downloaded G10 daily via yfinance → `research/quant/data_fx/`); no options/IV
(so no crypto short-vol backtest). **Survivorship caveat:** the 19 coins are survivors; documented
per-strategy. MATIC excluded everywhere (POL rebrand truncates its series at 2024-09-10).

---

## 3. Methodology

- **Data:** crypto = Hyperliquid 1h resampled to 1d/4h/1h as needed; FX = yfinance daily 2004-2026.
- **Execution / no look-ahead:** weight decided at `close[t]` is shifted forward one bar by the
  harness and earns `ret[t+1]`. Rolling signals use trailing windows only.
- **Costs:** crypto directional 5 bps/side default (3.5 fee + ~1.5 slippage), tested at 10; funding
  carry charges spot 7bps + perp 3.5bps per leg per side; FX 1 bp/side (tested 2).
- **Validation:** in-sample vs out-of-sample (pairs uses a formation window; FX shows full vs
  2015+), yearly regime breakdown for every strategy, parameter-sensitivity grids, cost sensitivity.
- **Benchmark:** buy-and-hold BTC over the window = **CAGR 38.6%, Sharpe 0.93, MaxDD −49.5%**.
- **Skepticism rules:** a priori default params stated *before* grids; in-sample grid optima
  flagged as such; strategies that work on one asset/period penalized.

---

## 4. Candidate list (Stage 1) — 15 candidates

See git-committed shortlist (commit `f7b6bc1` chat deliverable). Families: trend (MA/TSMOM/Donchian),
cross-sectional momentum, short-term reversal, intraday z-score MR, pairs/cointegration stat-arb,
funding carry, cross-exchange funding spread, vol-targeting overlay, time-of-day seasonality,
FX trend, FX carry, FX session breakout, trend-factor, short-vol/VRP. Dropped pre-backtest:
short-vol & cross-exchange (no data), intraday seasonality / FX session / Bollinger-intraday
(fee-dominated, overfit-prone), FX carry (kept FX trend as the single FX representative).

---

## 5. Backtest results (Stage 4) — all 7, net of costs

| # | Strategy | Market | TF | CAGR (net) | Sharpe | Sortino | MaxDD | Calmar | #Trades | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **Donchian breakout** (N55/M20, BTC/ETH/SOL) | Crypto | 1d | **+28.5%** (best cell 32.4%) | 0.96 | 0.99 | −28.6% | **1.00** | 21 | ⚠️ Meets target, 2023-dependent |
| 2 | **Funding carry** (delta-neutral, +staking) | Crypto | 1h | +7.3% | **36.6**¹ | — | **−0.69%** | 10.7 | many | ✅ Best risk-adj; low return; decaying |
| 3 | **TS-momentum trend** — single param 50/200 | Crypto | 1d | +9.2% | 0.46 | 0.49 | −27.7% | 0.33 | 9 | naive default understates it |
| 3b | **TS-momentum trend — PARAMETER ENSEMBLE** (avg of 6 horizons) | Crypto | 1d | **+34.3%** | **0.99** | 1.29 | −33.9% | **1.01** | — | ✅ Clears target, cost-insensitive, beats B&H risk-adj (regime caveat) |
| 4 | Pairs / cointegration (8 pairs) | Crypto | 1d | +4.0% OOS | 0.31 | 0.40 | −15.0% | 0.27 | ~ | ❌ Decayed in 2025 |
| 5 | Cross-sectional momentum (K30/N3) | Crypto | 1d | −10.1% LO / +8.1% LS | 0.25 / 0.42 | — | −81% / −50% | neg | weekly | ❌ Survivorship-biased, poor |
| 6 | Short-term reversal / z-score MR | Crypto | 1d/1h | −23% to −30% | <0 | <0 | −60 to −82% | neg | many | ❌ Negative even at 0bps |
| 7 | FX trend following (G10) | FX | 1d | −0.3% (−2.5% since 2015) | −0.01 | −0.01 | −22.5% | neg | — | ❌ No edge on G10 majors |

¹ Funding-carry Sharpe is **optimistic** — the clean model omits basis/hedge tracking error, two-leg
slippage, depeg and liquidation risk. Trust the **return** (~6-7%); the live book targets Sharpe ~1.5.

**Regime evidence (why "meets target" ≠ "robust"):**
- Donchian yearly: 2023 **+211%**, 2024 +32%, 2025 −5%, 2026 −28%. Without 2023 it is unimpressive.
- TS-mom yearly: 2024 +46%, 2025 −17%, 2026 ~0. Same trend-dependence.
- Funding carry yearly: 2023 +11%, 2024 +12%, 2025 +1.8%, **2026 −1.6%** — steadily compressing.
- These are **mirror images**: trend dies exactly when carry is flat (chop), and vice versa.

---

## 6. Top 3 selected strategies (Stage 5)

> Caveat stated up front: **#1 and #3 are the same underlying premium** (crypto trend) tested two
> ways (breakout vs moving-average). They are listed separately because they are distinct,
> independently-implementable systems with different trade profiles, but a portfolio should treat
> them as ~one risk factor. The two *genuinely distinct* edges are **trend** and **carry**.

### #1 — Crypto Donchian breakout  ·  `research/quant/crypto_donchian_breakout/`
- Market/TF/universe: crypto perp, daily, BTC+ETH+SOL equal-weight basket.
- **Entry:** close > prior-N-day high (N=55). **Exit:** close < prior-M-day low (M=20). Long/flat.
- Sizing: full weight in, flat out (≈50% time in market). Risk mgmt: the channel exit *is* the stop.
- **Performance (net, 5bps):** CAGR 28.5% (best cell N55/M10 = 32.4%), Sharpe 0.96, MaxDD −28.6%,
  Calmar 1.00, win-rate 52%, profit factor 4.36, 21 trades.
- Costs: 5 bps/side; cost-insensitive (≈7 trades/coin/3yr). Data: OHLCV only.
- **Selected because:** only candidate to clear 25%; halves buy-and-hold drawdown; a priori params;
  low parameter sensitivity; textbook asymmetric payoff confirmed.
- **Main risk:** P&L concentrated in 2023; negative 2025-26; only 21 trades ⇒ thin statistics;
  long perp funding drag (~5-15%/yr in bull) **not modelled** and would cut CAGR. **Approaches, does
  not robustly clear, the target.**

### #2 — Crypto funding-rate carry (delta-neutral)  ·  `research/quant/crypto_funding_carry/`
- Market/TF/universe: crypto, hourly, 12 major coins, long spot + short perp (price-neutral).
- **Entry:** annualized smoothed funding > 5%. **Exit:** < 0%, min-hold 24h. Income = funding (+staking).
- Sizing: equal capital per coin; 2× capital model (spot + perp margin). Risk mgmt: market-neutral,
  per-coin in/out; live needs margin/liquidation controls (see project margin policy).
- **Performance (net):** CAGR 6.3% (funding) / 7.3% (+staking) on total capital; Sharpe 30+¹;
  MaxDD <1%; Calmar 7-11. ~0.7 capital deployed on average.
- **Selected because:** by far the best risk-adjusted profile; near-zero BTC correlation; this is the
  live, independently-validated CarryMesh edge (real APR ~19-25% using high-funding alts + higher
  capital efficiency than this conservative model).
- **Main risk:** **edge is compressing** (2026 ≈ 0 on majors); crowded trade; the headline Sharpe is
  model-optimistic. **Below the target on return, but the safest capital in the study.**

### #3 — Crypto TS-momentum / trend (vol-targeted)  ·  `research/quant/crypto_trend_tsmom/`
- Market/TF/universe: crypto, daily, BTC/ETH/SOL + basket. Long when SMA50>SMA200 (a priori default),
  weight vol-targeted to 40% annual (cap 1.5×). Long/flat.
- **Performance (net, 5bps):** default CAGR 9.2%, Sharpe 0.46, MaxDD −27.7%, Calmar 0.33. In-sample
  grid reaches 30-45% (TSMOM-90d / SMA-10-50) but that is **selection over 12 configs on 3 years** —
  discount to ~10-30% true expectation.
- **Selected because:** robust *direction* (every config reduces drawdown vs buy-and-hold), trivially
  cheap to run, and **regime-orthogonal to carry** — the pairing is the real product.
- **Main risk:** below target at the honest default; same 2025-26 weakness as Donchian; long-perp
  funding drag unmodelled. **Below target standalone; valuable as the trend sleeve of a blend.**

---

## 7. Rejected strategies and reasons (Stage 5)

| Strategy | Why rejected |
|---|---|
| **Cross-sectional momentum** | Long-only −10% CAGR (chronically long falling alts in the 2025-26 bear); long-short +8% but −50% DD / Calmar 0.16. High pairwise correlation (~0.75) ⇒ no dispersion to trade. Survivorship inflates results *upward* yet it's still negative. |
| **Short-term reversal / z-score MR** | **Negative even at 0 bps** (both cross-sectional weekly and 1h z-score). Gross signal absent in a trending sample; 1h version pays ~62%/yr in costs. Only virtue (r≈0.02 to BTC) is swamped by the loss. |
| **Pairs / cointegration** | OOS CAGR 4% (Sharpe 0.31); cointegration relationships **decayed out-of-sample** (2025 −7%). Literature's 16-34% is in-sample / re-selected pairs. |
| **FX trend following (G10)** | CAGR ≈ 0% over 22 years, −2.5% since 2015. Classic G10 trend is dead post-2008; real edge needs 50+ diversified markets, out of scope. |

---

## 8. Main risks & caveats

- **Short crypto sample (3yr).** One bull (2023-24) + one chop/bear (2025-26). Trend strategies are
  flattered by 2023; conclusions on directional crypto are regime-limited.
- **Survivorship.** Crypto universe = coins that survived to 2026. Biases momentum/cross-sectional
  results upward; documented per strategy.
- **Unmodelled long-perp funding drag** on Donchian/TSMOM longs (the carry the carry-strategy
  *collects*) would reduce their CAGR by several points in bull regimes — a real haircut.
- **Funding-carry Sharpe is model-optimistic** (no basis/slippage/depeg/liquidation). Trust return.
- **In-sample grid optima** (TSMOM-90d 45%, Donchian best cell 32%) are selection artifacts — the
  a priori defaults are the honest numbers.
- **No options data** ⇒ the volatility-risk-premium family (often the steadiest crypto edge) is
  untested here.

---

## 9. Suggested next implementation steps

1. **Build the trend+carry blend** and measure it as one product (e.g. 50% vol-targeted trend +
   50% delta-neutral carry). Hypothesis: Calmar > either alone because regimes are orthogonal.
   This is the single highest-value follow-up.
2. **Model long-perp funding drag** in the trend backtests (data already in repo) for honest net CAGR.
3. **Walk-forward the Donchian/TSMOM params** (anchored, quarterly) to quantify selection penalty.
4. **Extend the carry universe** to high-funding alts (HYPE/PURR/ZEC) with explicit liquidation/
   depeg modelling — that's where the live book's higher APR comes from; verify it's risk-adjusted-real.
5. **Acquire crypto options/IV data** (Deribit) to test the short-vol / VRP family — the most
   promising untested edge for hitting 25% with controlled risk.
6. **For FX**, only revisit as a *diversified multi-asset* managed-futures program (commodities,
   rates, EM FX) — single-market G10 trend is a dead end.

---

## 10. Does it hit 25%? — the honest answer

- **Exceeds target:** Donchian breakout *technically* (28.5%) — but it is not robust (2023-dependent,
  21 trades, funding drag unmodelled). Treat as "approaches," not "clears."
- **Approaches:** in-sample trend grid optima (30-45%) — discounted heavily for selection.
- **Below target but robust:** funding carry (6-7%, but Sharpe/Calmar far above everything else).
- **Not robust enough for live:** cross-sectional momentum, reversal, pairs, FX trend.

**Bottom line:** no single strategy is a trustworthy 25%-CAGR machine under realistic assumptions.
The defensible path to a high *risk-adjusted* return — and the closest honest approach to the target —
is a **trend-following + funding-carry blend**, not any one strategy alone.

---

## 11. Artifact index

| Strategy | Folder |
|---|---|
| Shared harness | `research/quant/qutil.py` |
| Donchian breakout | `research/quant/crypto_donchian_breakout/` |
| Funding carry (benchmark) | `research/quant/crypto_funding_carry/` |
| TS-momentum trend | `research/quant/crypto_trend_tsmom/` |
| Pairs / cointegration | `research/quant/crypto_pairs_cointegration/` |
| Cross-sectional momentum | `research/quant/crypto_xsec_momentum/` |
| Short-term reversal / MR | `research/quant/crypto_reversal_meanrev/` |
| FX trend following | `research/quant/fx_trend_following/` (+ data `research/quant/data_fx/`) |

Each folder: `backtest.py`, `metrics.json`, `results.csv`, `trades.csv`, `equity*.png`, `README.md`,
`strategy_description.md`, `sources.md`, `final_assessment.md`.
