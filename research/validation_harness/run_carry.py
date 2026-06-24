"""
КАЛИБРОВКА стенда: прогон carry (FRAB-семейство) как ground-truth «GO».
Запуск:  PYTHONPATH=.. python run_carry.py

carry прибылен live → если стенд даёт ему DSR>0.95, порог честный и price-return
NO-GO (pairs/trend/spread) реальны; если carry проваливает — порог слишком жёсткий.
"""
from __future__ import annotations

from pathlib import Path

from harness import run_harness, save_json
from report import print_report
from strategies.carry_pkg import CarryPackage
from costs import TAKER


def main() -> None:
    print("#" * 72)
    print("##### CARRY (FRAB-семейство) через стенд — калибровка порога #####")
    print("#" * 72)
    # purge = макс. lookback меню (smoothing 168h) с запасом → 30 дней (как дефолт).
    rep = run_harness(CarryPackage(TAKER), purge=720, embargo=24)
    print_report(rep)
    out = Path(__file__).parent / "run_carry.json"
    save_json(rep, out)
    print(f"\nJSON → {out.name}")


if __name__ == "__main__":
    main()
