# FX trend following (G10 daily)

Time-series momentum (blended 3/6/12-month) on 7 G10 USD pairs, vol-targeted equal-weight basket.

- **Data:** yfinance daily, 2004-2026 (~22yr), `research/quant/data_fx/`. NOT in original repo — downloaded for this study.
- **Run:** `source .venv/bin/activate && python3 research/quant/fx_trend_following/backtest.py`
- **Result:** CAGR ≈ 0% full sample, **−2.5% in 2015+**. REJECT — see `final_assessment.md`.
- Costs: 1 bp/side default (2 bp tested); cost is not the issue — there is no gross edge on G10.

This is the study's FX representative. The trend premium is better captured in crypto
(`research/quant/crypto_trend_tsmom`, `research/quant/crypto_donchian_breakout`).
