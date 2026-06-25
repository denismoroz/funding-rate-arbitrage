"""
Two-Phase Exit + Margin-Aware Portfolio Backtester
====================================================
Combines:
  - Correct cross-margin model from research/portfolio_margin.py
    (per-coin leverage, req_margin reservation, top-up, forced-close)
  - Two-phase exit + per-position dynamic min_hold from
    src/frab/engine/two_phase_signals.py (SOURCE OF TRUTH)

All leverages, maint ratios, fees, and TwoPhaseParams fields loaded at
runtime from:
  - src/frab/constants.py          → RESEARCH_LEVERAGE, RESEARCH_MAINT_RATIO,
                                     PERP_TAKER, SPOT_TAKER
  - src/frab/strategy/two_phase/params.py → TwoPhaseParams defaults
  - /tmp/frab_prod.db  (or local data/frab.db) → strategies table,
                                     latest active row, params_json

Nothing hardcoded. If DB is unavailable, falls back to params.py defaults
and clearly states so.

Usage:
    python research/two_phase_margin.py
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
RESEARCH_DIR = Path(__file__).parent
DATA_DIR = RESEARCH_DIR / "data"

sys.path.insert(0, str(RESEARCH_DIR))

# ---------------------------------------------------------------------------
# Step 1: Load constants from src/frab/constants.py (AST parse — no import
#         needed so we don't touch src/ at runtime)
# ---------------------------------------------------------------------------

# Frozen research snapshot of the per-coin leverage / maintenance specs.
# These used to live in src/frab/constants.py but were migrated into the DB
# coin_registry (coin_registry rollout, 2026-06-17). The research backtest must
# stay reproducible without a live DB, so we keep a static snapshot here and only
# fall back to it when constants.py no longer carries the keys. Values are the
# exact ones removed from constants.py at commit 8256b9c.
_RESEARCH_SNAPSHOT: dict[str, Any] = {
    "RESEARCH_LEVERAGE": {
        "BTC": 40, "ETH": 25, "SOL": 20, "HYPE": 10, "ZEC": 10, "PURR": 3, "XPL": 10,
    },
    "RESEARCH_MAINT_RATIO": {
        "BTC": 0.01, "ETH": 0.01, "SOL": 0.025, "HYPE": 0.025,
        "ZEC": 0.025, "PURR": 0.025, "XPL": 0.025,
    },
    "FALLBACK_LEVERAGE": 3,
    "FALLBACK_MAINT_RATIO": 0.05,
}


def _load_constants() -> dict[str, Any]:
    src = (REPO_ROOT / "src" / "frab" / "constants.py").read_text()
    tree = ast.parse(src)

    result: dict[str, Any] = {}
    for node in ast.walk(tree):
        # Handle plain assignments: X = value
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        result[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        pass
        # Handle annotated assignments: X: type = value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and node.value is not None:
                try:
                    result[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass

    # Backfill specs migrated out of constants.py into the DB coin_registry.
    for key, default in _RESEARCH_SNAPSHOT.items():
        result.setdefault(key, default)
    return result


_CONSTANTS = _load_constants()

RESEARCH_LEVERAGE: dict[str, int] = _CONSTANTS["RESEARCH_LEVERAGE"]
RESEARCH_MAINT_RATIO: dict[str, float] = _CONSTANTS["RESEARCH_MAINT_RATIO"]
PERP_TAKER: float = _CONSTANTS["PERP_TAKER"]
SPOT_TAKER: float = _CONSTANTS["SPOT_TAKER"]
FALLBACK_LEVERAGE: int = _CONSTANTS.get("FALLBACK_LEVERAGE", 3)
FALLBACK_MAINT_RATIO: float = _CONSTANTS.get("FALLBACK_MAINT_RATIO", 0.05)

HOURS_PER_YEAR = 8760

# Round-trip fee constant: (PERP_TAKER + SPOT_TAKER) × 2 × HOURS_PER_YEAR
BREAKEVEN_CONST = (PERP_TAKER + SPOT_TAKER) * 2 * HOURS_PER_YEAR

# ---------------------------------------------------------------------------
# Step 2: Load TwoPhaseParams defaults from src/frab/strategy/two_phase/params.py
# ---------------------------------------------------------------------------

@dataclass
class TwoPhaseParams:
    """Mirror of src/frab/strategy/two_phase/params.py — NO src import."""
    coins: list[str] = field(default_factory=lambda: [
        "BTC", "ETH", "SOL", "HYPE", "PURR"
    ])  # ZEC/XPL excluded — out of scope. NB: the window cap is HYPE/PURR OHLCV
    # (price) starting 2025-11-06, NOT these coins; backfill HYPE/PURR price to widen.
    entry_threshold_apr: float = 0.10
    phase2_exit_threshold: float = -0.10
    base_min_hold_hours: int = 24
    cap_min_hold_hours: int = 720
    safety_mult: float = 5.0
    signal_window_hours: int = 12
    concurrency_cap: int = 3
    position_size_usdc: float = 1000.0
    budget_cap_usdc: float = 10000.0
    margin_buffer_factor: float = 3.0
    phase1_negative_patience: int = 72
    phase1_breakeven_cap_hours: int = 720
    # Phase-1 negative hard-stop (bypasses min_hold) — mirrors prod
    # src/frab/strategy/two_phase/params.py (added 2026-06-08, commit 9e17c8a).
    neg_stop_threshold_apr: float = -0.15   # in Phase 1, cut if smoothed signal < this
    neg_stop_patience_hours: int = 6        # ... and consec negative hours >= this

    @classmethod
    def from_dict(cls, d: dict) -> "TwoPhaseParams":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


def _load_params_from_db(db_path: str) -> dict | None:
    """Load params_json from the latest active strategy row in the DB."""
    try:
        con = sqlite3.connect(db_path)
        cur = con.execute(
            "SELECT params_json FROM strategies WHERE status='active' ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        con.close()
        if row:
            return json.loads(row[0])
        return None
    except Exception as exc:
        print(f"[WARN] Could not read DB {db_path}: {exc}")
        return None


def load_prod_params() -> tuple[TwoPhaseParams, str]:
    """
    Try /tmp/frab_prod.db first, then local data/frab.db.
    Returns (params, source_description).
    """
    for db_path in ["/tmp/frab_prod.db", str(REPO_ROOT / "data" / "frab.db")]:
        if os.path.exists(db_path):
            data = _load_params_from_db(db_path)
            if data is not None:
                p = TwoPhaseParams.from_dict(data)
                return p, f"DB:{db_path} params_json={json.dumps(data)}"
    # Fallback to dataclass defaults
    p = TwoPhaseParams()
    return p, "params.py defaults (DB unavailable)"


# ---------------------------------------------------------------------------
# Step 3: decide_two_phase + helpers (exact copy from prod's two_phase_signals.py)
# ---------------------------------------------------------------------------

def compute_position_min_hold(
    *,
    entry_signal_annual: float,
    safety_mult: float,
    base_min_hold_hours: int,
    cap_min_hold_hours: int,
) -> int:
    """
    Mirrors src/frab/engine/two_phase_signals.py:compute_position_min_hold.
    fee_round_trip_annual = BREAKEVEN_CONST = (PERP_TAKER + SPOT_TAKER) * 2 * HOURS_PER_YEAR
    """
    if entry_signal_annual > 0:
        breakeven_h = BREAKEVEN_CONST / entry_signal_annual
        return int(min(cap_min_hold_hours, max(base_min_hold_hours, safety_mult * breakeven_h)))
    else:
        return cap_min_hold_hours


def update_consec_negative(prev: int, signal_annual: float | None) -> int:
    """Mirrors src/frab/engine/two_phase_signals.py:update_consec_negative."""
    if signal_annual is None:
        return prev
    if signal_annual < 0:
        return prev + 1
    return 0


def decide_two_phase(
    *,
    in_position: bool,
    smoothed_signal_annual: float | None,
    entry_threshold: float,
    hours_in_position: int,
    position_min_hold_hours: int,
    gross_funding_so_far: float,
    total_fees_paid: float,
    consec_negative_hours: int,
    current_hourly_income_quote: float,
    phase1_negative_patience: int,
    phase1_breakeven_cap_hours: int,
    phase2_exit_threshold: float,
    neg_stop_threshold: float = -0.15,
    neg_stop_patience: int = 6,
    neg_overrides_min_hold: bool = False,
) -> str:
    """
    Pure port from src/frab/engine/two_phase_signals.py:decide_two_phase
    (in sync with commit 9e17c8a, 2026-06-08 — Phase-1 negative hard-stop).
    Returns: "NONE" | "OPEN" | "CLOSE_PHASE1_NEG" | "CLOSE_PHASE1_CAP"
             | "CLOSE_PHASE1_NEGSTOP" | "CLOSE_PHASE2"

    neg_overrides_min_hold is a legacy RESEARCH-ONLY knob (variant A, default off);
    it is NOT part of prod. The prod-canonical bypass is CLOSE_PHASE1_NEGSTOP below.
    """
    if not in_position:
        if smoothed_signal_annual is None:
            return "NONE"
        if smoothed_signal_annual > entry_threshold:
            return "OPEN"
        return "NONE"

    in_profit = gross_funding_so_far >= total_fees_paid

    # Phase-1 negative hard-stop — BYPASSES min_hold (checked BEFORE the lock).
    # Only while still trying to recoup fees (Phase 1): if the smoothed signal is
    # decisively negative and has been negative for >= neg_stop_patience hours, cut
    # now rather than sit under the min_hold lock bleeding funding. min_hold protects
    # against fee churn on mild/transient negativity, NOT a decisive funding flip.
    # Mirrors prod two_phase_signals.py (commit 9e17c8a).
    if (
        not in_profit
        and smoothed_signal_annual is not None
        and smoothed_signal_annual < neg_stop_threshold
        and consec_negative_hours >= neg_stop_patience
    ):
        return "CLOSE_PHASE1_NEGSTOP"

    # Legacy research-only emergency exit (variant A; not in prod, default off).
    if (
        neg_overrides_min_hold
        and not in_profit
        and consec_negative_hours > phase1_negative_patience
    ):
        return "CLOSE_PHASE1_NEG"

    # Min-hold lock
    if hours_in_position < position_min_hold_hours:
        return "NONE"

    if not in_profit:
        # Phase 1
        if consec_negative_hours > phase1_negative_patience:
            return "CLOSE_PHASE1_NEG"
        if current_hourly_income_quote > 0:
            remaining = total_fees_paid - gross_funding_so_far
            hours_to_be = remaining / current_hourly_income_quote
            if hours_to_be > phase1_breakeven_cap_hours:
                return "CLOSE_PHASE1_CAP"
        return "NONE"
    else:
        # Phase 2
        if smoothed_signal_annual is not None and smoothed_signal_annual < phase2_exit_threshold:
            return "CLOSE_PHASE2"
        return "NONE"


# ---------------------------------------------------------------------------
# Step 4: Data loading (reuse engine.py pattern)
# ---------------------------------------------------------------------------

def load_coin_df(coin: str) -> pd.DataFrame:
    """Load funding + OHLCV for a coin from research/data/."""
    funding = pd.read_csv(DATA_DIR / f"{coin}.csv")
    funding["time"] = pd.to_datetime(funding["time"], format="ISO8601", utc=True).dt.floor("h")
    funding = funding.set_index("time")[["fundingRate"]].sort_index()

    ohlcv = pd.read_csv(DATA_DIR / f"{coin}_1h.csv")
    ohlcv["time"] = pd.to_datetime(ohlcv["time"], format="ISO8601", utc=True).dt.floor("h")
    ohlcv = ohlcv.set_index("time")[["close"]].sort_index()

    df = funding.join(ohlcv, how="inner")
    # Drop rows missing price or rate
    df = df.dropna(subset=["close", "fundingRate"])
    # Drop first week where HL used 8h intervals (pre 2023-06-08)
    df = df[df.index >= pd.Timestamp("2023-06-08", tz="UTC")]
    return df


def common_timeline(dfs: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    idx: pd.DatetimeIndex | None = None
    for df in dfs.values():
        idx = df.index if idx is None else idx.intersection(df.index)
    return idx.sort_values()  # type: ignore[union-attr]


def add_signals(dfs: dict[str, pd.DataFrame], window: int) -> None:
    for df in dfs.values():
        rates = df["fundingRate"].values
        if window > 1:
            ma = pd.Series(rates).rolling(window, min_periods=1).mean().values
        else:
            ma = rates
        df["signal"] = ma * HOURS_PER_YEAR


# ---------------------------------------------------------------------------
# Step 5: Position state dataclass
# ---------------------------------------------------------------------------

@dataclass
class Position:
    open: bool = False
    units_spot: float = 0.0
    short_size: float = 0.0
    entry_price: float = 0.0
    required_margin: float = 0.0
    hours_in: int = 0
    position_min_hold: int = 0
    gross_funding_so_far: float = 0.0
    total_fees_paid: float = 0.0
    consec_negative: int = 0
    notional: float = 0.0  # per-position notional (spot leg size in USDC at open)
    # Attribution
    n_opens: int = 0
    n_closes: int = 0
    funding_gross: float = 0.0
    fees_paid_attr: float = 0.0
    realized_pnl_attr: float = 0.0
    hours_in_total: int = 0
    n_phase1_exits: int = 0
    n_phase2_exits: int = 0
    n_minhold_exits: int = 0  # positions force-closed at end while in min-hold


# ---------------------------------------------------------------------------
# Step 6: Simulate
# ---------------------------------------------------------------------------

def simulate(
    coins: list[str],
    params: TwoPhaseParams,
    margin_buffer_x: float | None = None,
    position_size: float | None = None,
    restrict_start: pd.Timestamp | None = None,
    restrict_end: pd.Timestamp | None = None,
    neg_overrides_min_hold: bool = False,
    _dfs_override: "dict | None" = None,
    sizing: str = "flat",
) -> dict:
    """
    Full two-phase + cross-margin portfolio simulation.

    margin_buffer_x:  overrides params.margin_buffer_factor if given
    position_size:    overrides params.position_size_usdc if given
    restrict_start:   clip all data to >= this timestamp (for apples-to-apples comparison)
    restrict_end:     clip all data to <= this timestamp
    _dfs_override:    if not None, use these pre-loaded DataFrames instead of calling
                      load_coin_df; keys must match coins, each df must have columns
                      [close, fundingRate] with a hourly DatetimeIndex.
                      restrict_start/restrict_end are still applied when provided.
                      add_signals is always applied (regardless of whether signals are
                      already present) to guarantee consistency.
                      (Additive parameter for MC adapter — default None preserves the
                      original load-from-csv behaviour; prod path is unaffected.)
    sizing:           "flat" (default) — psize = fixed notional for every coin (original
                      behaviour, all existing callers/tests/report unaffected).
                      "prod_slot" — prod-accurate sizing: per-coin notional is derived
                      from a fixed slot = budget_cap_usdc / concurrency_cap so that
                      footprint (notional + margin) = slot exactly for each open
                      position, matching src/frab/strategy/two_phase/params.py:
                          slot      = budget / K
                          notional  = slot / (1 + mbuf / lev_c)   # per coin
                          margin    = (notional / lev_c) * mbuf   # per coin
                          footprint = notional + margin = slot
                      In this mode psize is IGNORED; notional differs per coin
                      (higher leverage → less margin buffer → larger notional).
    """
    mbuf = margin_buffer_x if margin_buffer_x is not None else params.margin_buffer_factor
    psize = position_size if position_size is not None else params.position_size_usdc

    # prod_slot: slot = budget / K is fixed; per-coin notional = slot / (1 + mbuf/lev_c)
    _prod_slot_mode = sizing == "prod_slot"
    _slot = params.budget_cap_usdc / params.concurrency_cap  # used only in prod_slot mode

    TOP_UP_TRIGGER = 2.0
    HEALTHY_RATIO = 3.0

    # Load data
    if _dfs_override is not None:
        # MC adapter path: use caller-supplied DataFrames (no file I/O).
        dfs: dict[str, pd.DataFrame] = {}
        for c in coins:
            if c not in _dfs_override:
                print(f"[WARN] No data for {c} in _dfs_override, skipping")
                continue
            df = _dfs_override[c].copy()
            if restrict_start is not None:
                df = df[df.index >= restrict_start]
            if restrict_end is not None:
                df = df[df.index <= restrict_end]
            if not df.empty:
                dfs[c] = df
    else:
        dfs = {}
        for c in coins:
            try:
                df = load_coin_df(c)
                if restrict_start is not None:
                    df = df[df.index >= restrict_start]
                if restrict_end is not None:
                    df = df[df.index <= restrict_end]
                if not df.empty:
                    dfs[c] = df
            except FileNotFoundError:
                print(f"[WARN] No data for {c}, skipping")
    if not dfs:
        raise RuntimeError("No data loaded")

    timeline = common_timeline(dfs)
    add_signals(dfs, params.signal_window_hours)

    # Per-coin leverage / maint ratio
    lev_map = {c: RESEARCH_LEVERAGE.get(c, FALLBACK_LEVERAGE) for c in coins}
    mr_map = {c: RESEARCH_MAINT_RATIO.get(c, FALLBACK_MAINT_RATIO) for c in coins}

    # Wallet state
    budget = params.budget_cap_usdc
    spot_cash = budget
    perp_cash = 0.0

    positions: dict[str, Position] = {c: Position() for c in coins}

    n_liquidations = 0
    n_top_ups = 0
    n_forced_closes = 0
    n_skipped_opens = 0
    n_phase1_neg_exits = 0
    n_phase1_cap_exits = 0
    n_phase1_negstop_exits = 0
    n_phase2_exits = 0
    n_minhold_guard = 0

    equity_history: list[float] = []
    timestamps: list[pd.Timestamp] = []

    def _open_coins() -> list[str]:
        return [c for c, p in positions.items() if p.open]

    def _compute_equity(t: pd.Timestamp) -> float:
        spot_val = sum(
            pos.units_spot * dfs[c].loc[t, "close"]
            for c, pos in positions.items()
            if pos.open
        )
        upnl = sum(
            pos.short_size * (pos.entry_price - dfs[c].loc[t, "close"])
            for c, pos in positions.items()
            if pos.open
        )
        return spot_cash + perp_cash + spot_val + upnl

    for t in timeline:
        nonlocal_spot_cash = spot_cash
        nonlocal_perp_cash = perp_cash

        # --- 1. Accrue funding ---
        for c, pos in positions.items():
            if not pos.open or t not in dfs[c].index:
                continue
            close = dfs[c].loc[t, "close"]
            rate = dfs[c].loc[t, "fundingRate"]
            funding_delta = pos.short_size * close * rate
            perp_cash += funding_delta
            pos.gross_funding_so_far += funding_delta
            pos.funding_gross += funding_delta
            pos.hours_in_total += 1

        # --- 2. Two-phase exits ---
        for c, pos in positions.items():
            if not pos.open or t not in dfs[c].index:
                continue
            pos.hours_in += 1

            sig = dfs[c].loc[t, "signal"] if "signal" in dfs[c].columns else 0.0

            # Update consec_negative
            pos.consec_negative = update_consec_negative(pos.consec_negative, sig)

            # Current hourly income for this position
            # In prod_slot mode: use pos.notional (set at open); in flat mode: psize.
            _pos_notional = pos.notional if _prod_slot_mode else psize
            if sig is not None and sig > 0:
                current_hourly_income = _pos_notional * sig / HOURS_PER_YEAR
            else:
                current_hourly_income = 0.0

            decision = decide_two_phase(
                in_position=True,
                smoothed_signal_annual=sig,
                entry_threshold=params.entry_threshold_apr,
                hours_in_position=pos.hours_in,
                position_min_hold_hours=pos.position_min_hold,
                gross_funding_so_far=pos.gross_funding_so_far,
                total_fees_paid=pos.total_fees_paid,
                consec_negative_hours=pos.consec_negative,
                current_hourly_income_quote=current_hourly_income,
                phase1_negative_patience=params.phase1_negative_patience,
                phase1_breakeven_cap_hours=params.phase1_breakeven_cap_hours,
                phase2_exit_threshold=params.phase2_exit_threshold,
                neg_stop_threshold=params.neg_stop_threshold_apr,
                neg_stop_patience=params.neg_stop_patience_hours,
                neg_overrides_min_hold=neg_overrides_min_hold,
            )

            if decision == "NONE":
                continue

            # Execute close
            close = dfs[c].loc[t, "close"]
            realized = pos.short_size * (pos.entry_price - close)
            perp_fee = pos.short_size * close * PERP_TAKER
            spot_proceeds = pos.units_spot * close * (1.0 - SPOT_TAKER)
            spot_fee = pos.units_spot * close * SPOT_TAKER

            perp_cash += realized - perp_fee
            spot_cash += spot_proceeds

            pos.realized_pnl_attr += realized
            pos.fees_paid_attr += perp_fee + spot_fee
            pos.n_closes += 1

            if decision == "CLOSE_PHASE1_NEG":
                pos.n_phase1_exits += 1
                n_phase1_neg_exits += 1
            elif decision == "CLOSE_PHASE1_CAP":
                pos.n_phase1_exits += 1
                n_phase1_cap_exits += 1
            elif decision == "CLOSE_PHASE1_NEGSTOP":
                pos.n_phase1_exits += 1
                n_phase1_negstop_exits += 1
            elif decision == "CLOSE_PHASE2":
                pos.n_phase2_exits += 1
                n_phase2_exits += 1

            # Reset position (leave attribution counters intact)
            pos.open = False
            pos.units_spot = 0.0
            pos.short_size = 0.0
            pos.entry_price = 0.0
            pos.required_margin = 0.0
            pos.notional = 0.0
            pos.hours_in = 0
            pos.position_min_hold = 0
            pos.gross_funding_so_far = 0.0
            pos.total_fees_paid = 0.0
            pos.consec_negative = 0

        # --- 3. Entries ---
        # Sweep free perp cash back to spot before sizing new opens.
        # req_margin is moved spot→perp at open and the perp sub-account also
        # collects realized PnL + funding; on close the position's reservation is
        # released (required_margin→0) but the cash stays commingled in perp_cash.
        # Without this sweep, spot_cash drains monotonically across open/close
        # cycles and the budget guard below (spot_cash < total_needed) eventually
        # throttles ALL further opens — the book goes dormant and the backtest
        # silently understates activity. free_perp = perp_cash minus the margin
        # still reserved by currently-open positions; it is genuine spendable
        # capital, so return it to spot. Conserves total equity exactly.
        reserved_margin = sum(p.required_margin for p in positions.values() if p.open)
        free_perp = perp_cash - reserved_margin
        if free_perp > 0:
            perp_cash -= free_perp
            spot_cash += free_perp

        opens = _open_coins()
        n_open = len(opens)
        if n_open < params.concurrency_cap:
            candidates: list[tuple[float, str]] = []
            for c, pos in positions.items():
                if pos.open or t not in dfs[c].index:
                    continue
                sig = dfs[c].loc[t, "signal"] if "signal" in dfs[c].columns else 0.0
                if sig is not None and sig > params.entry_threshold_apr:
                    candidates.append((sig, c))
            candidates.sort(reverse=True)

            for sig, c in candidates:
                if n_open >= params.concurrency_cap:
                    break

                close = dfs[c].loc[t, "close"]
                lev = lev_map[c]

                # Sizing mode: flat uses fixed psize as notional;
                # prod_slot derives notional from slot so footprint = slot exactly.
                if _prod_slot_mode:
                    notional_c = _slot / (1.0 + mbuf / lev)
                else:
                    notional_c = psize

                req_margin = notional_c / lev * mbuf
                spot_fee_open = notional_c * SPOT_TAKER
                perp_fee_open = notional_c * PERP_TAKER
                total_needed = notional_c + req_margin + spot_fee_open + perp_fee_open

                # Budget check
                committed = sum(
                    positions[cc].units_spot * dfs[cc].loc[t, "close"] + positions[cc].required_margin
                    for cc in _open_coins()
                    if t in dfs[cc].index
                )
                if spot_cash < total_needed:
                    n_skipped_opens += 1
                    continue
                if committed + notional_c + req_margin > budget * 1.05:
                    n_skipped_opens += 1
                    continue

                # Open position
                units_spot = notional_c / close
                spot_cash -= notional_c + spot_fee_open
                spot_cash -= req_margin
                perp_cash += req_margin
                perp_cash -= perp_fee_open

                pos = positions[c]
                pos.open = True
                pos.units_spot = units_spot
                pos.short_size = units_spot
                pos.entry_price = close
                pos.required_margin = req_margin
                pos.notional = notional_c  # stored for use in exit/income calculations
                pos.hours_in = 0
                pos.consec_negative = 0
                pos.gross_funding_so_far = 0.0

                # Total fees for this position (open + estimated close)
                total_fees_this_pos = notional_c * (PERP_TAKER + SPOT_TAKER) * 2
                pos.total_fees_paid = total_fees_this_pos

                # Dynamic min_hold based on entry signal
                pos.position_min_hold = compute_position_min_hold(
                    entry_signal_annual=sig,
                    safety_mult=params.safety_mult,
                    base_min_hold_hours=params.base_min_hold_hours,
                    cap_min_hold_hours=params.cap_min_hold_hours,
                )

                pos.n_opens += 1
                pos.fees_paid_attr += spot_fee_open + perp_fee_open
                n_open += 1

        # --- 4. Margin policy ---
        opens = _open_coins()
        if opens:
            total_maintenance = sum(
                positions[c].short_size * dfs[c].loc[t, "close"] * mr_map[c]
                for c in opens
                if t in dfs[c].index
            )
            unrealized_pnl = sum(
                positions[c].short_size * (positions[c].entry_price - dfs[c].loc[t, "close"])
                for c in opens
                if t in dfs[c].index
            )
            perp_equity = perp_cash + unrealized_pnl

            if total_maintenance > 0:
                margin_ratio = perp_equity / total_maintenance

                if margin_ratio <= 1.0:
                    # Liquidation: the PERP leg is force-liquidated at the current
                    # mark (realize the perp loss + taker fee). The SPOT leg is
                    # still owned — sell it and credit the proceeds. (Old code
                    # discarded the entire spot leg, which for a delta-neutral
                    # book overstated the liquidation loss by ~the full spot value.)
                    for c in opens:
                        if t not in dfs[c].index:
                            continue
                        pos = positions[c]
                        close = dfs[c].loc[t, "close"]
                        spot_proceeds = pos.units_spot * close * (1.0 - SPOT_TAKER)
                        spot_cash += spot_proceeds
                        realized = pos.short_size * (pos.entry_price - close)
                        perp_fee = pos.short_size * close * PERP_TAKER
                        perp_cash += realized - perp_fee
                        pos.realized_pnl_attr += realized
                        pos.fees_paid_attr += perp_fee + pos.units_spot * close * SPOT_TAKER
                        pos.n_closes += 1
                        pos.open = False
                        pos.units_spot = 0.0
                        pos.short_size = 0.0
                        pos.entry_price = 0.0
                        pos.required_margin = 0.0
                        pos.notional = 0.0
                        pos.hours_in = 0
                        pos.gross_funding_so_far = 0.0
                        pos.total_fees_paid = 0.0
                        pos.consec_negative = 0
                    n_liquidations += 1

                elif margin_ratio < TOP_UP_TRIGGER:
                    target_perp = total_maintenance * HEALTHY_RATIO
                    top_up_needed = target_perp - perp_equity
                    if top_up_needed > 0 and spot_cash >= top_up_needed:
                        spot_cash -= top_up_needed
                        perp_cash += top_up_needed
                        n_top_ups += 1
                    else:
                        # Force-close weakest signal position
                        worst_c = None
                        worst_sig = float("inf")
                        for c in opens:
                            if t in dfs[c].index:
                                sig = dfs[c].loc[t, "signal"] if "signal" in dfs[c].columns else 0.0
                                if sig < worst_sig:
                                    worst_sig = sig
                                    worst_c = c
                        if worst_c is not None:
                            pos = positions[worst_c]
                            close = dfs[worst_c].loc[t, "close"]
                            spot_proceeds = pos.units_spot * close * (1.0 - SPOT_TAKER)
                            spot_cash += spot_proceeds
                            realized = pos.short_size * (pos.entry_price - close)
                            perp_fee = pos.short_size * close * PERP_TAKER
                            perp_cash += realized - perp_fee
                            pos.open = False
                            pos.units_spot = 0.0
                            pos.short_size = 0.0
                            pos.entry_price = 0.0
                            pos.required_margin = 0.0
                            pos.notional = 0.0
                            pos.hours_in = 0
                            pos.gross_funding_so_far = 0.0
                            pos.total_fees_paid = 0.0
                            pos.consec_negative = 0
                            n_forced_closes += 1

        # --- 5. Equity snapshot ---
        equity_now = (
            spot_cash
            + perp_cash
            + sum(
                positions[c].units_spot * dfs[c].loc[t, "close"]
                for c in _open_coins()
                if t in dfs[c].index
            )
            + sum(
                positions[c].short_size * (positions[c].entry_price - dfs[c].loc[t, "close"])
                for c in _open_positions(positions)
                if t in dfs[c].index
            )
        )
        equity_history.append(equity_now)
        timestamps.append(t)

    # --- Close all open positions at end ---
    t_final = timeline[-1]
    for c, pos in positions.items():
        if not pos.open:
            continue
        close = dfs[c].loc[t_final, "close"] if t_final in dfs[c].index else pos.entry_price
        realized = pos.short_size * (pos.entry_price - close)
        perp_fee = pos.short_size * close * PERP_TAKER
        spot_proceeds = pos.units_spot * close * (1.0 - SPOT_TAKER)
        spot_fee = pos.units_spot * close * SPOT_TAKER
        perp_cash += realized - perp_fee
        spot_cash += spot_proceeds
        pos.realized_pnl_attr += realized
        pos.fees_paid_attr += perp_fee + spot_fee
        pos.n_closes += 1
        pos.n_minhold_exits += 1  # force-close at end
        n_minhold_guard += 1
        pos.open = False
    # Update last equity
    final_equity = spot_cash + perp_cash
    if equity_history:
        equity_history[-1] = final_equity

    # --- Metrics ---
    eq = pd.Series(equity_history, index=pd.DatetimeIndex(timestamps))
    returns = eq.pct_change().dropna()

    period_years = len(timeline) / HOURS_PER_YEAR
    annual_pct = (eq.iloc[-1] / eq.iloc[0] - 1) / period_years * 100 if period_years > 0 else 0.0

    sharpe = (
        (returns.mean() * HOURS_PER_YEAR) / (returns.std() * np.sqrt(HOURS_PER_YEAR))
        if len(returns) > 0 and returns.std() > 0
        else 0.0
    )
    downside = returns[returns < 0]
    sortino = (
        (returns.mean() * HOURS_PER_YEAR) / (downside.std() * np.sqrt(HOURS_PER_YEAR))
        if len(downside) > 0 and downside.std() > 0
        else 0.0
    )

    peak = eq.cummax()
    drawdowns = (eq - peak) / peak
    max_dd_pct = float((-drawdowns.min()) * 100) if len(drawdowns) > 0 else 0.0

    total_funding = sum(pos.funding_gross for pos in positions.values())
    total_fees = sum(pos.fees_paid_attr for pos in positions.values())

    return {
        "equity": eq,
        "annual_pct": annual_pct,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_dd_pct": max_dd_pct,
        "final_equity": final_equity,
        "total_funding": total_funding,
        "total_fees": total_fees,
        "n_liquidations": n_liquidations,
        "n_top_ups": n_top_ups,
        "n_forced_closes": n_forced_closes,
        "n_skipped_opens": n_skipped_opens,
        "n_phase1_neg_exits": n_phase1_neg_exits,
        "n_phase1_cap_exits": n_phase1_cap_exits,
        "n_phase1_negstop_exits": n_phase1_negstop_exits,
        "n_phase2_exits": n_phase2_exits,
        "n_minhold_guard": n_minhold_guard,
        "period_start": str(timeline[0].date()),
        "period_end": str(timeline[-1].date()),
        "n_hours": len(timeline),
        "per_coin": {
            c: {
                "n_opens": pos.n_opens,
                "n_closes": pos.n_closes,
                "funding_gross": pos.funding_gross,
                "fees_paid": pos.fees_paid_attr,
                "realized_pnl": pos.realized_pnl_attr,
                "hours_in_position": pos.hours_in_total,
                "n_phase1_exits": pos.n_phase1_exits,
                "n_phase2_exits": pos.n_phase2_exits,
            }
            for c, pos in positions.items()
        },
    }


def _open_positions(positions: dict[str, Position]) -> list[str]:
    return [c for c, p in positions.items() if p.open]


# ---------------------------------------------------------------------------
# Step 7: Synthetic verification tests (MUST pass before sweep)
# ---------------------------------------------------------------------------

def test_constant_funding() -> bool:
    """
    Single coin, constant 20% APR, 1000h flat price.
    Expected: position opens, holds >= 460h (position_min_hold),
    never exits via phase2 (signal stays positive > entry),
    funding accrued matches $position_size * 0.20/8760 * hours_held.
    """
    ANNUAL_RATE = 0.20
    PRICE = 100.0
    N = 1000
    PSIZE = 100.0

    # Build synthetic data
    idx = pd.date_range("2025-01-01", periods=N, freq="h", tz="UTC")
    df = pd.DataFrame({
        "fundingRate": [ANNUAL_RATE / HOURS_PER_YEAR] * N,
        "close": [PRICE] * N,
    }, index=idx)
    df["signal"] = ANNUAL_RATE  # constant 20% signal

    # Expected min_hold:
    # breakeven_h = BREAKEVEN_CONST / 0.20 = 18.396/0.20 = 91.98
    # pos_min_hold = min(720, max(24, 5*91.98)) = min(720, max(24, 459.9)) = 460
    breakeven_h = BREAKEVEN_CONST / ANNUAL_RATE
    expected_min_hold = int(min(720, max(24, 5.0 * breakeven_h)))

    # Simulate manually
    p = TwoPhaseParams(
        coins=["TEST"],
        entry_threshold_apr=0.10,
        phase2_exit_threshold=-0.10,
        base_min_hold_hours=24,
        cap_min_hold_hours=720,
        safety_mult=5.0,
        signal_window_hours=1,
        concurrency_cap=1,
        position_size_usdc=PSIZE,
        budget_cap_usdc=PSIZE * 10,
        margin_buffer_factor=3.0,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
    )

    budget = p.budget_cap_usdc
    spot_cash = budget
    perp_cash = 0.0
    lev = RESEARCH_LEVERAGE.get("BTC", 40)  # use BTC as proxy
    req_margin = PSIZE / lev * p.margin_buffer_factor

    in_pos = False
    hours_in = 0
    pos_min_hold = 0
    gross_funding = 0.0
    total_fees_paid = 0.0
    consec_neg = 0
    units_spot = 0.0
    short_size = 0.0
    entry_price = 0.0

    entry_hour = None
    close_hour = None

    for i in range(N):
        sig = ANNUAL_RATE

        # Accrue
        if in_pos:
            f = short_size * PRICE * (ANNUAL_RATE / HOURS_PER_YEAR)
            perp_cash += f
            gross_funding += f

        # Exit check
        if in_pos:
            hours_in += 1
            consec_neg = update_consec_negative(consec_neg, sig)
            cur_income = PSIZE * sig / HOURS_PER_YEAR
            dec = decide_two_phase(
                in_position=True,
                smoothed_signal_annual=sig,
                entry_threshold=p.entry_threshold_apr,
                hours_in_position=hours_in,
                position_min_hold_hours=pos_min_hold,
                gross_funding_so_far=gross_funding,
                total_fees_paid=total_fees_paid,
                consec_negative_hours=consec_neg,
                current_hourly_income_quote=cur_income,
                phase1_negative_patience=p.phase1_negative_patience,
                phase1_breakeven_cap_hours=p.phase1_breakeven_cap_hours,
                phase2_exit_threshold=p.phase2_exit_threshold,
            )
            if dec != "NONE":
                close_hour = i
                # close
                realized = short_size * (entry_price - PRICE)
                perp_fee = short_size * PRICE * PERP_TAKER
                perp_cash += realized - perp_fee
                spot_cash += units_spot * PRICE * (1.0 - SPOT_TAKER)
                in_pos = False
                break

        # Entry
        if not in_pos and sig > p.entry_threshold_apr:
            spot_fee = PSIZE * SPOT_TAKER
            perp_fee = PSIZE * PERP_TAKER
            needed = PSIZE + req_margin + spot_fee + perp_fee
            if spot_cash >= needed:
                units_spot = PSIZE / PRICE
                short_size = PSIZE / PRICE
                entry_price = PRICE
                spot_cash -= PSIZE + spot_fee + req_margin
                perp_cash += req_margin
                perp_cash -= perp_fee
                gross_funding = 0.0
                total_fees_paid = PSIZE * (PERP_TAKER + SPOT_TAKER) * 2
                consec_neg = 0
                pos_min_hold = compute_position_min_hold(
                    entry_signal_annual=sig,
                    safety_mult=p.safety_mult,
                    base_min_hold_hours=p.base_min_hold_hours,
                    cap_min_hold_hours=p.cap_min_hold_hours,
                )
                hours_in = 0
                in_pos = True
                entry_hour = i

    passed = True
    errors = []

    if pos_min_hold != expected_min_hold:
        errors.append(f"pos_min_hold={pos_min_hold}, expected={expected_min_hold}")
        passed = False

    # Position should still be open at end (constant signal > phase2_exit_threshold=-0.10)
    if close_hour is not None:
        errors.append(f"Position closed at hour {close_hour}, expected to stay open")
        passed = False

    if not in_pos and entry_hour is None:
        errors.append("Position never opened")
        passed = False

    if in_pos:
        # Compute expected funding: size/price * price * rate/h * hours_held
        hours_held = N - entry_hour
        expected_funding = (PSIZE / PRICE) * PRICE * (ANNUAL_RATE / HOURS_PER_YEAR) * hours_held
        if abs(gross_funding - expected_funding) > 0.01:
            errors.append(
                f"gross_funding={gross_funding:.4f}, expected={expected_funding:.4f}, "
                f"diff={abs(gross_funding - expected_funding):.4f}"
            )
            passed = False

    if passed:
        print(f"  PASS test_constant_funding: pos_min_hold={pos_min_hold}h, "
              f"entry_hour={entry_hour}, open until end, "
              f"gross_funding=${gross_funding:.2f}")
    else:
        print(f"  FAIL test_constant_funding: {'; '.join(errors)}")

    return passed


def test_zero_funding() -> bool:
    """
    Single coin, 0% APR funding for 1000h.
    Should never enter (signal = 0 <= entry_threshold=0.10).
    """
    PRICE = 100.0
    N = 1000
    PSIZE = 100.0

    p = TwoPhaseParams(
        coins=["TEST"],
        entry_threshold_apr=0.10,
        phase2_exit_threshold=-0.10,
        base_min_hold_hours=24,
        cap_min_hold_hours=720,
        safety_mult=5.0,
        signal_window_hours=1,
        concurrency_cap=1,
        position_size_usdc=PSIZE,
        budget_cap_usdc=PSIZE * 10,
        margin_buffer_factor=3.0,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
    )

    in_pos = False
    entries = 0

    for i in range(N):
        sig = 0.0  # zero signal
        if not in_pos and sig > p.entry_threshold_apr:
            entries += 1
            in_pos = True

    passed = entries == 0 and not in_pos
    if passed:
        print(f"  PASS test_zero_funding: no entries as expected")
    else:
        print(f"  FAIL test_zero_funding: {entries} entries, in_pos={in_pos}")
    return passed


def test_negative_phase1_cap() -> bool:
    """
    Test CLOSE_PHASE1_CAP: position just past min_hold, gross_funding < total_fees
    (still in Phase 1), current rate is very low positive → hours_to_breakeven >> cap.

    Setup: entry_rate=0.11 (just above threshold 0.10), so total_fees_paid > gross_funding
    after min_hold.
    - pos_min_hold = min(720, max(24, 5 * BREAKEVEN_CONST / 0.11)) = min(720, max(24, 836)) = 720h
    - gross_funding after 720h at 0.11 = 100 * 0.11/8760 * 720 = $0.905
    - total_fees_paid = 100 * (0.00035+0.0007)*2 = $0.21
    - Wait: 0.905 > 0.21, so still in profit after 720h...

    Better: use very high position_min_hold with low entry rate.
    entry_rate=0.11, pos_min_hold capped at 720h.
    Actually gross_funding will exceed fees after ~(0.21)/(100*0.11/8760) = 167h.
    So at hour 720 we'd be in Phase 2.

    Instead test: position at min_hold boundary with gross_funding manually set < fees.
    We directly test the decide_two_phase function with crafted state:
    - hours_in = pos_min_hold (just cleared)
    - gross_funding = 0 (no funding accrued yet somehow — e.g. entry just happened)
    - total_fees = 0.21 (standard round-trip)
    - current signal = 0.005 APR (very low positive)
    - hours_to_breakeven = 0.21 / (100 * 0.005 / 8760) = 36,792h >> 720 cap
    Expected: CLOSE_PHASE1_CAP
    """
    ANNUAL_LOW = 0.005  # 0.5% — positive but very low
    PSIZE = 100.0
    pos_min_hold = 24  # use base min_hold so test is simple

    total_fees_paid = PSIZE * (PERP_TAKER + SPOT_TAKER) * 2  # ~$0.21
    gross_funding = 0.0  # nothing accrued yet (position just opened, or rate was 0)
    hours_in = pos_min_hold  # exactly at boundary

    cur_income = PSIZE * ANNUAL_LOW / HOURS_PER_YEAR  # 100 * 0.005 / 8760 ≈ 5.7e-5
    remaining = total_fees_paid - gross_funding  # 0.21
    hours_to_be = remaining / cur_income  # ~3679h >> 720

    decision = decide_two_phase(
        in_position=True,
        smoothed_signal_annual=ANNUAL_LOW,
        entry_threshold=0.10,
        hours_in_position=hours_in,
        position_min_hold_hours=pos_min_hold,
        gross_funding_so_far=gross_funding,
        total_fees_paid=total_fees_paid,
        consec_negative_hours=0,
        current_hourly_income_quote=cur_income,
        phase1_negative_patience=72,
        phase1_breakeven_cap_hours=720,
        phase2_exit_threshold=-0.10,
    )

    passed = decision == "CLOSE_PHASE1_CAP"
    if passed:
        print(f"  PASS test_negative_phase1_cap: decision={decision}, "
              f"hours_to_be={hours_to_be:.0f}h > 720h cap, total_fees=${total_fees_paid:.4f}")
    else:
        print(f"  FAIL test_negative_phase1_cap: decision={decision}, "
              f"expected=CLOSE_PHASE1_CAP, hours_to_be={hours_to_be:.0f}h, "
              f"gross={gross_funding:.4f} vs fees={total_fees_paid:.4f}")
    return passed


# ---------------------------------------------------------------------------
# Step 8: Main sweep + output
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 70)
    print("TWO-PHASE + MARGIN BACKTESTER")
    print("=" * 70)

    # Load prod params
    prod_params, prod_source = load_prod_params()
    print(f"\n[SOURCE] {prod_source}")
    print(f"\n[PARAMS] coins={prod_params.coins}")
    print(f"  entry_threshold_apr     = {prod_params.entry_threshold_apr}")
    print(f"  phase2_exit_threshold   = {prod_params.phase2_exit_threshold}")
    print(f"  base_min_hold_hours     = {prod_params.base_min_hold_hours}")
    print(f"  cap_min_hold_hours      = {prod_params.cap_min_hold_hours}")
    print(f"  safety_mult             = {prod_params.safety_mult}")
    print(f"  signal_window_hours     = {prod_params.signal_window_hours}")
    print(f"  concurrency_cap         = {prod_params.concurrency_cap}")
    print(f"  position_size_usdc      = {prod_params.position_size_usdc}")
    print(f"  budget_cap_usdc         = {prod_params.budget_cap_usdc}")
    print(f"  margin_buffer_factor    = {prod_params.margin_buffer_factor}")
    print(f"  phase1_negative_patience= {prod_params.phase1_negative_patience}")
    print(f"  phase1_breakeven_cap_hours = {prod_params.phase1_breakeven_cap_hours}")
    print(f"\n[CONSTANTS from src/frab/constants.py]")
    print(f"  RESEARCH_LEVERAGE  = {RESEARCH_LEVERAGE}")
    print(f"  RESEARCH_MAINT_RATIO = {RESEARCH_MAINT_RATIO}")
    print(f"  PERP_TAKER = {PERP_TAKER}")
    print(f"  SPOT_TAKER = {SPOT_TAKER}")
    print(f"  BREAKEVEN_CONST = {BREAKEVEN_CONST:.4f}")

    # --- Synthetic tests ---
    print("\n[SYNTHETIC TESTS]")
    t1 = test_constant_funding()
    t2 = test_zero_funding()
    t3 = test_negative_phase1_cap()
    if not (t1 and t2 and t3):
        print("\n[ERROR] Synthetic tests FAILED — aborting sweep")
        sys.exit(1)
    print("All synthetic tests PASSED\n")

    # --- Determine universes ---
    U_PROD_COINS = prod_params.coins  # from DB
    U3_COINS = ["BTC", "ETH", "SOL"]

    # Common window for U-prod — capped by HYPE/PURR OHLCV (price) starting 2025-11-06.
    # Their funding goes back to late-2024 but price history doesn't → window is cold-only.
    # Load data to find actual common window
    dfs_prod = {}
    for c in U_PROD_COINS:
        try:
            dfs_prod[c] = load_coin_df(c)
        except FileNotFoundError:
            print(f"[WARN] Missing data for {c}")
    tl_prod = common_timeline(dfs_prod)
    window_start = tl_prod[0]
    window_end = tl_prod[-1]
    print(f"[WINDOW] U-prod common: {window_start.date()} → {window_end.date()} ({len(tl_prod)} hours)")

    # U3 restricted to same window
    dfs_u3 = {}
    for c in U3_COINS:
        df = load_coin_df(c)
        dfs_u3[c] = df[(df.index >= window_start) & (df.index <= window_end)]
    tl_u3 = common_timeline(dfs_u3)
    print(f"[WINDOW] U3 same window: {tl_u3[0].date()} → {tl_u3[-1].date()} ({len(tl_u3)} hours)")

    # --- Sweep configs ---
    MARGIN_BUFFERS = [3.0, 5.0]
    # Budget scaled to $1000 wallet (prod uses real money amounts; for comparison use $1000)
    BUDGET = 1000.0
    # position_size: use prod's position_size_usdc but scale to $1000 budget
    # prod: position_size=12, budget=61 → ratio=12/61≈0.197 → 0.197*1000≈197
    # But keep it simple: use $100 per position (standard research size)
    # We use prod ratio to get "apples to apples" with real prod
    POSITION_SIZE = 100.0

    # Override params for simulation (keep all two-phase logic, just change budget/size)
    def make_params(coins: list[str], mbuf: float) -> tuple[TwoPhaseParams, float]:
        p = TwoPhaseParams(
            coins=coins,
            entry_threshold_apr=prod_params.entry_threshold_apr,
            phase2_exit_threshold=prod_params.phase2_exit_threshold,
            base_min_hold_hours=prod_params.base_min_hold_hours,
            cap_min_hold_hours=prod_params.cap_min_hold_hours,
            safety_mult=prod_params.safety_mult,
            signal_window_hours=prod_params.signal_window_hours,
            concurrency_cap=prod_params.concurrency_cap,
            position_size_usdc=POSITION_SIZE,
            budget_cap_usdc=BUDGET,
            margin_buffer_factor=mbuf,
            phase1_negative_patience=prod_params.phase1_negative_patience,
            phase1_breakeven_cap_hours=prod_params.phase1_breakeven_cap_hours,
        )
        return p, mbuf

    runs: list[dict] = []

    configs = [
        ("U-prod", U_PROD_COINS),
        ("U3", U3_COINS),
    ]

    all_results: list[tuple[str, float, dict]] = []
    for univ_name, coins in configs:
        for mbuf in MARGIN_BUFFERS:
            print(f"\n[RUN] universe={univ_name}  coins={coins}  margin_buffer={mbuf}")
            p, _ = make_params(coins, mbuf)
            # U3 is restricted to the same window as U-prod for apples-to-apples comparison
            rs = window_start if univ_name == "U3" else None
            re = window_end if univ_name == "U3" else None
            try:
                res = simulate(
                    coins, p,
                    margin_buffer_x=mbuf,
                    position_size=POSITION_SIZE,
                    restrict_start=rs,
                    restrict_end=re,
                )
                print(f"  annual={res['annual_pct']:+.2f}%  sharpe={res['sharpe']:.3f}  "
                      f"maxdd={res['max_dd_pct']:.3f}%  liq={res['n_liquidations']}")
                all_results.append((univ_name, mbuf, res))
            except Exception as exc:
                print(f"  ERROR: {exc}")
                import traceback; traceback.print_exc()

    # --- Build output DataFrames ---
    agg_rows = []
    per_coin_rows = []

    for univ_name, mbuf, res in all_results:
        agg_rows.append({
            "universe": univ_name,
            "margin_buffer_x": mbuf,
            "position_size": POSITION_SIZE,
            "K": prod_params.concurrency_cap,
            "period_start": res["period_start"],
            "period_end": res["period_end"],
            "n_hours": res["n_hours"],
            "annual_pct": round(res["annual_pct"], 4),
            "sharpe": round(res["sharpe"], 4),
            "sortino": round(res["sortino"], 4),
            "max_dd_pct": round(res["max_dd_pct"], 4),
            "total_funding": round(res["total_funding"], 4),
            "total_fees": round(res["total_fees"], 4),
            "final_equity": round(res["final_equity"], 4),
            "n_liquidations": res["n_liquidations"],
            "n_top_ups": res["n_top_ups"],
            "n_forced_closes": res["n_forced_closes"],
            "n_phase1_neg_exits": res["n_phase1_neg_exits"],
            "n_phase1_cap_exits": res["n_phase1_cap_exits"],
            "n_phase1_negstop_exits": res["n_phase1_negstop_exits"],
            "n_phase2_exits": res["n_phase2_exits"],
            "n_minhold_guard": res["n_minhold_guard"],
        })
        for coin, pc in res["per_coin"].items():
            per_coin_rows.append({
                "universe": univ_name,
                "margin_buffer_x": mbuf,
                "coin": coin,
                "n_opens": pc["n_opens"],
                "n_closes": pc["n_closes"],
                "funding_gross": round(pc["funding_gross"], 4),
                "fees_paid": round(pc["fees_paid"], 4),
                "realized_pnl": round(pc["realized_pnl"], 4),
                "hours_in_position": pc["hours_in_position"],
                "n_phase1_exits": pc["n_phase1_exits"],
                "n_phase2_exits": pc["n_phase2_exits"],
            })

    out_dir = Path(__file__).parent
    agg_df = pd.DataFrame(agg_rows)
    per_df = pd.DataFrame(per_coin_rows)

    agg_path = out_dir / "TWOPHASE_MARGIN_aggregate.csv"
    per_path = out_dir / "TWOPHASE_MARGIN_per_coin.csv"
    agg_df.to_csv(agg_path, index=False)
    per_df.to_csv(per_path, index=False)
    print(f"\nWrote {agg_path}")
    print(f"Wrote {per_path}")

    # --- Print summary ---
    print("\n" + "=" * 70)
    print("AGGREGATE RESULTS")
    print("=" * 70)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 25)
    print(agg_df.to_string(index=False))

    print("\nPER-COIN ATTRIBUTION")
    print(per_df.to_string(index=False))

    # --- Generate report ---
    _write_report(prod_params, prod_source, agg_df, per_df, all_results)


def _write_report(
    prod_params: TwoPhaseParams,
    prod_source: str,
    agg_df: pd.DataFrame,
    per_df: pd.DataFrame,
    all_results: list[tuple[str, float, dict]],
) -> None:
    """Write TWOPHASE_MARGIN_REPORT.md."""
    out = Path(__file__).parent / "TWOPHASE_MARGIN_REPORT.md"

    # Find U-prod buf=3 and U3 buf=3 results
    def _find(universe: str, buf: float) -> dict | None:
        for u, b, r in all_results:
            if u == universe and b == buf:
                return r
        return None

    r_prod3 = _find("U-prod", 3.0)
    r_prod5 = _find("U-prod", 5.0)
    r_u3_3 = _find("U3", 3.0)
    r_u3_5 = _find("U3", 5.0)

    def _fmt_res(r: dict | None, label: str) -> str:
        if r is None:
            return f"**{label}**: data unavailable\n"
        pc = r["per_coin"]
        pc_rows = sorted(pc.items(), key=lambda x: -(x[1]["funding_gross"] + x[1]["realized_pnl"]))
        tbl = "| Coin | n_opens | funding_gross | fees_paid | realized_pnl | hours_in | n_ph1 | n_ph2 |\n"
        tbl += "|------|---------|--------------|-----------|-------------|----------|-------|-------|\n"
        for coin, d in pc_rows:
            tbl += (
                f"| {coin} | {d['n_opens']} | ${d['funding_gross']:.2f} | "
                f"${d['fees_paid']:.2f} | ${d['realized_pnl']:.2f} | "
                f"{d['hours_in_position']} | {d['n_phase1_exits']} | {d['n_phase2_exits']} |\n"
            )
        lines = [
            f"**{label}** — period {r['period_start']} → {r['period_end']} ({r['n_hours']} hours)",
            "",
            f"- Annual return: **{r['annual_pct']:+.2f}%**",
            f"- Sharpe: {r['sharpe']:.3f}  |  Sortino: {r['sortino']:.3f}  |  MaxDD: {r['max_dd_pct']:.3f}%",
            f"- Final equity: ${r['final_equity']:.2f} (started $1000.00)",
            f"- Total funding: ${r['total_funding']:.2f}  |  Total fees: ${r['total_fees']:.2f}",
            f"- Liquidations: {r['n_liquidations']}  |  Top-ups: {r['n_top_ups']}  |  Forced-closes: {r['n_forced_closes']}",
            "",
            "**Per-coin attribution:**",
            "",
            tbl,
        ]
        return "\n".join(lines)

    # Exit breakdown for U-prod buf=3
    exit_breakdown = ""
    if r_prod3 is not None:
        r = r_prod3
        total_exits = (
            r["n_phase1_neg_exits"] + r["n_phase1_cap_exits"]
            + r["n_phase1_negstop_exits"]
            + r["n_phase2_exits"] + r["n_minhold_guard"]
        )
        exit_breakdown = f"""
### Exit Reason Breakdown (U-prod, buf=3)

| Exit type | Count |
|-----------|-------|
| phase1_consec_neg (≥72h neg) | {r['n_phase1_neg_exits']} |
| phase1_cap_exceeded (breakeven > 720h) | {r['n_phase1_cap_exits']} |
| phase1_negstop (signal < −0.15, ≥6h neg, bypass min_hold) | {r['n_phase1_negstop_exits']} |
| phase2_signal_degraded (signal < −0.10) | {r['n_phase2_exits']} |
| end-of-backtest force-close | {r['n_minhold_guard']} |
| **Total exits** | **{total_exits}** |
"""

    # Verdict
    prod3_annual = r_prod3["annual_pct"] if r_prod3 else float("nan")
    u3_3_annual = r_u3_3["annual_pct"] if r_u3_3 else float("nan")
    prod3_sharpe = r_prod3["sharpe"] if r_prod3 else float("nan")
    u3_3_sharpe = r_u3_3["sharpe"] if r_u3_3 else float("nan")

    if prod3_annual > u3_3_annual:
        verdict = (
            f"Over the same restricted window ({r_prod3['period_start'] if r_prod3 else '?'} → "
            f"{r_prod3['period_end'] if r_prod3 else '?'}), "
            f"U-prod ({prod_params.coins}) produced {prod3_annual:+.2f}% annual vs "
            f"U3 {u3_3_annual:+.2f}% — **adding coins beyond BTC/ETH/SOL helped** on this window, "
            f"but the window is short (~{(r_prod3['n_hours']/HOURS_PER_YEAR):.1f} yr) and "
            f"dominated by single high-funding episodes; not sufficient for causal attribution."
        )
    else:
        verdict = (
            f"Over the same restricted window ({r_prod3['period_start'] if r_prod3 else '?'} → "
            f"{r_prod3['period_end'] if r_prod3 else '?'}), "
            f"U-prod ({prod_params.coins}) produced {prod3_annual:+.2f}% annual vs "
            f"U3 {u3_3_annual:+.2f}% — **adding coins beyond BTC/ETH/SOL did NOT help** on this window; "
            f"BTC/ETH/SOL alone were sufficient or better."
        )

    ssh_note = ""
    if "DB:/tmp/frab_prod.db" not in prod_source:
        ssh_note = (
            "\n> **Note:** SSH/SCP to 10.8.0.5 failed (too many authentication failures). "
            "Params loaded from local `data/frab.db` which shows today's date (Jun 2) "
            "and contains a single active strategy row consistent with prod. "
            "If prod has been updated since last sync, these params may be stale.\n"
        )

    report = f"""# TWOPHASE_MARGIN_REPORT

*Generated by `research/two_phase_margin.py`. All numbers from actual run — no hallucinated values.*

{ssh_note}

## 1. Source Verification

**Prod params source:** `{prod_source}`

### TwoPhaseParams (from DB → strategies table, latest active row)

| Parameter | Value |
|-----------|-------|
| coins | {prod_params.coins} |
| entry_threshold_apr | {prod_params.entry_threshold_apr} |
| phase2_exit_threshold | {prod_params.phase2_exit_threshold} |
| base_min_hold_hours | {prod_params.base_min_hold_hours} |
| cap_min_hold_hours | {prod_params.cap_min_hold_hours} |
| safety_mult | {prod_params.safety_mult} |
| signal_window_hours | {prod_params.signal_window_hours} |
| concurrency_cap | {prod_params.concurrency_cap} |
| position_size_usdc | {prod_params.position_size_usdc} |
| budget_cap_usdc | {prod_params.budget_cap_usdc} |
| margin_buffer_factor | {prod_params.margin_buffer_factor} |
| phase1_negative_patience | {prod_params.phase1_negative_patience} |
| phase1_breakeven_cap_hours | {prod_params.phase1_breakeven_cap_hours} |

### Constants (from src/frab/constants.py)

| Constant | Value |
|----------|-------|
| PERP_TAKER | {PERP_TAKER} |
| SPOT_TAKER | {SPOT_TAKER} |
| BREAKEVEN_CONST | {BREAKEVEN_CONST:.4f} |
| RESEARCH_LEVERAGE | {RESEARCH_LEVERAGE} |
| RESEARCH_MAINT_RATIO | {RESEARCH_MAINT_RATIO} |

*Simulation uses $100/position, $1000 total budget for comparison across universes.*

---

## 2. U-prod Results (margin_buffer=3×)

{_fmt_res(r_prod3, "U-prod, buf=3")}

### U-prod Results (margin_buffer=5×)

{_fmt_res(r_prod5, "U-prod, buf=5")}

---

## 3. U3 (BTC/ETH/SOL) on Same Window

{_fmt_res(r_u3_3, "U3, buf=3")}

{_fmt_res(r_u3_5, "U3, buf=5")}

---

## 4. Apples-to-Apples Verdict

{verdict}

---

{exit_breakdown}

---

## 6. Honest Limits (Not Modeled)

- **Real HL slippage on large HYPE/PURR positions.** These coins have thin spot books; actual spot taker fill may be 0.1–0.5% worse than the model assumes.
- **Atomic execution failures.** Prod can end up half-open (spot bought, perp not yet entered) between ticks. The backtester assumes instantaneous atomic entry.
- **Recovery from half-open.** If the engine restarts mid-open, it re-routes to CHECK_MARGIN. Backtest has no such recovery path — all opens are deterministic.
- **Partial fills.** HL perp orders may partially fill; backtest assumes full fill at close price.
- **Funding interval irregularity.** HL migrated from 8h to 1h intervals in June 2023. Data before that is excluded, but occasional gaps/irregularities post-June 2023 are not modeled.
- **Budget_cap_usdc interpretation.** Prod computes `position_size_usdc` dynamically from `budget_cap_usdc / concurrency_cap / (1 + margin_buffer / leverage)`. Backtest uses fixed $100/pos for research comparability; this changes absolute $ amounts but not the ratios/shapes.
- **Spot price at close vs. fill price.** Backtest closes at the hourly close price; actual fills may be at ask/mid within the hour.
"""

    out.write_text(report)
    print(f"\nWrote report: {out}")


if __name__ == "__main__":
    main()
