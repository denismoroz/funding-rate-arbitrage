"""
Ф3 — Time-varying β через Kalman filter.

State = [β, α] (наклон + интерсепт), observation = resid_a (y),
regressor = resid_b (x). Модель:
    y_t = α_t + β_t * x_t + ε_t       (observation noise R)
    [β_t, α_t] = [β_{t-1}, α_{t-1}] + η_t  (process noise Q=q*I)

Параметры q (process noise) и R (obs noise) — aprioри, НЕ фитятся по PnL.
По умолчанию: q=1e-4 (β дрейфует медленно), R=1e-2 (умеренный obs шум).

Seam-safe: фильтр рекурсивен — β_t зависит только от y_{1..t}, x_{1..t}.
Считается на ПОЛНОМ ряду один раз; CPCV-маски лишь отбирают строки.

fit() обновляет q/R по train_idx (ML-оценка через innovations) — это легитимно:
только параметры шума обновляются, не сам фильтр re-run на OOS.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class KalmanConfig:
    q: float   # process noise variance (per step)
    R: float   # observation noise variance


# Apriorные значения (PLAN: «task априорно или фитить только на train_idx»)
DEFAULT_Q = 1e-4
DEFAULT_R = 1e-2


def kalman_beta(
    resid_a: np.ndarray,
    resid_b: np.ndarray,
    q: float = DEFAULT_Q,
    R: float = DEFAULT_R,
) -> tuple[np.ndarray, np.ndarray]:
    """Kalman-фильтр: возвращает (beta_t, alpha_t) — массивы длины n.

    State x = [β, α], state-transition F = I, obs H_t = [x_t, 1].
    Начальный P = diag(1.0, 1.0) (слабая априорная уверенность).

    Первые несколько баров (warm-up) имеют β близко к 0 (начальное состояние).
    Стенд должен обеспечить purge >= warm-up периода (≈100 баров).

    Returns
    -------
    beta  : np.ndarray[n]   time-varying β_t
    alpha : np.ndarray[n]   time-varying α_t (intercept)
    """
    n = len(resid_a)
    assert len(resid_b) == n

    # State estimate
    x = np.array([0.0, 0.0])   # [β, α]
    P = np.eye(2)               # state covariance
    Q = q * np.eye(2)           # process noise

    beta_out = np.empty(n)
    alpha_out = np.empty(n)

    for t in range(n):
        # Predict
        # x_pred = F x = x (F=I)
        P_pred = P + Q

        xt = resid_b[t]
        H = np.array([xt, 1.0])   # obs row [x_t, 1]

        # Innovation
        y_hat = H @ x
        S = float(H @ P_pred @ H) + R
        K = P_pred @ H / S         # Kalman gain

        # Update with observation y_t = resid_a[t]
        innov = resid_a[t] - y_hat
        x = x + K * innov
        P = (np.eye(2) - np.outer(K, H)) @ P_pred

        beta_out[t] = x[0]
        alpha_out[t] = x[1]

    return beta_out, alpha_out


def fit_kalman_noise(
    resid_a: np.ndarray,
    resid_b: np.ndarray,
    train_idx: np.ndarray,
    q_grid: tuple[float, ...] = (1e-5, 1e-4, 5e-4, 1e-3),
    R_grid: tuple[float, ...] = (1e-3, 1e-2, 5e-2),
) -> KalmanConfig:
    """Оценить q/R на train_idx через innovations likelihood (grid search).

    Только train_idx используется для оценки → seam-safe.
    Возвращает KalmanConfig с лучшими q, R.
    """
    train_a = resid_a[train_idx]
    train_b = resid_b[train_idx]

    best_ll = -np.inf
    best_cfg = KalmanConfig(q=DEFAULT_Q, R=DEFAULT_R)

    for q in q_grid:
        for R in R_grid:
            ll = _innovations_loglik(train_a, train_b, q, R)
            if ll > best_ll:
                best_ll = ll
                best_cfg = KalmanConfig(q=q, R=R)

    return best_cfg


def _innovations_loglik(
    resid_a: np.ndarray,
    resid_b: np.ndarray,
    q: float,
    R: float,
) -> float:
    """Innovations-based log-likelihood (Gaussian KF)."""
    n = len(resid_a)
    x = np.array([0.0, 0.0])
    P = np.eye(2)
    Q = q * np.eye(2)
    ll = 0.0
    for t in range(n):
        P_pred = P + Q
        H = np.array([resid_b[t], 1.0])
        S = float(H @ P_pred @ H) + R
        innov = resid_a[t] - float(H @ x)
        ll -= 0.5 * (np.log(2 * np.pi * S) + innov ** 2 / S)
        K = P_pred @ H / S
        x = x + K * innov
        P = (np.eye(2) - np.outer(K, H)) @ P_pred
    return ll


# ── self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    rng = np.random.default_rng(42)
    n = 500
    # Simulate a cointegrated pair with time-varying β drifting 0.5→0.8
    true_beta = np.linspace(0.5, 0.8, n)
    true_alpha = 0.1
    x = rng.standard_normal(n).cumsum() * 0.1
    y = true_alpha + true_beta * x + rng.standard_normal(n) * 0.05

    beta_hat, alpha_hat = kalman_beta(y, x, q=1e-4, R=1e-2)

    # Check convergence after warmup
    err_beta = np.abs(beta_hat[100:] - true_beta[100:]).mean()
    print(f"Mean β tracking error (after warmup): {err_beta:.4f}")
    assert err_beta < 0.15, f"Kalman β tracking too poor: {err_beta}"

    # Test fit_kalman_noise
    train_idx = np.arange(250)
    cfg = fit_kalman_noise(y, x, train_idx)
    print(f"Fitted q={cfg.q}, R={cfg.R}")
    print("self-test passed.")
