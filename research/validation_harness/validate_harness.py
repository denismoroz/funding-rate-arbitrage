"""
Ф6 — прогон эталонов через ВЕСЬ стенд и проверка известных ответов.
Запуск:  PYTHONPATH=.. python validate_harness.py

Если эти ассерты падают — чинить стенд, не стратегию.
"""
from __future__ import annotations

import numpy as np

from harness import run_harness
from report import print_report
from strategies.baselines_pkg import (
    NoisePackage, CheatPackage, sim_longflat, sig_buyhold, COINS)
from costs import TAKER
from engine import load_data, TOTAL_CAPITAL


def check_buyhold_matches_direct() -> None:
    """BUY&HOLD: кумулятив pnl симулятора ≈ прямой price-return × капитал."""
    df = load_data("BTC")
    pnl = sim_longflat(df, sig_buyhold(df), TAKER)
    sim_total = float(np.sum(pnl))
    ret = df["price_return"].values
    # прямой: held=pos сдвинут на 1, pos≡1 ⇒ held[0]=0, остальное ret[i]
    direct = float(np.sum(ret[1:]) * TOTAL_CAPITAL) - TOTAL_CAPITAL * TAKER.spot_cost
    rel = abs(sim_total - direct) / (abs(direct) + 1e-9)
    print(f"[buy&hold] sim_total={sim_total:.1f}  direct={direct:.1f}  rel.err={rel:.2e}")
    assert rel < 1e-6, (sim_total, direct)


def main() -> None:
    check_buyhold_matches_direct()

    print("\n##### ЭТАЛОН 1: NOISE-меню #####")
    rep_noise = run_harness(NoisePackage())
    print_report(rep_noise)

    print("\n##### ЭТАЛОН 2: LOOK-AHEAD CHEAT #####")
    rep_cheat = run_harness(CheatPackage())
    print_report(rep_cheat)

    # ── известные ответы ──────────────────────────────────────────────────────
    dN, pN = rep_noise.dsr["dsr"], rep_noise.pbo.pbo
    dC, pC = rep_cheat.dsr["dsr"], rep_cheat.pbo.pbo
    print("\n" + "=" * 72)
    print("ПРОВЕРКА ИЗВЕСТНЫХ ОТВЕТОВ")
    print("=" * 72)
    print(f"  noise : DSR={dN:.3f} (ждём низкий)   PBO={pN:.3f} (ждём высокий)")
    print(f"  cheat : DSR={dC:.3f} (ждём ≈1)       PBO={pC:.3f} (ждём ≈0)")

    assert dC > 0.95, ("cheat DSR должен быть ≈1", dC)
    assert pC < 0.10, ("cheat PBO должен быть ≈0", pC)
    assert dN < dC, ("noise DSR должен быть ниже cheat", dN, dC)
    assert pN > pC, ("noise PBO должен быть выше cheat", pN, pC)
    print("\n✅ стенд воспроизводит известные ответы — валиден.")


if __name__ == "__main__":
    main()
