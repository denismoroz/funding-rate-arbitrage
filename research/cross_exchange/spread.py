"""
Cross-exchange funding-spread carry engine.

ИДЕЯ (см. PLAN.md): тот же perp-контракт (BTC, ETH, …) имеет РАЗНЫЙ funding на
разных биржах (HL vs Binance vs Bybit). Delta-neutral по цене, НЕ-нейтрально по
funding: short-perp на бирже с высоким funding (лонги платят тебе), long-perp на
бирже с низким/отрицательным funding. Net carry за период =
`funding_rich − funding_cheap = spread`, до костов. Ценовой pnl двух perp-ног
ВЗАИМНО ГАСИТСЯ (delta-neutral) → моделируем ТОЛЬКО funding-spread pnl.

CAVEAT (потолок честности, НЕустранён в этих данных): нет синхронных perp-mark'ов
между биржами → реальный basis-риск (расхождение цен на входе/выходе,
неатомарность исполнения, ликвидация одной ноги) НЕ моделируется. Спред-carry тут
funding-only. Это сознательное упрощение, помеченное явно.

ЗНАК funding: `fundingRate > 0` ⇒ лонги платят шортам. Позиция per-coin =
`sign(spread)` где `spread = f_a − f_b`:
  - spread > 0 (venue A богаче): short A (получаешь f_a), long B (платишь f_b) →
    позиция +1, carry = +1 · spread = f_a − f_b > 0.
  - spread < 0 (venue B богаче): зеркально, позиция −1, carry = −1 · spread > 0.
В обоих случаях carry = `position · spread = |spread|` при удержании. Так
`position · spread` — единая формула carry, знак спреда учтён.

CADENCE: HL/Drift = 1h funding, Binance/Bybit/Backpack = 8h. Приводим к ОБЩЕЙ 8h
сетке (00/08/16 UTC). Для 1h: СУММИРУЕМ 8 часовых fundings в 8h-бакет (funding за
период аддитивен как carry → 8×1h = одно 8h-начисление). Спред считаем на
выровненной 8h-сетке (inner join по времени).

TIMING / NO LOOK-AHEAD (зеркалит trend.donchian_signal):
  Сигнал spread_signal причинный: позиция в баре t решается из spread≤t (stateful
  hysteresis смотрит назад). НО зарабатывает она с ЛАГОМ: позиция, установленная в
  баре t (торгуешь в t), держится ВХОДЯ в t+1 и собирает funding следующих периодов.
  portfolio_returns_spread начисляет carry[t] = position[t-1] · spread[t] (позиция,
  держимая входя в t). Вход из flat в баре t зарабатывает 0 на баре t. Так нет
  заглядывания в spread[t]: чтобы собрать funding периода t, надо БЫЛО держать пару
  до t. Кост сделки платится в баре установки (t), как PLAN: «зарабатывает следующий
  период».

Только numpy/pandas.
"""

from pathlib import Path

import numpy as np
import pandas as pd


# ── Загрузка funding ─────────────────────────────────────────────────────────

def load_funding(venue_dir: Path, coin: str) -> "pd.Series | None":
    """Прочитать `{venue_dir}/{coin}.csv` → Series fundingRate, индекс UTC time.

    Колонки CSV ВАРЬИРУЮТСЯ между биржами (HANDLE BOTH):
      - HL: `coin,fundingRate,premium,time,annualizedPct` (1h).
      - Binance/Bybit: `time,fundingRate` (8h).
    Обе содержат `time` (ISO8601 UTC, у HL с дробными секундами) и `fundingRate`.

    Возврат: pd.Series(float) индексирована UTC-datetime, ОТСОРТИРОВАНА по времени,
    дедуплицирована по времени (keep=last на дубликат timestamp). None если файла
    нет.
    """
    venue_dir = Path(venue_dir)
    path = venue_dir / f"{coin}.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    if "time" not in df.columns or "fundingRate" not in df.columns:
        return None
    # ISO8601: HL пишет дробные секунды (…+00:00 c .139000) → format="ISO8601".
    t = pd.to_datetime(df["time"], format="ISO8601", utc=True)
    s = pd.Series(df["fundingRate"].astype(float).to_numpy(), index=t, name=coin)
    s = s.sort_index()
    # Дедуп на одинаковый timestamp (keep last наблюдение).
    s = s[~s.index.duplicated(keep="last")]
    return s


# ── Выравнивание на общую 8h-сетку ───────────────────────────────────────────

def resample_8h(funding: pd.Series, native_interval_h: int) -> pd.Series:
    """Привести funding на общую 8h-сетку, заякоренную в 00/08/16 UTC.

    1h-биржи (native_interval_h == 1, HL/Drift): СУММИРУЕМ 8 часовых fundings в
    каждый 8h-бакет. funding за период — аддитивный carry, поэтому сумма 8×1h в
    одно 8h-начисление корректна. Якорь сетки = epoch (origin="epoch"), метка
    бакета = левая граница (00/08/16), интервал полуоткрыт слева [t, t+8h).

    8h-биржи (native_interval_h == 8, Binance/Bybit/Backpack): выравниваем на ТУ
    ЖЕ epoch-заякоренную 8h-сетку через resample(...).sum(). Это no-op-ish
    выравнивание, которое также коллапсит любой дубликат и floor'ит off-grid
    timestamp (у HL/Binance дробные секунды) в канонический бакет 00/08/16 → индекс
    1-в-1 совпадает с просуммированной 1h-сеткой → inner join работает.

    Периоды без данных дропаются (НЕ фабрикуем нули): resample создаёт пустые
    бакеты как 0.0 (sum пустого = 0), но мы переоткрываем только реально занятые
    бакеты, переиндексируя на бакеты, где был хотя бы один исходный отсчёт.

    Возврат: 8h pd.Series.
    """
    if native_interval_h not in (1, 8):
        raise ValueError(f"resample_8h: native_interval_h must be 1 or 8, got {native_interval_h}")
    if len(funding) == 0:
        return funding.copy()

    # sum() в каждый epoch-заякоренный 8h-бакет [t, t+8h), метка = левая граница.
    summed = funding.resample("8h", origin="epoch", label="left", closed="left").sum()
    # resample заполняет пропущенные бакеты нулями (sum пустого = 0). Чтобы НЕ
    # фабриковать нулевые периоды там, где данных нет, оставляем только бакеты, в
    # которые попал хотя бы один исходный отсчёт.
    counts = funding.resample("8h", origin="epoch", label="left", closed="left").count()
    summed = summed[counts > 0]
    return summed


# ── Панель спредов ───────────────────────────────────────────────────────────

def build_spread_panel(venue_a: "tuple[Path, int]",
                       venue_b: "tuple[Path, int]",
                       coins: "list[str]") -> dict:
    """Собрать панель спредов f_a − f_b на общей 8h-сетке.

    venue_a / venue_b = (data_dir, native_interval_h). Для каждой монеты:
    load_funding + resample_8h обеих бирж, inner-join по 8h-индексу, spread =
    f_a − f_b. Монета попадает в панель ТОЛЬКО если присутствует в ОБЕИХ биржах
    (иначе пропускаем — нет пары, нет спреда).

    Сборка: gap-free 8h DatetimeIndex = union индексов всех монет; колонки = монеты
    (порядок входного `coins`, оставшиеся после фильтра). NaN там, где у монеты нет
    данных в этот период (pre-listing / пропуск) — НЕ заполняем нулями.

    Возврат: {
      "coins": [монеты, присутствующие в обеих биржах],
      "spread": DataFrame(8h index × coins) = f_a − f_b,
      "f_a":    DataFrame(тот же index/columns) = funding venue A на 8h-сетке,
      "f_b":    DataFrame(тот же index/columns) = funding venue B на 8h-сетке,
    }
    Все три DataFrame делят ОДИН index и columns.
    """
    dir_a, iv_a = venue_a
    dir_b, iv_b = venue_b

    per_coin = {}  # coin -> (f_a_8h, f_b_8h, spread) на пересечении времён
    coins_present = []
    for coin in coins:
        sa = load_funding(dir_a, coin)
        sb = load_funding(dir_b, coin)
        if sa is None or sb is None:
            continue  # нет в одной из бирж → нет пары
        fa = resample_8h(sa, iv_a)
        fb = resample_8h(sb, iv_b)
        common = fa.index.intersection(fb.index)  # inner join по времени
        if len(common) == 0:
            continue  # нет перекрытия по времени
        fa = fa.reindex(common)
        fb = fb.reindex(common)
        per_coin[coin] = (fa, fb, fa - fb)
        coins_present.append(coin)

    if not coins_present:
        empty = pd.DataFrame()
        return {"coins": [], "spread": empty, "f_a": empty.copy(), "f_b": empty.copy()}

    # Union всех 8h-индексов → gap-free общий индекс (отсортирован).
    full_index = None
    for coin in coins_present:
        idx = per_coin[coin][2].index
        full_index = idx if full_index is None else full_index.union(idx)
    full_index = full_index.sort_values()

    f_a = pd.DataFrame(index=full_index, columns=coins_present, dtype=float)
    f_b = pd.DataFrame(index=full_index, columns=coins_present, dtype=float)
    spread = pd.DataFrame(index=full_index, columns=coins_present, dtype=float)
    for coin in coins_present:
        fa, fb, sp = per_coin[coin]
        f_a[coin] = fa.reindex(full_index)   # NaN где монеты нет в этот период
        f_b[coin] = fb.reindex(full_index)
        spread[coin] = sp.reindex(full_index)

    return {"coins": coins_present, "spread": spread, "f_a": f_a, "f_b": f_b}


# ── Сигнал спреда (STATEFUL hysteresis) ──────────────────────────────────────

def spread_signal(spread: pd.DataFrame, threshold: float = 0.0,
                  hysteresis: float = 0.0) -> pd.DataFrame:
    """Stateful позиция per-coin в {-1, 0, +1} из спреда (зеркалит donchian_signal).

    Правило (causal, threshold ≥ hysteresis):
      - ВХОД: когда flat и `|spread[t]| > threshold` → позиция = sign(spread[t]).
      - ДЕРЖИМ: будучи в позиции, держим пока `|spread[t]| > hysteresis` (гистерезис
        против чаттера у границы). Позиция сохраняет знак ВХОДА, даже если спред
        качнулся (но НЕ перевернулся ниже гистерезиса).
      - ВЫХОД: когда `|spread[t]| <= hysteresis` → flat (0).
      - РАЗВОРОТ: если, держа позицию, спред крупно перевернул знак и
        `|spread[t]| > threshold` в обратную сторону — берём новый sign(spread[t])
        (полноценный сигнал противоположного направления переворачивает позицию,
        как у donchian противоположный пробой).

    Конвенция времени (NO LOOK-AHEAD): позиция в период t решается из spread[t]
    (наблюдаемый funding-режим), и эта позиция собирает carry периода t в
    portfolio_returns_spread. Используются только spread≤t — stateful цикл смотрит
    назад (prev pos), НЕ вперёд.

    NaN spread → flat (0) И СБРОС состояния: позиция не тянется через дырку в
    данных (pre-listing / пропуск), как у donchian_signal с notna-гейтом.

    Возврат: DataFrame формы spread, значения {-1.0, 0.0, +1.0}, без NaN.
    """
    assert threshold >= hysteresis, (
        f"spread_signal: threshold ({threshold}) must be >= hysteresis ({hysteresis})")

    out = pd.DataFrame(0.0, index=spread.index, columns=spread.columns)
    for c in spread.columns:
        sp = spread[c].to_numpy()
        col = np.zeros(len(sp), dtype=float)
        pos = 0.0
        for i in range(len(sp)):
            x = sp[i]
            if np.isnan(x):
                pos = 0.0                      # дырка → сброс состояния, flat
            elif pos == 0.0:
                if abs(x) > threshold:         # вход из flat по сильному сигналу
                    pos = float(np.sign(x))
                # иначе остаёмся flat
            else:
                # в позиции: проверяем выход / разворот / удержание
                if abs(x) <= hysteresis:
                    pos = 0.0                  # спред схлопнулся → выход в flat
                elif np.sign(x) != pos and abs(x) > threshold:
                    pos = float(np.sign(x))    # сильный разворот → флип
                # иначе держим текущую позицию (stateful hold)
            col[i] = pos
        out[c] = col
    return out


# ── Книга: дневная нетто-pnl спред-carry ─────────────────────────────────────

def portfolio_returns_spread(positions: pd.DataFrame, spread: pd.DataFrame,
                             taker_a_bps: float, taker_b_bps: float,
                             slip_bps: float = 0.2) -> pd.Series:
    """Дневная нетто-pnl книги cross-exchange spread-carry (одна серия = вся книга).

    CARRY (funding-only, БЕЗ ценовой ноги — delta-neutral, см. модульный caveat):
      per-период per-coin carry = `position[t-1,c] · spread[t,c]` (ЛАГ на период,
      NO LOOK-AHEAD): funding периода t собирает позиция, держимая ВХОДЯ в t (т.е.
      установленная в t-1). Вход из flat в баре t зарабатывает 0 на баре t.

    КОСТ (turnover на смену позиции). Определение «ноги» (документировано явно,
    self-test это ассертит):
      ОДНА единица |Δposition| = открыть ИЛИ закрыть ОДНУ cross-venue пару = 2 ноги
      (одна на бирже A + одна на бирже B). Стоимость единицы Δ:
        unit_cost = (taker_a_bps + taker_b_bps + 2·slip_bps) / 1e4
      где 2·slip — слиппедж на ОБЕ ноги. Тогда:
        - вход из flat (Δ=1): 2 ноги = taker_a + taker_b + 2·slip. ✓
        - выход в flat (Δ=1): тоже 2 ноги (закрыть пару). ✓
        - флип +1→−1 (Δ=2): 4 ноги = закрыть старую пару (2) + открыть новую (2)
          = 2 · unit_cost. ✓
      Кост периода t для монеты = `|Δposition| · unit_cost`. Списывается ТОЛЬКО при
      смене позиции (Δ≠0); при удержании Δ=0 → кост 0.

    EQUAL-WEIGHT: per-период нетто-pnl книги = СРЕДНЕЕ per-coin нетто-pnl по
    монетам, присутствующим в этот период (делим сумму на число non-NaN монет
    периода — книга это per-период mean, а не sum, растущий с размером юниверса).
    Период без активных монет → 0.

    АГРЕГАЦИЯ: 8h-периодные нетто-pnl суммируются в ДНЕВНЫЕ на выходе
    (resample("1D").sum()).

    Возврат: pd.Series дневной нетто-pnl книги, индексирована датами (для harness).
    """
    pos = positions.reindex_like(spread)
    sp = spread

    unit_cost = (taker_a_bps + taker_b_bps + 2.0 * slip_bps) / 1e4

    # Маска присутствия монеты в период (спред не NaN). Позиция там по построению
    # spread_signal flat при NaN-спреде, но опираемся на спред как источник истины.
    present = sp.notna()

    # NO LOOK-AHEAD: позиция, УСТАНОВЛЕННАЯ в баре t (торгуешь в t, кост платится в
    # t), зарабатывает funding СЛЕДУЮЩИХ периодов — carry периода t собирает позиция,
    # которую держал ВХОДЯ в t, т.е. установленная в t-1. Поэтому carry лагается на
    # один период: carry[t] = position[t-1] · spread[t]. Вход из flat в баре t
    # зарабатывает 0 на баре t (входя в t был flat) и начинает зарабатывать с t+1.
    # (Эффективный lookback книги ≥1 период → Task C берёт purge ≥ 1 период, плюс
    # окно rolling-стата если threshold на нём; при константном threshold purge=1.)
    pos_filled = pos.where(present, 0.0)
    pos_held = pos_filled.shift(1).fillna(0.0)       # позиция, держимая ВХОДЯ в t
    carry = (pos_held * sp).where(present, 0.0)      # лаг: position[t-1] · spread[t]

    # Turnover per-coin: |Δposition| между последовательными периодами = сделка,
    # выставленная в баре t (кост платится в t, позиция в силе с t+1). Первый период
    # считаем от flat (prev=0). NaN-позиции (вне present) → 0, чтобы дырка не
    # порождала ложный turnover; spread_signal уже сбрасывает в 0 при NaN.
    dpos = pos_filled.diff()
    dpos.iloc[0] = pos_filled.iloc[0]        # вход из flat в первом периоде
    cost = dpos.abs() * unit_cost

    net = carry - cost                        # per-coin нетто per период

    # Equal-weight: per-период mean по присутствующим монетам.
    n_present = present.sum(axis=1)
    book_per_period = net.where(present, np.nan).sum(axis=1) / n_present.replace(0, np.nan)
    book_per_period = book_per_period.fillna(0.0)

    # 8h → daily.
    daily = book_per_period.resample("1D").sum()
    daily.name = "spread_net"
    return daily


# ── Hand-checkable self-test ─────────────────────────────────────────────────

if __name__ == "__main__":
    n_pass = 0

    def check(cond, msg):
        global n_pass
        assert cond, msg
        n_pass += 1

    # ── 1) resample_8h: 8 часовых баров → один 8h-бакет (сумма) ───────────────
    # Строим непрерывный 8-часовой отрезок 1h-фандинга от полуночи (00:00..07:00).
    h_idx = pd.date_range("2026-01-01 00:00", periods=8, freq="1h", tz="UTC")
    hourly = pd.Series([0.0001] * 8, index=h_idx, name="X")
    r1 = resample_8h(hourly, native_interval_h=1)
    check(len(r1) == 1,
          f"resample_8h: 8 hourly bars from 00:00 must collapse to ONE 8h bucket, got {len(r1)}")
    check(r1.index[0] == pd.Timestamp("2026-01-01 00:00", tz="UTC"),
          f"resample_8h: bucket must be anchored at 00:00 UTC, got {r1.index[0]}")
    check(np.isclose(r1.iloc[0], 8 * 0.0001),
          f"resample_8h: 8×0.0001 must SUM to {8*0.0001}, got {r1.iloc[0]}")

    # 16 часовых баров → два бакета (00:00 и 08:00), каждый сумма 8.
    h_idx2 = pd.date_range("2026-01-01 00:00", periods=16, freq="1h", tz="UTC")
    hourly2 = pd.Series(np.arange(16, dtype=float) * 1e-5, index=h_idx2)
    r2 = resample_8h(hourly2, native_interval_h=1)
    check(len(r2) == 2, f"resample_8h: 16 hourly bars → 2 buckets, got {len(r2)}")
    check(np.isclose(r2.iloc[0], sum(range(0, 8)) * 1e-5),
          "resample_8h: first bucket = sum of hours 0..7")
    check(np.isclose(r2.iloc[1], sum(range(8, 16)) * 1e-5),
          "resample_8h: second bucket = sum of hours 8..15")

    # 8h-биржа: уже на сетке, off-grid дробные секунды флорятся в 00/08/16.
    e_idx = pd.to_datetime(
        ["2026-01-01 00:00:00.123+00:00", "2026-01-01 08:00:00.456+00:00"], utc=True)
    eight = pd.Series([0.0002, -0.0001], index=e_idx)
    r8 = resample_8h(eight, native_interval_h=8)
    check(len(r8) == 2, f"resample_8h(8h): 2 bars stay 2 buckets, got {len(r8)}")
    check(r8.index[0] == pd.Timestamp("2026-01-01 00:00", tz="UTC")
          and r8.index[1] == pd.Timestamp("2026-01-01 08:00", tz="UTC"),
          f"resample_8h(8h): off-grid stamps must floor to 00:00/08:00 grid:\n{r8.index}")
    check(np.isclose(r8.iloc[0], 0.0002) and np.isclose(r8.iloc[1], -0.0001),
          "resample_8h(8h): values preserved on alignment")
    # Сетки 1h-сумм и 8h-выравнивания совпадают по индексу → inner join работает.
    check(r8.index[0] == r1.index[0],
          "resample_8h: 1h-summed and 8h-aligned grids must share the same anchor")

    # ── Toy панель: 2 монеты, 6 8h-периодов, числа подобраны вручную ──────────
    t_idx = pd.date_range("2026-02-01 00:00", periods=6, freq="8h", tz="UTC")
    #   COINA spread: + + (dip) + −  →  тестирует знак, threshold, hysteresis, флип.
    #   COINB spread: содержит NaN-дырку → flat + сброс состояния.
    sp_a = [0.0010, 0.0010, 0.0001, 0.0010, -0.0010, -0.0010]
    sp_b = [0.0010, np.nan, 0.0010, 0.0010, 0.0010, 0.0010]
    spread = pd.DataFrame({"COINA": sp_a, "COINB": sp_b}, index=t_idx)

    # ── 2) spread_signal: знак, threshold, hysteresis, флип, NaN-сброс ────────
    thr, hys = 0.0005, 0.0002
    pos = spread_signal(spread, threshold=thr, hysteresis=hys)

    a = pos["COINA"]
    # t0: |0.0010|>thr → вход long (+1) = sign(+).
    check(a.iloc[0] == 1.0, f"signal: COINA t0 enter long sign(+):\n{a}")
    # t1: held, |0.0010|>hys → hold +1.
    check(a.iloc[1] == 1.0, f"signal: COINA t1 hold long:\n{a}")
    # t2: spread dips to 0.0001 → |0.0001|<=hys(0.0002) → EXIT to flat (no chatter
    # past the hysteresis floor).
    check(a.iloc[2] == 0.0,
          f"signal: COINA t2 |spread|<=hys must EXIT flat:\n{a}")
    # t3: |0.0010|>thr → re-enter long.
    check(a.iloc[3] == 1.0, f"signal: COINA t3 re-enter long:\n{a}")
    # t4: spread flips to −0.0010, |·|>thr → FLIP to short (−1).
    check(a.iloc[4] == -1.0, f"signal: COINA t4 strong reversal must FLIP short:\n{a}")
    # t5: held short, still strong negative → hold −1.
    check(a.iloc[5] == -1.0, f"signal: COINA t5 hold short:\n{a}")

    b = pos["COINB"]
    # t0: |0.0010|>thr → long. t1: NaN → flat + reset.
    check(b.iloc[0] == 1.0, f"signal: COINB t0 long:\n{b}")
    check(b.iloc[1] == 0.0, f"signal: COINB t1 NaN must be flat (reset):\n{b}")
    # t2: real again, |0.0010|>thr → re-enter long (state was reset, so this is a
    # fresh entry not a hold-through-gap).
    check(b.iloc[2] == 1.0, f"signal: COINB t2 re-enter after gap:\n{b}")

    # Hysteresis anti-chatter: a small dip that stays ABOVE hys must HOLD, not exit.
    sp_hold = pd.DataFrame({"H": [0.0010, 0.0003, 0.0010]},
                           index=pd.date_range("2026-03-01", periods=3, freq="8h", tz="UTC"))
    pos_hold = spread_signal(sp_hold, threshold=0.0005, hysteresis=0.0002)
    check(list(pos_hold["H"]) == [1.0, 1.0, 1.0],
          f"signal: dip 0.0003 (>hys 0.0002) must HOLD long, not chatter:\n{pos_hold['H']}")

    # threshold>=hysteresis assert fires when violated.
    try:
        spread_signal(spread, threshold=0.0001, hysteresis=0.0005)
        check(False, "signal: threshold<hysteresis must raise AssertionError")
    except AssertionError as e:
        check("threshold" in str(e), "signal: threshold<hysteresis assert message")

    # ── 3) portfolio_returns_spread: LAGGED carry, cost-on-flip, leg formula ──
    # CONVENTION (no look-ahead): carry[t] = position[t-1]·spread[t]; cost charged
    # at the bar a position is ESTABLISHED/changed. Entry bar earns 0 (was flat).
    TA, TB, SLIP = 3.5, 5.0, 0.2     # HL / Binance taker + slip bps
    unit = (TA + TB + 2 * SLIP) / 1e4   # cost of ONE |Δpos| unit = 2 legs

    # (a) Pure hold (3 8h periods = ONE UTC day). Steady +1, spread +0.0010.
    #   pos_held = [0,1,1] → carry = [0, 0.0010, 0.0010] = 0.0020 (entry bar earns 0).
    #   cost = entry only = 1·unit. net = 0.0020 − unit.
    hold_pos = pd.DataFrame({"C": [1.0, 1.0, 1.0]},
                            index=pd.date_range("2026-05-01", periods=3, freq="8h", tz="UTC"))
    hold_sp = pd.DataFrame({"C": [0.0010, 0.0010, 0.0010]}, index=hold_pos.index)
    hold_daily = portfolio_returns_spread(hold_pos, hold_sp, TA, TB, slip_bps=SLIP)
    check(len(hold_daily) == 1, f"portfolio: 3 same-day 8h periods → 1 daily bar, got {len(hold_daily)}")
    check(np.isclose(hold_daily.iloc[0], 0.0020 - unit),
          f"portfolio(hold): entry bar earns 0, cost only at entry: {hold_daily.iloc[0]} "
          f"!= {0.0020 - unit}")

    # (b) Entry then FLIP (3 periods, 1 day). pos [1,1,-1], spread [+,+,−].
    #   pos_held = [0,1,1] → carry = [0, +1·0.0010, +1·(−0.0010)] = [0, +0.0010, −0.0010] = 0
    #     (held LONG into the bar whose spread flipped negative → eats the negative).
    #   cost = entry(1·unit) + flip Δ=2 (2·unit) = 3·unit. net = 0 − 3·unit.
    s_idx = pd.date_range("2026-04-01 00:00", periods=3, freq="8h", tz="UTC")
    flip_pos = pd.DataFrame({"C": [1.0, 1.0, -1.0]}, index=s_idx)
    flip_sp = pd.DataFrame({"C": [0.0010, 0.0010, -0.0010]}, index=s_idx)
    flip_daily = portfolio_returns_spread(flip_pos, flip_sp, TA, TB, slip_bps=SLIP)
    check(np.isclose(flip_daily.iloc[0], 0.0 - 3 * unit),
          f"portfolio(flip): held-long eats flip-bar negative carry; flip = 4-leg cost: "
          f"{flip_daily.iloc[0]} != {-3 * unit}")

    # (c) Cost charged ONLY on a change: same as (a) but assert a 2-bar steady hold
    #   spanning ONE day earns one held bar of carry and one entry cost.
    two_idx = pd.date_range("2026-06-01 00:00", periods=2, freq="8h", tz="UTC")
    two_pos = pd.DataFrame({"C": [1.0, 1.0]}, index=two_idx)
    two_sp = pd.DataFrame({"C": [0.0010, 0.0010]}, index=two_idx)
    two_daily = portfolio_returns_spread(two_pos, two_sp, TA, TB, slip_bps=SLIP)
    # pos_held=[0,1] → carry=[0,0.0010]=0.0010; cost=entry(unit). net=0.0010−unit.
    check(np.isclose(two_daily.iloc[0], 0.0010 - unit),
          f"portfolio(hold cost-once): {two_daily.iloc[0]} != {0.0010 - unit}")

    # ── 4) NaN handling: gap → flat + reset, ZERO carry/cost contribution ─────
    nan_idx = pd.date_range("2026-07-01 00:00", periods=3, freq="8h", tz="UTC")
    nan_sp = pd.DataFrame({"C": [0.0010, np.nan, 0.0010]}, index=nan_idx)
    nan_pos = spread_signal(nan_sp, threshold=0.0005, hysteresis=0.0002)
    check(nan_pos.iloc[1, 0] == 0.0,
          f"NaN: position must be flat during the gap:\n{nan_pos['C']}")
    nan_daily = portfolio_returns_spread(nan_pos, nan_sp, TA, TB, slip_bps=SLIP)
    # pos = [1, 0, 1] (long, gap reset, re-enter). pos_held = [0,1,0];
    #   carry = (pos_held·sp).where(present[T,F,T]) = [0, masked→0, 0·0.0010=0] = 0
    #   (lagged convention: short holds across a gap never span a clean earning bar).
    #   cost: |Δpos| on [1,0,1] = [1,1,1], BUT the gap bar (p1) is not `present` so it
    #   is EXCLUDED from the per-period mean aggregation → its close-trade cost drops
    #   (you can't trade at a delisted/gapped bar). Counted: p0 entry + p2 re-entry =
    #   2·unit. net = 0 − 2·unit. (Conservative: under-charges one close on rare gaps.)
    check(np.isclose(nan_daily.iloc[0], 0.0 - 2 * unit),
          f"NaN: gap contributes 0 carry; gap-bar cost masked → 2 trades counted: "
          f"{nan_daily.iloc[0]} != {-2 * unit}")

    # ── 5) Equal-weight: book = per-period MEAN over present coins ─────────────
    # 2 coins both present, 2 periods (1 day). C1 spread 0.0010, C2 spread 0.0020.
    ew_idx = pd.date_range("2026-08-01 00:00", periods=2, freq="8h", tz="UTC")
    ew_sp = pd.DataFrame({"C1": [0.0010, 0.0010], "C2": [0.0020, 0.0020]}, index=ew_idx)
    ew_pos = pd.DataFrame({"C1": [1.0, 1.0], "C2": [1.0, 1.0]}, index=ew_idx)
    ew_daily = portfolio_returns_spread(ew_pos, ew_sp, TA, TB, slip_bps=SLIP)
    #  C1 net = [−unit, 0.0010]; C2 net = [−unit, 0.0020].
    #  per-period mean: p0 = −unit; p1 = (0.0010+0.0020)/2 = 0.0015. daily = −unit + 0.0015.
    check(np.isclose(ew_daily.iloc[0], -unit + 0.0015),
          f"equal-weight: per-period mean over present coins: {ew_daily.iloc[0]} "
          f"!= {-unit + 0.0015}")

    # NaN coin excluded from the per-period mean: C2 NaN at p0 → p0 mean over C1 only.
    ew2_sp = pd.DataFrame({"C1": [0.0010, 0.0010], "C2": [np.nan, 0.0020]}, index=ew_idx)
    ew2_pos = pd.DataFrame({"C1": [1.0, 1.0], "C2": [0.0, 1.0]}, index=ew_idx)
    ew2_daily = portfolio_returns_spread(ew2_pos, ew2_sp, TA, TB, slip_bps=SLIP)
    #  p0 present = {C1} → mean = C1 net[0] = −unit.
    #  p1 present = {C1,C2}: C1 net[1]=0.0010; C2 (pos_held=[0,0], entry cost at p1) net[1]=−unit.
    #     mean = (0.0010 − unit)/2. daily = −unit + (0.0010 − unit)/2.
    check(np.isclose(ew2_daily.iloc[0], -unit + (0.0010 - unit) / 2.0),
          f"equal-weight: NaN coin excluded from p0 mean: {ew2_daily.iloc[0]} "
          f"!= {-unit + (0.0010 - unit) / 2.0}")

    print("\n=== spread.py self-test ===")
    print("\nresample_8h (8×0.0001 hourly → one 8h bucket):")
    print(r1.to_string())
    print("\nspread_signal (thr=0.0005, hys=0.0002):")
    print(pos.to_string())
    print("\nportfolio daily net (single coin, entry+flip, lagged carry):")
    print(flip_daily.round(8).to_string())
    print(f"\n  unit-leg cost = {unit:.8f}, flip-day net = {flip_daily.iloc[0]:.8f}")
    print(f"\nOK: {n_pass} spread.py self-tests passed")
