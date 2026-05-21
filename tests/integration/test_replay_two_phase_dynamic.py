"""Integration test: replay real CSV data through full TwoPhaseDynamic stack.

Two tests:
1. smoke  — 6-month replay of prod TwoPhaseDynamic, sanity-checks plausible activity.
2. parity — compare prod TwoPhaseDynamic vs research simulate_two_phase_dynamic on
             the same windowed dataset.
"""
from __future__ import annotations

import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from frab.engine.loop import Engine
from frab.events.bus import EventBus
from frab.exchanges.atomic import AtomicExecutor
from frab.strategies.two_phase_dynamic import TwoPhaseDynamic, TwoPhaseDynamicParams

# ---------------------------------------------------------------------------
# Re-use helpers from the Strategy A integration test.
# They live in the same repo so direct import is acceptable for tests.
# ---------------------------------------------------------------------------
from tests.integration.test_replay_strategy_a import (
    ReplayMarketData,
    _ReplayExecutor,
    _common_timeline,
    _load_coin_data,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "research" / "data"

COINS = ("BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE")

# 6-month window — enough for two-phase exit logic to fire
WINDOW_START = datetime(2024, 1, 1, tzinfo=UTC)
WINDOW_END = datetime(2024, 7, 1, tzinfo=UTC)

# TwoPhaseDynamic config chosen from the SUMMARY / two_phase_dynamic_results sweep
C_PARAMS = dict(
    coins=COINS,
    entry_threshold=0.10,
    signal_window_hours=12,
    base_min_hold_hours=24,
    safety_mult=5.0,
    cap_min_hold_hours=720,
    phase1_negative_patience=72,
    phase1_breakeven_cap_hours=720,
    phase2_exit_threshold=-0.10,
    concurrency_cap=3,
    position_size_usdc=1000.0,
    fee_round_trip_annual=18.396,
)


# ---------------------------------------------------------------------------
# Helper: run research simulate_two_phase_dynamic on a restricted time window
# ---------------------------------------------------------------------------

def _run_research_simulation(coins, window_start, window_end):
    """Run research simulate_two_phase_dynamic on data restricted to window.

    Monkey-patches tpd.load_data to return only rows inside [window_start, window_end).
    Returns dict with total_trades, final_pnl, capital_base, phase1_exits,
    phase2_exits, avg_min_hold.
    """
    # Ensure research/ is importable
    research_dir = str(REPO_ROOT / "research")
    if research_dir not in sys.path:
        sys.path.insert(0, research_dir)

    import importlib
    import research.two_phase_dynamic as tpd
    from research.engine import load_data as _orig_load_data, TOTAL_CAPITAL

    # Naive UTC timestamps for comparison with research DatetimeIndex (no tz)
    ws_naive = pd.Timestamp(window_start).tz_convert(None)
    we_naive = pd.Timestamp(window_end).tz_convert(None)

    def _windowed_load_data(coin):
        df = _orig_load_data(coin)
        if df.empty:
            return df
        # research DatetimeIndex is tz-aware UTC; convert filter bounds accordingly
        ws = pd.Timestamp(window_start)
        we = pd.Timestamp(window_end)
        if df.index.tzinfo is None:
            ws = ws_naive
            we = we_naive
        return df[(df.index >= ws) & (df.index < we)]

    original_load = tpd.load_data
    tpd.load_data = _windowed_load_data
    try:
        pnl, cap, info = tpd.simulate_two_phase_dynamic(
            list(coins),
            max_concurrent=C_PARAMS["concurrency_cap"],
            entry_threshold=C_PARAMS["entry_threshold"],
            signal_window=C_PARAMS["signal_window_hours"],
            base_min_hold=C_PARAMS["base_min_hold_hours"],
            safety_mult=C_PARAMS["safety_mult"],
            cap_min_hold=C_PARAMS["cap_min_hold_hours"],
            phase1_negative_patience=C_PARAMS["phase1_negative_patience"],
            phase1_breakeven_cap_hours=C_PARAMS["phase1_breakeven_cap_hours"],
            phase2_exit_threshold=C_PARAMS["phase2_exit_threshold"],
        )
    finally:
        tpd.load_data = original_load

    capital_base = C_PARAMS["concurrency_cap"] * TOTAL_CAPITAL
    final_pnl = float(pnl.sum())
    return {
        "total_trades": info["total_trades"],
        "final_pnl": final_pnl,
        "capital_base": capital_base,
        "phase1_exits": info["phase1_exits"],
        "phase2_exits": info["phase2_exits"],
        "avg_min_hold": info["avg_min_hold_assigned"],
    }


# ---------------------------------------------------------------------------
# Test 1 — Smoke replay
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_two_phase_dynamic_six_months_smoke():
    """Prod TwoPhaseDynamic drives cleanly through 6 months of real data."""
    # 1. Load data
    data_by_coin: dict[str, list] = {}
    for coin in COINS:
        rows = _load_coin_data(coin, WINDOW_START, WINDOW_END)
        assert len(rows) > 0, f"no data for {coin} in window"
        data_by_coin[coin] = rows

    timeline = _common_timeline(data_by_coin)
    assert len(timeline) >= 24 * 28 * 6, (
        f"timeline too short: {len(timeline)} hours (expected ~4320)"
    )

    # 2. Build prod stack
    market = ReplayMarketData(data_by_coin)
    bus = EventBus()
    replay_executor = _ReplayExecutor(
        market_data=market,
        spot_taker_bps=7.0,   # matches research SPOT_TAKER = 0.00070
        perp_taker_bps=3.5,   # matches research PERP_TAKER = 0.00035
        extra_slip_bps=0.0,
    )
    executor = AtomicExecutor(replay_executor, bus, max_attempts=1, sleep_between_attempts=())
    params = TwoPhaseDynamicParams(**C_PARAMS)
    strategy = TwoPhaseDynamic(params=params, executor=executor)
    initial_cash = strategy.cash

    engine = Engine(market_data=market, strategy=strategy, coins=COINS)

    # 3. Drive engine
    opens_per_coin: dict[str, int] = {c: 0 for c in COINS}
    closes_per_coin: dict[str, int] = {c: 0 for c in COINS}
    total_fills = 0
    last_equity = None
    first_opened_min_hold: tuple[str, int] | None = None

    for ts in timeline:
        market.set_now(ts)
        outcome = await engine.tick_once(ts)
        assert outcome.equity is not None
        last_equity = outcome.equity
        if outcome.tick_report is not None:
            total_fills += len(outcome.tick_report.fills)
            for coin in outcome.tick_report.opened:
                opens_per_coin[coin] += 1
            for coin in outcome.tick_report.closed:
                closes_per_coin[coin] += 1
            # Capture first opened_min_holds entry for C-specific assertion
            if first_opened_min_hold is None and outcome.tick_report.opened_min_holds:
                first_opened_min_hold = outcome.tick_report.opened_min_holds[0]

    total_opens = sum(opens_per_coin.values())
    total_closes = sum(closes_per_coin.values())

    print(f"\ntotal_opens={total_opens}")
    print(f"total_closes={total_closes}")
    print(f"total_fills={total_fills}")
    print(f"last_equity.total_equity={last_equity.total_equity:.2f}")
    print(f"initial_cash={initial_cash:.2f}")
    print(f"opens_per_coin={opens_per_coin}")
    print(f"closes_per_coin={closes_per_coin}")

    # 4. Assertions
    assert total_opens >= 5, (
        f"expected >=5 opens over 6m with entry=0.10, got {total_opens}"
    )
    assert total_closes <= total_opens, (
        f"more closes than opens: {total_closes} > {total_opens}"
    )
    assert last_equity is not None
    assert 0.9 * initial_cash <= last_equity.total_equity <= 1.2 * initial_cash, (
        f"final equity {last_equity.total_equity:.2f} outside [0.9x, 1.2x] of initial {initial_cash:.2f}"
    )
    # fills = 2 legs per open + 2 legs per close
    assert total_fills == 2 * (total_opens + total_closes), (
        f"expected {2 * (total_opens + total_closes)} fills, got {total_fills}"
    )

    # TwoPhaseDynamic-specific: opened_min_holds must have been populated
    assert first_opened_min_hold is not None, (
        "no opened_min_holds populated — TwoPhaseDynamic not recording min_hold correctly"
    )
    coin_oh, min_hold_val = first_opened_min_hold
    assert coin_oh in COINS, f"opened_min_holds coin {coin_oh!r} not in universe"
    assert C_PARAMS["base_min_hold_hours"] <= min_hold_val <= C_PARAMS["cap_min_hold_hours"], (
        f"min_hold {min_hold_val} outside [{C_PARAMS['base_min_hold_hours']}, {C_PARAMS['cap_min_hold_hours']}]"
    )
    print(f"first opened_min_hold: coin={coin_oh}, min_hold={min_hold_val}h  ✓")


# ---------------------------------------------------------------------------
# Test 2 — Parity vs research
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_two_phase_dynamic_parity_with_research():
    """Prod TwoPhaseDynamic must produce opens/pnl comparable to research simulate_two_phase_dynamic."""
    # 1. Load same 6-month data
    data_by_coin: dict[str, list] = {}
    for coin in COINS:
        rows = _load_coin_data(coin, WINDOW_START, WINDOW_END)
        assert len(rows) > 0, f"no data for {coin}"
        data_by_coin[coin] = rows

    timeline = _common_timeline(data_by_coin)

    # 2. Prod run
    market = ReplayMarketData(data_by_coin)
    bus = EventBus()
    replay_executor = _ReplayExecutor(
        market_data=market,
        spot_taker_bps=7.0,
        perp_taker_bps=3.5,
        extra_slip_bps=0.0,
    )
    executor = AtomicExecutor(replay_executor, bus, max_attempts=1, sleep_between_attempts=())
    params = TwoPhaseDynamicParams(**C_PARAMS)
    strategy = TwoPhaseDynamic(params=params, executor=executor)
    initial_cash = strategy.cash

    engine = Engine(market_data=market, strategy=strategy, coins=COINS)

    total_opens = 0
    total_closes = 0
    last_equity = None

    for ts in timeline:
        market.set_now(ts)
        outcome = await engine.tick_once(ts)
        last_equity = outcome.equity
        if outcome.tick_report is not None:
            total_opens += len(outcome.tick_report.opened)
            total_closes += len(outcome.tick_report.closed)

    assert last_equity is not None
    final_pnl_prod = last_equity.total_equity - initial_cash

    # 3. Research run (monkey-patched to same window)
    research = _run_research_simulation(COINS, WINDOW_START, WINDOW_END)

    # 4. Print diagnostics (visible with pytest -s)
    print(f"\n--- Parity Diagnostics ---")
    print(f"prod:     opens={total_opens}, closes={total_closes}, final_pnl={final_pnl_prod:.2f}")
    print(
        f"research: trades={research['total_trades']}, final_pnl={research['final_pnl']:.2f}, "
        f"p1_exits={research['phase1_exits']}, p2_exits={research['phase2_exits']}, "
        f"avg_min_hold={research['avg_min_hold']}"
    )
    if research["total_trades"] > 0:
        ratio_trades = total_opens / research["total_trades"]
        print(f"ratio prod_opens/research_trades = {ratio_trades:.3f}")
    if abs(research["final_pnl"]) > 1e-6:
        ratio_pnl = final_pnl_prod / research["final_pnl"]
        print(f"ratio prod_pnl/research_pnl = {ratio_pnl:.3f}")

    # 5. Assertions — ±25% on trades (prod has PaperExecutor fees vs research flat fees,
    #    plus signal window edge differences), ±25% or ±$50 on pnl.
    research_trades = research["total_trades"]
    trade_tolerance = max(2, int(0.25 * research_trades))
    assert abs(total_opens - research_trades) <= trade_tolerance, (
        f"PARITY FAIL: prod opens={total_opens} vs research trades={research_trades} "
        f"(tolerance ±{trade_tolerance}). "
        f"Ratio={total_opens / research_trades:.3f} — check two_phase_signals.py logic."
    )

    tolerance_dollars = max(50.0, 0.25 * abs(research["final_pnl"]))
    assert abs(final_pnl_prod - research["final_pnl"]) <= tolerance_dollars, (
        f"PARITY FAIL: prod pnl={final_pnl_prod:.2f} vs research pnl={research['final_pnl']:.2f} "
        f"(tolerance ±{tolerance_dollars:.2f}). "
        f"Gap={final_pnl_prod - research['final_pnl']:.2f} — check funding accrual or fee model."
    )

    print("PARITY OK")
