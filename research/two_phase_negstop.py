"""
Two-Phase + Phase-1 NEGATIVE HARD-STOP (вариант A).

Цель: честно измерить идею «фандинг ушёл в минус → руби», ОБХОДЯ динамический
min_hold. Существующий two_phase_exit.py использует фиксированный base_min_hold и
НЕ моделирует прод-овский динамический min_hold (cap 720ч), из-за которого SOL #26
заперт на 30 дней. Здесь baseline = точная прод-логика (two_phase_dynamic), а
вариант A добавляет негативный hard-stop поверх неё.

Baseline (прод): entry 0.10, signal_window 12, base 24, mult 5, cap 720,
                 phase1_neg_patience 72, phase1_cap 720, phase2_exit -0.10.

Вариант A: если позиция в Phase 1 (fees не отбиты) И сглаженный сигнал
           < neg_stop_threshold И consec_negative >= neg_stop_patience →
           закрыть НЕМЕДЛЕННО, игнорируя position_min_hold (bypass=True).
Контроль:  то же, но bypass=False (негстоп срабатывает только ПОСЛЕ min_hold) —
           чтобы отделить эффект самого bypass от эффекта более строгого порога.

Доп. метрика bleed_hours: часы, проведённые открытым в Phase 1 при
rate < neg_stop_threshold — прямая мера «кровотечения», которое чинит A.
"""
import sys
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engine import (
    load_data,
    POSITION_SIZE, TOTAL_CAPITAL, PERP_TAKER, SPOT_TAKER, HOURS_PER_YEAR,
)
from adaptive_entry import metrics_window

BREAKEVEN_CONST = (PERP_TAKER + SPOT_TAKER) * 2 * HOURS_PER_YEAR  # 18.396
TOTAL_FEES_CYCLE = POSITION_SIZE * (PERP_TAKER + SPOT_TAKER) * 2   # $4.20


def simulate(
    coins,
    *,
    max_concurrent: int,
    entry_threshold: float,
    signal_window: int,
    base_min_hold: int,
    safety_mult: float,
    cap_min_hold: int,
    phase1_negative_patience: int,
    phase1_breakeven_cap_hours: int,
    phase2_exit_threshold: float,
    # Вариант A params (negstop disabled when neg_stop_patience is None)
    neg_stop_threshold: float | None = None,
    neg_stop_patience: int | None = None,
    neg_stop_bypass_min_hold: bool = True,
    # bleed metric measured at this threshold regardless of negstop
    bleed_threshold: float = -0.10,
) -> tuple:
    datas = {}
    for c in coins:
        df = load_data(c)
        if not df.empty:
            datas[c] = df

    common_idx = pd.DatetimeIndex(sorted(set().union(*[set(df.index) for df in datas.values()])))
    n = len(common_idx)

    state = {}
    for c, df in datas.items():
        df2 = df.reindex(common_idx)
        rates = df2["fundingRate"].values
        close = df2["close"].values
        if signal_window > 1:
            sig = pd.Series(rates).rolling(signal_window, min_periods=1).mean().values * HOURS_PER_YEAR
        else:
            sig = rates * HOURS_PER_YEAR
        state[c] = {
            "rates": rates, "close": close, "signal": sig,
            "valid": ~np.isnan(close) & ~np.isnan(rates),
            "in_position": False, "short_size": 0.0, "units_spot": 0.0,
            "entry_price": 0.0, "hours_since": 0,
            "cash": TOTAL_CAPITAL, "equity_prev": TOTAL_CAPITAL, "trades": 0,
            "gross_funding_so_far": 0.0, "total_fees_paid": 0.0,
            "consec_negative": 0, "position_min_hold": 0,
        }

    pnl_per_hour = np.zeros(n)
    cap_per_hour = np.zeros(n)
    opens_per_hour = np.zeros(n, dtype=int)
    closed_hold_hours = []
    holds_assigned = []
    phase1_exits = phase2_exits = negstop_exits = 0
    bleed_hours = 0  # hours open, phase1, rate < bleed_threshold

    def close_position(s, P):
        s["cash"] += s["short_size"] * (s["entry_price"] - P)
        s["cash"] -= s["short_size"] * P * PERP_TAKER
        s["cash"] += s["units_spot"] * P
        s["cash"] -= s["units_spot"] * P * SPOT_TAKER
        s["short_size"] = 0.0
        s["units_spot"] = 0.0
        s["entry_price"] = 0.0
        s["in_position"] = False
        s["gross_funding_so_far"] = 0.0
        s["total_fees_paid"] = 0.0
        s["consec_negative"] = 0
        s["position_min_hold"] = 0

    for i in range(n):
        # 1) Funding accrual
        for c, s in state.items():
            if s["valid"][i] and s["in_position"]:
                P = s["close"][i]
                hourly = s["short_size"] * P * s["rates"][i]
                s["cash"] += hourly
                s["gross_funding_so_far"] += hourly

        # 2) Exit
        for c, s in state.items():
            if not s["valid"][i] or not s["in_position"]:
                continue
            s["hours_since"] += 1
            rate = s["signal"][i]

            # consec_negative tracked EVERY hour (even under min_hold lock)
            if rate < 0:
                s["consec_negative"] += 1
            else:
                s["consec_negative"] = 0

            in_profit = s["gross_funding_so_far"] >= s["total_fees_paid"]

            # bleed metric
            if (not in_profit) and rate < bleed_threshold:
                bleed_hours += 1

            should_exit = False
            is_negstop = False

            # --- Вариант A: negative hard-stop ---
            if (neg_stop_patience is not None and not in_profit
                    and rate < neg_stop_threshold
                    and s["consec_negative"] >= neg_stop_patience):
                if neg_stop_bypass_min_hold or s["hours_since"] >= s["position_min_hold"]:
                    should_exit = True
                    is_negstop = True

            # --- Standard prod gate + two-phase logic ---
            if not should_exit:
                if s["hours_since"] < s["position_min_hold"]:
                    continue  # locked by min_hold
                income = POSITION_SIZE * rate / HOURS_PER_YEAR
                if not in_profit:
                    if s["consec_negative"] > phase1_negative_patience:
                        should_exit = True
                    elif income > 0:
                        rem = s["total_fees_paid"] - s["gross_funding_so_far"]
                        if rem / income > phase1_breakeven_cap_hours:
                            should_exit = True
                else:
                    if rate < phase2_exit_threshold:
                        should_exit = True

            if should_exit:
                P = s["close"][i]
                closed_hold_hours.append(s["hours_since"])
                if is_negstop:
                    negstop_exits += 1
                elif not in_profit:
                    phase1_exits += 1
                else:
                    phase2_exits += 1
                close_position(s, P)

        # 3-4) Entry — top-K by signal
        active = [c for c, s in state.items() if s["in_position"]]
        slots_free = max_concurrent - len(active)
        if slots_free > 0:
            cands = [(c, state[c]["signal"][i]) for c, s in state.items()
                     if s["valid"][i] and not s["in_position"] and s["signal"][i] > entry_threshold]
            cands.sort(key=lambda x: -x[1])
            for c, _ in cands[:slots_free]:
                s = state[c]
                P = s["close"][i]
                entry_rate = s["signal"][i]
                if entry_rate > 0:
                    pos_min_hold = int(min(cap_min_hold,
                                           max(base_min_hold, safety_mult * BREAKEVEN_CONST / entry_rate)))
                else:
                    pos_min_hold = cap_min_hold
                s["position_min_hold"] = pos_min_hold
                holds_assigned.append(pos_min_hold)
                s["units_spot"] = POSITION_SIZE / P
                s["cash"] -= POSITION_SIZE + POSITION_SIZE * SPOT_TAKER
                s["short_size"] = POSITION_SIZE / P
                s["entry_price"] = P
                s["cash"] -= POSITION_SIZE * PERP_TAKER
                s["in_position"] = True
                s["hours_since"] = 0
                s["gross_funding_so_far"] = 0.0
                s["total_fees_paid"] = TOTAL_FEES_CYCLE
                s["consec_negative"] = 0
                s["trades"] += 1
                opens_per_hour[i] += 1

        # 5) MTM
        hp = hc = 0.0
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            P = s["close"][i]
            spnl = s["short_size"] * (s["entry_price"] - P) if s["in_position"] else 0.0
            eq = s["cash"] + s["units_spot"] * P + spnl
            hp += eq - s["equity_prev"]
            s["equity_prev"] = eq
            if s["in_position"]:
                hc += TOTAL_CAPITAL
        pnl_per_hour[i] = hp
        cap_per_hour[i] = hc

    # Final close
    for c, s in state.items():
        if not s["in_position"]:
            continue
        vc = s["close"][s["valid"]]
        if len(vc) == 0:
            continue
        P = vc[-1]
        s["cash"] += s["short_size"] * (s["entry_price"] - P) - s["short_size"] * P * PERP_TAKER
        s["cash"] += s["units_spot"] * P - s["units_spot"] * P * SPOT_TAKER
        pnl_per_hour[-1] += s["cash"] - s["equity_prev"]
        s["equity_prev"] = s["cash"]

    info = {
        "opens_per_hour": opens_per_hour,
        "avg_hold_h": round(float(np.mean(closed_hold_hours)), 1) if closed_hold_hours else 0.0,
        "phase1_exits": phase1_exits,
        "phase2_exits": phase2_exits,
        "negstop_exits": negstop_exits,
        "bleed_hours": bleed_hours,
        "avg_min_hold": round(float(np.mean(holds_assigned)), 1) if holds_assigned else 0.0,
    }
    return pnl_per_hour, cap_per_hour, info


# Prod params
PROD = dict(
    max_concurrent=3, entry_threshold=0.10, signal_window=12,
    base_min_hold=24, safety_mult=5.0, cap_min_hold=720,
    phase1_negative_patience=72, phase1_breakeven_cap_hours=720,
    phase2_exit_threshold=-0.10,
)
PROD_COINS = ["BTC", "ETH", "SOL", "HYPE", "ZEC", "PURR", "XPL"]


def run_row(label, coins, capital_base, **overrides):
    cfg = {**PROD, **overrides}
    pnl, cap, info = simulate(coins, **cfg)
    n = len(pnl)
    rows = []
    for period, start in [("full", 0), ("last_180d", max(0, n - 180 * 24)), ("last_90d", max(0, n - 90 * 24))]:
        m = metrics_window(pnl, cap, info["opens_per_hour"], capital_base, start, n)
        rows.append({
            "label": label, "period": period,
            "annual": m["annual"], "max_dd": m["max_dd"], "calmar": m["calmar"],
            "sharpe": m["sharpe"], "trades": m["trades"], "tim_pct": m["time_in_market_pct"],
            "avg_hold_h": info["avg_hold_h"], "avg_min_hold": info["avg_min_hold"],
            "p1_exits": info["phase1_exits"], "p2_exits": info["phase2_exits"],
            "negstop_exits": info["negstop_exits"], "bleed_h": info["bleed_hours"],
        })
    return rows


def main():
    coins = PROD_COINS
    capital_base = PROD["max_concurrent"] * TOTAL_CAPITAL
    rows = []

    print("baseline (prod, no negstop) ...")
    rows += run_row("baseline_prod", coins, capital_base)

    # Variant A: bypass min_hold. Sweep threshold × patience.
    for thr in [-0.05, -0.10, -0.20]:
        for pat in [6, 12, 24]:
            print(f"A bypass thr={thr} pat={pat} ...")
            rows += run_row(f"A_bypass_thr{thr}_pat{pat}", coins, capital_base,
                            neg_stop_threshold=thr, neg_stop_patience=pat,
                            neg_stop_bypass_min_hold=True)

    # Control: negstop WITHOUT bypass (fires only after min_hold) — isolate bypass effect
    for thr in [-0.10]:
        for pat in [12, 24]:
            print(f"ctrl no-bypass thr={thr} pat={pat} ...")
            rows += run_row(f"ctrl_nobypass_thr{thr}_pat{pat}", coins, capital_base,
                            neg_stop_threshold=thr, neg_stop_patience=pat,
                            neg_stop_bypass_min_hold=False)

    df = pd.DataFrame(rows)
    out = Path(__file__).parent / "two_phase_negstop_results.csv"
    df.to_csv(out, index=False)

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    for period in ["full", "last_180d", "last_90d"]:
        print(f"\n===== {period} =====")
        sub = df[df["period"] == period].drop(columns=["period"])
        print(sub.to_string(index=False))
    print(f"\nsaved → {out}")


if __name__ == "__main__":
    main()
