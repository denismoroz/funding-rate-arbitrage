"""
Ф2 — адаптер под harness.Package. Юнит = ПАРА (не монета).

Контракт стенда:
  coins         → список пар (в виде строк "A/B")
  load(pair_id) → df с обеими ногами + BTC + residuals + funding
  selected(pair_id, df) → PairsStrategy
  menu(pair_id, df)     → {config_name: full-period pnl Series}

PBO/DSR считаются поверх МНОЖЕСТВА ПАР-КАНДИДАТОВ (PLAN §1):
  «переносится ли вперёд выбор лучшей-по-бэктесту пары?»

Seam-safe:
  - load() кэширует df (precomputed signals добавляются в simulate)
  - menu() считает полнопериодный pnl КАЖДОГО config для каждой пары
    на ПОЛНОМ df (lookback цел)
  - purge >= BTC_RESID_WINDOW + Z_WINDOW_MAX + KALMAN_WARMUP = 90+90+50 ≈ 230 дней
    (используем 240 в run_pairs.py)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ── sys.path для локальных импортов ───────────────────────────────────────────
_HERE = Path(__file__).parent
_HARNESS = _HERE.parent / "validation_harness"
_CRYPTO = _HERE.parent / "cross_sectional" / "crypto"
_RESEARCH = _HERE.parent

for _p in (_HARNESS, _CRYPTO, _RESEARCH, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from costs import Costs, TAKER
from pairs_data import CANDIDATE_PAIRS, load_pair_df
from pairs_strategy import (
    MENU_CONFIGS,
    SELECTED_CONFIG_NAME,
    PairConfig,
    PairsStrategy,
    precompute_signals,
    simulate_pair,
)

# ── Purge (дни) = BTC_RESID_WINDOW + Z_WINDOW_MAX + Kalman_warmup ──────────
# BTC_RESID_WINDOW=90, max Z_WINDOW=90, Kalman warmup≈50 → sum=230 → round up
PURGE_DAYS = 240


def _pair_id(pair: tuple[str, str]) -> str:
    return f"{pair[0]}/{pair[1]}"


def _parse_pair(pair_id: str) -> tuple[str, str]:
    a, b = pair_id.split("/")
    return a, b


class _PairStrategy:
    """Адаптер под contract.Strategy для одной пары.

    Предзагруженный df (с precomputed сигналами) хранится в self._df_cache.
    simulate() использует df переданный стендом (мы кэшируем по id чтобы не
    пересчитывать на каждом сегменте).
    """

    def __init__(
        self,
        pair_id: str,
        config_name: str,
        menu_configs: dict[str, PairConfig],
    ):
        self.name = f"pairs_{config_name}_{pair_id}"
        self._pair_id = pair_id
        self._config_name = config_name
        self._menu_configs = menu_configs
        self._df_precomputed_id: int | None = None

    def fit(
        self,
        df: pd.DataFrame,
        train_idx: np.ndarray,
        costs: Costs,
    ) -> str:
        """Выбрать лучший config по Sharpe на train_idx.

        Возвращает config_name.
        """
        from engine import compute_metrics
        from contract import contiguous_slices

        best_name = self._config_name
        best_sr = -np.inf

        for name, cfg in self._menu_configs.items():
            df_work = df.copy()
            precompute_signals(df_work, cfg)
            train_pnl = []
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
        """PnL на смежном сегменте."""
        cfg_name = config or self._config_name
        cfg = self._menu_configs.get(cfg_name, self._menu_configs[self._config_name])

        # Кэш: пересчитываем только если df или config изменился
        cache_key = (id(df), cfg_name)
        if not hasattr(self, "_cache_key") or self._cache_key != cache_key:
            precompute_signals(df, cfg)
            self._cache_key = cache_key

        return simulate_pair(df, seg, cfg)


class PairsPackage:
    """Package для validation_harness. Юнит = пара.

    Параметры
    ----------
    pairs : список пар или None → CANDIDATE_PAIRS
    menu_configs : словарь конфигов или None → MENU_CONFIGS
    selected_name : имя выбранного конфига (для DSR)
    costs : Costs (передаётся стендом, не используется напрямую — наши косты
            hardcoded в PERP_COST_PER_LEG)
    """

    name = "Pairs Cointegration Mean-Reversion (crypto, BTC-neutral)"
    selected_name: str

    def __init__(
        self,
        pairs: list[tuple[str, str]] | None = None,
        menu_configs: dict[str, PairConfig] | None = None,
        selected_name: str = SELECTED_CONFIG_NAME,
        costs: Costs = TAKER,
    ):
        self._pairs = pairs if pairs is not None else CANDIDATE_PAIRS
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
        """Загрузить df для пары.

        Кэшируем по pair_id чтобы не перечитывать при каждом вызове из harness.
        """
        if pair_id not in self._df_cache:
            pair = _parse_pair(pair_id)
            try:
                df = load_pair_df(pair)
            except Exception as e:
                print(f"  load({pair_id}) failed: {e}")
                df = None
            self._df_cache[pair_id] = df
        return self._df_cache[pair_id]

    def selected(self, pair_id: str, df: pd.DataFrame) -> _PairStrategy:
        """Стратегия для CPCV OOS (выбранный конфиг)."""
        return _PairStrategy(pair_id, self.selected_name, self._menu_configs)

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


# ── self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing PairsPackage...")
    pkg = PairsPackage()
    print(f"  coins (pairs): {len(pkg.coins)}")
    print(f"  selected_name: {pkg.selected_name}")
    print(f"  menu_configs: {len(pkg._menu_configs)}")

    # Test load
    pair_id = pkg.coins[0]
    print(f"\n  Loading {pair_id}...")
    df = pkg.load(pair_id)
    if df is None:
        print("  LOAD FAILED")
    else:
        print(f"  df shape: {df.shape}")
        print(f"  df cols: {list(df.columns)}")

        # Test menu
        print(f"\n  Computing menu for {pair_id}...")
        m = pkg.menu(pair_id, df)
        print(f"  menu configs: {sorted(m.keys())[:5]}...")
        print(f"  sample pnl sum ({list(m.keys())[0]}): {m[list(m.keys())[0]].sum():.2f}")

        # Test selected strategy
        strat = pkg.selected(pair_id, df)
        seg = slice(250, 350)
        cfg = pkg.selected_name
        pnl = strat.simulate(df, seg, cfg, TAKER)
        print(f"\n  simulate (seg 250:350): pnl.sum={pnl.sum():.2f}")

    print("\nself-test passed.")
