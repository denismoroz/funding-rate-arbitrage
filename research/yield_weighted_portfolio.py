"""Yield-weighted portfolio construction analysis.

Compares four allocation schemes on corrected (interval-aware) inputs:
  1. Equal-weight     — 1/7 per coin, baseline
  2. Yield-weighted   — weight ∝ per-coin occupied-APR (decoupled, with LST)
  3. Top-3 concentrated — HYPE / AVAX / SOL only, equal-weight among them
  4. Capped-tilt      — overweight leaders but cap any single coin ≤ 25%
                        and any single venue ≤ 50%

Inputs match portfolio_50k_model.py (corrected 2026-06-05):
  funding from interval-aware CROSS_VENUE_BACKTEST_REPORT.md
  staking from research/staking/staking_inputs.csv (conservative)
  Backpack is NEVER the best venue after the interval-aware fix.
"""

import math

# ── corrected inputs (same as portfolio_50k_model.py) ─────────────────────────
FUNDING = {
    "BTC":  (9.23,  "HL"),
    "ETH":  (8.06,  "Aster"),
    "SOL":  (6.14,  "Aster"),
    "HYPE": (19.40, "HL"),
    "AVAX": (10.49, "Aster"),
    "LINK": (11.21, "HL"),
    "DOGE": (7.94,  "Aster"),
}

STAKING = {
    "BTC":  0.0,
    "ETH":  2.5,
    "SOL":  6.5,
    "HYPE": 2.2,
    "AVAX": 4.5,
    "LINK": 0.0,
    "DOGE": 0.0,
}

COINS = list(FUNDING.keys())

# ── model parameters ───────────────────────────────────────────────────────────
BUFFER     = 3.0    # 3× buffer for decoupled (segregated margin)
LEVERAGE   = 10.0   # representative short leverage
FEE_DRAG   = 1.5    # annual % fee drag
EPS_UNIFIED = 0.10  # small cash cushion for unified


def coin_income(coin):
    """Gross income % per unit notional (funding + staking - fee_drag)."""
    f = FUNDING[coin][0]
    s = STAKING[coin]
    return f + s - FEE_DRAG


def coin_apr(coin, arch, leverage=LEVERAGE):
    """Per-coin occupied-APR for an architecture."""
    if arch == "decoupled":
        occ = 1.0 + BUFFER / leverage
    else:  # unified
        occ = 1.0 + EPS_UNIFIED
    return coin_income(coin) / occ


def portfolio_metrics(weights, leverage=LEVERAGE):
    """
    Given a dict {coin: weight} (weights must sum to 1), return:
      - decoupled APR (with LST)
      - unified APR (with LST)
      - 50/50 blend APR
      - max_coin_weight
      - max_venue_weight  (HL vs Aster vs Backpack)
      - effective_N (inverse Herfindahl 1/Σwᵢ²)
    """
    dec_apr = sum(weights[c] * coin_apr(c, "decoupled", leverage) for c in weights)
    uni_apr = sum(weights[c] * coin_apr(c, "unified",   leverage) for c in weights)
    blend_apr = 0.5 * dec_apr + 0.5 * uni_apr

    # venue concentration
    venue_w = {}
    for c, w in weights.items():
        v = FUNDING[c][1]
        venue_w[v] = venue_w.get(v, 0.0) + w
    max_venue_w = max(venue_w.values())
    max_venue_name = max(venue_w, key=venue_w.get)

    max_coin_w = max(weights.values())
    max_coin_name = max(weights, key=weights.get)

    herfindahl = sum(w ** 2 for w in weights.values())
    eff_n = 1.0 / herfindahl

    return dict(
        dec_apr=dec_apr,
        uni_apr=uni_apr,
        blend_apr=blend_apr,
        max_coin_w=max_coin_w,
        max_coin_name=max_coin_name,
        max_venue_w=max_venue_w,
        max_venue_name=max_venue_name,
        eff_n=eff_n,
        venue_w=venue_w,
    )


# ── scheme 1: equal-weight ────────────────────────────────────────────────────
def equal_weight():
    n = len(COINS)
    return {c: 1.0 / n for c in COINS}


# ── scheme 2: yield-weighted ──────────────────────────────────────────────────
def yield_weighted():
    """Weight ∝ per-coin decoupled occupied-APR (with LST)."""
    raw = {c: coin_apr(c, "decoupled") for c in COINS}
    total = sum(raw.values())
    return {c: v / total for c, v in raw.items()}


# ── scheme 3: top-3 concentrated (HYPE / AVAX / SOL) ─────────────────────────
def top3_concentrated():
    top3 = ["HYPE", "AVAX", "SOL"]
    return {c: (1.0 / 3.0 if c in top3 else 0.0) for c in COINS}


# ── scheme 4: capped-tilt ─────────────────────────────────────────────────────
def capped_tilt(max_coin=0.25, max_venue=0.50):
    """
    Start from yield-weighted, then iteratively cap coins at max_coin and
    redistribute the excess uniformly to uncapped coins. Then verify venue
    caps; if violated, pull from heaviest venue coin and redistribute.

    Returns final weights.
    """
    weights = yield_weighted()

    # iterative coin-cap enforcement
    for _ in range(100):
        capped = {c: min(w, max_coin) for c, w in weights.items()}
        excess = sum(weights[c] - capped[c] for c in weights if weights[c] > max_coin)
        if excess < 1e-10:
            break
        uncapped = [c for c in weights if capped[c] < max_coin]
        if not uncapped:
            break
        add_each = excess / len(uncapped)
        for c in uncapped:
            capped[c] = min(capped[c] + add_each, max_coin)
        weights = capped

    # venue-cap enforcement: pull weight from heaviest coin of offending venue
    for _ in range(100):
        venue_w = {}
        for c, w in weights.items():
            v = FUNDING[c][1]
            venue_w[v] = venue_w.get(v, 0.0) + w
        offending = {v: w for v, w in venue_w.items() if w > max_venue + 1e-10}
        if not offending:
            break
        for v, vw in offending.items():
            excess = vw - max_venue
            venue_coins = sorted(
                [c for c in weights if FUNDING[c][1] == v],
                key=lambda c: weights[c], reverse=True
            )
            other_coins = [c for c in weights if FUNDING[c][1] != v]
            for c in venue_coins:
                take = min(weights[c], excess)
                weights[c] -= take
                excess -= take
                # redistribute to others uniformly
                per_other = take / len(other_coins)
                for oc in other_coins:
                    weights[oc] = min(weights[oc] + per_other, max_coin)
                if excess < 1e-10:
                    break

    # renormalize (floating-point safety)
    total = sum(weights.values())
    return {c: w / total for c, w in weights.items()}


def print_scheme(name, weights):
    m = portfolio_metrics(weights)
    print(f"\n{'─'*64}")
    print(f"SCHEME: {name}")
    print(f"{'─'*64}")

    # per-coin table
    print(f"  {'coin':>5}  {'venue':>8}  {'weight':>7}  {'income%':>8}  "
          f"{'decoupled':>9}  {'unified':>8}")
    for c in COINS:
        w = weights[c]
        inc = coin_income(c)
        dec = coin_apr(c, "decoupled")
        uni = coin_apr(c, "unified")
        print(f"  {c:>5}  {FUNDING[c][1]:>8}  {w:>6.1%}  {inc:>8.2f}%  "
              f"{dec:>8.2f}%  {uni:>7.2f}%")

    # venue breakdown
    print(f"\n  Venue exposure:  " +
          "  ".join(f"{v} {p:.1%}" for v, p in sorted(m["venue_w"].items())))

    print(f"\n  Portfolio APR (with LST):")
    print(f"    100% decoupled : {m['dec_apr']:>6.2f}%")
    print(f"    50/50 blend    : {m['blend_apr']:>6.2f}%")
    print(f"    100% unified   : {m['uni_apr']:>6.2f}%")

    print(f"\n  Concentration metrics:")
    print(f"    Max single coin  : {m['max_coin_name']:>5} {m['max_coin_w']:>6.1%}")
    print(f"    Max single venue : {m['max_venue_name']:>8} {m['max_venue_w']:>6.1%}")
    print(f"    Effective N      : {m['eff_n']:.2f}  (1/Σwᵢ²; max 7)")
    return m


def main():
    print("=" * 64)
    print("YIELD-WEIGHTED PORTFOLIO ANALYSIS")
    print("Corrected inputs: interval-aware funding + staking_inputs.csv")
    print("Buffer 3× | Leverage 10× | Fee drag 1.5% | 7 coins")
    print("=" * 64)

    print("\nPer-coin income (funding + staking - fee_drag):")
    for c in COINS:
        f, v = FUNDING[c]
        s = STAKING[c]
        inc = coin_income(c)
        print(f"  {c:>5} | {v:>8} | funding {f:.2f}% + staking {s:.1f}% "
              f"- fee 1.5% = income {inc:.2f}%")

    schemes = [
        ("1. Equal-weight (1/7 each)",            equal_weight()),
        ("2. Yield-weighted (∝ decoupled APR)",    yield_weighted()),
        ("3. Top-3 concentrated (HYPE/AVAX/SOL)",  top3_concentrated()),
        ("4. Capped-tilt (≤25% coin, ≤50% venue)", capped_tilt()),
    ]

    results = []
    for name, w in schemes:
        m = print_scheme(name, w)
        results.append((name, m))

    # summary comparison table
    print(f"\n\n{'='*64}")
    print("SUMMARY COMPARISON TABLE (with LST staking)")
    print(f"{'='*64}")
    print(f"  {'Scheme':<38}  {'decoupled':>9}  {'50/50':>7}  {'unified':>8}  "
          f"{'max-coin':>9}  {'max-venue':>10}  {'eff-N':>6}")
    for name, m in results:
        short = name.split("(")[0].strip()
        print(f"  {short:<38}  {m['dec_apr']:>8.2f}%  {m['blend_apr']:>6.2f}%  "
              f"{m['uni_apr']:>7.2f}%  {m['max_coin_w']:>8.1%}  {m['max_venue_w']:>9.1%}  "
              f"{m['eff_n']:>6.2f}")

    # explicit answer to the key question
    eq_blend   = results[0][1]["blend_apr"]
    yw_blend   = results[1][1]["blend_apr"]
    t3_blend   = results[2][1]["blend_apr"]
    cap_blend  = results[3][1]["blend_apr"]
    print(f"\nKey question: how many extra APR points from tilting?")
    print(f"  Yield-weighted vs equal-weight : +{yw_blend-eq_blend:.2f} pp  (50/50 blend)")
    print(f"  Capped-tilt    vs equal-weight : +{cap_blend-eq_blend:.2f} pp")
    print(f"  Top-3 conc.    vs equal-weight : +{t3_blend-eq_blend:.2f} pp")
    print(f"\nDoes any SAFE scheme reach 14% occupied-APR (50/50 blend)?")
    for name, m in results:
        short = name.split("(")[0].strip()
        tag = "YES" if m["blend_apr"] >= 14.0 else "NO "
        print(f"  {tag}  {short}  →  {m['blend_apr']:.2f}%")


if __name__ == "__main__":
    main()
