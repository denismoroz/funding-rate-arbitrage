"""$50k cross-venue funding-harvest + LST yield model.

Two architectures for the spot leg, and a blend of them:

  DECOUPLED (safe):   spot held off-venue as LST (cold/DeFi), short perp + its
                      margin buffer on the best-funding venue. Buffer is ADDITIVE
                      capital → dilutes APR on occupied, but only the buffer is at
                      venue counterparty risk. This IS segregated margin → buffer
                      3× is the floor (rally-liquidation test).

  UNIFIED (efficient): spot held ON the venue as collateral for the short (LST must
                      be accepted as collateral to keep staking yield). occupied ≈
                      notional, no additive buffer → higher APR, but full spot is at
                      venue risk and few venues accept LST collateral.

  BLEND x: fraction x of capital run unified, (1-x) decoupled. x=0.5 = the user's
           50/50 idea.

APR is on OCCUPIED capital:
  decoupled: occupied = N + N*buffer/leverage
  unified:   occupied = N * (1 + eps)         (spot doubles as collateral)
  income    = (funding + staking - fee_drag) * N    [staking=0 if no LST]

Research only. Funding = cold-regime best-venue means (2025-01→2026-04) from
backpack/aster/HL comparison CSVs. Staking yields are APPROXIMATE market rates
(late-2025/early-2026) — flagged, need verification before any real allocation.
"""

# ── inputs ────────────────────────────────────────────────────────────────────

# best-venue cold-regime funding (annualized %), source: research/{aster,backpack}/
#   regime_comparison.csv + HL columns therein
FUNDING = {  # coin: (annualized_funding_pct, venue)
    "BTC":  (9.23,  "HL"),
    "ETH":  (8.25,  "Backpack"),
    "SOL":  (6.13,  "Aster"),
    "HYPE": (19.40, "HL"),
    "AVAX": (10.49, "Aster"),
    "LINK": (19.94, "Backpack"),
    "DOGE": (10.89, "Backpack"),
}

# APPROXIMATE staking yields via LST (%). 0 = no practical liquid staking.
# TODO verify with real historical rates (jitoSOL/sAVAX/wstETH/HYPE-LST).
STAKING = {
    "SOL":  7.5,   # jitoSOL / mSOL
    "AVAX": 5.0,   # sAVAX (BENQI)
    "ETH":  3.0,   # wstETH
    "HYPE": 2.5,   # stHYPE / kHYPE (young)
    "BTC":  0.0,   # no native staking
    "LINK": 0.0,   # staking capped/illiquid → impractical
    "DOGE": 0.0,   # PoW, none
}

COINS = list(FUNDING.keys())

# ── parameters ────────────────────────────────────────────────────────────────
BUFFER = 3.0        # segregated-margin floor (rally-liquidation test)
LEVERAGE = 10.0     # representative cross-venue short leverage (prod uses up to 20×)
FEE_DRAG = 1.5      # annual %, round-trip swaps+taker amortized over long holds
EPS_UNIFIED = 0.10  # small cash cushion on top of collateralizing spot


def coin_apr(coin, use_lst, arch, leverage=LEVERAGE):
    f = FUNDING[coin][0]
    s = STAKING[coin] if use_lst else 0.0
    income = f + s - FEE_DRAG  # per unit notional
    if arch == "decoupled":
        occ = 1.0 + BUFFER / leverage
    else:  # unified
        occ = 1.0 + EPS_UNIFIED
    return income / occ


def portfolio_apr(use_lst, arch, leverage=LEVERAGE):
    return sum(coin_apr(c, use_lst, arch, leverage) for c in COINS) / len(COINS)


def blend(use_lst, x_unified, leverage=LEVERAGE):
    return (x_unified * portfolio_apr(use_lst, "unified", leverage)
            + (1 - x_unified) * portfolio_apr(use_lst, "decoupled", leverage))


def main():
    print(f"params: buffer {BUFFER}× | leverage {LEVERAGE}× | fee drag {FEE_DRAG}% "
          f"| equal-weight 7 coins\n")

    for use_lst in (False, True):
        tag = "WITH LST staking" if use_lst else "plain spot (no staking)"
        print(f"════ {tag} ════")
        print(f"{'coin':>5}{'venue':>10}{'funding%':>10}{'staking%':>10}"
              f"{'decoupled':>11}{'unified':>10}")
        for c in COINS:
            f, v = FUNDING[c]
            s = STAKING[c] if use_lst else 0.0
            print(f"{c:>5}{v:>10}{f:>10.2f}{s:>10.1f}"
                  f"{coin_apr(c,use_lst,'decoupled'):>10.2f}%{coin_apr(c,use_lst,'unified'):>9.2f}%")
        dec = portfolio_apr(use_lst, "decoupled")
        uni = portfolio_apr(use_lst, "unified")
        print(f"{'PORTF':>5}{'':>10}{'':>10}{'':>10}{dec:>10.2f}%{uni:>9.2f}%")
        print(f"   100% decoupled (safe): {dec:.2f}%   "
              f"100% unified (efficient): {uni:.2f}%   "
              f"50/50 blend: {blend(use_lst,0.5):.2f}%\n")

    print("════ 50/50 blend across unified-fraction (WITH LST) ════")
    print(f"{'x_unified':>10}{'APR':>8}{'spot@venue-risk':>18}")
    for x in (0.0, 0.25, 0.5, 0.75, 1.0):
        print(f"{x*100:>9.0f}%{blend(True,x):>7.2f}%{x*100:>16.0f}%")
    print("\nx_unified = fraction of capital in unified (spot on-venue as collateral).")
    print("'spot@venue-risk' = share of spot exposed to a single venue blowing up.")
    print("50/50 = half the spot safe in cold/LST, half on-venue; APR midpoint.\n")

    print("sensitivity (WITH LST, 50/50):")
    for lev in (10.0, 20.0):
        print(f"  leverage {lev:.0f}× → 50/50 {blend(True,0.5,lev):.2f}% | "
              f"100% decoupled {portfolio_apr(True,'decoupled',lev):.2f}%")


if __name__ == "__main__":
    main()
