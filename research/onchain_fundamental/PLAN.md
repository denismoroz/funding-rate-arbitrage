# PLAN — Cross-Sectional Fundamental Momentum (onchain_fundamental)

## Pre-registered 2026-06-25

### Hypothesis

"Cross-sectional fundamental momentum": crypto tokens whose **real on-chain economics
(fees/revenue) are GROWING** outperform those whose fees are shrinking.

Long the top tercile by fee-growth, short the bottom, dollar-neutral, weekly rebalance.
Candidate THIRD orthogonal factor distinct from price-momentum (XSMOM) and carry/funding
(FRAB), because the input is protocol economics — not price.

### Null hypothesis (what likely happens)

(a) Price already reflects fundamentals → no edge (alpha=0).
(b) Fee-growth is just price-momentum in disguise → |corr| with XSMOM-proxy > 0.3
    → not a new axis.
(c) ~2yr usable history + ~10-18 coins → statistical power too low to clear DSR > 0.95.

### Universe

Two groups (normalized separately, then concatenated for ranking):

**DeFi app-revenue tokens (~10):**  
AAVE (keyword: "aave"), UNI ("uniswap"), JUP ("jupiter"), ENA ("ethena"),
JTO ("jito"), CRV ("curve"), LINK ("chainlink"), EIGEN ("eigen"),
PENDLE ("pendle"), ZRO ("layerzero").

**Chain gas-fee tokens (~8):**  
ETH ("ethereum"), SOL ("solana"), TRX ("tron"), BNB ("bsc"),
ARB ("arbitrum"), AVAX ("avalanche"), SUI ("sui"), INJ ("injective").

### Data source

DefiLlama public API (no key):
- `GET https://api.llama.fi/overview/fees` → protocol list with slugs
- `GET https://api.llama.fi/summary/fees/{slug}?dataType=dailyFees` → daily fee series

Aggregation: sum daily series of ALL protocols whose name contains the keyword
(case-insensitive). Handles multi-version protocols (Aave V2+V3, Uniswap V2+V3, etc.).
Cached in `data/raw_*.json` (gitignored if large).

### Signal design

1. Build daily panel `fees[date, coin]` (USD, token-level aggregated).
2. Signal = **fee-growth ensemble**: average of log-growth over 3 lookbacks:
   - `log(trailing_30d_fees[t] / trailing_30d_fees[t-30])`
   - `log(trailing_60d_fees[t] / trailing_60d_fees[t-60])`
   - `log(trailing_90d_fees[t] / trailing_90d_fees[t-90])`
   
   Each computed as: sum of daily fees in window [t-N+1, t] vs sum in [t-2N+1, t-N].
   NO look-ahead: signal at t uses fees with index <= t only.
   
3. Z-score WITHIN group (DeFi vs DeFi, chain vs chain) using `xsec.zscore_cross_section`,
   then concatenate the two group z-scores into one panel.
4. Weights via `xsec.rank_to_weights` (tercile, dollar-neutral).
5. PnL via `xsec.portfolio_returns(costs_bps=4.4, rebal_every=7)`.
6. Price/fwd_ret from `cryptodata.load_panel(coins=[...])`.

### Verdict gates (pre-committed)

GO requires ALL of:
- DSR > 0.95 (Sharpe survives multi-test deflation)
- PBO < 0.20 (best IS config transfers OOS)
- |corr(book_pnl, XSMOM-proxy)| < 0.30
- |corr(book_pnl, FRAB-carry-proxy)| < 0.30
- |corr(book_pnl, BTC-buyhold)| < 0.30

### Known limitations / caveats

1. **Survivorship bias**: all 18 coins are alive today; DefiLlama may restate history.
   Cannot fully fix; treated as a caveat on IS results.
2. **Thin early cross-section**: ENA, EIGEN, JUP, ZRO, JTO are 2024+ listings.
   Effective usable cross-section >= 8 coins begins ~2024-Q1.
3. **Point-in-time fee reporting**: fees may be restated by DefiLlama post-hoc.
4. **Short history**: ~2yr full cross-section is the known statistical-power limiter.
   Low DSR is the expected null outcome given this (see null (c)).
5. **Fee ≠ price**: fee-growth signal is economically distinct from price return,
   but operational correlation to price-momentum is an empirical question.

### Self-tests (correctness invariants)

(a) **Cheat test**: feed fwd_ret as signal → must produce large positive Sharpe
    (proves weight[t] earns fwd_ret[t]).
(b) **No-look-ahead test**: shift signal +1 day (use future fees) → must change PnL
    (proves temporal alignment is real).
(c) **Deterministic pipeline test**: hand-computed growth+zscore+weights on a small
    panel, compare to implementation.

### Files

- `PLAN.md` — this file (pre-registration)
- `fees_data.py` — DefiLlama fetch, aggregate, cache, panel builder
- `fees_signal.py` — growth signal + z-score + ensemble
- `onchain_pkg.py` — harness package adapter
- `selftest.py` — self-tests (a/b/c above)
- `run_onchain.py` — main runner (harness + orthogonality) → `run_onchain.json`
- `FINDINGS.md` — verdict table, caveats, GO/NO-GO

### Orthogonality prize

If |corr(book_pnl, XSMOM-proxy)| > 0.3 → fee-growth ≈ price-momentum, not a new axis.
If |corr(book_pnl, FRAB-carry-proxy)| > 0.3 → driven by same HL-funding dynamics.
Orthogonality is the main prize; Sharpe > 0 alone is insufficient.
