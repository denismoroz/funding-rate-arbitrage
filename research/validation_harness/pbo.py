"""
Ф4 — Probability of Backtest Overfitting (PBO) через CSCV.
Bailey, Borwein, López de Prado, Zhu (2017), "The Probability of Backtest Overfitting".

Вопрос, на который отвечает PBO: если я выбираю конфиг, лучший IN-SAMPLE, как
часто он оказывается НИЖЕ медианы OUT-OF-SAMPLE? Высокий PBO → выбор по бэктесту
не переносится вперёд (оверфит-машина).

Алгоритм CSCV (Combinatorial Symmetric CV):
  1. Матрица R (T наблюдений × N конфигов) — поперодная доходность каждого конфига.
  2. Режем T строк на S смежных кусков (S чётное).
  3. Для каждой комбинации S/2 кусков как IS (комплемент = OOS), всего C(S, S/2):
       - n* = argmax Sharpe конфигов на IS;
       - ω = относительный ранг n* в OOS-Sharpe (ω→1 лучший, ω→0 худший);
       - λ = logit(ω). λ≤0 ⇔ IS-лучший провалился (ниже медианы OOS).
  4. PBO = доля сплитов с λ≤0.

Симметрия (IS и OOS равного размера, перебор всех комбинаций) делает оценку
несмещённой и устойчивой к одному удачному периоду.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

import numpy as np
from scipy.stats import rankdata


@dataclass
class PBOResult:
    pbo: float                       # P(IS-лучший ниже медианы OOS)
    n_splits: int
    n_configs: int
    S: int
    lambdas: np.ndarray = field(repr=False, default=None)   # logit-ранги
    median_oos_rank: float = float("nan")    # медианный OOS-ранг IS-лучшего (0..1)
    is_best_counts: dict = field(default_factory=dict)       # как часто кто IS-лучший


def _sharpe_cols(sub: np.ndarray) -> np.ndarray:
    """Поперодный Sharpe по каждому столбцу подматрицы (T_sub × N)."""
    mu = sub.mean(axis=0)
    sd = sub.std(axis=0, ddof=1)
    out = np.zeros_like(mu)
    nz = sd > 0
    out[nz] = mu[nz] / sd[nz]
    return out


def pbo(R: np.ndarray, S: int = 16, names: list[str] | None = None) -> PBOResult:
    """PBO по матрице доходностей R (T × N конфигов). S — число кусков (чётное)."""
    R = np.asarray(R, dtype=float)
    T, N = R.shape
    if N < 2:
        raise ValueError("нужно >= 2 конфигов в меню")
    if S % 2 != 0:
        raise ValueError("S должно быть чётным")
    if S > T:
        raise ValueError("S больше числа наблюдений")

    chunks = np.array_split(np.arange(T), S)
    lambdas: list[float] = []
    best_counts = np.zeros(N, dtype=int)

    for is_combo in combinations(range(S), S // 2):
        is_set = set(is_combo)
        is_rows = np.concatenate([chunks[i] for i in is_combo])
        oos_rows = np.concatenate([chunks[i] for i in range(S) if i not in is_set])

        is_perf = _sharpe_cols(R[is_rows])
        oos_perf = _sharpe_cols(R[oos_rows])

        n_star = int(np.argmax(is_perf))
        best_counts[n_star] += 1

        # относительный ранг IS-лучшего среди OOS (1=лучший … N=худший → ω в (0,1))
        oos_rank = rankdata(oos_perf)[n_star]      # 1..N, средний при ничьих
        omega = oos_rank / (N + 1)
        lambdas.append(float(np.log(omega / (1.0 - omega))))

    lam = np.asarray(lambdas)
    res = PBOResult(
        pbo=float(np.mean(lam <= 0.0)),
        n_splits=lam.size,
        n_configs=N,
        S=S,
        lambdas=lam,
        median_oos_rank=float(np.median(1.0 / (1.0 + np.exp(-lam)))),
    )
    if names is not None:
        res.is_best_counts = {names[i]: int(best_counts[i]) for i in np.argsort(-best_counts)}
    return res


# ── self-test ────────────────────────────────────────────────────────────────
def _selftest() -> None:
    rng = np.random.default_rng(11)
    print("=" * 70)
    print("pbo.py self-test — PBO на известном ответе (проверяем НАПРАВЛЕНИЕ)")
    print("=" * 70)
    T, N, S = 6000, 20, 16
    # NB: на чистом iid-шуме PBO < 0.5, а не =0.5 — у каждого столбца есть
    # устойчивая «глобальная удача», персистящая IS↔OOS. Поэтому надёжный якорь —
    # МОНОТОННЫЙ порядок edge < noise < overfit, а не точное попадание в 0.5.

    # 1) чистый шум — нейтральная точка отсчёта
    R_noise = rng.standard_normal((T, N))
    r1 = pbo(R_noise, S=S)
    print(f"[noise]    N={N}: PBO={r1.pbo:.3f}  "
          f"медианный OOS-ранг IS-лучшего={r1.median_oos_rank:.2f}")

    # 2) один конфиг с настоящим edge среди шума → IS- и OOS-лучший → PBO низкий
    R_edge = rng.standard_normal((T, N))
    R_edge[:, 0] += 0.06          # стабильный поперодный снос у конфига 0
    names = [f"cfg{i}" for i in range(N)]
    r2 = pbo(R_edge, S=S, names=names)
    print(f"[edge]     N={N}: PBO={r2.pbo:.3f}  (ожидание ≈0, edge переносится)  "
          f"IS-лучший чаще всего: {next(iter(r2.is_best_counts))}")

    # 3) «оверфит-меню»: каждый конфиг — гений только на СВОЁМ куске IS, мёртв
    #    везде ещё → IS-лучший каждый раз новый, OOS-провал → PBO высокий
    R_of = rng.standard_normal((T, N))
    chunks = np.array_split(np.arange(T), N)
    for i in range(N):
        R_of[chunks[i], i] += 5.0
    r3 = pbo(R_of, S=S)
    print(f"[overfit]  N={N}: PBO={r3.pbo:.3f}  (ожидание высокий — спайк-фиттинг)")

    assert r2.pbo < 0.05, ("edge должен давать низкий PBO", r2.pbo)
    assert r3.pbo > 0.5, ("overfit должен давать высокий PBO", r3.pbo)
    assert r2.pbo < r1.pbo < r3.pbo, ("ожидался порядок edge<noise<overfit",
                                       r2.pbo, r1.pbo, r3.pbo)
    print("\nself-test passed (порядок edge<noise<overfit + крайние якоря).")


if __name__ == "__main__":
    _selftest()
