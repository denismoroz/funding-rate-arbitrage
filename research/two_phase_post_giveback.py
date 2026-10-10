"""
Phase-2 giveback gate: A/B против прод-правила выхода из POST_BREAKEVEN.

Прод сейчас: позиция в плюсе, 8ч-сигнал < −10% → закрыть. Живой кейс 2026-10-08
(HYPE #23): 8 часов минуса стоили ~0.012% номинала, переворот — 0.21%.

Вариант: закрывать только когда сигнал < порога И фандинг, отданный от пика,
>= mult × комиссии полного круга. mult=0 — текущее правило.

Параметры = прод params_json (strategy id=1), не дефолты класса.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from engine import TOTAL_CAPITAL
from two_phase_negstop import run_row

PROD_LIVE = dict(
    max_concurrent=4, entry_threshold=0.10, signal_window=8,
    base_min_hold=24, safety_mult=5.0, cap_min_hold=720,
    phase1_negative_patience=24, phase1_breakeven_cap_hours=720,
    phase2_exit_threshold=-0.10,
    neg_stop_threshold=-0.15, neg_stop_patience=6, neg_stop_bypass_min_hold=True,
    latch_post=True,
)
UNIVERSES = {
    "live5": ["BTC", "ETH", "SOL", "HYPE", "PURR"],
    "wide7": ["BTC", "ETH", "SOL", "HYPE", "PURR", "AVAX", "ZEC"],
}
MULTS = [0.0, 0.25, 0.5, 1.0, 2.0]


def main():
    capital_base = PROD_LIVE["max_concurrent"] * TOTAL_CAPITAL
    rows = []
    for uni, coins in UNIVERSES.items():
        for mult in MULTS:
            for r in run_row(f"{uni}_mult{mult}", coins, capital_base,
                             **PROD_LIVE, phase2_giveback_fee_mult=mult):
                rows.append({"universe": uni, "mult": mult, **r})
    df = pd.DataFrame(rows).drop(columns=["label", "avg_min_hold", "negstop_exits", "bleed_h"])
    df.to_csv(Path(__file__).parent / "two_phase_post_giveback_results.csv", index=False)
    pd.set_option("display.width", 200)
    for period in ["full", "last_180d", "last_90d"]:
        print(f"\n=== {period} ===")
        print(df[df.period == period].drop(columns=["period"]).to_string(index=False))


if __name__ == "__main__":
    main()
