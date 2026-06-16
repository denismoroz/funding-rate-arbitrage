"""
real_fill_audit.py — Measure REAL realized execution cost per leg from the
live production trading DB, to calibrate the maker/post-only cost model
(research/execution/maker_model.py) whose core assumptions
(taker baseline 8.5bps = 3.5 fee + 5.0 implied slippage) are UNMEASURED.

READ-ONLY w.r.t. production. We analyze a COPY of the prod DB at
/tmp/frab_prod_audit.db (scp'd from dis@10.8.0.5). We do NOT mutate prod and do
NOT touch src/frab.

WHAT WE COMPUTE (honest, with sample-size caveats)
==================================================
  1. Realized fee per leg (bps): fee_bps = fee / (|qty|*price) * 1e4.
     Aggregated median/mean/p25/p75, split spot-vs-perp, by coin, by side.
     Expect perp ~3.5bps, spot ~7bps. CONFIRM/refute the 3.5bps perp taker.
  2. Realized slippage per leg (bps): match each fill to the nearest prices.mark
     for the SAME coin within +-1h. slippage_bps = side_sign*(price-mark)/mark*1e4,
     side_sign = +1 buy (paying ABOVE mark = positive cost), -1 sell. Report the
     match-gap distribution so the reader sees how trustworthy each match is.
  3. Total realized cost per leg = fee_bps + slippage_bps, perp-only median —
     THE headline vs research 8.5bps.
  4. Coverage: n_fills, n perp, n spot, date range, distinct coins, mark matches.

DATA NOTES (verified by inspection of the copied prod DB)
=========================================================
  - fills.slippage_bps is a PLACEHOLDER (only values 100.0 / 200.0) — NOT real.
    We reconstruct slippage from prices.mark, ignoring the stored column.
  - fills.fee is the REAL HL fee (USDC), backfilled from HL userFills.
  - prices.spot is ALWAYS NULL in this DB; only prices.mark is populated. We use
    `mark` as the reference for BOTH spot and perp legs. The HL spot price tracks
    the perp mark closely, but this means spot slippage is measured vs the PERP
    mark — a documented approximation (flagged in output).
  - Spot vs perp is determined by positions.instrument (enum SPOT/PERP), joined
    via fills.position_id. There is NO market-type column in prices; coins are
    NOT prefixed/suffixed. FRAB legs: SPOT leg is LONG, PERP leg is SHORT.
  - is_paper is 0 for all rows (real fills only).

CAVEATS (stated everywhere)
===========================
  - ~11 days live, FRAB-only, 44 fills (22 perp / 22 spot) -> INDICATIVE, NOT
    definitive. Tiny sample.
  - Slippage via nearest-mark is an APPROXIMATION (no order-book snapshot at fill
    time). Match-gap distribution is reported.
  - Does NOT measure passive fill-rate (we've never posted maker orders). That
    still needs a live post-only A/B test.
  - Do NOT extrapolate spot costs to the perp-only research books (XSMOM/trend).

ENV
===
  .venv/bin/python research/execution/real_fill_audit.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

DB_PATH = Path("/tmp/frab_prod_audit.db")
_HERE = Path(__file__).parent
_OUT = _HERE / "real_fill_audit.json"

MARK_MATCH_WINDOW_MS = 3_600_000  # +-1h around fill ts to find nearest mark

# Research / maker_model assumptions we are calibrating
RESEARCH_COSTS_BPS = 8.5
ASSUMED_TAKER_FEE = 3.5
ASSUMED_IMPLIED_SLIPPAGE = 5.0


def _pctiles(arr):
    """median/mean/p25/p75/min/max/n for a list of floats (NaN-safe)."""
    a = np.asarray([x for x in arr if x is not None and not np.isnan(x)], dtype=float)
    if a.size == 0:
        return {"n": 0, "median": None, "mean": None, "p25": None,
                "p75": None, "min": None, "max": None}
    return {
        "n": int(a.size),
        "median": round(float(np.median(a)), 4),
        "mean": round(float(np.mean(a)), 4),
        "p25": round(float(np.percentile(a, 25)), 4),
        "p75": round(float(np.percentile(a, 75)), 4),
        "min": round(float(np.min(a)), 4),
        "max": round(float(np.max(a)), 4),
    }


def load_fills(con):
    """Join fills -> positions to label each fill with coin/instrument/side."""
    rows = con.execute(
        """
        SELECT f.id, f.position_id, f.ts_ms, f.side, f.qty, f.price, f.fee,
               p.coin, p.instrument, p.side AS pos_side, f.is_paper
        FROM fills f
        JOIN positions p ON f.position_id = p.id
        ORDER BY f.ts_ms
        """
    ).fetchall()
    return rows


def nearest_mark(con, coin, ts_ms):
    """Nearest prices.mark for `coin` within +-window. Returns (mark, gap_ms)
    or (None, None) if nothing in range."""
    row = con.execute(
        """
        SELECT mark, ts_ms FROM prices
        WHERE coin = ? AND ts_ms BETWEEN ? AND ?
        ORDER BY ABS(ts_ms - ?) ASC
        LIMIT 1
        """,
        (coin, ts_ms - MARK_MATCH_WINDOW_MS, ts_ms + MARK_MATCH_WINDOW_MS, ts_ms),
    ).fetchone()
    if row is None or row[0] is None:
        return None, None
    return float(row[0]), int(abs(row[1] - ts_ms))


def main():
    if not DB_PATH.exists():
        print(f"ERROR: prod DB copy not found at {DB_PATH}.\n"
              f"  scp dis@10.8.0.5:/Users/dis/prj/funding-rate-arbitrage/data/frab.db "
              f"{DB_PATH}\n"
              f"This script is READ-ONLY and analyzes the COPY only.", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(str(DB_PATH))
    fills = load_fills(con)

    per_fill = []
    gaps = []
    for (fid, pos_id, ts_ms, fside, qty, price, fee, coin, instrument,
         pos_side, is_paper) in fills:
        notional = abs(qty) * price
        fee_bps = fee / notional * 1e4 if notional > 0 else None

        side = (fside or "").lower()
        # buy/long pays ABOVE mark = positive cost; sell/short fills BELOW = positive
        if side in ("long", "buy", "b"):
            side_sign = 1.0
        elif side in ("short", "sell", "s", "a"):
            side_sign = -1.0
        else:
            side_sign = 1.0  # fallback; FRAB sides are long/short

        mark, gap = nearest_mark(con, coin, ts_ms)
        if mark is not None and mark > 0:
            slip_bps = side_sign * (price - mark) / mark * 1e4
            gaps.append(gap)
        else:
            slip_bps = None

        total_bps = (fee_bps + slip_bps) if (fee_bps is not None
                                             and slip_bps is not None) else None

        per_fill.append({
            "fill_id": fid, "coin": coin, "instrument": instrument,
            "fill_side": side, "ts_ms": ts_ms, "qty": qty, "price": price,
            "fee": fee, "notional_usdc": round(notional, 4),
            "fee_bps": round(fee_bps, 4) if fee_bps is not None else None,
            "mark": mark, "mark_gap_ms": gap,
            "slippage_bps": round(slip_bps, 4) if slip_bps is not None else None,
            "total_bps": round(total_bps, 4) if total_bps is not None else None,
        })

    # ── Splits ────────────────────────────────────────────────────────────────
    def subset(pred):
        return [r for r in per_fill if pred(r)]

    perp = subset(lambda r: r["instrument"] == "PERP")
    spot = subset(lambda r: r["instrument"] == "SPOT")

    def agg(rows, key):
        return _pctiles([r[key] for r in rows])

    by_instrument = {}
    for label, rows in (("perp", perp), ("spot", spot)):
        by_instrument[label] = {
            "n_fills": len(rows),
            "fee_bps": agg(rows, "fee_bps"),
            "slippage_bps": agg(rows, "slippage_bps"),
            "total_bps": agg(rows, "total_bps"),
        }

    coins = sorted({r["coin"] for r in per_fill})
    by_coin = {}
    for c in coins:
        rows = subset(lambda r, c=c: r["coin"] == c)
        inst = sorted({r["instrument"] for r in rows})
        by_coin[c] = {
            "instruments": inst,
            "is_spot_present": "SPOT" in inst,
            "n_fills": len(rows),
            "perp": {
                "n": len([r for r in rows if r["instrument"] == "PERP"]),
                "fee_bps": agg([r for r in rows if r["instrument"] == "PERP"], "fee_bps"),
                "slippage_bps": agg([r for r in rows if r["instrument"] == "PERP"], "slippage_bps"),
                "total_bps": agg([r for r in rows if r["instrument"] == "PERP"], "total_bps"),
            },
            "spot": {
                "n": len([r for r in rows if r["instrument"] == "SPOT"]),
                "fee_bps": agg([r for r in rows if r["instrument"] == "SPOT"], "fee_bps"),
                "slippage_bps": agg([r for r in rows if r["instrument"] == "SPOT"], "slippage_bps"),
                "total_bps": agg([r for r in rows if r["instrument"] == "SPOT"], "total_bps"),
            },
        }

    # By side (FRAB: spot=LONG/buy, perp=SHORT/sell; "open vs close" not directly
    # recorded per fill, so we report by fill side as the determinable proxy).
    by_side = {}
    for s in sorted({r["fill_side"] for r in per_fill}):
        rows = subset(lambda r, s=s: r["fill_side"] == s)
        by_side[s] = {
            "n_fills": len(rows),
            "fee_bps": agg(rows, "fee_bps"),
            "slippage_bps": agg(rows, "slippage_bps"),
            "total_bps": agg(rows, "total_bps"),
        }

    # ── Coverage ──────────────────────────────────────────────────────────────
    ts_all = [r["ts_ms"] for r in per_fill]
    n_matched = len([r for r in per_fill if r["slippage_bps"] is not None])
    gap_dist = _pctiles([float(g) for g in gaps])
    coverage = {
        "n_fills_total": len(per_fill),
        "n_perp": len(perp),
        "n_spot": len(spot),
        "n_paper": len([r for r in fills if r[-1]]),  # is_paper True
        "n_distinct_coins": len(coins),
        "coins": coins,
        "date_range_utc": {
            "start": _ms_to_iso(min(ts_all)) if ts_all else None,
            "end": _ms_to_iso(max(ts_all)) if ts_all else None,
            "span_days": round((max(ts_all) - min(ts_all)) / 86_400_000, 2) if ts_all else None,
        },
        "n_fills_with_mark_match": n_matched,
        "mark_match_window_ms": MARK_MATCH_WINDOW_MS,
        "mark_match_gap_ms_distribution": gap_dist,
        "strategy": "FRAB two_phase (funding-arb) only; XSMOM not live",
    }

    # ── Calibration verdict ───────────────────────────────────────────────────
    perp_fee = by_instrument["perp"]["fee_bps"]["median"]
    perp_slip = by_instrument["perp"]["slippage_bps"]["median"]
    perp_total = by_instrument["perp"]["total_bps"]["median"]
    spot_fee = by_instrument["spot"]["fee_bps"]["median"]
    spot_slip = by_instrument["spot"]["slippage_bps"]["median"]
    spot_total = by_instrument["spot"]["total_bps"]["median"]

    fee_verdict = (
        f"Realized perp fee median = {perp_fee} bps vs assumed taker fee "
        f"{ASSUMED_TAKER_FEE} bps (note: micro ~$12 notionals; some fills land "
        f"at exactly 3.5 bps, others ~4.32 from HL fee rounding at tiny size — "
        f"base perp taker IS 3.5)."
    )
    slip_verdict = (
        f"Realized perp slippage median = {perp_slip} bps vs assumed implied "
        f"slippage {ASSUMED_IMPLIED_SLIPPAGE} bps (nearest-mark approximation, "
        f"median match gap {gap_dist['median']} ms)."
    )
    total_verdict = (
        f"Realized perp TOTAL cost/leg median = {perp_total} bps vs research "
        f"baseline {RESEARCH_COSTS_BPS} bps."
    )

    if perp_total is None:
        headline = "INSUFFICIENT DATA"
    elif perp_total > RESEARCH_COSTS_BPS * 1.15:
        headline = "8.5bps is TOO LOW (real cost higher)"
    elif perp_total < RESEARCH_COSTS_BPS * 0.85:
        headline = "8.5bps is TOO HIGH (real cost lower) — research is conservative"
    else:
        headline = "8.5bps is ABOUT RIGHT"

    verdict = (
        f"CALIBRATION ({coverage['date_range_utc']['span_days']}d, FRAB-only, "
        f"{coverage['n_fills_total']} fills / {coverage['n_perp']} perp — "
        f"INDICATIVE, NOT definitive). {fee_verdict} {slip_verdict} "
        f"{total_verdict} VERDICT: {headline}. "
        f"maker_model defaults: taker_fee={ASSUMED_TAKER_FEE} "
        f"({'CONFIRMED' if perp_fee is not None and abs(perp_fee - ASSUMED_TAKER_FEE) < 1.0 else 'OFF — see realized'}); "
        f"implied_slippage={ASSUMED_IMPLIED_SLIPPAGE} "
        f"(realized perp slippage {perp_slip} bps). "
        f"Maker-switch implication: spread_capture is the maker prize and it is "
        f"bounded by REAL slippage; if realized slippage << 5bps the maker upside "
        f"is SMALLER than maker_model assumes; if >= 5bps the prize is bigger. "
        f"Spot context (do NOT apply to perp-only research books): fee "
        f"{spot_fee} bps, slippage {spot_slip} bps, total {spot_total} bps. "
        f"CAVEATS: nearest-mark is an approximation vs the PERP mark "
        f"(prices.spot is NULL); does NOT measure passive fill-rate (we have "
        f"never posted maker orders — still needs a live post-only A/B)."
    )

    out = {
        "test": "real_fill_audit",
        "description": "Realized execution cost per leg from the live prod DB, "
                       "to calibrate maker_model 8.5bps (=3.5 fee + 5.0 slippage). "
                       "READ-ONLY copy of prod DB. INDICATIVE — ~11d, FRAB-only.",
        "source_db": str(DB_PATH),
        "data_notes": {
            "slippage_bps_column": "PLACEHOLDER in DB (only 100/200); reconstructed "
                                   "from prices.mark instead.",
            "prices_spot_null": "prices.spot is always NULL; mark used for BOTH "
                                "spot and perp legs (spot slip measured vs perp mark).",
            "spot_vs_perp": "positions.instrument (SPOT/PERP) via fills.position_id. "
                            "No market-type column in prices; coins not prefixed.",
            "frab_legs": "SPOT leg = LONG/buy, PERP leg = SHORT/sell (funding carry).",
            "is_paper": "0 for all fills (real fills only).",
        },
        "assumptions_under_test": {
            "research_costs_bps_per_leg": RESEARCH_COSTS_BPS,
            "maker_model_taker_fee_bps": ASSUMED_TAKER_FEE,
            "maker_model_implied_slippage_bps": ASSUMED_IMPLIED_SLIPPAGE,
        },
        "coverage": coverage,
        "by_instrument": by_instrument,
        "by_coin": by_coin,
        "by_fill_side": by_side,
        "headline_perp": {
            "fee_bps_median": perp_fee,
            "slippage_bps_median": perp_slip,
            "total_bps_median": perp_total,
            "vs_research_8.5": headline,
        },
        "headline_spot_context": {
            "fee_bps_median": spot_fee,
            "slippage_bps_median": spot_slip,
            "total_bps_median": spot_total,
            "note": "Do NOT extrapolate to perp-only research books. The spot "
                    "slippage_bps is CONTAMINATED by the spot-vs-perp BASIS: spot "
                    "LONG legs fill systematically BELOW the perp mark (negative "
                    "'slippage') because prices.mark is the PERP mark and spot "
                    "trades below perp under positive funding. So spot total "
                    f"({spot_total} bps) UNDERSTATES true spot exec cost; the spot "
                    "FEE median (~6.7-7.0 bps) is the reliable spot number.",
        },
        "caveats": [
            "~11 days live, FRAB-only, 44 fills (22 perp/22 spot) -> INDICATIVE, "
            "NOT definitive. Tiny sample.",
            "Slippage via nearest prices.mark is an APPROXIMATION (no order-book "
            "snapshot at fill time). Match-gap distribution reported (median ~4s).",
            "prices.spot is NULL; spot-leg slippage is measured vs the PERP mark, "
            "so spot 'slippage' is dominated by the spot-vs-perp BASIS, not exec "
            "quality. Perp slippage is clean (perp fills vs perp mark).",
            "Perp fee shows ~3.5 (true base taker) and ~4.32 (HL rounding on micro "
            "~$12 notionals) — at larger live size the realized perp fee -> 3.5.",
            "Does NOT measure passive fill-rate (never posted maker orders). A "
            "live post-only A/B test is still required to price maker fill-rate.",
            "Do NOT extrapolate spot costs to the perp-only research books "
            "(XSMOM/trend).",
        ],
        "verdict": verdict,
        "per_fill": per_fill,
    }
    _OUT.write_text(json.dumps(out, indent=2))

    # ── Print summary ─────────────────────────────────────────────────────────
    print("=" * 78)
    print("REAL FILL AUDIT — realized execution cost per leg (live prod DB)")
    print("=" * 78)
    print(f"\nCOVERAGE: {coverage['n_fills_total']} fills "
          f"({coverage['n_perp']} perp / {coverage['n_spot']} spot), "
          f"{coverage['n_distinct_coins']} coins, "
          f"{coverage['date_range_utc']['span_days']}d "
          f"({coverage['date_range_utc']['start']} -> {coverage['date_range_utc']['end']})")
    print(f"  mark matches: {n_matched}/{len(per_fill)}  "
          f"gap ms median={gap_dist['median']} p75={gap_dist['p75']} max={gap_dist['max']}")
    print(f"  SMALL SAMPLE, FRAB-only, XSMOM not live -> INDICATIVE not definitive.")

    print(f"\nPERP (apples-to-apples vs research 8.5):")
    print(f"  fee_bps      median={perp_fee}  mean={by_instrument['perp']['fee_bps']['mean']}"
          f"  p25={by_instrument['perp']['fee_bps']['p25']} p75={by_instrument['perp']['fee_bps']['p75']}")
    print(f"  slippage_bps median={perp_slip}  mean={by_instrument['perp']['slippage_bps']['mean']}"
          f"  p25={by_instrument['perp']['slippage_bps']['p25']} p75={by_instrument['perp']['slippage_bps']['p75']}")
    print(f"  TOTAL/leg    median={perp_total}  mean={by_instrument['perp']['total_bps']['mean']}")
    print(f"  >>> vs research 8.5: {headline}")

    print(f"\nSPOT (context — do NOT apply to perp-only research books):")
    print(f"  fee_bps median={spot_fee}  slippage_bps median={spot_slip}  TOTAL median={spot_total}")

    print(f"\nVERDICT:\n  {verdict}")
    print(f"\n[written] {_OUT}")
    con.close()


def _ms_to_iso(ms):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    main()
