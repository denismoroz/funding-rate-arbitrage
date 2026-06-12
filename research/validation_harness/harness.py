"""
Ф5 — оркестратор стенда: один вход (пакет стратегии + монеты) → единый честный
вердикт (OOS-распределение CPCV + DSR + PBO), print и JSON.

Контракт пакета (Package):
  selected_name : имя конфига, который претендуем «выбрать» (он же оценивается DSR).
  coins         : список монет.
  load(coin)            -> df  (обычно engine.load_data)
  selected(coin, df)    -> Strategy   # для CPCV OOS-распределения
  menu(coin, df)        -> {name: pd.Series(pnl, index=df.index)}  # ВСЕ конфиги,
                          полнопериодный почасовой pnl — для PBO и DSR.

Три измерения вердикта:
  1. OOS CPCV (runner) — пул сегментов по всем монетам: устойчив ли эдж по режимам.
  2. PBO (CSCV) — на ПОРТФЕЛЬНОЙ матрице меню (equal-weight по монетам): переносится
     ли выбор лучшего-по-бэктесту конфига вперёд.
  3. DSR — Sharpe выбранного конфига, дефлейтнутый на размер меню (мультитест).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd

from costs import Costs, TAKER
from contract import Strategy
from metrics import dsr_from_returns, moments
from pbo import pbo, PBOResult
from runner import run_cpcv, OOSReport, _summarize


class Package(Protocol):
    name: str
    selected_name: str
    coins: list[str]

    def load(self, coin: str) -> pd.DataFrame: ...
    def selected(self, coin: str, df: pd.DataFrame) -> Strategy: ...
    def menu(self, coin: str, df: pd.DataFrame) -> dict[str, pd.Series]: ...


@dataclass
class HarnessReport:
    name: str
    selected_name: str
    coins: list[str]
    pooled_oos: OOSReport                       # распределение OOS по всем монетам
    per_coin_oos: dict[str, OOSReport] = field(repr=False, default_factory=dict)
    pbo: PBOResult = None
    dsr: dict = field(default_factory=dict)
    menu_names: list[str] = field(default_factory=list)
    n_portfolio_hours: int = 0


def _portfolio_menu(menu_by_coin: dict[str, dict[str, pd.Series]]) -> tuple[list[str], np.ndarray, pd.DatetimeIndex]:
    """Из {coin: {name: pnl-series}} собрать портфельную матрицу (T × N конфигов):
    для каждого конфига — equal-weight среднее по монетам на пересечении времени."""
    names = sorted({nm for m in menu_by_coin.values() for nm in m})
    port_cols: dict[str, pd.Series] = {}
    for nm in names:
        series = [m[nm].rename(coin) for coin, m in menu_by_coin.items() if nm in m]
        joined = pd.concat(series, axis=1, join="inner")
        port_cols[nm] = joined.mean(axis=1)
    port = pd.DataFrame(port_cols).dropna()
    return names, port.values, port.index


def run_harness(
    pkg: Package,
    *,
    costs: Costs = TAKER,
    n_groups: int = 6,
    k: int = 2,
    purge: int = 720,
    embargo: int = 24,
    S: int = 16,
) -> HarnessReport:
    per_coin: dict[str, OOSReport] = {}
    all_segs = []
    menu_by_coin: dict[str, dict[str, pd.Series]] = {}

    for coin in pkg.coins:
        df = pkg.load(coin)
        if df is None or len(df) < n_groups * 2:
            continue
        rep = run_cpcv(pkg.selected(coin, df), df,
                       n_groups=n_groups, k=k, purge=purge, embargo=embargo, costs=costs)
        per_coin[coin] = rep
        all_segs.extend(rep.segs)
        menu_by_coin[coin] = pkg.menu(coin, df)

    pooled = _summarize(all_segs)
    pooled.strategy = pkg.selected_name

    names, R_port, idx = _portfolio_menu(menu_by_coin)
    pbo_res = pbo(R_port, S=S, names=names)

    if pkg.selected_name in names:
        sel = names.index(pkg.selected_name)
    else:
        sel = int(np.argmax([moments(R_port[:, j]).sr for j in range(R_port.shape[1])]))
    trial_sharpes = np.array([moments(R_port[:, j]).sr for j in range(R_port.shape[1])])
    dsr = dsr_from_returns(R_port[:, sel], trial_sharpes)
    dsr["selected"] = names[sel]

    return HarnessReport(
        name=pkg.name,
        selected_name=pkg.selected_name,
        coins=list(per_coin.keys()),
        pooled_oos=pooled,
        per_coin_oos=per_coin,
        pbo=pbo_res,
        dsr=dsr,
        menu_names=names,
        n_portfolio_hours=len(idx),
    )


def to_dict(rep: HarnessReport) -> dict:
    return {
        "name": rep.name,
        "selected_name": rep.selected_name,
        "coins": rep.coins,
        "n_portfolio_hours": rep.n_portfolio_hours,
        "menu_size": len(rep.menu_names),
        "pooled_oos": {
            "n_segments": rep.pooled_oos.n_segments,
            "dist": rep.pooled_oos.dist,
            "frac_calmar_pos": rep.pooled_oos.frac_calmar_pos,
            "frac_sharpe_pos": rep.pooled_oos.frac_sharpe_pos,
        },
        "pbo": {"pbo": rep.pbo.pbo, "n_splits": rep.pbo.n_splits,
                "S": rep.pbo.S, "median_oos_rank": rep.pbo.median_oos_rank,
                "is_best_counts": rep.pbo.is_best_counts},
        "dsr": rep.dsr,
        "per_coin_calmar_median": {
            c: r.dist.get("calmar", {}).get("median") for c, r in rep.per_coin_oos.items()
        },
    }


def save_json(rep: HarnessReport, path: str | Path) -> None:
    Path(path).write_text(json.dumps(to_dict(rep), indent=2, ensure_ascii=False))
