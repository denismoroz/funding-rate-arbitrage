# LST Depeg History — Funding-Rate Arbitrage Portfolio

_Research date: 2026-06-05._

**Why this matters:** In a delta-neutral funding harvest, we hold the spot leg as an LST (e.g., jitoSOL instead of SOL). The short perp tracks the underlying asset price (SOL), not the LST price. When the LST temporarily trades at a discount to the underlying ("depeg"), the gap is **unhedged loss** — the spot leg falls extra while the perp hedge does not compensate. This loss persists until repeg.

---

## 1. stETH / wstETH (Lido — Ethereum)

### June 2022 Depeg (Primary Event)
| Attribute | Value |
|-----------|-------|
| Date | May–September 2022 (peak: June 11–13, 2022) |
| Max depeg | **~7%** (stETH traded at 0.93 ETH at the worst point) |
| Trigger | Terra/LUNA collapse (May 7) → forced stETH liquidations; Celsius liquidity crisis; Alameda/FTX coordinated ~$75M stETH dump |
| Duration | ~4 months at >2% discount (May–September 2022) |
| Recovery | Partial recovery post-Celsius resolution (~2% discount by Aug 2022); near-full recovery after the Merge (Sep 15, 2022); full parity restored post-Shanghai upgrade (April 2023) |
| Systemic cause | Pre-Shanghai, stETH could not be redeemed for ETH; price was purely secondary-market supply/demand. Large holders (Celsius, 3AC) were forced to sell at whatever price. |

### Post-2022 Behavior
- Since Shanghai (April 2023 ETH withdrawals enabled): wstETH trades within **0.1–0.3%** of ETH parity continuously; redemption arb keeps it tight.
- Minor Lido node slashing event Feb 2023: <20 ETH penalty across entire protocol; no visible peg impact.
- **Since mid-2023: no depeg exceeding 0.5% observed.**

**Sources:** Nansen on-chain forensics https://www.nansen.ai/research/on-chain-forensics-demystifying-steth-depeg; CoinDesk Jun 2022 https://www.coindesk.com/business/2022/06/29/nansen-casts-blame-for-steth-de-peg-on-terra; protos.com https://protos.com/ethereums-largest-staking-service-finally-regains-steth-peg

---

## 2. rETH (Rocket Pool — Ethereum)

### June 2022
| Attribute | Value |
|-----------|-------|
| Date | June 2022 (concurrent with stETH event) |
| Max depeg | **~3–4%** (smaller and shorter than stETH) |
| Recovery | Faster than stETH; rETH hit near-parity after the Merge (Sep 2022) |
| Note | Smaller pool size = less secondary-market overhang; also less Celsius exposure than stETH |

### Post-2022 Behavior
- rETH has **consistently traded at a slight premium** to redemption value since the Merge (reflecting its scarcity and withdrawal rights).
- No notable depeg events in 2023–2025.

**Source:** Mirror.xyz analysis https://mirror.xyz/jasperthefriendlyghost.eth/pnaLyH6W4j58vfypsOKHciF_BM5HFvTkouTd9uThesM; Origin Protocol comparison https://www.originprotocol.com/liquid-staking-tokens

---

## 3. weETH (ether.fi — Ethereum Restaking)

### History
- weETH launched in late 2023; no major peg incidents in its short history.
- The token wraps eETH (rebasing) so it is "unwrapped" vs ETH, not vs staked ETH; it carries EigenLayer restaking risk on top of Lido-style validator risk.
- **No depeg exceeding ~0.5% has been publicly documented for weETH.**
- Key risk is not a peg break per se but **EigenLayer slashing** — could reduce the underlying ETH value without triggering a secondary-market depeg of the LST vs ETH. This is a different risk category.

**Source:** ether.fi docs https://etherfi.gitbook.io/etherfi/ether.fi-whitepaper/ether.fi-re-staking

---

## 4. mSOL (Marinade Finance — Solana)

### December 2023 Depeg (Primary Event)
| Attribute | Value |
|-----------|-------|
| Date | December 12, 2023 |
| Max depeg | **~15–20%** at the intra-day low (price dropped from ~$78 to ~$66 vs SOL) |
| Trigger | Single wallet sold ~68,536 mSOL (~$8M) via 9 transactions over ~20 minutes into thin DEX liquidity |
| Duration | **Intra-day** — arb bots bought back within hours; no overnight depeg |
| Recovery | Same day; price returned to fair value |
| Downstream | Mass liquidations on Kamino and Marginfi lending protocols where mSOL was used as collateral; social media debate about LST risk management |

### November 2022 (FTX Collapse)
- jitoSOL did not yet exist (launched Nov 2022 as a new protocol). mSOL experienced elevated spread against SOL but no documented severe depeg — Solana DEX liquidity was thin across the board during the FTX fallout but no single large forced seller caused a structural depeg in mSOL/SOL.

**Sources:** SolanaFloor https://solanafloor.com/news/marinade-finances-m-sol-token-depeg-triggers-major-debate-and-liquidations-in-de-fi-world; Solana Compass https://solanacompass.com/learn/Lightspeed/the-marginfi-vs-solend-debate-lessons-from-msols-depeg

---

## 5. jitoSOL (Jito — Solana)

### History
- jitoSOL launched November 2022.
- **No notable depeg events documented in 2022–2025.** The December 2023 mSOL depeg did not spill over into jitoSOL; different pool and liquidity profile.
- The token has grown to the largest Solana LST by TVL (~14.5M SOL by Jan 2025), which improves its DEX liquidity depth and reduces susceptibility to the type of thin-market sell that hit mSOL.
- jitoSOL/SOL ratio is monotonically increasing (as designed); secondary-market price in USDC fluctuates with SOL but the jitoSOL/SOL exchange rate itself has not depegged.

**Source:** Solana Compass pool stats; Jito Dec 2023 month-in-review https://www.jito.network/blog/jito-december-month-in-review/

---

## 6. sAVAX (BENQI — Avalanche)

### History
- No major depeg event found in the research window (2022–2025).
- sAVAX is redeemable for AVAX at the BENQI protocol rate (not a secondary DEX price); however it can trade at a discount on Trader Joe or other DEXs when liquidity is thin.
- BENQI docs note: "1 sAVAX is always redeemable for 1 AVAX equivalent, but may trade at a discount on secondary markets due to lower liquidity vs AVAX."
- Anchor Protocol added sAVAX as collateral (March 2022) and Aave community expanded the cap (July 2022) without incident.
- **No published depeg > 1% found; low secondary-market trading volume limits both liquidity and depeg risk relative to Ethereum LSTs.**

**Source:** BENQI docs https://docs.benqi.fi/benqi-liquid-staking/overview; Exponential.fi https://exponential.fi/assets/b4099758-b7a6-4ee3-95e2-b19d930297cb

---

## 7. kHYPE / stHYPE (Kinetiq / Hyperbeat — Hyperliquid)

### History
- kHYPE launched July 15, 2025. stHYPE is similarly young.
- **No depeg events documented** — not enough history.
- Key structural risk: Hyperliquid L1 is a new chain (launched 2024); validator set is small and relatively centralized compared to ETH/SOL. Any validator slashing or chain-level incident could affect kHYPE/HYPE parity.
- staking redemption delay is not documented in public sources — flag as unknown.

**Source:** OAK Research https://oakresearch.io/en/analyses/innovations/kinetiq-khype-catalyst-liquid-staking-hyperliquid

---

## Hedge-Break Risk Summary

| LST | Worst Depeg Observed | Duration | Chain Maturity | Liquidity Depth | Hedge Risk Rating |
|-----|----------------------|----------|----------------|-----------------|-------------------|
| wstETH | **7%** (Jun 2022) | ~4 months | High (post-Shanghai: redeemable) | Very high ($16B TVL) | **Low** post-2023; was **Critical** pre-Shanghai |
| rETH | ~4% (Jun 2022) | ~3 months | High | Medium ($700M TVL) | **Low** post-2023 |
| weETH | <0.5% (no major event) | n/a | Medium | High ($3.3B TVL) | **Low-Medium** (EigenLayer slashing adds tail risk) |
| jitoSOL | **Not depegged** (monitor only) | n/a | Medium-High | High ($938M TVL) | **Low** |
| mSOL | **~15–20%** (Dec 2023) | Intra-day | Medium-High | Medium | **Medium** (thin DEX liquidity risk) |
| sAVAX | <1% (estimated; no events found) | n/a | Medium | Low-Medium | **Low-Medium** (low secondary-market volume) |
| kHYPE/stHYPE | Unknown (< 1 year old) | n/a | Low | Low | **High (unquantifiable)** |

### Ranking: Safest to Riskiest

1. **wstETH** — post-Shanghai, redeemable on-chain; largest LST pool; deepest liquidity. The 2022 event is now structurally prevented.
2. **rETH** — even less secondary-market overhang than stETH historically; persistent premium suggests no forced-seller dynamics.
3. **jitoSOL** — no historical depeg; largest Solana LST; best DEX liquidity on Solana.
4. **weETH** — similar to wstETH but adds EigenLayer restaking tail risk.
5. **sAVAX** — no documented depeg but small ecosystem; thin DEX liquidity means a large forced seller could move price.
6. **mSOL** — one documented 15–20% intra-day depeg in Dec 2023; structural liquidity risk in thin-market conditions.
7. **kHYPE / stHYPE** — brand new (<1 year), unproven chain, no depeg history to assess. Highest uncertainty.

---

## Rough "Expected Depeg Drag" Estimates

These are rough probability-weighted annualized drags on the spot leg return, for illustrative use in the model. Methodology: P(depeg event per year) × average magnitude × (days duration / 365).

| LST | P(event/yr) | Avg magnitude if event | Avg duration | Est. annual drag |
|-----|-------------|------------------------|--------------|-----------------|
| wstETH | ~2% | 1% (minor; major events structurally prevented) | 3 days | **~0.02 bp** |
| rETH | ~2% | 1% | 2 days | **~0.01 bp** |
| weETH | ~3% | 1.5% (incl. restaking tail) | 3 days | **~0.04 bp** |
| jitoSOL | ~5% | 3% (hypothetical; extrapolated from mSOL) | 0.5 days | **~0.02 bp** |
| mSOL | ~15% | 5% (observed: 1 event in ~2 years) | 0.5 days | **~0.10 bp** |
| sAVAX | ~5% | 2% | 1 day | **~0.03 bp** |
| kHYPE | **unquantifiable** | — | — | **Flag; use zero for model, but treat yield as uncertain** |

_These drags are all very small relative to the staking yield itself (2–6.5%), so depeg drag is not a dominant risk in the model. The bigger risk from depegs is the tail scenario: a severe correlated market stress event (like June 2022) where a large sustained depeg coincides with a period of negative funding — compounding losses. This is a scenario-level risk, not an expected-value calculation._

---

## Key Recommendation for Model

- **Use jitoSOL for SOL** (largest pool, deepest Solana liquidity, no depeg history). Use mSOL only if jitoSOL capacity unavailable.
- **Use wstETH for ETH** (post-Shanghai safety; deepest pool). rETH is acceptable alternative.
- **Use sAVAX for AVAX** (only option; low documented risk; accept thin-liquidity tail).
- **For HYPE: proceed with caution.** kHYPE yield is real but the protocol is <1 year old and the chain is new. Model it at a conservative 2.0–2.2% and flag as "unvalidated by history."
- **BTC/LINK/DOGE: 0% staking.** Confirmed no practical LST for hedged spot leg.
