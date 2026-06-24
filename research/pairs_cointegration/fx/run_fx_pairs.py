"""
Ф8 — Вердикт FX pairs: run_harness(FXPairsPackage()) + orthogonality gate.

Запуск из research/pairs_cointegration/fx/:
  ../../../.venv/bin/python run_fx_pairs.py

Сохраняет run_fx_pairs.json.

Orthogonality-бенчмарки (PLAN §2b адаптированы для FX):
  (a) USD-фактор buy&hold — equal-weight long всех 9 XXXUSD = USD-weakening bet.
      Наша стратегия нейтрализована к нему (USD-residual), поэтому |corr| должна
      быть низкой — хороший контроль.
  (b) FX carry (FRAB-аналог) — equal-weight long высокодоходных (AUD/NZD/NOK),
      short низкодоходных (JPY/CHF) по carry ранжировке — аналог frab-carry из
      крипты (earned-while-holding carry). Если наша стратегия просто carry-picking,
      |corr| будет высокой.
  (c) FX momentum (XSMOM-аналог) — 12-1 cross-sectional momentum (long top-3,
      short bottom-3 по 12m return) — аналог xsmom из крипты.

Примечание: крипто-прокси (BTC, HL funding) НЕ используются, т.к. данные
отдельны от FX-панели и пересечение временных рядов не гарантировано. Используем
FX-специфичные прокси — они более релевантны для FX-портфеля и явно отмечены.
"""
from __future__ import annotations

import json
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

from harness import run_harness, to_dict as harness_to_dict
from report import print_report
from costs import TAKER

from fx_pairs_pkg import FXPairsPackage, PURGE_DAYS
from fx_pairs_data import ALL_PAIRS, _get_panel, CURRENCIES


# ── Orthogonality benchmarks ──────────────────────────────────────────────────

def _usd_factor_buyhold() -> pd.Series:
    """USD-фактор buy&hold: equal-weight long всех 9 XXXUSD.

    Возвращает дневную fwd_ret equal-weight (движение без затрат).
    USD слабеет → все XXXUSD растут → положительный return.
    Служит контрольным бенчмарком: USD-neutral стратегия должна быть некоррелирована.
    """
    P = _get_panel()
    fwd_ret = P["fwd_ret"]   # per-currency next-day return
    # Equal-weight mean (dollar-neutral в сторону "buy everything vs USD")
    ew = fwd_ret.mean(axis=1).dropna()
    return ew.rename("usd_factor_buyhold")


def _fx_carry_proxy() -> pd.Series:
    """FX carry proxy (аналог FRAB carry).

    Long top-3 по rate differential vs USD, short bottom-3.
    Перебалансировка ежемесячно (~21 дней).
    Нейтрализован к USD-фактору (equal-weight long−short).
    Аналог FRAB-carry: earn while holding high-yield currencies.
    """
    P = _get_panel()
    fwd_ret = P["fwd_ret"]
    short_rate = P["short_rate"]
    usd_rate = P["usd_rate"]["USD"]

    rate_diff = short_rate.sub(usd_rate, axis=0)   # foreign − USD, %p.a.

    n = len(fwd_ret)
    rebal_every = 21
    pnl_rows: list[float] = []
    idx_list: list = []

    weights = pd.Series(0.0, index=CURRENCIES)
    for t in range(n - 1):
        if t % rebal_every == 0:
            rd = rate_diff.iloc[t].dropna()
            if len(rd) >= 6:
                ranked = rd.rank()
                n_leg = 3
                longs  = ranked.nlargest(n_leg).index
                shorts = ranked.nsmallest(n_leg).index
                weights = pd.Series(0.0, index=CURRENCIES)
                weights[longs]  =  1.0 / n_leg
                weights[shorts] = -1.0 / n_leg

        fr = fwd_ret.iloc[t]
        pnl_t = float((weights * fr).sum())
        pnl_rows.append(pnl_t)
        idx_list.append(fwd_ret.index[t])

    s = pd.Series(pnl_rows, index=idx_list, name="fx_carry_proxy")
    return s.dropna()


def _fx_momentum_proxy() -> pd.Series:
    """FX momentum proxy (аналог XSMOM momentum).

    12-month−1-month cross-sectional momentum:
    Long top-3, short bottom-3 по 11m return (skip 1m reversal).
    Ежемесячная перебалансировка.
    """
    P = _get_panel()
    price = P["price"]
    fwd_ret = P["fwd_ret"]

    n = len(price)
    mom_window = 252   # ~12 месяцев
    skip = 21          # skip 1 месяц
    rebal_every = 21
    n_leg = 3

    pnl_rows: list[float] = []
    idx_list: list = []

    weights = pd.Series(0.0, index=CURRENCIES)
    for t in range(mom_window + skip, n - 1):
        if (t - mom_window - skip) % rebal_every == 0:
            # 12m−1m return: от t−mom_window до t−skip
            p_start = price.iloc[t - mom_window]
            p_end   = price.iloc[t - skip]
            mom = (p_end / p_start - 1.0).dropna()
            if len(mom) >= 6:
                ranked = mom.rank()
                longs  = ranked.nlargest(n_leg).index
                shorts = ranked.nsmallest(n_leg).index
                weights = pd.Series(0.0, index=CURRENCIES)
                weights[longs]  =  1.0 / n_leg
                weights[shorts] = -1.0 / n_leg

        fr = fwd_ret.iloc[t]
        pnl_t = float((weights * fr).sum())
        pnl_rows.append(pnl_t)
        idx_list.append(fwd_ret.index[t])

    s = pd.Series(pnl_rows, index=idx_list, name="fx_momentum_proxy")
    return s.dropna()


def compute_orthogonality(book_pnl: pd.Series) -> dict[str, float]:
    """Корреляция book PnL к трём FX-бенчмаркам."""
    benchmarks = {
        "corr_USD_factor_buyhold": _usd_factor_buyhold(),
        "corr_FX_carry_proxy":     _fx_carry_proxy(),
        "corr_FX_momentum_proxy":  _fx_momentum_proxy(),
    }

    result: dict[str, float] = {}
    for name, bench in benchmarks.items():
        if bench.empty:
            print(f"  Warning: {name} proxy empty")
            result[name] = float("nan")
            continue
        common = book_pnl.index.intersection(bench.index)
        if len(common) < 30:
            print(f"  Warning: {name} overlap < 30 days ({len(common)})")
            result[name] = float("nan")
            continue
        c = float(np.corrcoef(
            book_pnl.loc[common].values,
            bench.loc[common].values,
        )[0, 1])
        result[name] = c

    return result


def build_book_pnl(pkg: FXPairsPackage) -> pd.Series:
    """Equal-weight book PnL из selected config по всем парам."""
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
    print("FX PAIRS COINTEGRATION — Ф8: ВЕРДИКТ")
    print("=" * 72)
    print(f"  Пул пар: {len(ALL_PAIRS)} (все C(9,2) G10)")
    print(f"  purge: {PURGE_DAYS} дней  embargo: 24  n_groups=6 k=2")
    print("  Бенчмарки orthogonality: USD-factor, FX-carry, FX-momentum (все FX, не крипто)")

    pkg = FXPairsPackage()
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
    print("ORTHOGONALITY GATE")
    print("=" * 72)
    print("  (Бенчмарки — FX-специфичные, не крипто; крипто-прокси не подходят)")
    print("  Строю book PnL...")
    book_pnl = build_book_pnl(pkg)
    print(f"  book PnL: {len(book_pnl)} days, sum={book_pnl.sum():.2f}")

    orth = compute_orthogonality(book_pnl)
    print(f"\n  {'benchmark':<30}{'corr':>8}{'gate':>8}")
    for name, corr in orth.items():
        gate = "PASS" if abs(corr) < 0.30 else "FAIL"
        print(f"  {name:<30}{corr:>8.3f}{gate:>8}")

    # ── Светофор ─────────────────────────────────────────────────────────────
    dsr = rep.dsr.get("dsr", 0.0)
    pbo = rep.pbo.pbo
    orth_pass = all(abs(v) < 0.30 for v in orth.values() if not np.isnan(v))
    go = dsr > 0.95 and pbo < 0.20 and orth_pass

    oos_dist = rep.pooled_oos.dist
    sharpe_median = oos_dist.get("sharpe", {}).get("median", float("nan"))
    calmar_median = oos_dist.get("calmar", {}).get("median", float("nan"))
    frac_sharpe_pos = rep.pooled_oos.frac_sharpe_pos
    n_segments = rep.pooled_oos.n_segments

    print("\n" + "=" * 72)
    print("ИТОГОВЫЙ ВЕРДИКТ")
    print("=" * 72)
    print(f"  DSR={dsr:.3f}           {'OK' if dsr > 0.95 else 'FAIL'} (порог >0.95)")
    print(f"  PBO={pbo:.3f}           {'OK' if pbo < 0.20 else 'FAIL'} (порог <0.20)")
    print(f"  OOS медиана Sharpe: {sharpe_median:.3f}   ({n_segments} сегментов)")
    print(f"  OOS медиана Calmar: {calmar_median:.3f}")
    print(f"  Доля сегментов Sharpe>0: {frac_sharpe_pos*100:.1f}%")
    print(f"  Orth gate: {'OK' if orth_pass else 'FAIL'} (|corr|<0.30 для всех)")
    print(f"\n  {'GO — рассмотреть live' if go else 'NO-GO — закрыть гипотезу'}")

    # ── Сохранить JSON ────────────────────────────────────────────────────────
    result = {
        **harness_to_dict(rep),
        "purge_days": PURGE_DAYS,
        "orthogonality": orth,
        "orthogonality_note": (
            "Benchmarks are FX-specific (USD factor, FX carry, FX momentum); "
            "crypto proxies (BTC, HL funding) were NOT used because the FX panel "
            "is separate and intersection is not guaranteed."
        ),
        "book_pnl_sum": float(book_pnl.sum()),
        "book_pnl_days": int(len(book_pnl)),
        "verdict": "GO" if go else "NO-GO",
        "gates": {
            "dsr_pass": dsr > 0.95,
            "pbo_pass": pbo < 0.20,
            "orth_pass": orth_pass,
        },
        "oos_summary": {
            "sharpe_median": float(sharpe_median) if not np.isnan(sharpe_median) else None,
            "calmar_median": float(calmar_median) if not np.isnan(calmar_median) else None,
            "frac_sharpe_pos": float(frac_sharpe_pos),
            "n_segments": int(n_segments),
        },
    }

    out_path = _HERE / "run_fx_pairs.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n  Сохранено: {out_path}")


if __name__ == "__main__":
    main()
