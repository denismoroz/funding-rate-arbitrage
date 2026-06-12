"""C4 — честный дневной разбор крипто cross-sectional книги (Opus review).

Стенд годовит по HOURS_PER_YEAR=8760 (часовая модель), книга дневная → OOS levels
раздуты. Здесь считаем ПРАВИЛЬНЫЕ дневные метрики напрямую из полнопериодных
pnl-серий меню, плюс декомпозиция momentum vs carry и их корреляция.
"""
import numpy as np
import pandas as pd

from crypto_pkg import CryptoXSecPackage

PPY = 365  # дневные периоды в году


def metrics(pnl: pd.Series) -> dict:
    r = pnl.dropna().values
    if len(r) < 30:
        return {}
    mean, std = r.mean(), r.std(ddof=0)
    sharpe = (mean / std) * np.sqrt(PPY) if std > 0 else 0.0
    ann = mean * PPY
    eq = np.cumprod(1.0 + r)
    dd = 1.0 - eq / np.maximum.accumulate(eq)
    maxdd = dd.max()
    calmar = ann / maxdd if maxdd > 1e-9 else float("nan")
    hit = (r > 0).mean()
    return dict(n=len(r), ann=ann, sharpe=sharpe, maxdd=maxdd,
               calmar=calmar, hit=hit, mean=mean, std=std)


def main():
    pkg = CryptoXSecPackage()
    df = pkg.load("XSEC")
    menu = pkg.menu("XSEC", df)  # {name: full-period daily pnl series}

    print(f"=== Честные ДНЕВНЫЕ метрики (annualize √365), полный период ===")
    print(f"{'config':<10}{'ann%':>9}{'sharpe':>9}{'maxDD%':>9}{'calmar':>9}{'hit%':>8}")
    rows = {}
    for nm in ["mom30", "mom60", "mom90", "carry", "blend"]:
        m = metrics(menu[nm])
        rows[nm] = m
        print(f"{nm:<10}{100*m['ann']:>9.2f}{m['sharpe']:>9.2f}"
              f"{100*m['maxdd']:>9.2f}{m['calmar']:>9.2f}{100*m['hit']:>8.1f}")

    # Корреляция momentum(60) vs carry — диверсифицирует ли carry?
    a = menu["mom60"].dropna()
    b = menu["carry"].dropna()
    j = pd.concat([a, b], axis=1, join="inner").dropna()
    corr = np.corrcoef(j.iloc[:, 0], j.iloc[:, 1])[0, 1]
    print(f"\ncorr(mom60, carry) daily pnl = {corr:+.3f}")
    print(f"blend Sharpe {rows['blend']['sharpe']:.2f} vs mom60 {rows['mom60']['sharpe']:.2f}"
          f" / carry {rows['carry']['sharpe']:.2f}  → blend помогает? "
          f"{'да' if rows['blend']['sharpe'] > max(rows['mom60']['sharpe'], rows['carry']['sharpe']) else 'нет'}")


if __name__ == "__main__":
    main()
