"""
Ф1 — Purged K-Fold + CPCV splitter (фундамент стенда).

Зачем (López de Prado, "Advances in Financial ML", гл. 7 & "Probability of
Backtest Overfitting"):
  - обычный train/test и даже walk-forward дают ОДИН путь → одна цифра, которая
    легко артефакт режима;
  - CPCV (Combinatorial Purged CV) разбивает ряд на N групп, тестирует на всех
    C(N,k) комбинациях k групп → *распределение* OOS-метрик, а не точечная оценка;
  - **purge** убирает из train бары, чьё окно признаков/лейбла перекрывает test
    (иначе утечка через lookback-моментум: train-бар сразу ПОСЛЕ test считает
    pct_change, заглядывая внутрь test);
  - **embargo** добавляет буфер ПОСЛЕ каждой test-группы — глушит остаточную
    серийную корреляцию у границы.

Контракт: всё работает на целочисленном индексе [0, n). Стенд подаёт длину ряда,
получает пути (train_idx, test_idx) без утечки. Никакой привязки к стратегии.

Самопроверка (python splitter.py): train∩test=∅ на каждом пути, корректное число
путей, демонстрация purge/embargo на игрушечном ряде.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import comb

import numpy as np


@dataclass(frozen=True)
class Split:
    """Один CPCV-путь."""
    test_groups: tuple[int, ...]   # id групп, ушедших в test
    train_idx: np.ndarray          # индексы train (после purge+embargo)
    test_idx: np.ndarray           # индексы test


def make_groups(n: int, n_groups: int) -> list[np.ndarray]:
    """Разбить [0, n) на n_groups СМЕЖНЫХ групп ~равного размера.

    Смежность критична: финансовый ряд автокоррелирован, случайный shuffle-fold
    протёк бы соседними барами. Группы — это куски времени.
    """
    if n_groups < 2:
        raise ValueError("n_groups must be >= 2")
    if n_groups > n:
        raise ValueError("n_groups must be <= n")
    return [g for g in np.array_split(np.arange(n), n_groups)]


def _train_after_purge(
    n: int,
    test_groups_idx: list[np.ndarray],
    purge: int,
    embargo: int,
) -> np.ndarray:
    """Train = всё, что НЕ test, минус purge-зона вокруг каждой test-группы и
    embargo-зона после неё.

    purge применяется СИММЕТРИЧНО (purge баров до начала и после конца каждой
    test-группы) — закрывает оба канала утечки: forward-label у границы слева и
    backward-feature lookback у границы справа. embargo — дополнительный
    односторонний буфер после test (конвенция LdP).
    """
    keep = np.ones(n, dtype=bool)
    for g in test_groups_idx:
        a, b = int(g[0]), int(g[-1])           # [a, b] включительно — test
        lo = max(0, a - purge)
        hi = min(n, b + 1 + purge + embargo)   # +1: b включительно
        keep[lo:hi] = False                     # выкидываем test + буферы из train
    return np.flatnonzero(keep)


def cpcv(
    n: int,
    n_groups: int = 6,
    k: int = 2,
    purge: int = 720,
    embargo: int = 24,
) -> list[Split]:
    """Combinatorial Purged CV.

    Параметры
    ---------
    n          : длина ряда (число баров).
    n_groups   : N — на сколько смежных групп режем.
    k          : сколько групп в test на каждой комбинации (обычно 2).
    purge      : баров отрезать с КАЖДОЙ стороны test-группы из train.
                 Дефолт 720h = 30 суток = макс. lookback моментума в B.
    embargo    : доп. буфер после test-группы (часы).

    Возвращает список Split (их C(N,k)). Гарантия: train_idx ∩ test_idx = ∅.

    Замечание о «путях»: каждая группа попадает в test в C(N-1,k-1) комбинациях;
    число реконструируемых backtest-путей = k·C(N,k)/N. Здесь возвращаем сами
    комбинации; сборка путей и метрик — на оркестраторе (Ф2/Ф4).
    """
    if k < 1 or k >= n_groups:
        raise ValueError("k must satisfy 1 <= k < n_groups")
    groups = make_groups(n, n_groups)
    splits: list[Split] = []
    for combo in combinations(range(n_groups), k):
        test_groups_idx = [groups[i] for i in combo]
        test_idx = np.concatenate(test_groups_idx)
        train_idx = _train_after_purge(n, test_groups_idx, purge, embargo)
        # инвариант: никакого пересечения
        assert np.intersect1d(train_idx, test_idx, assume_unique=False).size == 0
        splits.append(Split(tuple(combo), train_idx, test_idx))
    return splits


def purged_kfold(
    n: int,
    n_groups: int = 6,
    purge: int = 720,
    embargo: int = 24,
) -> list[Split]:
    """Частный случай CPCV при k=1 — классический Purged K-Fold (один путь walk
    через все фолды). Удобен как дешёвый sanity-прогон до полного CPCV."""
    return cpcv(n, n_groups=n_groups, k=1, purge=purge, embargo=embargo)


def n_paths(n_groups: int, k: int) -> int:
    """Число backtest-путей, реконструируемых из CPCV (LdP): k·C(N,k)/N."""
    return k * comb(n_groups, k) // n_groups


# ── self-test ────────────────────────────────────────────────────────────────
def _selftest() -> None:
    print("=" * 70)
    print("splitter.py self-test")
    print("=" * 70)

    # 1) число комбинаций = C(N,k); train∩test=∅ всегда
    n, N, k = 12_000, 6, 2
    splits = cpcv(n, n_groups=N, k=k, purge=720, embargo=24)
    assert len(splits) == comb(N, k), (len(splits), comb(N, k))
    for s in splits:
        assert np.intersect1d(s.train_idx, s.test_idx).size == 0
    print(f"[ok] N={N} k={k}: {len(splits)} комбинаций = C({N},{k})={comb(N,k)}; "
          f"train∩test=∅ на всех; backtest-путей={n_paths(N, k)}")

    # 2) purge реально вырезает буфер вокруг test (train не примыкает к test)
    s0 = splits[0]
    test_lo, test_hi = int(s0.test_idx.min()), int(s0.test_idx.max())
    # ближайший train слева/справа должен отстоять >= purge (где ряд позволяет)
    left = s0.train_idx[s0.train_idx < test_lo]
    if left.size and test_lo - 720 > 0:
        gap = test_lo - int(left.max())
        assert gap > 720, gap
        print(f"[ok] purge: зазор train→test слева = {gap}h (> purge=720)")

    # 3) игрушечный ряд — глазами видно вырезанные зоны
    print("\nИгрушка: n=40, N=4, k=1, purge=3, embargo=2")
    for s in purged_kfold(40, n_groups=4, purge=3, embargo=2):
        tr = set(s.train_idx.tolist())
        line = "".join(
            "T" if i in set(s.test_idx.tolist()) else ("." if i in tr else " ")
            for i in range(40)
        )
        print(f"  test-group {s.test_groups[0]}: |{line}|  "
              f"(train={len(s.train_idx)}, test={len(s.test_idx)})")
    print("  легенда: T=test, .=train, ' '=вырезано (purge/embargo)")

    # 4) деградация бюджета train с ростом purge (честная цена дисциплины)
    print("\nДоля train от ряда (n=12000, N=6, k=2) при разных purge:")
    for pg in (0, 168, 720, 1440):
        fr = np.mean([len(s.train_idx) for s in
                      cpcv(n, n_groups=N, k=k, purge=pg, embargo=24)]) / n
        print(f"  purge={pg:5d}h → train≈{fr*100:5.1f}% ряда")

    print("\nself-test passed.")


if __name__ == "__main__":
    _selftest()
