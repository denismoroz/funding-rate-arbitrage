# Staking Rates Research — Funding-Rate Arbitrage Portfolio

_Research date: 2026-06-05. Covers 2025–early-2026 window (our backtest period)._

---

## Per-Coin LST Rate Table

| Coin | Primary LST | Conservative APR | Recent APR (Jun 2026) | Source + Date | Notes |
|------|-------------|------------------|-----------------------|---------------|-------|
| SOL  | jitoSOL (Jito) | **6.5%** | 5.62–7.71% | Solana Compass / Jito Network, Jun 2026 | Base ~5–6% + MEV boost ~1–2%. Mid-2024 was 8%+; cooling as stake grows |
| SOL  | mSOL (Marinade) | **5.8%** | 6.1% | StakePoint blog, 2026 | No MEV capture; slightly lower than jitoSOL; liquid redemption |
| AVAX | sAVAX (BENQI) | **4.5%** | ~4.97% | StakingRewards / CoinMarketCap, Jun 2026 | Avalanche base staking; variable, historically 4.5–5.5% |
| ETH  | wstETH (Lido) | **2.5%** | 2.38–2.45% | vaults.fyi Jun 4 2026; Everstake 2024 report | Network APR fell from ~4.3% (Jan 2024) → ~3.1% (Dec 2024) → ~2.5% (2026) as validator count grew |
| ETH  | rETH (Rocket Pool) | **2.0%** | 1.99% | vaults.fyi Jun 4 2026 | Lower than stETH due to smaller pool / fewer MEV opportunities |
| ETH  | weETH (ether.fi) | **2.5%** | 2.45–3.1% | vaults.fyi Jun 4 2026; ether.fi docs Sep 2025 | Base = same as stETH; restaking layer adds ~0–0.5% real ETH yield (EigenLayer emissions have declined sharply) |
| HYPE | kHYPE (Kinetiq) | **2.2%** | ~2.37% | MEXC/TechBullion 2026; Kinetiq Jul 2025 launch | Base HYPE validator rate ~2.18–2.37%; young protocol, launched Jul 15 2025 |
| HYPE | stHYPE (Hyperbeat) | **2.0%** | 2.44% | StakingRewards 2026 | Similar to kHYPE; smaller TVL competitor |
| BTC  | — | **0%** | — | — | PoW; no native staking. Wrapped BTC yield protocols (Lombard, Solv) are cross-chain credit risk, not suitable as a clean spot leg |
| LINK | stLINK (stake.link) | **~4%** | ~4–4.3% | stake.link / Everstake 2025 | Pool capacity capped at 45M LINK; 28-day unbonding; impractical for hedged spot (see below) |
| DOGE | — | **0%** | — | — | PoW / no PoS staking mechanism; no liquid staking exists |

---

## Key Sources

- Solana Compass jitoSOL pool stats — https://solanacompass.com/stake-pools/Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb
- StakePoint jitoSOL vs mSOL 2026 comparison — https://stakepoint.app/blog/jitosol-vs-msol-vs-bsol-best-solana-liquid-staking-2026
- vaults.fyi ETH staking APY (updated Jun 4 2026) — https://blog.vaults.fyi/eth-staking-yield/
- StakingRewards sAVAX — https://www.stakingrewards.com/asset/benqi-liquid-staked-avax
- Kinetiq (kHYPE) analysis — https://oakresearch.io/en/analyses/innovations/kinetiq-khype-catalyst-liquid-staking-hyperliquid
- MEXC kHYPE staking guide 2026 — https://www.mexc.com/news/712558
- Figment MEV all-time highs Nov 2024 — https://www.figment.io/insights/jito-solana-and-maximal-extractable-value-mev-driving-all-time-high-staking-reward-rates-with-figment/
- Everstake ETH 2024 Annual Report — https://everstake.one/crypto-reports/ethereum-2024-staking-insights-and-analysis
- Chainlink staking v0.2 capacity / cooldown — https://chain.link/economics/staking

---

## Why BTC, DOGE, and LINK Are Rated 0% (No Practical LST for Hedged Spot)

**BTC:** Proof-of-Work; no native staking. Liquid BTC yield wrappers (Lombard Protocol, Solv Protocol) work by bridging BTC to EVM chains and restaking it — the spot leg is no longer simple BTC, it is a cross-chain derivative with bridge and smart-contract risk. Unacceptable for a delta-neutral strategy where clean spot-vs-perp hedging is required.

**DOGE:** Proof-of-Work; merged-mined with Litecoin. No staking mechanism exists. There is no credible liquid staking protocol for DOGE.

**LINK (Chainlink v0.2):** Native LINK staking exists and yields ~4–4.3%, but it is impractical as a liquid spot leg because:
1. The community pool is capped at 45M LINK and has been at capacity since late 2023 — new entrants cannot stake without waiting for a vacancy.
2. Unbonding requires a 28-day cooldown followed by a 7-day claim window — during that window you cannot exit the hedge quickly if funding reverses.
3. stake.link (stLINK LST) offers faster liquidity but is thin ($<200M TVL) and re-introduces smart-contract risk on top of the LINK staking risk.
For these reasons we use 0% staking APR for LINK in the model.

---

## Comparison to Our Model Guesses

Current model (`research/portfolio_50k_model.py`) uses:
```
SOL 7.5%, AVAX 5.0%, ETH 3.0%, HYPE 2.5%, BTC/LINK/DOGE 0%
```

| Coin | Model Guess | Conservative Real | Assessment |
|------|-------------|-------------------|------------|
| SOL  | 7.5% | 6.5% | **Optimistic by ~1pp.** 7.5% was achievable at MEV peak (late 2024) but current jitoSOL is 5.6–7.7%; 6.5% is a defensible 2025-window average. Our guess errs high. |
| AVAX | 5.0% | 4.5% | **Slightly optimistic.** sAVAX historically tracks Avalanche validator rate (~4.5–5.5%); current is ~4.97%. Guess is inside the range but near the top. |
| ETH  | 3.0% | 2.5% | **Optimistic by ~0.5pp.** ETH network staking yield compressed steadily through 2024-2026 as validator count grew. 3.0% was achievable in early-to-mid 2024 but the current run-rate is 2.38–2.45%. |
| HYPE | 2.5% | 2.2% | **Slightly optimistic.** kHYPE/stHYPE yield ~2.37–2.44% currently, but kHYPE only launched July 2025 so there is no 2024 history. Base validator rate is 2.18%. Our 2.5% is above observed rates; flag as uncertain. |
| BTC/LINK/DOGE | 0% | 0% | **Correct.** No practical LST yield for hedged spot. |

**Bottom line:** The model is ~0.5–1.0 pp optimistic on SOL, AVAX, and ETH staking. Adjusting to conservative numbers reduces blended portfolio staking contribution by roughly 20–25 bps of gross return. This is a modest but real overstatement to correct before final allocation decisions.
