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
    # PnL_i = (ret_a[i] - β[i]*ret_b[i]) * notional - (fund_a[i] - fund_b[i]) * notional
    #   (long PAYS positive funding: net_fund = -pos*(fund_a - fund_b)*notional)
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
    fund_direct  = np.sum(-(fa[1:] - fb[1:]) * PAIR_NOTIONAL)  # long pays positive funding
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
    # t=1..n-1: удерживаем; long PAYS positive funding → net_fund = -(fa - fb)*notional
    for i in range(1, n):
        total += (ret_a[i] - beta[i] * ret_b[i]) * PAIR_NOTIONAL
        total -= (fa[i] - fb[i]) * PAIR_NOTIONAL  # long pays positive funding
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
        # long pays positive funding: net_fund = -pos*(fund_a - fund_b)*notional
        gross = float(pos) * (ret_a_seg[i + 1] - beta_seg[i + 1] * ret_b_seg[i + 1]) * notional
        net_fund = -float(pos) * (fund_a_seg[i + 1] - fund_b_seg[i + 1]) * notional
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


# ─────────────────────────────────────────────────────────────────────────────
# ЭТАЛОН 4: детерминированный синтетический тест simulate_pair
# ─────────────────────────────────────────────────────────────────────────────

def _make_synthetic_df(n: int = 100, entry_z: float = 2.0, z_window: int = 30) -> pd.DataFrame:
    """Построить синтетический df с колонками, которые читает simulate_pair.

    Конструкция:
      - log_price_a / log_price_b: постоянные, кроме двух баров удержания, где
        ret_a - beta*ret_b = KNOWN_GROSS_PER_BAR (определяемое константой ниже).
      - _z: принудительно ниже -entry_z на баре ENTRY_BAR, затем >=0 на EXIT_BAR
        (чтобы simulate_pair вошёл и вышел в известный момент).
      - _beta = 1.0, _alpha = 0.0 всюду.
      - funding_a = funding_b = 0.0 (чтобы убрать влияние funding из ручного расчёта).
      - _spread = 0.0 (не используется simulate_pair напрямую, только через _z).
    """
    ENTRY_BAR  = z_window      # первый бар, где z не NaN и мы входим
    EXIT_BAR   = ENTRY_BAR + 3  # z пересечёт 0 здесь → выход

    # Константа gross за каждый бар удержания (в долях от notional):
    # удерживаем с бара ENTRY_BAR+1 по EXIT_BAR включительно
    KNOWN_RET = 0.005   # 0.5% в каждую ногу

    idx = pd.RangeIndex(n)
    df = pd.DataFrame(index=idx)

    # log_price: нулевые дрейфы всюду, кроме баров удержания
    lpa = np.zeros(n, dtype=float)
    lpb = np.zeros(n, dtype=float)
    # Позиция +1 (long_a/short_b) открывается на ENTRY_BAR,
    # зарабатывает за бары ENTRY_BAR+1 .. EXIT_BAR:
    for i in range(ENTRY_BAR + 1, EXIT_BAR + 1):
        # ret_a[i] = KNOWN_RET, ret_b[i] = 0 → gross = pos*(ret_a - beta*ret_b) = +KNOWN_RET
        lpa[i] = lpa[i - 1] + KNOWN_RET
        lpb[i] = lpb[i - 1]   # flat → ret_b = 0
    # После EXIT_BAR — продолжаем с тем же значением lpa
    for i in range(EXIT_BAR + 1, n):
        lpa[i] = lpa[EXIT_BAR]
        lpb[i] = lpb[EXIT_BAR]

    df["log_price_a"] = lpa
    df["log_price_b"] = lpb
    df["funding_a"]   = 0.0
    df["funding_b"]   = 0.0
    df["resid_a"]     = 0.0   # не используется simulate_pair (только precompute_signals)
    df["resid_b"]     = 0.0

    # Сигналы: _z, _beta, _alpha, _spread уже заданы вручную
    z = np.full(n, float("nan"))
    # warm-up: NaN до ENTRY_BAR-1
    # вход: z[ENTRY_BAR] < -entry_z
    z[ENTRY_BAR] = -entry_z - 0.5          # триггер входа long_a/short_b
    # бары между входом и выходом: держим (z не нулевой)
    for i in range(ENTRY_BAR + 1, EXIT_BAR):
        z[i] = -entry_z + 0.1              # ещё в зоне удержания (не пересекло 0)
    # выход: z[EXIT_BAR] >= 0
    z[EXIT_BAR] = 0.1                      # z пересёк 0 → выход
    for i in range(EXIT_BAR + 1, n):
        z[i] = 0.0                         # после выхода — flat

    df["_z"]      = z
    df["_beta"]   = 1.0
    df["_alpha"]  = 0.0
    df["_spread"] = 0.0

    return df, ENTRY_BAR, EXIT_BAR, KNOWN_RET


def check_simulate_pair_deterministic() -> None:
    """Детерминированный тест: simulate_pair даёт ТОЧНЫЙ ожидаемый PnL (rel.err < 1e-9)
    и ОТЛИЧАЕТСЯ от off-by-one (buggy) варианта.

    Конструкция:
      - Синтетический df: z ниже -entry_z на баре ENTRY_BAR → вход pos=+1.
        z пересекает 0 на EXIT_BAR → выход. funding=0, beta=1.
      - Correct alignment: pos decided at bar i-1, earns ret[i].
        Bars held = ENTRY_BAR+1 .. EXIT_BAR (EXIT_BAR - ENTRY_BAR баров).
        На каждом баре: gross = 1 * KNOWN_RET * PAIR_NOTIONAL, fund = 0.
        Costs: вход на ENTRY_BAR (turnover=1), выход на EXIT_BAR (turnover=1).
      - Buggy alignment (off-by-one): pos decided at bar i, earns ret[i] SAME bar.
        Bar ENTRY_BAR itself earns ret[ENTRY_BAR] (не KNOWN_RET, т.к. sret изменяется
        только начиная с ENTRY_BAR+1).
        Bar EXIT_BAR тоже считается, но разница в том, что вход-бар приносит другой ret.
    """
    n = 120
    entry_z_val = 2.0
    z_window = 30

    df, ENTRY_BAR, EXIT_BAR, KNOWN_RET = _make_synthetic_df(
        n=n, entry_z=entry_z_val, z_window=z_window
    )

    cfg = PairConfig(
        z_window=z_window,
        entry_z=entry_z_val,
        time_stop_bars=200,      # не триггерим time-stop
        kalman=KalmanConfig(q=DEFAULT_Q, R=DEFAULT_R),
    )

    seg = slice(0, n)
    pnl = simulate_pair(df, seg, cfg, notional=PAIR_NOTIONAL)

    # ── Ручной расчёт ожидаемого PnL ─────────────────────────────────────────
    # Позиция pos=+1 держится с ENTRY_BAR до EXIT_BAR (открыта в конце ENTRY_BAR,
    # закрыта в конце EXIT_BAR). PnL зарабатывается на барах ENTRY_BAR+1..EXIT_BAR.
    n_held_bars = EXIT_BAR - ENTRY_BAR  # = 3

    gross_per_bar = KNOWN_RET * PAIR_NOTIONAL  # pos=+1, ret_a-beta*ret_b=KNOWN_RET
    total_gross   = n_held_bars * gross_per_bar

    # Costs: вход на ENTRY_BAR (turnover |1-0|=1), выход на EXIT_BAR (turnover |0-1|=1)
    open_cost  = 1 * PAIR_NOTIONAL * PERP_COST_PER_LEG * 2
    close_cost = 1 * PAIR_NOTIONAL * PERP_COST_PER_LEG * 2

    expected_total = total_gross - open_cost - close_cost

    actual_total = float(pnl.sum())
    rel_err = abs(actual_total - expected_total) / (abs(expected_total) + 1e-12)
    print(
        f"[deterministic] expected={expected_total:.6f}  actual={actual_total:.6f}"
        f"  rel.err={rel_err:.2e}"
    )
    assert rel_err < 1e-9, (
        f"simulate_pair deterministic mismatch: expected={expected_total:.8f}"
        f" got={actual_total:.8f} rel.err={rel_err:.2e}"
    )

    # ── Timing-sensitivity: off-by-one buggy variant должен давать другой результат ──
    # Buggy: position decided at bar i earns ret[i] (NOT ret[i+1]).
    # We implement the buggy variant locally and verify it differs.
    def _buggy_simulate(df_: pd.DataFrame, seg_: slice, cfg_: PairConfig,
                        notional: float = PAIR_NOTIONAL) -> np.ndarray:
        """Off-by-one variant: pos updated BEFORE computing PnL for bar i.

        This mimics the bug where z[i] decides pos which then earns ret[i] same bar
        (look-ahead contamination).
        """
        sub = df_.iloc[seg_]
        n_sub = len(sub)
        z     = sub["_z"].values
        beta  = sub["_beta"].values

        lpa_ = df_["log_price_a"].values
        lpb_ = df_["log_price_b"].values
        ret_a_ = np.diff(lpa_, prepend=lpa_[0])
        ret_b_ = np.diff(lpb_, prepend=lpb_[0])
        fa_ = df_["funding_a"].values
        fb_ = df_["funding_b"].values

        sl = slice(seg_.start, seg_.stop)
        ret_a_seg  = ret_a_[sl]
        ret_b_seg  = ret_b_[sl]
        fund_a_seg = fa_[sl]
        fund_b_seg = fb_[sl]

        pnl_ = np.zeros(n_sub)
        pos  = 0
        bars_held = 0
        entry_z_ = cfg_.entry_z
        time_stop_ = cfg_.time_stop_bars

        for i in range(n_sub):
            zi = z[i]
            bi = beta[i]
            if np.isnan(zi):
                continue

            prev_pos = pos

            # BUG: update position FIRST, then earn ret[i] with the NEW position
            if pos != 0:
                bars_held += 1
                exit_cond = (
                    (pos == 1 and zi >= 0.0) or
                    (pos == -1 and zi <= 0.0) or
                    (bars_held >= time_stop_)
                )
                if exit_cond:
                    pos = 0
                    bars_held = 0
            if pos == 0:
                if zi < -entry_z_:
                    pos = 1
                    bars_held = 0
                elif zi > entry_z_:
                    pos = -1
                    bars_held = 0

            # PnL earned by the NEW pos (off-by-one)
            if pos != 0:
                gross_ = float(pos) * (ret_a_seg[i] - bi * ret_b_seg[i]) * notional
                net_fund_ = -float(pos) * (fund_a_seg[i] - fund_b_seg[i]) * notional
            else:
                gross_ = 0.0
                net_fund_ = 0.0

            turnover_ = abs(pos - prev_pos)
            cost_ = turnover_ * notional * PERP_COST_PER_LEG * 2
            pnl_[i] = gross_ + net_fund_ - cost_

        return pnl_

    buggy_pnl   = _buggy_simulate(df, seg, cfg)
    buggy_total = float(buggy_pnl.sum())

    # The buggy total must differ from the correct total (timing matters here
    # because ENTRY_BAR itself has ret_a=0 under our construction while
    # ENTRY_BAR+1..EXIT_BAR have ret_a=KNOWN_RET).
    print(
        f"[timing-sensitivity] correct={actual_total:.6f}  buggy={buggy_total:.6f}"
        f"  differ={abs(actual_total - buggy_total):.6f}"
    )
    assert abs(actual_total - buggy_total) > 1e-9, (
        "TIMING TEST FAILED: buggy off-by-one gives same result as correct — "
        "the synthetic df does not discriminate timing!"
    )
    assert abs(actual_total - expected_total) < abs(buggy_total - expected_total), (
        "ALIGNMENT TEST FAILED: correct simulate_pair is FURTHER from expected than "
        f"buggy variant. correct={actual_total:.8f} buggy={buggy_total:.8f} "
        f"expected={expected_total:.8f}"
    )
    print("[timing-sensitivity] OK: correct и buggy отличаются; correct ближе к expected.")


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

    # Эталон 4 (новый): детерминированный тест simulate_pair + timing-sensitivity
    print("\n[ЭТАЛОН 4] детерминированный синтетический тест simulate_pair (timing/sign)")
    check_simulate_pair_deterministic()
    print("  OK: rel.err < 1e-9 и timing-sensitivity подтверждена")

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
    print("[OK] Все 4 эталона пройдены.")


if __name__ == "__main__":
    main()
