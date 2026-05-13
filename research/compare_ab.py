"""
Сравнение всех вариантов стратегий A/B + buy & hold по монетам.
"""

import pandas as pd
from pathlib import Path
from engine import (
    COINS, STAKING_YIELD, load_data, simulate, buy_and_hold, compute_metrics,
    regime_below_ma,
)


def main():
    rows = []
    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            continue
        staking = STAKING_YIELD.get(coin, 0.0)
        n = len(df)

        pnl_cyc,  info_cyc  = simulate(df, staking, "A_cycle")
        pnl_keep, info_keep = simulate(df, staking, "A_spot_keep")
        pnl_b,    info_b    = simulate(df, staking, "B", regime_filter=regime_below_ma)
        bh_pnl              = buy_and_hold(df, staking)

        m_cyc  = compute_metrics(pnl_cyc)
        m_keep = compute_metrics(pnl_keep)
        m_b    = compute_metrics(pnl_b)
        m_bh   = compute_metrics(bh_pnl)

        rows.append({
            "coin":             coin,
            "stk%":             f"{staking*100:.0f}%",
            "buy_hold":         m_bh["annual_pct"],
            "bh_dd":            m_bh["max_dd_pct"],
            "A_cycle":          m_cyc["annual_pct"],
            "A_cyc_dd":         m_cyc["max_dd_pct"],
            "A_cyc_calmar":     m_cyc["calmar"],
            "A_keep":           m_keep["annual_pct"],
            "A_keep_dd":        m_keep["max_dd_pct"],
            "B":                m_b["annual_pct"],
            "B_dd":             m_b["max_dd_pct"],
            "B_calmar":         m_b["calmar"],
            "A_cyc_trades":     info_cyc["trades"],
            "B_trades":         info_b["trades"],
        })

    df_out = pd.DataFrame(rows).sort_values("A_cycle", ascending=False)

    print("\n" + "="*120)
    print("СРАВНЕНИЕ ВСЕХ СТРАТЕГИЙ (annualized %, max DD %)")
    print("entry=20% exit=-5% min_hold=72ч,  spot fee 0.07%, perp fee 0.035%")
    print("="*120)
    cols = ["coin","stk%","buy_hold","bh_dd","A_cycle","A_cyc_dd","A_cyc_calmar",
            "A_keep","A_keep_dd","B","B_dd","B_calmar"]
    print(df_out[cols].to_string(index=False))

    print("\n" + "="*120)
    print("ИТОГО (среднее по монетам)")
    print("="*120)
    avg = df_out[["buy_hold","A_cycle","A_keep","B"]].mean().round(2)
    avg_dd = df_out[["bh_dd","A_cyc_dd","A_keep_dd","B_dd"]].mean().round(2)
    print(f"  buy & hold:   annual={avg['buy_hold']:>7.2f}%   max_dd={avg_dd['bh_dd']:>6.2f}%")
    print(f"  A_cycle:      annual={avg['A_cycle']:>7.2f}%   max_dd={avg_dd['A_cyc_dd']:>6.2f}%")
    print(f"  A_spot_keep:  annual={avg['A_keep']:>7.2f}%   max_dd={avg_dd['A_keep_dd']:>6.2f}%")
    print(f"  B (MA200):    annual={avg['B']:>7.2f}%   max_dd={avg_dd['B_dd']:>6.2f}%")

    out = Path(__file__).parent / "compare_ab_results.csv"
    df_out.to_csv(out, index=False)
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
