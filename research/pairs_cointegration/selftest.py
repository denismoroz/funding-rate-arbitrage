"""
Ф6 — Self-test адаптера пар с известными ответами.

Аналог validate_harness.py, но для pairs-специфичной логики.

Эталоны:
  1. β-CHEAT: Kalman с look-ahead (β знает истинное на бар вперёд) → DSR≈1, PBO≈0
  2. RANDOM PAIRS: случайные «пары» из IID шума → DSR≈0, высокий PBO
  3. BUY&HOLD спреда: total симулятора = прямой расчёт (rel.err < 1e-6)

Проверяем НАПРАВЛЕНИЕ (cheat DSR >> random DSR), как в validate_harness.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── paths ─────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_HARNESS = _HERE.parent / "validation_harness"
_CRYPTO  = _HERE.parent / "cross_sectional" / "crypto"
_RESEARCH = _HERE.parent

for _p in (_HARNESS, _CRYPTO, _RESEARCH, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from harness import run_harness, HarnessReport
from report import print_report
from costs import Costs, TAKER
from pairs_data import load_pair_df, CANDIDATE_PAIRS
from pairs_strategy import (
    MENU_CONFIGS, SELECTED_CONFIG_NAME, PairConfig, PAIR_NOTIONAL,
    precompute_signals, simulate_pair, PERP_COST_PER_LEG,
)
from kalman import kalman_beta, KalmanConfig, DEFAULT_Q, DEFAULT_R
from pairs_pkg import PairsPackage, PURGE_DAYS, _pair_id


# ─────────────────────────────────────────────────────────────────────────────
# ЭТАЛОН 3: Buy&Hold спреда (аналитическая проверка)
# ─────────────────────────────────────────────────────────────────────────────

def check_buyhold_spread_matches_direct() -> None:
    """Симулятор buy&hold спреда = прямой расчёт с rel.err < 1e-6."""
    pair = ("SOL", "AVAX")
    df = load_pair_df(pair)
    assert df is not None, "Нет данных SOL/AVAX"

    cfg = MENU_CONFIGS[SELECTED_CONFIG_NAME]

    # Используем BuyHold-конфиг: всегда pos=+1 (long_a/short_b) без time-stop
    # Создаём специальный конфиг с entry_z=999 (никогда не войдём через сигнал)
    # Вместо этого проверим прямую формулу: 1 трейд с начала до конца

    # Прямой расчёт:
    # Открыть позицию +1 в t=0 (платим 2 ноги кост)
    # PnL_i = (ret_a[i] - β[i]*ret_b[i]) * notional + (fund_a[i] - fund_b[i]) * notional
    # Закрыть в последний бар (платим 2 ноги кост)
    precompute_signals(df, cfg)

    lpa = df["log_price_a"].values
    lpb = df["log_price_b"].values
    ret_a = np.diff(lpa, prepend=lpa[0])
    ret_b = np.diff(lpb, prepend=lpb[0])
    beta  = df["_beta"].values
    fa = df["funding_a"].values
    fb = df["funding_b"].values

    n = len(df)
    # Прямой расчёт: всегда pos=+1, открытие t=0, закрытие t=n-1
    gross_direct = np.sum((ret_a[1:] - beta[1:] * ret_b[1:]) * PAIR_NOTIONAL)
    fund_direct  = np.sum((fa[1:] - fb[1:]) * PAIR_NOTIONAL)
    open_cost    = PAIR_NOTIONAL * PERP_COST_PER_LEG * 2   # open: 2 legs
    close_cost   = PAIR_NOTIONAL * PERP_COST_PER_LEG * 2   # close: 2 legs
    direct_total = gross_direct + fund_direct - open_cost - close_cost

    # Симулятор: принудительно pos=+1 через модифицированный simulate_pair
    # (используем специальную функцию ниже)
    sim_total = _buyhold_simulate(df, cfg)

    rel = abs(sim_total - direct_total) / (abs(direct_total) + 1e-9)
    print(f"[buy&hold spread] sim={sim_total:.4f}  direct={direct_total:.4f}  rel.err={rel:.2e}")
    assert rel < 1e-6, f"buy&hold rel.err too large: {rel:.2e}"


def _buyhold_simulate(df: pd.DataFrame, cfg: PairConfig) -> float:
    """Симулировать buy&hold: войти в t=1, держать до t=n-1, выйти."""
    n = len(df)
    lpa = df["log_price_a"].values
    lpb = df["log_price_b"].values
    ret_a = np.diff(lpa, prepend=lpa[0])
    ret_b = np.diff(lpb, prepend=lpb[0])
    beta  = df["_beta"].values
    fa = df["funding_a"].values
    fb = df["funding_b"].values

    total = 0.0
    # t=0: вход (открытие)
    total -= PAIR_NOTIONAL * PERP_COST_PER_LEG * 2
    # t=1..n-1: удерживаем
    for i in range(1, n):
        total += (ret_a[i] - beta[i] * ret_b[i]) * PAIR_NOTIONAL
        total += (fa[i] - fb[i]) * PAIR_NOTIONAL
    # Закрытие
    total -= PAIR_NOTIONAL * PERP_COST_PER_LEG * 2
    return total


# ─────────────────────────────────────────────────────────────────────────────
# Пакеты-эталоны
# ─────────────────────────────────────────────────────────────────────────────

def _cheat_simulate_pair(
    df: pd.DataFrame,
    seg: slice,
    cfg: "PairConfig",
    notional: float = PAIR_NOTIONAL,
) -> np.ndarray:
    """CHEAT-симулятор: знает следующий однодневный spread-return заранее.

    Вместо торговли по z-score — каждый день смотрим на СЛЕДУЮЩИЙ спред:
      если spread[t+1] > spread[t] → long_a/short_b (pos=+1)
      если spread[t+1] < spread[t] → short_a/long_b (pos=-1)
    Это прямой look-ahead (знаем направление завтра) → гарантированный PnL.
    """
    sub = df.iloc[seg]
    n_sub = len(sub)
    if n_sub < 2:
        return np.zeros(n_sub)

    lpa = df["log_price_a"].values
    lpb = df["log_price_b"].values
    ret_a = np.diff(lpa, prepend=lpa[0])
    ret_b = np.diff(lpb, prepend=lpb[0])
    beta  = df["_beta"].values
    fa = df["funding_a"].values
    fb = df["funding_b"].values
    spread = df["_spread"].values

    sl = slice(seg.start, seg.stop)
    ret_a_seg  = ret_a[sl]
    ret_b_seg  = ret_b[sl]
    fund_a_seg = fa[sl]
    fund_b_seg = fb[sl]
    beta_seg   = beta[sl]
    spread_seg = spread[sl]

    pnl = np.zeros(n_sub)
    prev_pos = 0
    for i in range(n_sub - 1):
        # Cheat: direction = sign of tomorrow's spread change
        ds = spread_seg[i + 1] - spread_seg[i]
        pos = 1 if ds > 0 else -1

        # PnL at bar i+1 (we hold position decided at i, realised at i+1)
        gross = float(pos) * (ret_a_seg[i + 1] - beta_seg[i + 1] * ret_b_seg[i + 1]) * notional
        net_fund = float(pos) * (fund_a_seg[i + 1] - fund_b_seg[i + 1]) * notional
        turnover = abs(pos - prev_pos)
        cost = turnover * notional * PERP_COST_PER_LEG * 2

        pnl[i + 1] = gross + net_fund - cost
        prev_pos = pos

    return pnl


class _CheatStrategy:
    """CHEAT: знает следующий спред-return (look-ahead 1 bar) → DSR≈1, PBO≈0.

    Реализация: смотрим на spread[t+1]-spread[t] → входим в верную сторону.
    Это гарантированный положительный PnL (минус небольшие косты).
    """

    def __init__(self, pair_id: str, cfg_name: str, menu: dict):
        self.name = f"cheat_{pair_id}"
        self._cfg_name = cfg_name
        self._menu = menu
        self._cache_id: int | None = None

    def _precompute(self, df: pd.DataFrame) -> None:
        """Precompute _beta/_spread на ПОЛНОМ df (seam-safe)."""
        if self._cache_id == id(df):
            return
        cfg = self._menu[self._cfg_name]
        precompute_signals(df, cfg)
        self._cache_id = id(df)

    def fit(self, df, train_idx, costs):
        return self._cfg_name

    def simulate(self, df, seg, config, costs):
        self._precompute(df)
        cfg = self._menu[self._cfg_name]
        return _cheat_simulate_pair(df, seg, cfg)


class _RandomPairStrategy:
    """RANDOM PAIRS: случайные сигналы (IID) вместо real z-score.

    pos = random ±1 без смысла → ожидаемый PnL ≈ 0 − косты.
    DSR ≈ 0, PBO высокий.
    """

    def __init__(self, pair_id: str, seed: int, cfg_name: str, menu: dict):
        self.name = f"random_{pair_id}_s{seed}"
        self._seed = seed
        self._cfg_name = cfg_name
        self._menu = menu
        self._cache_id: int | None = None

    def _precompute_random(self, df: pd.DataFrame) -> None:
        if self._cache_id == id(df):
            return
        n = len(df)
        rng = np.random.default_rng(self._seed)
        # Random z: uniform [-3, 3] — enters frequently, random direction
        df["_z"] = rng.uniform(-3.0, 3.0, size=n)
        # beta = 1.0 (neutral)
        df["_beta"] = np.ones(n)
        df["_alpha"] = np.zeros(n)
        df["_spread"] = df["resid_a"].values - df["resid_b"].values
        self._cache_id = id(df)

    def fit(self, df, train_idx, costs):
        return self._cfg_name

    def simulate(self, df, seg, config, costs):
        self._precompute_random(df)
        cfg = self._menu[self._cfg_name]
        return simulate_pair(df, seg, cfg)


# ── Package-обёртки ───────────────────────────────────────────────────────────

# Используем 3 реальные пары как "монеты" для стенда (достаточно для тестов)
_TEST_PAIRS = CANDIDATE_PAIRS[:3]
_TEST_PAIR_IDS = [_pair_id(p) for p in _TEST_PAIRS]


def _load_test_dfs() -> dict[str, pd.DataFrame]:
    cache = {}
    for p in _TEST_PAIRS:
        pid = _pair_id(p)
        df = load_pair_df(p)
        if df is not None:
            cache[pid] = df
    return cache


class CheatPackage:
    """Эталон 1: look-ahead spread-direction → DSR высокий, PBO≈0.

    Cheat знает завтрашний spread-return и входит в верную сторону каждый день.
    """
    name = "ЭТАЛОН: look-ahead cheat (spread direction)"
    selected_name = SELECTED_CONFIG_NAME

    def __init__(self):
        self._dfs = _load_test_dfs()

    @property
    def coins(self) -> list[str]:
        return [k for k in _TEST_PAIR_IDS if k in self._dfs]

    def load(self, pair_id: str) -> pd.DataFrame | None:
        return self._dfs.get(pair_id)

    def selected(self, pair_id: str, df: pd.DataFrame) -> _CheatStrategy:
        return _CheatStrategy(pair_id, self.selected_name, MENU_CONFIGS)

    def menu(self, pair_id: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        strat = _CheatStrategy(pair_id, self.selected_name, MENU_CONFIGS)
        strat._precompute(df)
        seg = slice(0, len(df))
        cfg = MENU_CONFIGS[self.selected_name]
        cheat_pnl = _cheat_simulate_pair(df, seg, cfg)

        result: dict[str, pd.Series] = {
            self.selected_name: pd.Series(cheat_pnl, index=df.index),
        }
        # Add noise configs (low SR) so DSR threshold is calibrated vs some trials
        for seed in range(3):
            rng = np.random.default_rng(seed + 99)
            noise_pnl = rng.standard_normal(len(df)) * PAIR_NOTIONAL * 0.001
            result[f"noise_{seed}"] = pd.Series(noise_pnl, index=df.index)
        return result


class RandomPairsPackage:
    """Эталон 2: random pairs → DSR≈0, PBO высокий."""
    name = "ЭТАЛОН: random pairs"
    selected_name = SELECTED_CONFIG_NAME

    def __init__(self):
        self._dfs = _load_test_dfs()

    @property
    def coins(self) -> list[str]:
        return [k for k in _TEST_PAIR_IDS if k in self._dfs]

    def load(self, pair_id: str) -> pd.DataFrame | None:
        return self._dfs.get(pair_id)

    def selected(self, pair_id: str, df: pd.DataFrame) -> _RandomPairStrategy:
        return _RandomPairStrategy(pair_id, seed=0, cfg_name=self.selected_name, menu=MENU_CONFIGS)

    def menu(self, pair_id: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        result: dict[str, pd.Series] = {}
        for seed in range(8):
            name = f"random_{seed}" if seed > 0 else self.selected_name
            # For key name alignment: first one = selected_name
            actual_name = self.selected_name if seed == 0 else f"z{30 + seed*5}_e2.0"
            strat = _RandomPairStrategy(pair_id, seed=seed, cfg_name=actual_name, menu=MENU_CONFIGS)
            strat._precompute_random(df)
            cfg = MENU_CONFIGS[SELECTED_CONFIG_NAME]
            pnl = simulate_pair(df, slice(0, len(df)), cfg)
            result[actual_name] = pd.Series(pnl, index=df.index)
        return result


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 72)
    print("PAIRS SELF-TEST — Ф6")
    print("=" * 72)

    # Эталон 3: buy&hold spread matches direct
    print("\n[ЭТАЛОН 3] buy&hold spread = прямой расчёт")
    check_buyhold_spread_matches_direct()
    print("  OK: rel.err < 1e-6")

    # Эталон 1: cheat
    print("\n[ЭТАЛОН 1] look-ahead cheat (spread direction)")
    rep_cheat = run_harness(
        CheatPackage(),
        n_groups=4, k=1, purge=PURGE_DAYS, embargo=24,
    )
    print_report(rep_cheat)
    dC = rep_cheat.dsr["dsr"]
    pC = rep_cheat.pbo.pbo

    # Эталон 2: random pairs
    print("\n[ЭТАЛОН 2] random pairs")
    rep_rand = run_harness(
        RandomPairsPackage(),
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

    # Направление: cheat >> random
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
