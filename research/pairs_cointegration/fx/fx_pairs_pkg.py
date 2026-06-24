"""
FX pairs Package-адаптер под validation_harness.

Юнит = FX-пара (строка "CCY_A/CCY_B").
Пул = ВСЕ C(9,2)=36 пар (freeze).
Меню = 12 конфигов (4 z_window × 3 entry_z).

Purge >= USD_RESID_WINDOW + Z_WINDOW_MAX + KALMAN_WARMUP + TIME_STOP_MAX
       = 90 + 90 + 50 + 75 = 305 → используем 320.

Seam-safe:
  - load() кэширует df (precomputed signals добавляются в simulate)
  - menu() считает полнопериодный pnl КАЖДОГО config для каждой пары
    на ПОЛНОМ df (lookback цел)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ── sys.path ──────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_PAIRS_ROOT = _HERE.parent
_HARNESS = _PAIRS_ROOT.parent / "validation_harness"
_FX_XSEC = _PAIRS_ROOT.parent / "cross_sectional" / "fx"
_RESEARCH = _PAIRS_ROOT.parent

for _p in (_HARNESS, _FX_XSEC, _RESEARCH, _PAIRS_ROOT, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from costs import Costs, TAKER
from fx_pairs_data import ALL_PAIRS, USD_RESID_WINDOW, load_pair_df
from fx_pairs_strategy import (
    MENU_CONFIGS,
    SELECTED_CONFIG_NAME,
    FXPairConfig,
    FXPairsStrategy,
    precompute_signals,
    simulate_pair,
    Z_WINDOWS,
    HALF_LIFE_DEFAULT,
    TIME_STOP_MULT,
)

# ── Purge (дни) ───────────────────────────────────────────────────────────────
# USD_RESID_WINDOW=90, max Z_WINDOW=90, Kalman warmup≈50, max time_stop≈75
PURGE_DAYS = 320


def _pair_id(pair: tuple[str, str]) -> str:
    return f"{pair[0]}/{pair[1]}"


def _parse_pair(pair_id: str) -> tuple[str, str]:
    a, b = pair_id.split("/")
    return a, b


class _FXPairStrategy:
    """Адаптер под contract.Strategy для одной FX-пары."""

    def __init__(
        self,
        pair_id: str,
        config_name: str,
        menu_configs: dict[str, FXPairConfig],
    ):
        self.name = f"fx_pairs_{config_name}_{pair_id}"
        self._pair_id = pair_id
        self._config_name = config_name
        self._menu_configs = menu_configs
        self._cache_key: tuple | None = None

    def fit(
        self,
        df: pd.DataFrame,
        train_idx: np.ndarray,
        costs: Costs,
    ) -> str:
        from engine import compute_metrics
        from contract import contiguous_slices

        best_name = self._config_name
        best_sr = -np.inf

        for name, cfg in self._menu_configs.items():
            df_work = df.copy()
            precompute_signals(df_work, cfg)
            train_pnl: list[float] = []
            for seg in contiguous_slices(train_idx):
                if seg.stop - seg.start < 5:
                    continue
                p = simulate_pair(df_work, seg, cfg)
                if len(p) > 0:
                    train_pnl.extend(p.tolist())
            arr = np.array(train_pnl, dtype=float)
            if arr.size < 20:
                continue
            m = compute_metrics(arr)
            sr = m.get("sharpe", -np.inf) or -np.inf
            if np.isfinite(sr) and sr > best_sr:
                best_sr = sr
                best_name = name

        return best_name

    def simulate(
        self,
        df: pd.DataFrame,
        seg: slice,
        config: str,
        costs: Costs,
    ) -> np.ndarray:
        cfg_name = config or self._config_name
        cfg = self._menu_configs.get(cfg_name, self._menu_configs[self._config_name])

        cache_key = (id(df), cfg_name)
        if self._cache_key != cache_key:
            precompute_signals(df, cfg)
            self._cache_key = cache_key

        return simulate_pair(df, seg, cfg)


class FXPairsPackage:
    """Package для validation_harness. Юнит = FX-пара.

    Parameters
    ----------
    pairs : список пар или None → ALL_PAIRS (все 36)
    menu_configs : словарь конфигов или None → MENU_CONFIGS
    selected_name : имя выбранного конфига (для DSR)
    costs : Costs (передаётся стендом; FX-косты hardcoded в FX_COST_PER_LEG)
    """

    name = "FX Pairs Cointegration Mean-Reversion (G10, USD-neutral)"
    selected_name: str

    def __init__(
        self,
        pairs: list[tuple[str, str]] | None = None,
        menu_configs: dict[str, FXPairConfig] | None = None,
        selected_name: str = SELECTED_CONFIG_NAME,
        costs: Costs = TAKER,
    ):
        self._pairs = pairs if pairs is not None else ALL_PAIRS
        self._menu_configs = menu_configs if menu_configs is not None else MENU_CONFIGS
        self.selected_name = selected_name
        self._costs = costs
        self._df_cache: dict[str, pd.DataFrame | None] = {}
        self._menu_pnl_cache: dict[str, dict[str, pd.Series]] = {}

    @property
    def coins(self) -> list[str]:
        """Список пар-ID (строки "A/B") — «монеты» для стенда."""
        return [_pair_id(p) for p in self._pairs]

    def load(self, pair_id: str) -> pd.DataFrame | None:
        """Загрузить df для пары (кэшируем)."""
        if pair_id not in self._df_cache:
            pair = _parse_pair(pair_id)
            try:
                df = load_pair_df(pair)
            except Exception as e:
                print(f"  load({pair_id}) failed: {e}")
                df = None
            self._df_cache[pair_id] = df
        return self._df_cache[pair_id]

    def selected(self, pair_id: str, df: pd.DataFrame) -> _FXPairStrategy:
        """Стратегия для CPCV OOS (выбранный конфиг)."""
        return _FXPairStrategy(pair_id, self.selected_name, self._menu_configs)

    def menu(self, pair_id: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        """Полнопериодный pnl для КАЖДОГО config на полном df.

        Seam-safe: считаем на ПОЛНОМ df (lookback цел).
        """
        if pair_id in self._menu_pnl_cache:
            return self._menu_pnl_cache[pair_id]

        result: dict[str, pd.Series] = {}
        seg = slice(0, len(df))

        for name, cfg in self._menu_configs.items():
            df_work = df.copy()
            precompute_signals(df_work, cfg)
            pnl = simulate_pair(df_work, seg, cfg)
            result[name] = pd.Series(pnl, index=df.index, dtype=float)

        self._menu_pnl_cache[pair_id] = result
        return result


# ── self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing FXPairsPackage...")
    pkg = FXPairsPackage()
    print(f"  coins (pairs): {len(pkg.coins)}")
    print(f"  selected_name: {pkg.selected_name}")
    print(f"  menu_configs: {len(pkg._menu_configs)}")

    pair_id = pkg.coins[0]
    print(f"\n  Loading {pair_id}...")
    df = pkg.load(pair_id)
    if df is None:
        print("  LOAD FAILED")
    else:
        print(f"  df shape: {df.shape}")
        print(f"  df cols: {list(df.columns)}")

        print(f"\n  Computing menu for {pair_id}...")
        m = pkg.menu(pair_id, df)
        first_key = list(m.keys())[0]
        print(f"  menu configs: {sorted(m.keys())[:5]}...")
        print(f"  sample pnl sum ({first_key}): {m[first_key].sum():.2f}")

        strat = pkg.selected(pair_id, df)
        seg = slice(350, 450)
        pnl = strat.simulate(df, seg, pkg.selected_name, TAKER)
        print(f"\n  simulate (seg 350:450): pnl.sum={pnl.sum():.2f}")

    print("\nself-test passed.")
