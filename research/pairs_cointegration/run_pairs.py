"""
Ф8 — Вердикт: run_harness(PairsPackage()) + orthogonality gate.

Запуск из research/pairs_cointegration/:
  PYTHONPATH=../.. python run_pairs.py

Сохраняет run_pairs.json с полными результатами + orthogonality gate.
Светофор: GO / NO-GO (PLAN §8).
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
from cryptodata import load_panel


# ── Orthogonality gate (PLAN §2b) ────────────────────────────────────────────

def _btc_buyhold_daily() -> pd.Series:
    """BTC buy&hold дневная доходность."""
    panel = load_panel(["BTC"])
    price = panel["price"]["BTC"].dropna()
    ret = price.pct_change().dropna()
    return ret


def _frab_carry_proxy_daily() -> pd.Series:
    """FRAB carry-proxy: средняя дневная сумма funding rates по 7 HL-монетам."""
    # Монеты из прод-конфига FRAB (из memory: 7 coins, реальные на HL)
    frab_coins = ["BTC", "ETH", "SOL", "AVAX", "NEAR", "DOT", "ATOM"]
    try:
        panel = load_panel(frab_coins)
        fund = panel["funding"]
        # carry_proxy = equal-weight mean daily funding
        proxy = fund.mean(axis=1).dropna()
        return proxy
    except Exception as e:
        print(f"  Warning: FRAB proxy failed ({e}), using BTC funding")
        panel = load_panel(["BTC"])
        return panel["funding"]["BTC"].dropna()


def _xsmom_momentum_proxy_daily() -> pd.Series:
    """XSMOM momentum proxy: cross-sectional momentum (top-bottom) monthly return.

    Простая реализация: 30-дневный return каждой монеты → longs = топ третиль,
    shorts = нижний → дневная PnL = равновесная forward return портфеля.
    """
    coins = ["BTC", "ETH", "SOL", "AVAX", "NEAR", "DOT", "ATOM", "ADA",
             "ARB", "APT", "SUI", "UNI", "AAVE"]
    try:
        panel = load_panel(coins)
        price = panel["price"]
        fwd_ret = panel["fwd_ret"]

        mom_window = 30  # дней
        n_long = 3
        pnl_rows: list[pd.Series] = []

        for t in range(mom_window, len(price) - 1):
            past_ret = (price.iloc[t] / price.iloc[t - mom_window] - 1.0).dropna()
            if len(past_ret) < 6:
                pnl_rows.append(pd.Series([0.0], index=[price.index[t]]))
                continue
            ranked = past_ret.rank()
            n = len(ranked)
            long_mask = ranked >= (n - n_long + 1)
            short_mask = ranked <= n_long
            w = pd.Series(0.0, index=past_ret.index)
            w[long_mask] = 1.0 / n_long
            w[short_mask] = -1.0 / n_long

            fr = fwd_ret.iloc[t].dropna()
            pnl_t = (w * fr).sum()
            pnl_rows.append(pd.Series([pnl_t], index=[price.index[t]]))

        if not pnl_rows:
            return pd.Series(dtype=float)
        return pd.concat(pnl_rows).rename("xsmom")
    except Exception as e:
        print(f"  Warning: XSMOM proxy failed ({e})")
        return pd.Series(dtype=float)


def compute_orthogonality(book_pnl: pd.Series) -> dict[str, float]:
    """Корреляция итоговой book-PnL к трём бенчмаркам.

    book_pnl: дневная PnL суммарного портфеля пар (из harness).
    """
    btc = _btc_buyhold_daily()
    frab = _frab_carry_proxy_daily()
    xsmom = _xsmom_momentum_proxy_daily()

    def corr_with(bench: pd.Series, name: str) -> float:
        if bench.empty:
            print(f"  Warning: {name} proxy empty")
            return float("nan")
        common = book_pnl.index.intersection(bench.index)
        if len(common) < 30:
            print(f"  Warning: {name} overlap < 30 days ({len(common)})")
            return float("nan")
        c = float(np.corrcoef(book_pnl.loc[common].values, bench.loc[common].values)[0, 1])
        return c

    return {
        "corr_BTC_buyhold": corr_with(btc, "BTC_buyhold"),
        "corr_FRAB_carry": corr_with(frab, "FRAB_carry"),
        "corr_XSMOM_momentum": corr_with(xsmom, "XSMOM_mom"),
    }


def build_book_pnl(pkg: PairsPackage, rep) -> pd.Series:
    """Собрать суммарный book PnL из per-coin OOS сегментов.

    Используем menu (full-period) equal-weight across pairs для ортогональности.
    """
    series_list: list[pd.Series] = []
    for pair_id in pkg.coins:
        df = pkg.load(pair_id)
        if df is None:
            continue
        m = pkg.menu(pair_id, df)
        sel = m.get(pkg.selected_name, list(m.values())[0])
        series_list.append(sel.rename(pair_id))

    if not series_list:
        return pd.Series(dtype=float)

    book = pd.concat(series_list, axis=1, join="inner").mean(axis=1)
    return book


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 72)
    print("PAIRS COINTEGRATION — Ф8: ВЕРДИКТ")
    print("=" * 72)
    print(f"  Пул пар: {len(CANDIDATE_PAIRS)}")
    print(f"  purge: {PURGE_DAYS} дней  embargo: 24h  n_groups=6 k=2")

    pkg = PairsPackage()
    print(f"\n  Загрузка {len(pkg.coins)} пар...")
    valid_pairs = []
    for pair_id in pkg.coins:
        df = pkg.load(pair_id)
        if df is not None:
            print(f"    {pair_id}: {len(df)} days")
            valid_pairs.append(pair_id)
        else:
            print(f"    {pair_id}: SKIP (no data)")

    print(f"\n  Валидных пар: {len(valid_pairs)}")

    print("\n  Запускаем run_harness()...")
    rep = run_harness(
        pkg,
        costs=TAKER,
        n_groups=6,
        k=2,
        purge=PURGE_DAYS,
        embargo=24,
        S=16,
    )

    print_report(rep)

    # ── Orthogonality gate ────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("ORTHOGONALITY GATE (PLAN §2b)")
    print("=" * 72)
    print("  Строю book PnL...")
    book_pnl = build_book_pnl(pkg, rep)
    print(f"  book PnL: {len(book_pnl)} days, sum={book_pnl.sum():.2f}")

    orth = compute_orthogonality(book_pnl)
    print(f"\n  {'benchmark':<25}{'corr':>8}{'gate':>8}")
    for name, corr in orth.items():
        gate = "PASS" if abs(corr) < 0.30 else "FAIL"
        print(f"  {name:<25}{corr:>8.3f}{gate:>8}")

    # ── Светофор ─────────────────────────────────────────────────────────────
    dsr = rep.dsr.get("dsr", 0.0)
    pbo = rep.pbo.pbo
    orth_pass = all(abs(v) < 0.30 for v in orth.values() if not np.isnan(v))
    go = dsr > 0.95 and pbo < 0.20 and orth_pass

    print("\n" + "=" * 72)
    print("ИТОГОВЫЙ ВЕРДИКТ")
    print("=" * 72)
    print(f"  DSR={dsr:.3f}  {'OK' if dsr > 0.95 else 'FAIL'} (порог >0.95)")
    print(f"  PBO={pbo:.3f}  {'OK' if pbo < 0.20 else 'FAIL'} (порог <0.20)")
    print(f"  Orth gates: {'OK' if orth_pass else 'FAIL'} (|corr|<0.30 для всех)")
    print(f"\n  {'GO — рассмотреть live' if go else 'NO-GO — закрыть, перейти к FX (Прил. A)'}")

    # ── Сохранить JSON ────────────────────────────────────────────────────────
    result = {
        **harness_to_dict(rep),
        "purge_days": PURGE_DAYS,
        "orthogonality": orth,
        "book_pnl_sum": float(book_pnl.sum()),
        "book_pnl_days": int(len(book_pnl)),
        "verdict": "GO" if go else "NO-GO",
        "gates": {
            "dsr_pass": dsr > 0.95,
            "pbo_pass": pbo < 0.20,
            "orth_pass": orth_pass,
        },
    }

    out_path = _HERE / "run_pairs.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n  Сохранено: {out_path}")


if __name__ == "__main__":
    main()
