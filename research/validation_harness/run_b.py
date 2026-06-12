"""
Ф7 — прогон Strategy B через стенд. Честный вердикт PBO/DSR.
Запуск:  PYTHONPATH=.. python run_b.py
"""
from __future__ import annotations

from pathlib import Path

from harness import run_harness, save_json
from report import print_report
from strategies.b_pkg import BPackage
from costs import TAKER


def main() -> None:
    # purge = 60d (макс. lookback меню) для seam-safety; N=6/k=2 (длина ряда не даёт
    # больше групп при таком purge). Косты = TAKER + 5bps slip (honest re-run).
    # NB: maker-сценарий тут НЕ воспроизводим — simulate_constdollar хардкодит
    # taker-комиссии и читает только slippage; честный maker требует параметризации
    # комиссий в B-симуляторе (отдельная задача, см. memory: maker = главный рычаг B).
    print("#" * 72)
    print("##### Strategy B через стенд — косты: TAKER (5bps slip) #####")
    print("#" * 72)
    rep = run_harness(BPackage(TAKER), purge=60 * 24, embargo=24)
    print_report(rep)
    out = Path(__file__).parent / "run_b_taker.json"
    save_json(rep, out)
    print(f"\nJSON → {out.name}")


if __name__ == "__main__":
    main()
