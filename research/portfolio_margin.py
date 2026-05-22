"""Portfolio-level margin-aware backtest for Strategy A across N coins with cross-margin perp wallet."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from engine import load_data, smooth_funding, DEFAULT_MAINT_RATIO, HOURS_PER_YEAR

# Per-coin config: leverage caps reflect HL physical limits
PER_COIN_LEVERAGE = {
    "BTC": 20, "ETH": 20,
    "SOL": 10, "AVAX": 10, "LINK": 10,
    "AAVE": 5, "DOGE": 5,
}
PER_COIN_MAINT_RATIO = DEFAULT_MAINT_RATIO
COINS = list(PER_COIN_LEVERAGE.keys())

# Sizing + margin policy
POSITION_SIZE = 100.0
MARGIN_BUFFER_X = 3.0
TOP_UP_TRIGGER = 2.0
HEALTHY_RATIO = 3.0
CONCURRENCY_CAP = 3
BUDGET_CAP_USD = 1000.0

# Signal params (Strategy A, finalized)
ENTRY_THRESHOLD = 0.30
EXIT_THRESHOLD = -0.15
MIN_HOLD_HOURS = 120
SIGNAL_WINDOW_HOURS = 12

# Costs
PERP_TAKER = 0.00035
SPOT_TAKER = 0.00070

def load_coin_dfs(coins: list[str]) -> dict[str, pd.DataFrame]:
    """Load each coin's funding+price as a DataFrame indexed by hour."""
    out = {}
    for c in coins:
        out[c] = load_data(c, with_ohlcv=True)[['close', 'fundingRate']].copy()
    return out


def common_timeline(dfs: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    """Intersection of all coins' timestamps (master hourly timeline)."""
    idx = None
    for c, df in dfs.items():
        idx = df.index if idx is None else idx.intersection(df.index)
    return idx.sort_values()


def add_signals(dfs: dict[str, pd.DataFrame], window: int = SIGNAL_WINDOW_HOURS) -> None:
    """Add annualized funding-rate signal as a column 'signal' to each df."""
    for c, df in dfs.items():
        ma = smooth_funding(df['fundingRate'].values, window)
        df['signal'] = ma * HOURS_PER_YEAR


def init_state(coins: list[str], budget_cap_usd: float) -> dict:
    """Initialise simulation state dict."""
    positions = {
        c: {
            'open': False,
            'units_spot': 0.0,
            'short_size': 0.0,
            'entry_price': 0.0,
            'hours_in': 0,
            'required_margin': 0.0,
        }
        for c in coins
    }
    return {
        'spot_cash': budget_cap_usd,
        'perp_cash': 0.0,
        'positions': positions,
        'n_liquidations': 0,
        'n_top_ups': 0,
        'n_forced_closes': 0,
        'n_skipped_opens_capital': 0,
        'min_margin_ratio': float('inf'),
        'equity_history': [],
        'timestamp_history': [],
    }


def accrue_funding(state: dict, dfs: dict, t) -> dict:
    """Accrue hourly funding to perp_cash for each open short position."""
    for c, pos in state['positions'].items():
        if not pos['open']:
            continue
        close = dfs[c].loc[t, 'close']
        rate = dfs[c].loc[t, 'fundingRate']
        # Short receives funding when rate > 0
        state['perp_cash'] += pos['short_size'] * close * rate
    return state


def _open_positions(state: dict) -> list[str]:
    """Return list of coins currently in position."""
    return [c for c, pos in state['positions'].items() if pos['open']]


def apply_margin_policy(state: dict, dfs: dict, t) -> None:
    """Check margin health; liquidate or top-up as needed."""
    opens = _open_positions(state)
    if not opens:
        return

    # Compute total maintenance margin required and perp equity
    total_maintenance = 0.0
    unrealized_pnl = 0.0
    for c in opens:
        pos = state['positions'][c]
        close = dfs[c].loc[t, 'close']
        total_maintenance += pos['short_size'] * close * PER_COIN_MAINT_RATIO[c]
        unrealized_pnl += pos['short_size'] * (pos['entry_price'] - close)

    perp_equity = state['perp_cash'] + unrealized_pnl

    if total_maintenance <= 0:
        return

    margin_ratio = perp_equity / total_maintenance

    # Track worst margin ratio seen
    if margin_ratio < state['min_margin_ratio']:
        state['min_margin_ratio'] = margin_ratio

    if margin_ratio <= 1.0:
        # Liquidation cascade: lose all perp_cash, close all positions (no recovery)
        state['perp_cash'] = 0.0
        for c in opens:
            state['positions'][c] = {
                'open': False,
                'units_spot': 0.0,
                'short_size': 0.0,
                'entry_price': 0.0,
                'hours_in': 0,
                'required_margin': 0.0,
            }
        state['n_liquidations'] += 1

    elif margin_ratio < TOP_UP_TRIGGER:
        # Try to top-up perp wallet from spot_cash to reach HEALTHY_RATIO
        target_perp_equity = total_maintenance * HEALTHY_RATIO
        top_up_needed = target_perp_equity - perp_equity

        if top_up_needed > 0 and state['spot_cash'] >= top_up_needed:
            state['spot_cash'] -= top_up_needed
            state['perp_cash'] += top_up_needed
            state['n_top_ups'] += 1
        else:
            # Cannot top-up: forced-close weakest position (lowest signal)
            worst_coin = None
            worst_signal = float('inf')
            for c in opens:
                sig = dfs[c].loc[t, 'signal'] if 'signal' in dfs[c].columns else 0.0
                if sig < worst_signal:
                    worst_signal = sig
                    worst_coin = c

            if worst_coin is not None:
                pos = state['positions'][worst_coin]
                close = dfs[worst_coin].loc[t, 'close']
                # Recover spot leg proceeds
                spot_proceeds = pos['units_spot'] * close * (1.0 - SPOT_TAKER)
                state['spot_cash'] += spot_proceeds
                # Release required margin back to perp_cash
                state['perp_cash'] += pos['required_margin']
                # Close perp short with realized PnL (pay taker fee)
                realized = pos['short_size'] * (pos['entry_price'] - close)
                perp_fee = pos['short_size'] * close * PERP_TAKER
                state['perp_cash'] += realized - perp_fee
                # Reset position
                state['positions'][worst_coin] = {
                    'open': False,
                    'units_spot': 0.0,
                    'short_size': 0.0,
                    'entry_price': 0.0,
                    'hours_in': 0,
                    'required_margin': 0.0,
                }
                state['n_forced_closes'] += 1


def process_exits(state: dict, dfs: dict, t) -> None:
    """Close positions that have held long enough and signal dropped below threshold."""
    for c, pos in state['positions'].items():
        if not pos['open']:
            continue
        pos['hours_in'] += 1
        if pos['hours_in'] < MIN_HOLD_HOURS:
            continue
        sig = dfs[c].loc[t, 'signal'] if 'signal' in dfs[c].columns else 0.0
        if sig >= EXIT_THRESHOLD:
            continue

        close = dfs[c].loc[t, 'close']

        # Close perp short: realized PnL + release margin, pay taker fee
        realized = pos['short_size'] * (pos['entry_price'] - close)
        perp_fee = pos['short_size'] * close * PERP_TAKER
        state['perp_cash'] += realized - perp_fee + pos['required_margin']

        # Sell spot leg: receive proceeds minus taker fee
        spot_proceeds = pos['units_spot'] * close * (1.0 - SPOT_TAKER)
        state['spot_cash'] += spot_proceeds

        # Reset position
        state['positions'][c] = {
            'open': False,
            'units_spot': 0.0,
            'short_size': 0.0,
            'entry_price': 0.0,
            'hours_in': 0,
            'required_margin': 0.0,
        }


def process_entries(state: dict, dfs: dict, t) -> None:
    """Open new positions for coins with strong signals if budget allows."""
    opens = _open_positions(state)
    n_open = len(opens)

    if n_open >= CONCURRENCY_CAP:
        return

    # Coins eligible for entry
    candidates = []
    for c, pos in state['positions'].items():
        if pos['open']:
            continue
        if 'signal' not in dfs[c].columns:
            continue
        sig = dfs[c].loc[t, 'signal']
        if sig > ENTRY_THRESHOLD:
            candidates.append((sig, c))

    # Sort by signal descending (strongest first)
    candidates.sort(reverse=True)

    for sig, c in candidates:
        if n_open >= CONCURRENCY_CAP:
            break

        close = dfs[c].loc[t, 'close']
        req_margin = POSITION_SIZE / PER_COIN_LEVERAGE[c] * MARGIN_BUFFER_X
        spot_fee = POSITION_SIZE * SPOT_TAKER
        perp_fee = POSITION_SIZE * PERP_TAKER
        total_needed = POSITION_SIZE + req_margin + spot_fee + perp_fee

        # Compute currently committed capital (spot legs + margin already locked)
        committed = sum(
            p['units_spot'] * dfs[cc].loc[t, 'close'] + p['required_margin']
            for cc, p in state['positions'].items()
            if p['open']
        )
        committed_after_open = committed + POSITION_SIZE + req_margin

        if state['spot_cash'] < total_needed:
            state['n_skipped_opens_capital'] += 1
            continue

        if committed_after_open > BUDGET_CAP_USD * 1.05:
            state['n_skipped_opens_capital'] += 1
            continue

        # Open: buy spot leg
        units_spot = POSITION_SIZE / close
        state['spot_cash'] -= POSITION_SIZE + spot_fee

        # Move margin from spot_cash into perp wallet
        state['spot_cash'] -= req_margin
        state['perp_cash'] += req_margin

        # Pay perp entry fee from perp_cash
        state['perp_cash'] -= perp_fee

        state['positions'][c] = {
            'open': True,
            'units_spot': units_spot,
            'short_size': units_spot,   # short same notional qty
            'entry_price': close,
            'hours_in': 0,
            'required_margin': req_margin,
        }
        n_open += 1

        # Track peak committed capital
        if committed_after_open > state.get('peak_committed_capital', 0.0):
            state['peak_committed_capital'] = committed_after_open


def compute_equity(state: dict, dfs: dict, t) -> float:
    """Mark-to-market portfolio equity at timestamp t."""
    spot_value = sum(
        pos['units_spot'] * dfs[c].loc[t, 'close']
        for c, pos in state['positions'].items()
        if pos['open']
    )
    unrealized_pnl = sum(
        pos['short_size'] * (pos['entry_price'] - dfs[c].loc[t, 'close'])
        for c, pos in state['positions'].items()
        if pos['open']
    )
    return state['spot_cash'] + state['perp_cash'] + spot_value + unrealized_pnl


def simulate_portfolio(
    coins: list[str] = COINS,
    budget_cap_usd: float = BUDGET_CAP_USD,
    position_size: float = POSITION_SIZE,
    margin_buffer_x: float = MARGIN_BUFFER_X,
    top_up_trigger: float = TOP_UP_TRIGGER,
    healthy_ratio: float = HEALTHY_RATIO,
    concurrency_cap: int = CONCURRENCY_CAP,
    entry_threshold: float = ENTRY_THRESHOLD,
    exit_threshold: float = EXIT_THRESHOLD,
    min_hold_hours: int = MIN_HOLD_HOURS,
    signal_window_hours: int = SIGNAL_WINDOW_HOURS,
    perp_taker: float = PERP_TAKER,
    spot_taker: float = SPOT_TAKER,
) -> dict:
    """Run portfolio-level funding-rate arbitrage backtest.

    Returns a dict of performance metrics and simulation statistics.
    """
    # Override module-level constants if caller passes custom values
    # (use local vars for clarity; helpers reference module globals but
    # simulate_portfolio passes defaults through, so callers get expected behaviour)
    global POSITION_SIZE, MARGIN_BUFFER_X, TOP_UP_TRIGGER, HEALTHY_RATIO
    global CONCURRENCY_CAP, ENTRY_THRESHOLD, EXIT_THRESHOLD, MIN_HOLD_HOURS
    global SIGNAL_WINDOW_HOURS, PERP_TAKER, SPOT_TAKER
    _orig = (POSITION_SIZE, MARGIN_BUFFER_X, TOP_UP_TRIGGER, HEALTHY_RATIO,
             CONCURRENCY_CAP, ENTRY_THRESHOLD, EXIT_THRESHOLD, MIN_HOLD_HOURS,
             SIGNAL_WINDOW_HOURS, PERP_TAKER, SPOT_TAKER)
    POSITION_SIZE = position_size
    MARGIN_BUFFER_X = margin_buffer_x
    TOP_UP_TRIGGER = top_up_trigger
    HEALTHY_RATIO = healthy_ratio
    CONCURRENCY_CAP = concurrency_cap
    ENTRY_THRESHOLD = entry_threshold
    EXIT_THRESHOLD = exit_threshold
    MIN_HOLD_HOURS = min_hold_hours
    SIGNAL_WINDOW_HOURS = signal_window_hours
    PERP_TAKER = perp_taker
    SPOT_TAKER = spot_taker

    try:
        # Load data and build timeline
        dfs = load_coin_dfs(coins)
        timeline = common_timeline(dfs)
        add_signals(dfs, window=signal_window_hours)

        state = init_state(coins, budget_cap_usd)
        state['peak_committed_capital'] = 0.0
        initial_equity = budget_cap_usd

        total_funding = 0.0  # track funding received separately
        total_fees = 0.0

        equity_curve = []
        timestamps = []

        for t in timeline:
            # Increment hours_in is handled inside process_exits; accrue first
            funding_before = state['perp_cash']
            accrue_funding(state, dfs, t)
            funding_gained = state['perp_cash'] - funding_before
            if funding_gained > 0:
                total_funding += funding_gained

            apply_margin_policy(state, dfs, t)
            process_exits(state, dfs, t)
            process_entries(state, dfs, t)

            eq = compute_equity(state, dfs, t)
            equity_curve.append(eq)
            timestamps.append(t)

        # Final close: close all remaining positions at last bar
        last_t = timeline[-1]
        for c, pos in state['positions'].items():
            if not pos['open']:
                continue
            close = dfs[c].loc[last_t, 'close']
            realized = pos['short_size'] * (pos['entry_price'] - close)
            perp_fee = pos['short_size'] * close * PERP_TAKER
            spot_proceeds = pos['units_spot'] * close * (1.0 - SPOT_TAKER)
            state['perp_cash'] += realized - perp_fee + pos['required_margin']
            state['spot_cash'] += spot_proceeds
            total_fees += perp_fee + pos['units_spot'] * close * SPOT_TAKER
            state['positions'][c] = {
                'open': False,
                'units_spot': 0.0,
                'short_size': 0.0,
                'entry_price': 0.0,
                'hours_in': 0,
                'required_margin': 0.0,
            }

        final_equity = state['spot_cash'] + state['perp_cash']

        # --- Compute performance metrics ---
        eq_arr = np.array(equity_curve, dtype=float)
        n_hours = len(eq_arr)

        if n_hours < 2:
            raise ValueError("Too few data points to compute metrics")

        hourly_returns = np.diff(eq_arr) / eq_arr[:-1]
        mean_hr = hourly_returns.mean()
        std_hr = hourly_returns.std()

        annual_pct = mean_hr * HOURS_PER_YEAR * 100.0
        vol_pct = std_hr * np.sqrt(HOURS_PER_YEAR) * 100.0
        sharpe = (mean_hr / std_hr * np.sqrt(HOURS_PER_YEAR)) if std_hr > 0 else 0.0

        downside = hourly_returns[hourly_returns < 0]
        if len(downside) > 1 and downside.std() > 0:
            sortino = mean_hr / downside.std() * np.sqrt(HOURS_PER_YEAR)
        else:
            sortino = 0.0

        # Max drawdown
        peak = eq_arr[0]
        max_dd = 0.0
        for eq in eq_arr:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
        max_dd_pct = max_dd * 100.0

        calmar = annual_pct / max_dd_pct if max_dd_pct > 0 else 0.0

        min_mr = state['min_margin_ratio']
        if min_mr == float('inf'):
            min_mr = float('nan')

        return {
            'annual_pct': annual_pct,
            'vol_pct': vol_pct,
            'sharpe': sharpe,
            'sortino': sortino,
            'max_dd_pct': max_dd_pct,
            'calmar': calmar,
            'n_liquidations': state['n_liquidations'],
            'n_top_ups': state['n_top_ups'],
            'n_forced_closes': state['n_forced_closes'],
            'n_skipped_opens_capital': state['n_skipped_opens_capital'],
            'min_margin_ratio': min_mr,
            'peak_committed_capital': state.get('peak_committed_capital', 0.0),
            'final_equity': final_equity,
            'total_funding': total_funding,
            'total_fees': total_fees,
        }

    finally:
        # Restore module globals
        (POSITION_SIZE, MARGIN_BUFFER_X, TOP_UP_TRIGGER, HEALTHY_RATIO,
         CONCURRENCY_CAP, ENTRY_THRESHOLD, EXIT_THRESHOLD, MIN_HOLD_HOURS,
         SIGNAL_WINDOW_HOURS, PERP_TAKER, SPOT_TAKER) = _orig


if __name__ == "__main__":
    print("Running portfolio margin backtest...")
    results = simulate_portfolio()

    # Write CSV
    csv_path = Path(__file__).parent / "portfolio_margin_results.csv"
    columns = [
        'annual_pct', 'vol_pct', 'sharpe', 'sortino', 'max_dd_pct', 'calmar',
        'n_liquidations', 'n_top_ups', 'n_forced_closes', 'n_skipped_opens_capital',
        'min_margin_ratio', 'peak_committed_capital', 'final_equity',
        'total_funding', 'total_fees',
    ]
    row = {col: results[col] for col in columns}
    pd.DataFrame([row], columns=columns).to_csv(csv_path, index=False)
    print(f"Results written to {csv_path}")

    # Human-readable summary
    print("\n--- Portfolio Margin Backtest Summary ---")
    print(f"  Annual return      : {results['annual_pct']:+.2f}%")
    print(f"  Volatility (annual): {results['vol_pct']:.2f}%")
    print(f"  Sharpe ratio       : {results['sharpe']:.3f}")
    print(f"  Sortino ratio      : {results['sortino']:.3f}")
    print(f"  Max drawdown       : {results['max_dd_pct']:.2f}%")
    print(f"  Calmar ratio       : {results['calmar']:.3f}")
    print(f"  Liquidations       : {results['n_liquidations']}")
    print(f"  Top-ups            : {results['n_top_ups']}")
    print(f"  Forced closes      : {results['n_forced_closes']}")
    print(f"  Skipped (capital)  : {results['n_skipped_opens_capital']}")
    print(f"  Min margin ratio   : {results['min_margin_ratio']:.3f}")
    print(f"  Peak committed $   : {results['peak_committed_capital']:.2f}")
    print(f"  Final equity $     : {results['final_equity']:.2f}")
    print(f"  Total funding $    : {results['total_funding']:.2f}")
    print(f"  Total fees $       : {results['total_fees']:.2f}")
