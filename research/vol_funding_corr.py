"""
vol_funding_corr.py
-------------------
Гипотеза: высокая реализованная волатильность монеты → высокий funding rate.
Если связь сильная — можно использовать волатильность для динамической селекции монет.

Анализы A–E:
  A — Per-coin time-series Pearson corr (concurrent + lag-24h predictive)
  B — Cross-section Spearman rank corr (на каждом часу: rank_vol vs rank_funding)
  C — Bucketing по realized_vol_7d (quintiles) → avg funding_ma12
  D — Scatter: per-coin avg_vol vs avg_funding
  E — Predictive: vol прошлого месяца → funding текущего месяца
"""

import sys
import warnings
import numpy as np
import pandas as pd
from pathlib import Path

# ── Добавляем research/ в path для import engine ──────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from engine import load_data

warnings.filterwarnings("ignore")

U13 = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE", "UNI", "ARB", "OP", "TIA", "INJ", "WIF"]

HOURS_PER_YEAR = 8760
LAST_90D = pd.Timestamp("now", tz="UTC") - pd.Timedelta(days=90)


# ── Загрузка данных ───────────────────────────────────────────────────────────

def load_coin(coin: str) -> pd.DataFrame | None:
    try:
        df = load_data(coin, with_ohlcv=True)
    except FileNotFoundError as e:
        print(f"  WARNING: {coin} — файл не найден ({e}), пропускаем.")
        return None
    except Exception as e:
        print(f"  WARNING: {coin} — ошибка загрузки ({e}), пропускаем.")
        return None

    df = df[["close", "fundingRate"]].copy()
    df["price_return"] = df["close"].pct_change()

    # Annualized funding
    df["funding_ann"] = df["fundingRate"] * HOURS_PER_YEAR * 100  # %

    # Smoothed 12h rolling mean (как в стратегии)
    df["funding_ma12"] = (
        df["fundingRate"].rolling(12, min_periods=1).mean() * HOURS_PER_YEAR * 100
    )

    # Realized vol 24h (annualized): std за 24h × sqrt(8760) × 100
    df["realized_vol_24h"] = (
        df["price_return"].rolling(24, min_periods=12).std() * np.sqrt(HOURS_PER_YEAR) * 100
    )

    # Realized vol 7d (annualized): std за 168h × sqrt(8760) × 100
    df["realized_vol_7d"] = (
        df["price_return"].rolling(168, min_periods=72).std() * np.sqrt(HOURS_PER_YEAR) * 100
    )

    return df.dropna(subset=["realized_vol_24h", "realized_vol_7d", "funding_ma12"])


print("=" * 70)
print("VOL-FUNDING CORRELATION ANALYSIS")
print("=" * 70)
print(f"\nЗагрузка данных для {len(U13)} монет...")

coin_data: dict[str, pd.DataFrame] = {}
for c in U13:
    df = load_coin(c)
    if df is not None:
        coin_data[c] = df
        print(f"  {c:6s}: {len(df):,} часов  [{df.index[0].date()} — {df.index[-1].date()}]")

available = list(coin_data.keys())
print(f"\nДоступно монет: {len(available)}")


def period_slice(df: pd.DataFrame, period: str) -> pd.DataFrame:
    if period == "full":
        return df
    else:  # last_90d
        return df[df.index >= LAST_90D]


# ═══════════════════════════════════════════════════════════════════════════════
# АНАЛИЗ A — Per-coin time-series Pearson correlation
# ═══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("АНАЛИЗ A — Per-coin Pearson corr (funding_ma12 vs realized_vol)")
print("  corr_24h: funding(t) vs vol_24h(t)  [concurrent]")
print("  corr_lag: funding(t) vs vol_7d(t-24) [predictive, lag=-24h]")
print("=" * 70)

rows_A = []
for period in ["full", "last_90d"]:
    for coin in available:
        df = period_slice(coin_data[coin], period)
        if len(df) < 100:
            continue

        fma = df["funding_ma12"]
        v24 = df["realized_vol_24h"]
        v7d_lag = df["realized_vol_7d"].shift(24)  # vol(t-24h)

        valid_24 = fma.notna() & v24.notna()
        valid_lag = fma.notna() & v7d_lag.notna()

        corr_24 = fma[valid_24].corr(v24[valid_24]) if valid_24.sum() > 30 else np.nan
        corr_lag = fma[valid_lag].corr(v7d_lag[valid_lag]) if valid_lag.sum() > 30 else np.nan

        rows_A.append({
            "period": period,
            "coin": coin,
            "n_hours": len(df),
            "corr_concurrent": round(corr_24, 3) if np.isfinite(corr_24) else np.nan,
            "corr_lag24h": round(corr_lag, 3) if np.isfinite(corr_lag) else np.nan,
        })

df_A = pd.DataFrame(rows_A)

for period in ["full", "last_90d"]:
    sub = df_A[df_A["period"] == period].drop(columns="period").set_index("coin")
    print(f"\nПериод: {period}")
    print(sub.to_string())
    avg_c = sub["corr_concurrent"].mean()
    avg_l = sub["corr_lag24h"].mean()
    print(f"  Avg corr_concurrent={avg_c:.3f}  Avg corr_lag24h={avg_l:.3f}")

# Вывод
avg_full_c = df_A[df_A["period"] == "full"]["corr_concurrent"].mean()
avg_90d_c  = df_A[df_A["period"] == "last_90d"]["corr_concurrent"].mean()
avg_full_l = df_A[df_A["period"] == "full"]["corr_lag24h"].mean()
avg_90d_l  = df_A[df_A["period"] == "last_90d"]["corr_lag24h"].mean()

print(f"""
[ВЫВОД A] Средняя Pearson corr(concurrent): full={avg_full_c:.3f}, last_90d={avg_90d_c:.3f}
          Средняя Pearson corr(lag-24h):    full={avg_full_l:.3f}, last_90d={avg_90d_l:.3f}
  → {'Гипотеза подтверждается (умеренная+ связь)' if abs(avg_full_c) > 0.15 else 'Гипотеза слабая или не подтверждается (|corr| ≤ 0.15)'}""")


# ═══════════════════════════════════════════════════════════════════════════════
# АНАЛИЗ B — Cross-section Spearman rank correlation
# ═══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("АНАЛИЗ B — Cross-section Spearman rank corr (per-hour across coins)")
print("  На каждом часу: rank(vol_7d) vs rank(funding_ma12) по N монетам")
print("=" * 70)

# Строим панель
panel_vol  = pd.DataFrame({c: coin_data[c]["realized_vol_7d"]  for c in available})
panel_fund = pd.DataFrame({c: coin_data[c]["funding_ma12"] for c in available})

# Выравниваем по общим часам
common_idx = panel_vol.index.intersection(panel_fund.index)
panel_vol  = panel_vol.loc[common_idx]
panel_fund = panel_fund.loc[common_idx]

def compute_crosssection_spearman(pv: pd.DataFrame, pf: pd.DataFrame) -> pd.Series:
    """На каждом часу: Spearman corr рангов vol vs funding по монетам."""
    results = []
    for ts in pv.index:
        row_v = pv.loc[ts].dropna()
        row_f = pf.loc[ts, row_v.index].dropna()
        common = row_v.index.intersection(row_f.index)
        if len(common) < 4:
            results.append(np.nan)
            continue
        rv = row_v[common].rank()
        rf = row_f[common].rank()
        # Spearman = Pearson на рангах
        c = rv.corr(rf)
        results.append(c)
    return pd.Series(results, index=pv.index)

print("\nВычисление cross-section Spearman (это займёт немного времени)...")

for period in ["full", "last_90d"]:
    if period == "last_90d":
        pv = panel_vol[panel_vol.index >= LAST_90D]
        pf = panel_fund[panel_fund.index >= LAST_90D]
    else:
        pv = panel_vol
        pf = panel_fund

    # Ускорение: выборка каждые 6 часов (репрезентативно, намного быстрее)
    pv_s = pv.iloc[::6]
    pf_s = pf.iloc[::6]

    cs = compute_crosssection_spearman(pv_s, pf_s)
    avg = cs.mean()
    med = cs.median()
    pct_pos = (cs > 0).mean() * 100

    print(f"\nПериод: {period}")
    print(f"  N часов (выборка 6h): {len(cs)}")
    print(f"  Avg Spearman={avg:.3f}  Median={med:.3f}  % позитивных={pct_pos:.1f}%")

print(f"""
[ВЫВОД B] Cross-section анализ показывает, насколько монеты с высокой vol
  ОДНОВРЕМЕННО имеют высокий funding на каждом конкретном часу.
  Если avg Spearman > 0.2 — связь заметна.""")


# ═══════════════════════════════════════════════════════════════════════════════
# АНАЛИЗ C — Bucketing по realized_vol_7d (quintiles)
# ═══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("АНАЛИЗ C — Bucketing (coin,hour) по realized_vol_7d quintiles")
print("  → avg funding_ma12 в каждом бакете")
print("=" * 70)

# Собираем все (coin, hour) в один датафрейм
all_rows = []
for c in available:
    df = coin_data[c][["realized_vol_7d", "funding_ma12"]].copy()
    df["coin"] = c
    all_rows.append(df)
all_df = pd.concat(all_rows).dropna()

for period in ["full", "last_90d"]:
    if period == "last_90d":
        sub = all_df[all_df.index >= LAST_90D]
    else:
        sub = all_df

    sub = sub.copy()
    sub["vol_quintile"] = pd.qcut(sub["realized_vol_7d"], q=5,
                                   labels=["Q1 (low)", "Q2", "Q3", "Q4", "Q5 (high)"])
    bucket_stats = sub.groupby("vol_quintile", observed=True)["funding_ma12"].agg(
        avg_funding="mean", median_funding="median", count="count"
    ).round(3)

    print(f"\nПериод: {period}")
    print(bucket_stats.to_string())

    q1 = bucket_stats["avg_funding"].iloc[0]
    q5 = bucket_stats["avg_funding"].iloc[-1]
    diff = q5 - q1
    print(f"  Q5 − Q1 avg funding: {diff:+.3f}% ann.")
    print(f"  → {'Монотонная зависимость' if diff > 1 else 'Слабая или инвертированная зависимость'}")

print(f"""
[ВЫВОД C] Bucketing — прямой взгляд: платит ли высокий vol больше funding?
  Монотонный рост avg_funding от Q1→Q5 подтверждает гипотезу.""")


# ═══════════════════════════════════════════════════════════════════════════════
# АНАЛИЗ D — Per-coin scatter: avg_vol vs avg_funding
# ═══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("АНАЛИЗ D — Per-coin scatter: avg_vol_7d vs avg_funding_ma12")
print("  Pearson corr по 13 монетам (агрегированный взгляд)")
print("=" * 70)

rows_D = []
for period in ["full", "last_90d"]:
    data_pts = []
    for c in available:
        df = period_slice(coin_data[c], period)
        if len(df) < 50:
            continue
        avg_vol  = df["realized_vol_7d"].mean()
        avg_fund = df["funding_ma12"].mean()
        data_pts.append({"coin": c, "avg_vol": avg_vol, "avg_funding": avg_fund})
    df_D = pd.DataFrame(data_pts).set_index("coin").round(2)

    corr = df_D["avg_vol"].corr(df_D["avg_funding"])

    print(f"\nПериод: {period}  (Pearson corr={corr:.3f})")
    print(df_D.sort_values("avg_vol", ascending=False).to_string())
    print(f"  → Corr avg_vol vs avg_funding = {corr:.3f}  "
          f"({'сильная' if abs(corr) > 0.5 else 'слабая'} связь по {len(df_D)} монетам)")

print(f"""
[ВЫВОД D] Агрегированный scatter — самый честный взгляд.
  Если corr > 0.4 по 13 монетам — высоковолатильные монеты
  системно дают больше funding в среднем.""")


# ═══════════════════════════════════════════════════════════════════════════════
# АНАЛИЗ E — Predictive: vol прошлого месяца → funding следующего
# ═══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("АНАЛИЗ E — Predictive: avg vol(month M-1) → avg funding(month M)")
print("  Pearson corr по (coin×month) парам")
print("=" * 70)

# Собираем месячные агрегаты по монетам
monthly_rows = []
for c in available:
    df = coin_data[c][["realized_vol_7d", "funding_ma12"]].copy()
    df["month"] = df.index.to_period("M")
    monthly = df.groupby("month").agg(
        avg_vol=("realized_vol_7d", "mean"),
        avg_fund=("funding_ma12", "mean"),
    )
    monthly["coin"] = c
    monthly_rows.append(monthly)

monthly_all = pd.concat(monthly_rows).reset_index()

# Для каждой монеты: lag vol на 1 месяц
pred_rows = []
for c in available:
    sub = monthly_all[monthly_all["coin"] == c].sort_values("month").copy()
    sub["vol_prev_month"] = sub["avg_vol"].shift(1)
    sub = sub.dropna(subset=["vol_prev_month"])
    pred_rows.append(sub)

pred_df = pd.concat(pred_rows).dropna()

# Corr по всем (coin, month) парам
corr_pred = pred_df["vol_prev_month"].corr(pred_df["avg_fund"])
n_pairs = len(pred_df)

print(f"\nВсего пар (coin × month): {n_pairs}")
print(f"Pearson corr(vol_prev_month, avg_fund_current): {corr_pred:.3f}")

# Также показываем per-month cross-section: в каждом месяце rank по vol предыдущего → rank по funding
# Spearman по месяцу
monthly_cs_rows = []
for m in sorted(pred_df["month"].unique()):
    row = pred_df[pred_df["month"] == m]
    if len(row) < 4:
        continue
    sp = row["vol_prev_month"].rank().corr(row["avg_fund"].rank())
    monthly_cs_rows.append({"month": m, "spearman": sp, "n": len(row)})

df_mcs = pd.DataFrame(monthly_cs_rows)
avg_mcs = df_mcs["spearman"].mean()
pct_pos_mcs = (df_mcs["spearman"] > 0).mean() * 100

print(f"\nPer-month cross-section Spearman (vol_prev vs fund_curr):")
print(f"  Avg Spearman={avg_mcs:.3f}  % позитивных месяцев={pct_pos_mcs:.1f}%")
print(df_mcs.set_index("month").round(3).to_string())

print(f"""
[ВЫВОД E] Predictive анализ: vol прошлого месяца → funding следующего.
  Pearson corr={corr_pred:.3f}  Avg monthly Spearman={avg_mcs:.3f}
  → {'Предиктивная связь ЕСТЬ — vol помогает предсказывать funding' if abs(corr_pred) > 0.15 or abs(avg_mcs) > 0.15
     else 'Предиктивная связь СЛАБАЯ — vol прошлого месяца не предсказывает funding'}""")


# ═══════════════════════════════════════════════════════════════════════════════
# ИТОГОВЫЙ ВЫВОД
# ═══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("ИТОГОВЫЙ ВЫВОД")
print("=" * 70)
print(f"""
Анализ A (time-series Pearson):  full={avg_full_c:.3f}  last_90d={avg_90d_c:.3f}
  Lag-24h predictive:             full={avg_full_l:.3f}  last_90d={avg_90d_l:.3f}

Анализ B (cross-section Spearman): см. выше per-period
Анализ C (bucketing Q1→Q5):        см. разницу Q5−Q1 выше
Анализ D (per-coin scatter corr):   см. corr выше
Анализ E (predictive monthly):      corr={corr_pred:.3f}  Spearman={avg_mcs:.3f}

ЗАКЛЮЧЕНИЕ:
  1. Если |corr_A| > 0.15 — внутри монеты vol и funding коррелируют.
  2. Если cross-section Spearman > 0.2 — в конкретный час high-vol монета
     НЕ ОБЯЗАТЕЛЬНО имеет высокий funding (это важно для cross-section отбора).
  3. Bucketing (C) — самый наглядный тест: монотонный ли рост funding Q1→Q5?
  4. Per-coin scatter (D) — есть ли структурная разница между монетами по avg?
  5. Predictive (E): можно ли по vol прошлого месяца предсказать будущий funding?

Если связь сильная (A ≥ 0.2, C монотонна, E corr > 0.2):
  → Динамическая селекция по vol ИМЕЕТ СМЫСЛ.
  → Можно входить в монету при высоком vol (ожидая высокий funding).

Если связь слабая:
  → Vol не является надёжным предиктором funding.
  → Фиксированный универсум + прямой фильтр по funding_ma12 лучше.
""")
