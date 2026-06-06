# Sources & Literature

## Primary Academic Reference

**"Two-Tiered Structure of Cryptocurrency Funding Rate Markets"**  
MDPI Mathematics 14(2), 346 (2025)  
URL: https://www.mdpi.com/2227-7390/14/2/346  
Relevance: Empirical study of funding rate dynamics across crypto venues. Documents that funding rates exhibit persistent extremes that correspond to crowded positioning and subsequently revert. Provides theoretical basis for the contrarian signal used here.

## Supporting Literature

**Funding Rate Extremes as Contrarian Indicators**  
The core mechanism is well-documented in practitioner research: high positive funding → over-leveraged long positioning → mean reversion risk. Key references:

- Coinglass research portal (https://www.coinglass.com/): provides real-time and historical funding rate data used widely by practitioners to gauge crowding. Coinglass "Fear & Greed" composite treats extreme funding as a sentiment-based contrarian signal.

- Perpetual futures funding mechanics: any funding rate > 0 means longs pay shorts, incentivizing fresh shorts to enter, which tends to compress price premium. The equilibration mechanism is the theoretical underpinning for price reversion after funding spikes.

**Related quantitative work:**

- Cong, L. W., et al. (2023). "Crypto Wash Trading." NBER Working Paper. Discusses price manipulation and positioning signals in crypto markets.

- Liu, Y., & Tsyvinski, A. (2021). "Risks and Returns of Cryptocurrency." *Review of Financial Studies* 34(6). Documents momentum and reversal effects in crypto price series.

- Bianchi, D., Babiak, M., & Dickerson, A. (2022). "Trading Volume and Liquidity Provision in Cryptocurrency Markets." *Journal of Banking & Finance*. Relevant for understanding order flow and crowding dynamics.

**Perpetual futures mechanics (funding rate design):**

- Deribit Insights, "Perpetual Swap Mechanics" — explains that funding rate = clamp(mark_premium, cap) paid every 1h (HL) or 8h (Binance). High sustained rates indicate persistent positioning imbalance.

## Data Sources

- **Price data:** Hyperliquid 1h OHLCV, sourced from HL API (research/data/<COIN>_1h.csv). Covers 2023-06-01 to 2026-06-01 for major coins.
- **Funding rate data:** Hyperliquid hourly funding rates (research/data/<COIN>.csv). HL pays/charges funding every hour (unlike Binance/Bybit 8h cadence).

## Caveats on Literature Applicability

The academic work on funding rate predictability is primarily on 8-hour cadence (Binance/Bybit). Hyperliquid uses 1-hour cadence, which means:
1. Daily funding sum is the sum of 24 hourly payments (not 3 × 8h)
2. The signal window L=30 days covers 720 hourly payments — broadly similar information content to 90 daily observations on traditional exchanges
3. No peer-reviewed paper specifically validates the contrarian signal on HL-cadence data
