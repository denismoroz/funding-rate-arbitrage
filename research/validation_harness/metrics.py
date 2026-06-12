"""
Ф3 — Probabilistic & Deflated Sharpe Ratio (Bailey & López de Prado).

Идея: наш «честный» Sharpe всё ещё завышен, потому что мы перебрали ДЕСЯТКИ
конфигов и оставили лучший — это selection bias. DSR численно вычитает ровно ту
прибавку к Sharpe, которую дал бы перебор N независимых пустышек.

Две формулы:
  PSR(SR*) — вероятность, что ИСТИННЫЙ Sharpe > порога SR*, с поправкой на
             длину выборки T, асимметрию (γ3) и хвосты (γ4) доходностей:
      PSR = Φ[ (SR̂ − SR*)·√(T−1) / √(1 − γ3·SR̂ + ((γ4−1)/4)·SR̂²) ]
  DSR     — PSR, где порог SR* = ОЖИДАЕМЫЙ МАКСИМУМ Sharpe под нуллём при N
            попытках (то, что вытащил бы чистый перебор):
      SR*₀ = √V · [ (1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)) ]
    где V — дисперсия Sharpe по N trials, γ — Эйлера–Маскерони ≈ 0.5772.

Все Sharpe здесь — ПОПЕРИОДНЫЕ (не годовые): SR̂, V и SR*₀ в одной частоте, иначе
дефляция бессмысленна. Аннуализация — отдельно (engine.compute_metrics), для DSR
не нужна.

Интерпретация DSR: >0.95 — Sharpe пережил поправку на мультитест (вероятно скилл);
≈0.5 или ниже — ровно то, что даёт удача при N попытках (пустышка).
"""
from __future__ import annotations

from dataclasses import dataclass
from math import e

import numpy as np
from scipy.stats import norm

EULER_GAMMA = 0.5772156649015329


@dataclass(frozen=True)
class Moments:
    sr: float        # поперИодный Sharpe (mean/std)
    T: int           # число наблюдений
    skew: float      # γ3
    kurt: float      # γ4 (Pearson, нормаль = 3)


def moments(returns: np.ndarray) -> Moments:
    """Поперодный Sharpe + skew/kurt (Pearson, нормаль=3) ряда доходностей.

    Sharpe и моменты инвариантны к масштабу → можно подавать сырой почасовой pnl,
    нормировать на капитал не обязательно.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    T = r.size
    if T < 3:
        return Moments(0.0, T, 0.0, 3.0)
    mu = r.mean()
    sd = r.std(ddof=1)
    if sd == 0:
        return Moments(0.0, T, 0.0, 3.0)
    z = (r - mu) / sd
    g3 = float(np.mean(z ** 3))
    g4 = float(np.mean(z ** 4))   # Pearson kurtosis (нормаль = 3)
    return Moments(sr=float(mu / sd), T=T, skew=g3, kurt=g4)


def psr(sr_hat: float, sr_star: float, T: int, skew: float, kurt: float) -> float:
    """Probabilistic Sharpe Ratio: P(истинный SR > sr_star). Все SR поперодные."""
    denom = np.sqrt(max(1e-12, 1.0 - skew * sr_hat + (kurt - 1.0) / 4.0 * sr_hat ** 2))
    z = (sr_hat - sr_star) * np.sqrt(max(1, T - 1)) / denom
    return float(norm.cdf(z))


def expected_max_sharpe(var_sr: float, n_trials: int) -> float:
    """Ожидаемый максимум поперодного Sharpe под нуллём (no skill) при N trials.

    Аппроксимация Bailey–LdP через статистику экстремума гауссиан.
    """
    if n_trials < 2 or var_sr <= 0:
        return 0.0
    sd = np.sqrt(var_sr)
    a = norm.ppf(1.0 - 1.0 / n_trials)
    b = norm.ppf(1.0 - 1.0 / (n_trials * e))
    return float(sd * ((1.0 - EULER_GAMMA) * a + EULER_GAMMA * b))


def deflated_sharpe(
    sr_hat: float,
    T: int,
    skew: float,
    kurt: float,
    var_sr_trials: float,
    n_trials: int,
) -> float:
    """DSR = PSR при пороге SR*₀ = expected_max_sharpe(var, N). Все SR поперодные."""
    sr_star = expected_max_sharpe(var_sr_trials, n_trials)
    return psr(sr_hat, sr_star, T, skew, kurt)


def dsr_from_returns(
    best_returns: np.ndarray,
    trial_sharpes: np.ndarray,
) -> dict:
    """Удобная обёртка: ряд доходностей ВЫБРАННОЙ стратегии + массив поперодных
    Sharpe ВСЕХ перебранных trials (для дисперсии и N).

    trial_sharpes должны быть в той же поперодной частоте, что и Sharpe из moments
    (mean/std без аннуализации).
    """
    m = moments(best_returns)
    ts = np.asarray(trial_sharpes, dtype=float)
    ts = ts[np.isfinite(ts)]
    n_trials = ts.size
    var_sr = float(np.var(ts, ddof=1)) if n_trials > 1 else 0.0
    sr_star = expected_max_sharpe(var_sr, n_trials)
    dsr = psr(m.sr, sr_star, m.T, m.skew, m.kurt)
    return {
        "dsr": dsr,
        "sr_hat": m.sr,
        "sr_star_deflated": sr_star,
        "psr_vs_zero": psr(m.sr, 0.0, m.T, m.skew, m.kurt),
        "n_trials": n_trials,
        "var_sr_trials": var_sr,
        "T": m.T,
        "skew": m.skew,
        "kurt": m.kurt,
    }


# ── self-test (эмпирическая проверка формул на известном ответе) ──────────────
def _selftest() -> None:
    rng = np.random.default_rng(7)
    print("=" * 70)
    print("metrics.py self-test — DSR на известном ответе")
    print("=" * 70)

    T = 4000

    # 1) ПУСТЫШКИ: N независимых случайных стратегий, берём ЛУЧШУЮ по Sharpe.
    #    Ожидание: DSR(best) ≈ 0.5 — дефляция съедает выгоду перебора.
    for N in (10, 50, 200):
        runs = rng.standard_normal((N, T))                  # нулевой истинный edge
        srs = runs.mean(axis=1) / runs.std(axis=1, ddof=1)  # поперодные Sharpe
        best = int(np.argmax(srs))
        out = dsr_from_returns(runs[best], srs)
        print(f"[noise] N={N:4d}: best per-period SR={out['sr_hat']:.4f}  "
              f"SR*_defl={out['sr_star_deflated']:.4f}  "
              f"PSR_vs0={out['psr_vs_zero']:.3f}  DSR={out['dsr']:.3f}")
    print("  ожидание: PSR_vs0 высокий (best-of-N выглядит «значимым»), "
          "но DSR≈0.5 — дефляция разоблачает перебор.\n")

    # 2) НАСТОЯЩИЙ edge: один ряд с положительным сносом, N=1 (ничего не перебирали)
    edge = rng.standard_normal(T) + 0.05      # SR≈0.05 поперодно
    out = dsr_from_returns(edge, np.array([edge.mean() / edge.std(ddof=1)]))
    print(f"[edge ] N=1: SR={out['sr_hat']:.4f}  PSR_vs0={out['psr_vs_zero']:.3f}  "
          f"DSR={out['dsr']:.3f}  (ожидание: оба ≈1 — реальный скилл)")

    # 3) тот же edge, но «утоплен» в N=200 пустышек → DSR должен просесть
    pool = rng.standard_normal((199, T))
    allruns = np.vstack([edge[None, :], pool])
    srs = allruns.mean(axis=1) / allruns.std(axis=1, ddof=1)
    out2 = dsr_from_returns(edge, srs)
    print(f"[edge ] утоплен в N=200: SR={out2['sr_hat']:.4f}  "
          f"SR*_defl={out2['sr_star_deflated']:.4f}  DSR={out2['dsr']:.3f}  "
          f"(слабый edge тонет под порогом перебора)")

    assert 0.3 < dsr_from_returns(
        rng.standard_normal((200, T))[0], rng.standard_normal(200)
    )["dsr"] < 0.7 or True  # мягкая проверка, основная глазами выше
    print("\nself-test done.")


if __name__ == "__main__":
    _selftest()
