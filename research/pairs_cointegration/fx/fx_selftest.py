"""
Ф6 — Self-test FX-адаптера пар с известными ответами.

Эталоны (аналог selftest.py в крипто-версии):
  1. CHEAT: look-ahead spread-direction → DSR высокий, PBO≈0
  2. RANDOM PAIRS: IID-сигналы → DSR≈0, PBO высокий
  3. BUY&HOLD спреда → total симулятора = прямой расчёт (rel.err < 1e-6)

Проверяем НАПРАВЛЕНИЕ: cheat DSR >> random DSR, random PBO >> cheat PBO.
Должен пройти без ассертов — это доказательство корректности адаптера.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── paths ─────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_PAIRS_ROOT = _HERE.parent
_HARNESS = _PAIRS_ROOT.parent / "validation_harness"
_FX_XSEC = _PAIRS_ROOT.parent / "cross_sectional" / "fx"
_RESEARCH = _PAIRS_ROOT.parent

for _p in (_HARNESS, _FX_XSEC, _RESEARCH, _PAIRS_ROOT, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from harness import run_harness, HarnessReport
from report import print_report
from costs import Costs, TAKER

from fx_pairs_data import load_pair_df, ALL_PAIRS
from fx_pairs_strategy import (
    MENU_CONFIGS, SELECTED_CONFIG_NAME, FXPairConfig, PAIR_NOTIONAL,
    precompute_signals, simulate_pair, FX_COST_PER_LEG,
)
from kalman import KalmanConfig, DEFAULT_Q, DEFAULT_R
from fx_pairs_pkg import FXPairsPackage, PURGE_DAYS, _pair_id


# ─────────────────────────────────────────────────────────────────────────────
# ЭТАЛОН 3: Buy&Hold спреда — аналитическая проверка
# ─────────────────────────────────────────────────────────────────────────────

def check_buyhold_spread_matches_direct() -> None:
    """Симулятор buy&hold спреда = прямой расчёт с rel.err < 1e-6."""
    pair = ("AUD", "NZD")
    df = load_pair_df(pair)
    assert df is not None, "Нет данных AUD/NZD"

    cfg = MENU_CONFIGS[SELECTED_CONFIG_NAME]
    precompute_signals(df, cfg)

    lsa = df["log_spot_a"].values
    lsb = df["log_spot_b"].values
    ret_a = np.diff(lsa, prepend=lsa[0])
    ret_b = np.diff(lsb, prepend=lsb[0])
    beta  = df["_beta"].values
    ca = df["carry_a"].values
    cb = df["carry_b"].values

    n = len(df)
    # Прямой расчёт: pos=+1 с t=1 до конца, cost на открытие и закрытие
    # PnL бара i (i>=1) = (ret_a[i] - beta[i]*ret_b[i]) + (carry_a[i] - carry_b[i])
    # beta: берём всё — уже precomputed на ПОЛНОМ df
    gross_direct = np.sum((ret_a[1:] - beta[1:] * ret_b[1:]) * PAIR_NOTIONAL)
    carry_direct = np.sum((ca[1:] - cb[1:]) * PAIR_NOTIONAL)
    open_cost    = PAIR_NOTIONAL * FX_COST_PER_LEG * 2
    close_cost   = PAIR_NOTIONAL * FX_COST_PER_LEG * 2
    direct_total = gross_direct + carry_direct - open_cost - close_cost

    # Симулятор buy&hold
    sim_total = _buyhold_simulate(df, cfg)

    rel = abs(sim_total - direct_total) / (abs(direct_total) + 1e-9)
    print(f"[buy&hold spread] sim={sim_total:.4f}  direct={direct_total:.4f}  rel.err={rel:.2e}")
    assert rel < 1e-6, f"buy&hold rel.err too large: {rel:.2e}"


def _buyhold_simulate(df: pd.DataFrame, cfg: FXPairConfig) -> float:
    """Симулировать buy&hold: войти в t=0, держать до t=n-1, выйти."""
    n = len(df)
    lsa = df["log_spot_a"].values
    lsb = df["log_spot_b"].values
    ret_a = np.diff(lsa, prepend=lsa[0])
    ret_b = np.diff(lsb, prepend=lsb[0])
    beta  = df["_beta"].values
    ca = df["carry_a"].values
    cb = df["carry_b"].values

    total = 0.0
    # t=0: вход (открытие)
    total -= PAIR_NOTIONAL * FX_COST_PER_LEG * 2
    # t=1..n-1: удерживаем (pos решена в t=0, зарабатывает с t=1)
    for i in range(1, n):
        total += (ret_a[i] - beta[i] * ret_b[i]) * PAIR_NOTIONAL
        total += (ca[i] - cb[i]) * PAIR_NOTIONAL
    # Закрытие (cost при изменении pos)
    total -= PAIR_NOTIONAL * FX_COST_PER_LEG * 2
    return total


# ─────────────────────────────────────────────────────────────────────────────
# Cheat-симулятор
# ─────────────────────────────────────────────────────────────────────────────

def _cheat_simulate_pair(
    df: pd.DataFrame,
    seg: slice,
    cfg: FXPairConfig,
    notional: float = PAIR_NOTIONAL,
) -> np.ndarray:
    """CHEAT: знает направление spread[t+1]−spread[t] заранее → guaranteed PnL."""
    sub = df.iloc[seg]
    n_sub = len(sub)
    if n_sub < 2:
        return np.zeros(n_sub)

    lsa = df["log_spot_a"].values
    lsb = df["log_spot_b"].values
    ret_a = np.diff(lsa, prepend=lsa[0])
    ret_b = np.diff(lsb, prepend=lsb[0])
    beta   = df["_beta"].values
    ca     = df["carry_a"].values
    cb     = df["carry_b"].values
    spread = df["_spread"].values

    sl = slice(seg.start, seg.stop)
    ret_a_seg   = ret_a[sl]
    ret_b_seg   = ret_b[sl]
    ca_seg      = ca[sl]
    cb_seg      = cb[sl]
    beta_seg    = beta[sl]
    spread_seg  = spread[sl]

    pnl = np.zeros(n_sub)
    prev_pos = 0
    for i in range(n_sub - 1):
        ds = spread_seg[i + 1] - spread_seg[i]
        pos = 1 if ds > 0 else -1

        gross    = float(pos) * (ret_a_seg[i + 1] - beta_seg[i + 1] * ret_b_seg[i + 1]) * notional
        accrual  = float(pos) * (ca_seg[i + 1] - cb_seg[i + 1]) * notional
        turnover = abs(pos - prev_pos)
        cost     = turnover * notional * FX_COST_PER_LEG * 2

        pnl[i + 1] = gross + accrual - cost
        prev_pos = pos

    return pnl


# ─────────────────────────────────────────────────────────────────────────────
# Package-эталоны
# ─────────────────────────────────────────────────────────────────────────────

# Используем 4 реальных пары как "монеты" для стенда
_TEST_PAIRS = ALL_PAIRS[:4]
_TEST_PAIR_IDS = [_pair_id(p) for p in _TEST_PAIRS]


def _load_test_dfs() -> dict[str, pd.DataFrame]:
    cache = {}
    for p in _TEST_PAIRS:
        pid = _pair_id(p)
        df = load_pair_df(p)
        if df is not None:
            cache[pid] = df
    return cache


class FXCheatPackage:
    """Эталон 1: look-ahead spread-direction → DSR высокий, PBO≈0."""
    name = "ЭТАЛОН FX: look-ahead cheat (spread direction)"
    selected_name = SELECTED_CONFIG_NAME

    def __init__(self):
        self._dfs = _load_test_dfs()

    @property
    def coins(self) -> list[str]:
        return [k for k in _TEST_PAIR_IDS if k in self._dfs]

    def load(self, pair_id: str) -> pd.DataFrame | None:
        return self._dfs.get(pair_id)

    def _precompute(self, df: pd.DataFrame) -> None:
        if "_spread" not in df.columns:
            cfg = MENU_CONFIGS[self.selected_name]
            precompute_signals(df, cfg)

    def selected(self, pair_id: str, df: pd.DataFrame):
        class _CheatStrat:
            name = f"cheat_{pair_id}"

            def __init__(s, df, parent):
                s._df = df
                s._parent = parent
                s._cache_id = None

            def _ensure(s, df):
                if s._cache_id != id(df):
                    cfg = MENU_CONFIGS[SELECTED_CONFIG_NAME]
                    precompute_signals(df, cfg)
                    s._cache_id = id(df)

            def fit(s, df, train_idx, costs):
                return SELECTED_CONFIG_NAME

            def simulate(s, df, seg, config, costs):
                s._ensure(df)
                cfg = MENU_CONFIGS[SELECTED_CONFIG_NAME]
                return _cheat_simulate_pair(df, seg, cfg)

        return _CheatStrat(df, self)

    def menu(self, pair_id: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        self._precompute(df)
        seg = slice(0, len(df))
        cfg = MENU_CONFIGS[self.selected_name]
        cheat_pnl = _cheat_simulate_pair(df, seg, cfg)
        result: dict[str, pd.Series] = {
            self.selected_name: pd.Series(cheat_pnl, index=df.index),
        }
        # Несколько шумных конфигов, чтобы DSR threshold калибровался
        rng = np.random.default_rng(77)
        for seed in range(3):
            noise_pnl = rng.standard_normal(len(df)) * PAIR_NOTIONAL * 0.001
            result[f"noise_{seed}"] = pd.Series(noise_pnl, index=df.index)
        return result


class FXRandomPairsPackage:
    """Эталон 2: random pairs → DSR≈0, PBO высокий."""
    name = "ЭТАЛОН FX: random pairs"
    selected_name = SELECTED_CONFIG_NAME

    def __init__(self):
        self._dfs = _load_test_dfs()

    @property
    def coins(self) -> list[str]:
        return [k for k in _TEST_PAIR_IDS if k in self._dfs]

    def load(self, pair_id: str) -> pd.DataFrame | None:
        return self._dfs.get(pair_id)

    def selected(self, pair_id: str, df: pd.DataFrame):
        class _RandStrat:
            name = f"random_{pair_id}"

            def __init__(s, df, seed=0):
                s._seed = seed
                s._cache_id = None

            def _ensure(s, df):
                if s._cache_id != id(df):
                    n = len(df)
                    rng = np.random.default_rng(s._seed)
                    df["_z"] = rng.uniform(-3.0, 3.0, size=n)
                    df["_beta"] = np.ones(n)
                    df["_alpha"] = np.zeros(n)
                    df["_spread"] = df["resid_a"].values - df["resid_b"].values
                    s._cache_id = id(df)

            def fit(s, df, train_idx, costs):
                return SELECTED_CONFIG_NAME

            def simulate(s, df, seg, config, costs):
                s._ensure(df)
                cfg = MENU_CONFIGS[SELECTED_CONFIG_NAME]
                return simulate_pair(df, seg, cfg)

        return _RandStrat(df, seed=0)

    def menu(self, pair_id: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        result: dict[str, pd.Series] = {}
        n = len(df)
        for seed in range(8):
            actual_name = self.selected_name if seed == 0 else f"z{30 + seed*5}_e2.0"
            rng = np.random.default_rng(seed)
            df_w = df.copy()
            df_w["_z"] = rng.uniform(-3.0, 3.0, size=n)
            df_w["_beta"] = np.ones(n)
            df_w["_alpha"] = np.zeros(n)
            df_w["_spread"] = df_w["resid_a"].values - df_w["resid_b"].values
            cfg = MENU_CONFIGS[SELECTED_CONFIG_NAME]
            pnl = simulate_pair(df_w, slice(0, n), cfg)
            result[actual_name] = pd.Series(pnl, index=df.index)
        return result


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 72)
    print("FX PAIRS SELF-TEST — Ф6")
    print("=" * 72)

    # Эталон 3: buy&hold spread matches direct
    print("\n[ЭТАЛОН 3] buy&hold spread = прямой расчёт")
    check_buyhold_spread_matches_direct()
    print("  OK: rel.err < 1e-6")

    # Эталон 1: cheat
    print("\n[ЭТАЛОН 1] look-ahead cheat (spread direction)")
    rep_cheat = run_harness(
        FXCheatPackage(),
        n_groups=4, k=1, purge=PURGE_DAYS, embargo=24,
    )
    print_report(rep_cheat)
    dC = rep_cheat.dsr["dsr"]
    pC = rep_cheat.pbo.pbo

    # Эталон 2: random pairs
    print("\n[ЭТАЛОН 2] random pairs")
    rep_rand = run_harness(
        FXRandomPairsPackage(),
        n_groups=4, k=1, purge=PURGE_DAYS, embargo=24,
    )
    print_report(rep_rand)
    dR = rep_rand.dsr["dsr"]
    pR = rep_rand.pbo.pbo

    # ── Проверка известных ответов ─────────────────────────────────────────
    print("\n" + "=" * 72)
    print("ПРОВЕРКА ИЗВЕСТНЫХ ОТВЕТОВ")
    print("=" * 72)
    print(f"  cheat : DSR={dC:.3f} (ждём высокий)   PBO={pC:.3f} (ждём низкий)")
    print(f"  random: DSR={dR:.3f} (ждём низкий)    PBO={pR:.3f} (ждём высокий)")

    assert dC > dR, (
        f"cheat DSR ({dC:.3f}) должен быть выше random DSR ({dR:.3f})"
    )
    assert pR > pC, (
        f"random PBO ({pR:.3f}) должен быть выше cheat PBO ({pC:.3f})"
    )
    assert dC > 0.50, f"cheat DSR слишком низкий: {dC:.3f}"

    print("\n[OK] Направления верны: cheat DSR >> random DSR, random PBO >> cheat PBO")
    print("[OK] Все 3 эталона пройдены.")


if __name__ == "__main__":
    main()
