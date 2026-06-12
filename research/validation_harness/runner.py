"""
Ф2 — single-strategy CPCV runner: гоняет одну стратегию по всем CPCV-путям и
собирает РАСПРЕДЕЛЕНИЕ OOS-метрик (медиана/IQR/доля Calmar>0), а не одну цифру.

Единица оценки = (training-комплемент, один смежный test-сегмент). Каждая
физическая группа попадает в test в C(N-1,k-1) комбинациях с чуть разными
train-комплементами → распределение ловит и режимную, и обучающую вариативность.

Зависит от: splitter.cpcv, contract, engine.compute_metrics. DSR/PBO — Ф3/Ф4.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from contract import Strategy, contiguous_slices
from costs import Costs, TAKER
from engine import compute_metrics
from splitter import Split, cpcv


@dataclass
class SegResult:
    test_groups: tuple[int, ...]
    seg: tuple[int, int]          # (start, stop) в индексах ряда
    n_hours: int
    metrics: dict                 # выход engine.compute_metrics


@dataclass
class OOSReport:
    strategy: str
    n_segments: int
    segs: list[SegResult] = field(repr=False, default_factory=list)
    dist: dict = field(default_factory=dict)   # metric -> {median, iqr_lo, iqr_hi}
    frac_calmar_pos: float = float("nan")
    frac_sharpe_pos: float = float("nan")


_DIST_KEYS = ("annual_pct", "sharpe", "max_dd_pct", "calmar")


def _summarize(segs: list[SegResult]) -> OOSReport:
    rep = OOSReport(strategy="", n_segments=len(segs), segs=segs)
    for key in _DIST_KEYS:
        vals = np.array([s.metrics[key] for s in segs], dtype=float)
        vals = vals[np.isfinite(vals)]
        if vals.size:
            rep.dist[key] = {
                "median": float(np.median(vals)),
                "iqr_lo": float(np.percentile(vals, 25)),
                "iqr_hi": float(np.percentile(vals, 75)),
            }
    cal = np.array([s.metrics["calmar"] for s in segs], dtype=float)
    shp = np.array([s.metrics["sharpe"] for s in segs], dtype=float)
    rep.frac_calmar_pos = float(np.mean(cal > 0)) if cal.size else float("nan")
    rep.frac_sharpe_pos = float(np.mean(shp > 0)) if shp.size else float("nan")
    return rep


def run_cpcv(
    strat: Strategy,
    df: pd.DataFrame,
    *,
    n_groups: int = 6,
    k: int = 2,
    purge: int = 720,
    embargo: int = 24,
    costs: Costs = TAKER,
    splits: list[Split] | None = None,
) -> OOSReport:
    """Прогнать `strat` по CPCV на одном ряду `df`. Вернёт распределение OOS."""
    n = len(df)
    if splits is None:
        splits = cpcv(n, n_groups=n_groups, k=k, purge=purge, embargo=embargo)

    segs: list[SegResult] = []
    for sp in splits:
        cfg = strat.fit(df, sp.train_idx, costs)
        for seg in contiguous_slices(sp.test_idx):
            pnl = strat.simulate(df, seg, cfg, costs)
            segs.append(SegResult(
                test_groups=sp.test_groups,
                seg=(seg.start, seg.stop),
                n_hours=seg.stop - seg.start,
                metrics=compute_metrics(np.asarray(pnl, dtype=float)),
            ))

    rep = _summarize(segs)
    rep.strategy = getattr(strat, "name", strat.__class__.__name__)
    return rep


def print_report(rep: OOSReport, *, coin: str = "") -> None:
    head = f"OOS CPCV — {rep.strategy}" + (f" [{coin}]" if coin else "")
    print(head)
    print(f"  сегментов OOS: {rep.n_segments}")
    print(f"  {'metric':<12}{'median':>10}{'IQR_lo':>10}{'IQR_hi':>10}")
    for key in _DIST_KEYS:
        d = rep.dist.get(key)
        if d:
            print(f"  {key:<12}{d['median']:>10.2f}{d['iqr_lo']:>10.2f}{d['iqr_hi']:>10.2f}")
    print(f"  доля сегментов Calmar>0: {rep.frac_calmar_pos*100:5.1f}%   "
          f"Sharpe>0: {rep.frac_sharpe_pos*100:5.1f}%")


# ── smoke ────────────────────────────────────────────────────────────────────
def _smoke() -> None:
    from engine import load_data, STAKING_YIELD
    from strategies.baselines import BuyHold, AlwaysFlat

    coin = "BTC"
    df = load_data(coin)
    print(f"{coin}: {len(df)} баров ({len(df)/8760:.2f} лет)\n")

    rep = run_cpcv(BuyHold(STAKING_YIELD.get(coin, 0.0)), df)
    print_report(rep, coin=coin)
    print()
    rep_flat = run_cpcv(AlwaysFlat(), df)
    print_report(rep_flat, coin=coin)
    # AlwaysFlat: pnl≡0 → calmar/sharpe = 0 везде, доля >0 = 0%
    assert rep_flat.frac_calmar_pos == 0.0
    print("\nsmoke passed.")


if __name__ == "__main__":
    _smoke()
