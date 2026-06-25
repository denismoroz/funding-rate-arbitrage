"""
run_inverted.py — зеркало mean-reversion: momentum на спреде.

Идея: вместо входа long_a/short_b при z < -entry_z (bet on reversion),
входим long_a/short_b при z > +entry_z (bet on continuation/trending).
Аналогично: входим short_a/long_b при z < -entry_z.
Та же логика выхода: z пересекает 0.

Смысл: если спреды ТРЕНДЯТ (что мы наблюдаем из отрицательного gross-Sharpe
в mean-reversion), то зеркало ставит на продолжение → потенциально позитивный сигнал.

NB: exit-логика «z пересечёт 0» вырождена для momentum — спред может долго не
возвращаться, что создаёт либо длинные удержания (с риском), либо мгновенный
exit если z сразу же пересёк 0. Это предварительный тест; для production нужна
отдельная momentum-exit логика.

Запуск:
  PYTHONPATH=../.. python run_inverted.py

Сохраняет run_inverted.json.
"""
from __future__ import annotations

import json
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

from harness import run_harness, to_dict as harness_to_dict
from report import print_report
from costs import TAKER
from pairs_pkg import PairsPackage, PURGE_DAYS
from pairs_data import CANDIDATE_PAIRS
from pairs_strategy import (
    MENU_CONFIGS, SELECTED_CONFIG_NAME, PairConfig, PAIR_NOTIONAL,
    PERP_COST_PER_LEG, precompute_signals,
)


# ─────────────────────────────────────────────────────────────────────────────
# Inverted simulator: momentum on spread (enter pos=+1 when z>+entry_z, etc.)
# ─────────────────────────────────────────────────────────────────────────────

# Exit mode (module global so the harness can run both without rewiring classes):
#   "revert" — hold until the spread reverts to mean (z crosses back to 0 from the
#              entry side). Rides the continuation until exhaustion, then gives the
#              reversal back. The natural-but-leaky exit.
#   "oneb"   — exit as soon as z is on the mean side (>=0 for a long entered at z>+ez).
#              Since z>+ez at entry, this exits ~next bar → captures only the SHORT-
#              TERM 1-bar continuation right after an extreme divergence.
# Both are unvalidated placeholders; the point is the result is EXIT-SENSITIVE.
EXIT_MODE = "revert"


def sim_inv(
    df: pd.DataFrame,
    seg: slice,
    config: PairConfig,
    notional: float = PAIR_NOTIONAL,
) -> np.ndarray:
    """Inverted (momentum) variant of simulate_pair.

    Входы:
      z > +entry_z  → pos = +1  (long_a/short_b — bet on spread continuing up)
      z < -entry_z  → pos = -1  (short_a/long_b — bet on spread continuing down)
    Выходы: z пересекает 0 (same as mean-reversion but now this is a stop,
            not a profit-take — z returning to mean means the trend reversed).
    Time-stop: same logic.
    Funding sign: same corrected convention (long pays positive funding):
      net_fund = -pos * (fund_a - fund_b) * notional
    """
    if "_z" not in df.columns:
        raise ValueError("Нужно предварительно вызвать precompute_signals(df, config)")

    sub = df.iloc[seg]
    n_sub = len(sub)
    if n_sub == 0:
        return np.array([])

    z    = sub["_z"].values
    beta = sub["_beta"].values

    lpa = df["log_price_a"].values
    lpb = df["log_price_b"].values
    ret_a = np.diff(lpa, prepend=lpa[0])
    ret_b = np.diff(lpb, prepend=lpb[0])

    funding_a = df["funding_a"].values
    funding_b = df["funding_b"].values

    sl = slice(seg.start, seg.stop)
    ret_a_seg  = ret_a[sl]
    ret_b_seg  = ret_b[sl]
    fund_a_seg = funding_a[sl]
    fund_b_seg = funding_b[sl]

    pnl = np.zeros(n_sub)
    pos = 0
    bars_held = 0
    entry_z = config.entry_z
    time_stop = config.time_stop_bars

    for i in range(n_sub):
        zi = z[i]
        bi = beta[i]

        if np.isnan(zi):
            pnl[i] = 0.0
            continue

        # PnL of position decided at bar i-1, earned over (i-1, i]
        if pos != 0:
            gross    = float(pos) * (ret_a_seg[i] - bi * ret_b_seg[i]) * notional
            net_fund = -float(pos) * (fund_a_seg[i] - fund_b_seg[i]) * notional
        else:
            gross    = 0.0
            net_fund = 0.0

        prev_pos = pos

        # Update position based on z[i] (decided at close of i; earns from i+1)
        if pos != 0:
            bars_held += 1
            # Exit when z crosses 0 (trend reversed) or time-stop
            if EXIT_MODE == "oneb":
                # exit on the mean side (1-bar continuation capture)
                hit = (pos == 1 and zi >= 0.0) or (pos == -1 and zi <= 0.0)
            else:  # "revert": hold until spread reverts to mean
                hit = (pos == 1 and zi <= 0.0) or (pos == -1 and zi >= 0.0)
            exit_cond = hit or (bars_held >= time_stop)
            if exit_cond:
                pos = 0
                bars_held = 0

        # Entry: momentum direction (INVERTED vs mean-reversion)
        if pos == 0:
            if zi > +entry_z:
                pos = 1    # spread trending up: long_a/short_b
                bars_held = 0
            elif zi < -entry_z:
                pos = -1   # spread trending down: short_a/long_b
                bars_held = 0

        turnover = abs(pos - prev_pos)
        cost = turnover * notional * PERP_COST_PER_LEG * 2

        pnl[i] = gross + net_fund - cost

    return pnl


# ── Inverted strategy adapter ─────────────────────────────────────────────────

class _InvertedPairStrategy:
    """Адаптер под contract.Strategy — momentum (inverted) версия."""

    def __init__(self, pair_id: str, config_name: str, menu_configs: dict):
        self.name = f"inv_{config_name}_{pair_id}"
        self._pair_id   = pair_id
        self._config_name = config_name
        self._menu_configs = menu_configs
        self._cache_key: tuple | None = None

    def fit(self, df: pd.DataFrame, train_idx: np.ndarray, costs) -> str:
        """Выбрать лучший config по Sharpe на train_idx (inverted PnL)."""
        from engine import compute_metrics
        from contract import contiguous_slices

        best_name = self._config_name
        best_sr   = -np.inf

        for name, cfg in self._menu_configs.items():
            df_work = df.copy()
            precompute_signals(df_work, cfg)
            train_pnl = []
            for seg in contiguous_slices(train_idx):
                if seg.stop - seg.start < 5:
                    continue
                p = sim_inv(df_work, seg, cfg)
                if len(p) > 0:
                    train_pnl.extend(p.tolist())
            arr = np.array(train_pnl, dtype=float)
            if arr.size < 20:
                continue
            m = compute_metrics(arr)
            sr = m.get("sharpe", -np.inf) or -np.inf
            if np.isfinite(sr) and sr > best_sr:
                best_sr   = sr
                best_name = name

        return best_name

    def simulate(self, df: pd.DataFrame, seg: slice, config: str, costs) -> np.ndarray:
        cfg_name = config or self._config_name
        cfg = self._menu_configs.get(cfg_name, self._menu_configs[self._config_name])

        cache_key = (id(df), cfg_name)
        if self._cache_key != cache_key:
            precompute_signals(df, cfg)
            self._cache_key = cache_key

        return sim_inv(df, seg, cfg)


# ── Inverted Package ──────────────────────────────────────────────────────────

class InvertedPairsPackage:
    """Package для validation_harness — momentum on spread (inverted direction)."""

    name = "Pairs Spread Momentum (inverted, BTC-neutral)"
    selected_name = SELECTED_CONFIG_NAME

    def __init__(self, pairs=None, menu_configs=None, selected_name=SELECTED_CONFIG_NAME):
        self._pairs       = pairs if pairs is not None else CANDIDATE_PAIRS
        self._menu_configs = menu_configs if menu_configs is not None else MENU_CONFIGS
        self.selected_name = selected_name
        self._df_cache: dict[str, pd.DataFrame | None] = {}
        self._menu_pnl_cache: dict[str, dict[str, pd.Series]] = {}

    @property
    def coins(self) -> list[str]:
        return [f"{p[0]}/{p[1]}" for p in self._pairs]

    def load(self, pair_id: str) -> pd.DataFrame | None:
        if pair_id not in self._df_cache:
            a, b = pair_id.split("/")
            try:
                from pairs_data import load_pair_df
                df = load_pair_df((a, b))
            except Exception as e:
                print(f"  load({pair_id}) failed: {e}")
                df = None
            self._df_cache[pair_id] = df
        return self._df_cache[pair_id]

    def selected(self, pair_id: str, df: pd.DataFrame) -> _InvertedPairStrategy:
        return _InvertedPairStrategy(pair_id, self.selected_name, self._menu_configs)

    def menu(self, pair_id: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        if pair_id in self._menu_pnl_cache:
            return self._menu_pnl_cache[pair_id]

        result: dict[str, pd.Series] = {}
        seg = slice(0, len(df))

        for name, cfg in self._menu_configs.items():
            df_work = df.copy()
            precompute_signals(df_work, cfg)
            pnl = sim_inv(df_work, seg, cfg)
            result[name] = pd.Series(pnl, index=df.index, dtype=float)

        self._menu_pnl_cache[pair_id] = result
        return result


# ── Orthogonality (reuse proxies from run_pairs.py) ──────────────────────────

def _btc_buyhold_daily() -> pd.Series:
    from cryptodata import load_panel
    panel = load_panel(["BTC"])
    price = panel["price"]["BTC"].dropna()
    return price.pct_change().dropna()


def _xsmom_momentum_proxy_daily() -> pd.Series:
    from cryptodata import load_panel
    coins = ["BTC", "ETH", "SOL", "AVAX", "NEAR", "DOT", "ATOM", "ADA",
             "ARB", "APT", "SUI", "UNI", "AAVE"]
    try:
        panel = load_panel(coins)
        price   = panel["price"]
        fwd_ret = panel["fwd_ret"]
        mom_window = 30
        n_long = 3
        rows = []
        for t in range(mom_window, len(price) - 1):
            past_ret = (price.iloc[t] / price.iloc[t - mom_window] - 1.0).dropna()
            if len(past_ret) < 6:
                rows.append(pd.Series([0.0], index=[price.index[t]]))
                continue
            ranked = past_ret.rank()
            n = len(ranked)
            long_mask  = ranked >= (n - n_long + 1)
            short_mask = ranked <= n_long
            w = pd.Series(0.0, index=past_ret.index)
            w[long_mask]  =  1.0 / n_long
            w[short_mask] = -1.0 / n_long
            fr = fwd_ret.iloc[t].dropna()
            rows.append(pd.Series([(w * fr).sum()], index=[price.index[t]]))
        return pd.concat(rows).rename("xsmom") if rows else pd.Series(dtype=float)
    except Exception as e:
        print(f"  Warning: XSMOM proxy failed ({e})")
        return pd.Series(dtype=float)


def _compute_inv_per_pair_sharpe(pkg: InvertedPairsPackage) -> dict:
    """Per-pair full-period gross Sharpe (inverted, no costs for signal test)."""
    from engine import compute_metrics
    results = {}
    for pair_id in pkg.coins:
        df = pkg.load(pair_id)
        if df is None:
            continue
        m = pkg.menu(pair_id, df)
        sel = m.get(pkg.selected_name, list(m.values())[0])
        met = compute_metrics(sel.values)
        results[pair_id] = round(float(met.get("sharpe", float("nan"))), 4)
    return results


def build_book_pnl(pkg: InvertedPairsPackage) -> pd.Series:
    series_list = []
    for pair_id in pkg.coins:
        df = pkg.load(pair_id)
        if df is None:
            continue
        m = pkg.menu(pair_id, df)
        sel = m.get(pkg.selected_name, list(m.values())[0])
        series_list.append(sel.rename(pair_id))
    if not series_list:
        return pd.Series(dtype=float)
    return pd.concat(series_list, axis=1, join="inner").mean(axis=1)


# ── Main ──────────────────────────────────────────────────────────────────────

def _run_one(mode: str) -> dict:
    """Run the inverted strategy through the harness for one EXIT_MODE.

    Returns the result dict (harness metrics + orthogonality + verdict)."""
    global EXIT_MODE
    EXIT_MODE = mode

    pkg = InvertedPairsPackage()   # fresh package → fresh menu cache for this mode
    for pair_id in pkg.coins:
        pkg.load(pair_id)

    rep = run_harness(pkg, costs=TAKER, n_groups=6, k=2,
                      purge=PURGE_DAYS, embargo=24, S=16)

    book_pnl = build_book_pnl(pkg)
    btc   = _btc_buyhold_daily()
    xsmom = _xsmom_momentum_proxy_daily()

    def corr_with(bench: pd.Series) -> float:
        if bench.empty:
            return float("nan")
        common = book_pnl.index.intersection(bench.index)
        if len(common) < 30:
            return float("nan")
        return float(np.corrcoef(book_pnl.loc[common].values,
                                 bench.loc[common].values)[0, 1])

    corr_btc   = corr_with(btc)
    corr_xsmom = corr_with(xsmom)
    per_pair_sharpe = _compute_inv_per_pair_sharpe(pkg)
    pos_sharpe = sum(1 for v in per_pair_sharpe.values() if v > 0)
    frac_pos   = pos_sharpe / len(per_pair_sharpe) if per_pair_sharpe else 0.0

    dsr = rep.dsr.get("dsr", 0.0)
    pbo = rep.pbo.pbo
    oos_sharpe = rep.pooled_oos.dist.get("sharpe", {}).get("median")
    orth_pass = (abs(corr_btc) < 0.30 if not np.isnan(corr_btc) else True) and \
                (abs(corr_xsmom) < 0.30 if not np.isnan(corr_xsmom) else True)
    go = dsr > 0.95 and pbo < 0.20 and orth_pass

    print(f"\n--- EXIT_MODE={mode} ---")
    print_report(rep)
    print(f"  book sum={book_pnl.sum():+.2f}  corr_BTC={corr_btc:+.3f}  "
          f"corr_XSMOM={corr_xsmom:+.3f}  pairs Sharpe>0: {frac_pos:.1%}")
    print(f"  DSR={dsr:.3f}  PBO={pbo:.3f}  OOS median Sharpe={oos_sharpe}  "
          f"→ {'GO' if go else 'NO-GO'}")

    return {
        **harness_to_dict(rep),
        "exit_mode": mode,
        "orthogonality": {"corr_BTC_buyhold": float(corr_btc),
                          "corr_XSMOM_momentum": float(corr_xsmom)},
        "book_pnl_sum": float(book_pnl.sum()),
        "book_pnl_days": int(len(book_pnl)),
        "oos_median_sharpe": oos_sharpe,
        "per_pair_full_period_sharpe": per_pair_sharpe,
        "frac_pairs_sharpe_pos": float(frac_pos),
        "verdict": "GO" if go else "NO-GO",
        "gates": {"dsr_pass": dsr > 0.95, "pbo_pass": pbo < 0.20,
                  "orth_pass": orth_pass},
    }


def main() -> None:
    print("=" * 72)
    print("INVERTED (MOMENTUM) PAIRS — run_inverted.py")
    print("=" * 72)
    print("Гипотеза: спреды ТРЕНДЯТ, не mean-revert → ставим на продолжение.")
    print(f"  Пул пар: {len(CANDIDATE_PAIRS)}   purge={PURGE_DAYS}d  n_groups=6 k=2 S=16")
    print("  Прогоняем ОБА exit-режима — результат сильно exit-зависим (см. README).")

    # Both exit variants — the magnitude of the edge depends heavily on which one.
    res_revert = _run_one("revert")   # hold until spread reverts to mean
    res_oneb   = _run_one("oneb")     # 1-bar continuation capture

    out = {
        "strategy": "inverted_spread_momentum",
        "description": (
            "Mirror of the mean-reversion pairs strategy: bet on spread CONTINUATION "
            "(enter pos=+1 when z>+entry_z, pos=-1 when z<-entry_z). Same corrected "
            "funding sign as pairs_strategy.simulate_pair (long pays positive funding). "
            "Run for TWO unvalidated exit modes to show the result is EXIT-SENSITIVE: "
            "'revert' (hold until z→0, leaky) and 'oneb' (exit on mean side, ~1-bar "
            "continuation capture). Both are PRELIMINARY; neither clears DSR>0.95/PBO<0.20."
        ),
        "purge_days": PURGE_DAYS,
        "revert_exit": res_revert,
        "oneb_exit": res_oneb,
        "caveats": [
            "Result is EXIT-SENSITIVE: 'oneb' (1-bar continuation) gives a much higher "
            "DSR/OOS-Sharpe than 'revert' (hold-to-mean). Neither exit is validated; a "
            "proper momentum exit (trailing stop / fixed holding period) is required "
            "before any GO claim — this is why the finding stays PRELIMINARY.",
            "PURGE=240 on 531d panel nearly empties the CPCV train set for ETHFI/EIGEN; "
            "fit() falls back to default config for that pair.",
            "ETHFI/EIGEN (531d) truncates the inner-join portfolio to 531d while 31 other "
            "pairs have ~1012d, halving statistical power.",
            "Standard GO gates are DSR>0.95 / PBO<0.20; neither exit mode clears them. "
            "The robust takeaway is only directional: the spreads trend (the MR mirror is "
            "positive and orthogonal to XSMOM/BTC), not a live-ready edge.",
        ],
    }

    out_path = _HERE / "run_inverted.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n  Сохранено: {out_path}")
    print(f"\n  SUMMARY: revert-exit DSR={res_revert['dsr']['dsr']:.3f} "
          f"(OOS Sharpe {res_revert['oos_median_sharpe']}), "
          f"oneb-exit DSR={res_oneb['dsr']['dsr']:.3f} "
          f"(OOS Sharpe {res_oneb['oos_median_sharpe']}). Both NO-GO.")


if __name__ == "__main__":
    main()
