"""
Ф5 — форматирование вердикта стенда в человекочитаемый отчёт.

Светофор сверху, чтобы вывод читался за 5 секунд:
  DSR  >0.95 ✅  / 0.5–0.95 ⚠️ / <0.5 ❌   (Sharpe пережил мультитест?)
  PBO  <0.2  ✅  / 0.2–0.5  ⚠️ / >0.5 ❌    (выбор переносится OOS?)
"""
from __future__ import annotations

from harness import HarnessReport
from runner import _DIST_KEYS


def _dsr_light(d: float) -> str:
    return "✅" if d > 0.95 else ("⚠️" if d >= 0.5 else "❌")


def _pbo_light(p: float) -> str:
    return "✅" if p < 0.2 else ("⚠️" if p <= 0.5 else "❌")


def format_report(rep: HarnessReport) -> str:
    L: list[str] = []
    L.append("=" * 72)
    L.append(f"ВЕРДИКТ СТЕНДА — {rep.name}  (выбран: {rep.selected_name})")
    L.append("=" * 72)
    dsr = rep.dsr.get("dsr", float("nan"))
    pbov = rep.pbo.pbo
    L.append(f"  DSR = {dsr:.3f} {_dsr_light(dsr)}    "
             f"PBO = {pbov:.3f} {_pbo_light(pbov)}")
    L.append(f"  монет: {len(rep.coins)} {rep.coins}   "
             f"меню: {len(rep.menu_names)} конфигов   "
             f"портфельных часов: {rep.n_portfolio_hours}")

    L.append("\n— OOS-распределение (CPCV, пул сегментов по монетам) —")
    L.append(f"  сегментов: {rep.pooled_oos.n_segments}")
    L.append(f"  {'metric':<12}{'median':>10}{'IQR_lo':>10}{'IQR_hi':>10}")
    for key in _DIST_KEYS:
        d = rep.pooled_oos.dist.get(key)
        if d:
            L.append(f"  {key:<12}{d['median']:>10.2f}{d['iqr_lo']:>10.2f}{d['iqr_hi']:>10.2f}")
    L.append(f"  доля сегментов Calmar>0: {rep.pooled_oos.frac_calmar_pos*100:5.1f}%   "
             f"Sharpe>0: {rep.pooled_oos.frac_sharpe_pos*100:5.1f}%")

    L.append("\n— Deflated Sharpe (поправка на мультитест) —")
    L.append(f"  SR̂(поперодный) = {rep.dsr['sr_hat']:.4f}   "
             f"порог SR*_defl = {rep.dsr['sr_star_deflated']:.4f}")
    L.append(f"  PSR vs 0 = {rep.dsr['psr_vs_zero']:.3f}   →   DSR = {dsr:.3f} {_dsr_light(dsr)}")
    L.append(f"  trials(меню) = {rep.dsr['n_trials']}   T = {rep.dsr['T']}   "
             f"skew = {rep.dsr['skew']:.2f}  kurt = {rep.dsr['kurt']:.2f}")

    L.append("\n— PBO (CSCV: переносится ли выбор лучшего IS-конфига) —")
    L.append(f"  PBO = {pbov:.3f} {_pbo_light(pbov)}   "
             f"сплитов: {rep.pbo.n_splits}   медианный OOS-ранг IS-лучшего: "
             f"{rep.pbo.median_oos_rank:.2f}")
    if rep.pbo.is_best_counts:
        top = list(rep.pbo.is_best_counts.items())[:5]
        L.append("  чаще всего IS-лучший: " +
                 ", ".join(f"{n}×{c}" for n, c in top))

    L.append("\n— per-coin (медиана OOS Calmar) —")
    for c, r in rep.per_coin_oos.items():
        med = r.dist.get("calmar", {}).get("median", float("nan"))
        L.append(f"  {c:<6} {med:6.2f}")
    L.append("=" * 72)
    return "\n".join(L)


def print_report(rep: HarnessReport) -> None:
    print(format_report(rep))
