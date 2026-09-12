"""Close XSMOM perp positions that exist on HL but have no OPENED row in the DB.

Why these exist: before the accept_partial fix (274ab6d), a fill below tolerance
raised PartialFillError *before* the DB write, so the filled part stayed on the
exchange unlinked — invisible to the card and to every rebalance, yet holding
margin. JTO (-48, two partials) and BCH (-0.051) accumulated that way.

Safety rules built in:
  * a coin is only touched when the DB has NO active xsmom row for it, so a
    partially-tracked coin can never be closed by mistake;
  * FRAB coins are never touched — only the XSMOM wallet is used, and FRAB runs
    on a different address entirely;
  * dry-run by default; --apply is required to send any order.

Run ON PROD (the XSMOM wallet key lives in prod .env):
    uv run python scripts/close_xsmom_orphans.py            # show the plan
    uv run python scripts/close_xsmom_orphans.py --apply    # send market closes
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from frab.db.models import XsmomPosition as XsmomPositionRow
from frab.db.session import create_engine, make_session_factory
from frab.domain import XsmomState
from frab.exchanges.hyperliquid.exchange import HLExchange
from frab.settings import get_settings

# States whose coin is legitimately held and must never be closed by this script.
_ACTIVE = (XsmomState.NEW.value, XsmomState.OPENED.value, XsmomState.CLOSE.value)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually send market closes")
    ap.add_argument("--slippage", type=float, default=0.01)
    args = ap.parse_args()

    settings = get_settings()
    if settings.xsmom_hl_private_key is None or settings.xsmom_hl_account_address is None:
        print("XSMOM HL credentials are not configured", file=sys.stderr)
        return 2

    exchange = HLExchange(
        private_key=settings.xsmom_hl_private_key.get_secret_value(),
        account_address=settings.xsmom_hl_account_address,
        network=settings.hl_network,
        api_url=settings.hl_api_url,
    )
    try:
        perp_state, _spot = await exchange.get_account_snapshot()

        sf = make_session_factory(create_engine(settings.db_url))
        async with sf() as s:
            rows = (await s.execute(
                select(XsmomPositionRow).where(XsmomPositionRow.state.in_(_ACTIVE))
            )).scalars().all()
        tracked = {r.coin for r in rows}

        on_exchange = {
            ap_.coin: ap_ for ap_ in perp_state.asset_positions if ap_.szi != 0
        }
        orphans = {c: p for c, p in on_exchange.items() if c not in tracked}

        print(f"account value        ${perp_state.account_value:,.2f}")
        print(f"positions on HL      {len(on_exchange)}")
        print(f"tracked in DB        {len(tracked)}  {sorted(tracked)}")
        print(f"ORPHANS              {len(orphans)}  {sorted(orphans)}\n")
        if not orphans:
            print("nothing to close.")
            return 0

        freed = 0.0
        for coin, p in sorted(orphans.items()):
            freed += p.margin_used
            print(f"  {coin:<6} szi={p.szi:>10}  entry={p.position_value / abs(p.szi):>10.5f}  "
                  f"uPnL={p.unrealized_pnl:+7.2f}  margin={p.margin_used:>7.2f}")
        print(f"\n  margin that will be freed: ${freed:,.2f}")

        if not args.apply:
            print("\nDRY RUN — rerun with --apply to send market closes.")
            return 0

        print("\nsending market closes...")
        for coin in sorted(orphans):
            resp = await exchange._hl_client.market_close(coin, args.slippage)
            print(f"  {coin:<6} -> {resp}")

        # Verify: re-read the exchange and confirm the orphans are gone.
        perp_after, _ = await exchange.get_account_snapshot()
        left = {
            ap_.coin for ap_ in perp_after.asset_positions
            if ap_.szi != 0 and ap_.coin in orphans
        }
        print(f"\nafter: positions on HL "
              f"{sum(1 for a in perp_after.asset_positions if a.szi != 0)}, "
              f"account value ${perp_after.account_value:,.2f}")
        if left:
            print(f"STILL OPEN: {sorted(left)} — check manually", file=sys.stderr)
            return 1
        print("all orphans closed.")
        return 0
    finally:
        await exchange.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
