# Final assessment — Crypto funding-rate carry (delta-neutral) [BENCHMARK]

**Verdict: BELOW the 25% CAGR target on a clean major-coin universe (~6–7% CAGR), but
BEST-IN-CLASS risk-adjusted (Sharpe 30+, MDD <1%). Edge is real but compressing (2026 ~0).**

## Results (full sample 2023-06 → 2026-06, hourly)
| Variant | On TOTAL capital | On DEPLOYED capital |
|---|---|---|
| Funding only | CAGR **6.3%**, Sharpe 31.8, MDD −0.87%, Calmar 7.2 | CAGR 5.3%, Sharpe 17.5, MDD −2.0% |
| Funding + staking | CAGR **7.3%**, Sharpe 36.6, MDD −0.69%, Calmar 10.7 | CAGR 6.6%, Sharpe 21.6, MDD −1.7% |

Avg fraction of capital deployed: 0.73. Yearly (funding only): 2023 +10.7%, 2024 +12.0%,
**2025 +1.8%, 2026 −1.6%** — clear funding compression on majors.

## Interpretation
- Delta-neutral ⇒ near-zero correlation to BTC and ~1% max drawdown. The Sharpe is enormous
  precisely because realized vol is ~0.2% — this is a yield product, not a directional bet.
- **The headline Sharpe (30+) is optimistic.** This clean model omits: basis/hedge tracking
  error, execution slippage on two legs, spot-vs-perp price gaps at entry, stablecoin/staking
  depeg risk, and liquidation/margin events. Realistic live Sharpe is far lower (the live
  CarryMesh book targets ~1.5). Treat the **return** (~6–7%) as the trustworthy number and
  discount the Sharpe heavily.
- **Why the live project reports higher APR (~19–25%):** it trades high-funding alts
  (HYPE, PURR, ZEC) absent from this clean major universe, and runs at higher capital
  efficiency (unified margin) than this conservative 2× capital model. Those levers raise
  return but also raise idiosyncratic/liquidation risk. The clean major-coin number here is
  the conservative floor.

## Honesty notes
- Capital charged at 2× (spot + perp margin, 1×) — avoids the APR-inflation of crediting
  funding against perp margin only.
- Costs: spot 7bps + perp 3.5bps taker, each leg, on entry and exit. No look-ahead (signal at
  t from funding through t; income accrues t→t+1; costs at the state-change bar).
- Survivorship: majors that survived; high-funding-alt sleeve deliberately excluded to keep it
  clean and conservative.

## Role in the study
Included as the **incumbent benchmark**. It defines the "low-return / superb-risk-adjusted"
corner of the opportunity set that the directional strategies must be judged against.
