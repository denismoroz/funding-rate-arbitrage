"""Reconstruct TRUE per-position live PnL for XSMOM, independent of the
(buggy, all-zero) equity_snapshots decomposition columns."""
import sqlite3, datetime as dt

DB = "/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/619a0272-724d-477f-be5a-cd442e6762db/scratchpad/frab_prod.db"
c = sqlite3.connect(DB)

# latest daily close per coin = current mark
marks = dict(c.execute(
    "SELECT coin, close FROM xsmom_daily_prices WHERE day_ms=(SELECT MAX(day_ms) FROM xsmom_daily_prices)"
).fetchall())

# perp positions linked to xsmom
rows = c.execute("""
  SELECT p.id, p.coin, p.side, p.qty, p.entry_price, p.status, p.opened_at, p.closed_at
  FROM positions p JOIN xsmom_positions xp ON xp.perp_position_id=p.id
  ORDER BY p.opened_at
""").fetchall()

def sign(side): return 1.0 if side.upper()=="LONG" else -1.0

hdr = f"{'id':>3} {'coin':<6} {'side':<5} {'qty':>10} {'entry':>10} {'exit':>10} {'stat':<6} {'pricePnL':>9} {'fund':>7} {'fees':>7} {'net':>8}"
print(hdr); print("-"*len(hdr))

tot_price=tot_fund=tot_fee=0.0
open_price=open_fund=open_fee=0.0
closed_price=closed_fund=closed_fee=0.0
for pid,coin,side,qty,entry,status,o,cl in rows:
    # exit price
    if status=="CLOSED":
        # closing fill = opposite side of position side
        close_side = "short" if side.upper()=="LONG" else "long"
        f = c.execute("SELECT price FROM fills WHERE position_id=? AND side=? ORDER BY ts_ms DESC LIMIT 1",
                      (pid, close_side)).fetchone()
        exitp = f[0] if f else marks.get(coin)
    else:
        exitp = marks.get(coin)
    if exitp is None:
        print(f"WARN no mark for {coin}"); continue
    price_pnl = qty*(exitp-entry)*sign(side)
    fund = c.execute("SELECT COALESCE(SUM(amount),0) FROM funding_accruals WHERE position_id=?",(pid,)).fetchone()[0]
    fees = c.execute("SELECT COALESCE(SUM(fee),0) FROM fills WHERE position_id=?",(pid,)).fetchone()[0]
    net = price_pnl+fund-fees
    tot_price+=price_pnl; tot_fund+=fund; tot_fee+=fees
    if status=="CLOSED":
        closed_price+=price_pnl; closed_fund+=fund; closed_fee+=fees
    else:
        open_price+=price_pnl; open_fund+=fund; open_fee+=fees
    print(f"{pid:>3} {coin:<6} {side:<5} {qty:>10.4g} {entry:>10.5g} {exitp:>10.5g} {status:<6} {price_pnl:>9.3f} {fund:>7.3f} {fees:>7.3f} {net:>8.3f}")

print("-"*len(hdr))
print(f"OPEN   positions:  price_pnl={open_price:8.3f}  funding={open_fund:7.3f}  fees={open_fee:7.3f}  net={open_price+open_fund-open_fee:8.3f}")
print(f"CLOSED positions:  price_pnl={closed_price:8.3f}  funding={closed_fund:7.3f}  fees={closed_fee:7.3f}  net={closed_price+closed_fund-closed_fee:8.3f}")
print(f"TOTAL  (all):      price_pnl={tot_price:8.3f}  funding={tot_fund:7.3f}  fees={tot_fee:7.3f}  net={tot_price+tot_fund-tot_fee:8.3f}")
print()
print(f"Live 'HL portfolio uPnL' should ≈ OPEN price_pnl = {open_price:.3f}")
print(f"Live sleeve total PnL should ≈ TOTAL net = {tot_price+tot_fund-tot_fee:.3f}")
