# Sources — crypto funding-rate carry (delta-neutral)

- Funding-rate arbitrage delta-neutral guide (8–20% APY range; competition compressing):
  https://arbitragescanner.io/blog/crypto-funding-rate-arbitrage-guide
- "The Two-Tiered Structure of Cryptocurrency Funding Rate Markets", MDPI Mathematics 2026:
  https://www.mdpi.com/2227-7390/14/2/346
- Hedge Fund Journal — Liquibit market-neutral crypto (notes spot-vs-futures carry now <20% of
  book as competition rose): https://thehedgefundjournal.com/liquibit-market-neutral-crypto-strategy-traditional-trading/

Internal: this is the live CarryMesh strategy. See research/engine.py and project memory
(Strategy A). The clean model here is intentionally conservative vs the live book.

Data: research/data/<COIN>.csv (Hyperliquid hourly funding). Universe = 12 majors with full
history (MATIC excluded — POL rebrand). Staking yields are practitioner estimates.
