"""
Риск-анализ стратегий A_cycle, A_spot_keep и B по монетам.
Использует engine.simulate (правильная equity-модель, компаунд, реальные комиссии).
"""

import pandas as pd
from pathlib import Path
from engine import (
    COINS, STAKING_YIELD, load_data, simulate, compute_metrics,
    regime_below_ma,
)


def main():
    rows_cyc, rows_keep, rows_b = [], [], []

    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            continue
        staking = STAKING_YIELD.get(coin, 0.0)

        pnl_cyc,  _ = simulate(df, staking, "A_cycle")
        pnl_keep, _ = simulate(df, staking, "A_spot_keep")
        pnl_b,    _ = simulate(df, staking, "B", regime_filter=regime_below_ma)

        rows_cyc.append({"coin": coin, **compute_metrics(pnl_cyc)})
        rows_keep.append({"coin": coin, **compute_metrics(pnl_keep)})
        rows_b.append({"coin": coin, **compute_metrics(pnl_b)})

    df_cyc  = pd.DataFrame(rows_cyc).sort_values("calmar", ascending=False)
    df_keep = pd.DataFrame(rows_keep).sort_values("calmar", ascending=False)
    df_b    = pd.DataFrame(rows_b).sort_values("calmar", ascending=False)

    cols = ["coin","annual_pct","vol_pct","sharpe","sortino","max_dd_pct","calmar","win_rate","kelly_half"]

    print("\n" + "="*100)
    print("A_cycle — обе ноги открываются/закрываются вместе (4 комиссии за цикл)")
    print("="*100)
    print(df_cyc[cols].to_string(index=False))

    print("\n" + "="*100)
    print("A_spot_keep — спот удерживается всегда, перп динамически (стейкинг + спот риск)")
    print("="*100)
    print(df_keep[cols].to_string(index=False))

    print("\n" + "="*100)
    print("B — спот всегда + перп по MA200 + стейкинг")
    print("="*100)
    print(df_b[cols].to_string(index=False))

    print("\n" + "="*100)
    print("СРАВНЕНИЕ ТРЁХ СТРАТЕГИЙ (annualized %)")
    print("="*100)
    merged = (
        df_cyc[["coin","annual_pct"]].rename(columns={"annual_pct":"A_cycle"})
        .merge(df_keep[["coin","annual_pct"]].rename(columns={"annual_pct":"A_keep"}), on="coin")
        .merge(df_b[["coin","annual_pct"]].rename(columns={"annual_pct":"B"}), on="coin")
        .sort_values("A_cycle", ascending=False)
    )
    print(merged.to_string(index=False))

    out = Path(__file__).parent
    df_cyc.to_csv(out / "risk_a_cycle.csv", index=False)
    df_keep.to_csv(out / "risk_a_keep.csv", index=False)
    df_b.to_csv(out / "risk_b.csv", index=False)


if __name__ == "__main__":
    main()
