# Validation Harness

Переиспользуемый стенд, который судит торговую гипотезу по дисциплине López de
Prado и выдаёт **дефлейтнутый** вердикт вместо одной приятной цифры из бэктеста.

Три измерения вердикта:

| метрика | вопрос | хорошо |
|---|---|---|
| **OOS CPCV** | устойчив ли эдж по режимам (распределение, не точка)? | медиана Calmar>0, узкий IQR |
| **DSR** | пережил ли Sharpe поправку на число перебранных конфигов? | >0.95 |
| **PBO** | переносится ли «лучший по бэктесту» конфиг вперёд? | <0.2 |

DSR и PBO отвечают на РАЗНЫЕ вопросы и могут расходиться: высокий DSR +
PBO≈0.5 = «эдж в среднем реален, но выбрать/собрать его forward нельзя».

## Файлы

- `splitter.py` — Purged K-Fold + CPCV (purge симметричный + embargo). Фундамент.
- `runner.py` — прогон одной стратегии по CPCV → распределение OOS-метрик.
- `metrics.py` — PSR + Deflated Sharpe (поправка на мультитест).
- `pbo.py` — Probability of Backtest Overfitting через CSCV.
- `harness.py` — оркестратор: пакет стратегии + монеты → единый вердикт (+JSON).
- `report.py` — печать вердикта со светофором.
- `costs.py` — конфиг костов (TAKER / MAKER).
- `contract.py` — контракт стратегии (mask-based, seam-safe).
- `strategies/` — адаптеры: `baselines_pkg.py` (эталоны), `b_pkg.py` (Strategy B).
- `validate_harness.py` — Ф6: проверка стенда на известных ответах.
- `run_b.py` — Ф7: прогон Strategy B.

Все запуски из этой папки с `PYTHONPATH=..` (стенд переиспользует `research/engine.py`):

```bash
cd research/validation_harness
PYTHONPATH=.. python validate_harness.py    # самопроверка стенда
PYTHONPATH=.. python run_b.py               # вердикт по Strategy B
```

## Доверие к стенду (Ф6)

`validate_harness.py` гоняет эталоны с известным ответом — если они не сходятся,
баг в стенде, а не в стратегии:

- **look-ahead cheat** (подглядывает на бар вперёд) → DSR=1.000, PBO=0.000;
- **noise-меню** (случайные сигналы) → DSR≈0, PBO высокий (выбор не переносится);
- **buy&hold** → total симулятора = прямой расчёт (rel.err<1e-6).

NB: на чистом iid-шуме PBO выходит <0.5 (а не =0.5) — у каждого столбца есть
устойчивая «глобальная удача», персистящая IS↔OOS. Поэтому в self-test эталонов
проверяется НАПРАВЛЕНИЕ (cheat ≪ noise), а не точное попадание в 0.5.

## Как подключить новую гипотезу (≤30 строк)

Реализуй пакет с протоколом `harness.Package`:

```python
import pandas as pd
from engine import load_data, STAKING_YIELD
from costs import TAKER

class MyStrategy:                       # адаптер под контракт стенда (для OOS CPCV)
    name = "my_default"
    def fit(self, df, train_idx, costs):        # выбор конфига ТОЛЬКО по train_idx
        return None                              # (или верни выбранный конфиг)
    def simulate(self, df, seg, config, costs):  # pnl на смежном сегменте df.iloc[seg]
        return my_pnl(df.iloc[seg], config, costs)

class MyPackage:
    name = "Моя гипотеза"
    selected_name = "my_default"
    coins = ["BTC", "ETH", "SOL"]
    def load(self, coin):    return load_data(coin)
    def selected(self, coin, df):  return MyStrategy()
    def menu(self, coin, df):                    # ВСЕ конфиги: полнопериодный pnl
        return {nm: pd.Series(run_full(df, nm), index=df.index) for nm in MY_CONFIGS}

# прогон:
from harness import run_harness
from report import print_report
print_report(run_harness(MyPackage()))
```

Контракт **seam-safe**: считай сигналы на ПОЛНОМ `df` один раз (lookback цел),
маски/срезы лишь отбирают строки. Держи `purge ≥ макс. lookback` сигнала, иначе
окно train-бара дотянется до test (утечка).

## Вердикт по Strategy B (Ф7)

`run_b.py` (TAKER + 5bps, mom14|mom30, меню из 10 моментум-сигналов):

- **DSR = 0.931** ⚠️ — полнопериодный Sharpe реален (не флук 10 trials), но не
  железобетон;
- **PBO = 0.488** ⚠️ — выбор лучшего сигнала IS — coin-flip OOS (ранг 0.55,
  выбор размазан mom14|mom30 / mom60 / mom30);
- OOS медиана Calmar 0.98, IQR [0.11, 3.14]; per-coin TIA/INJ слабые.

Численно подтверждает прежний вывод: эдж B **реален in-sample-aggregate, но не
стационарен** — собрать его forward нельзя. B остаётся закрытой как самостоятельная
альфа. (maker-сценарий тут не воспроизводится — `simulate_constdollar` хардкодит
taker; честный maker = отдельная задача.)
