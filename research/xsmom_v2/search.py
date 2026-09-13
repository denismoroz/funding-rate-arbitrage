"""XSMOM v2 — diagnostics of the prod config, full pre-registered grid, selection, verdict (see PLAN.md).

Stages (results -> results.json, pnl matrix -> scratchpad):
  1. selftest of the engine against xsec.portfolio_returns
  2. diagnostics of the prod-like book: years, legs, funding, costs, rebalance-weekday lottery
  3. the 1152-config grid (tranched weekly rebalance), net of funding and 4.4 bps
  4. selection on DISCOVERY, DSR over all trials, PBO, CPCV selection transfer
  5. HOLDOUT, opened once for the selected config (4.4 and 8.5 bps) and the prod-like baseline
"""
from __future__ import annotations

import itertools, json, sys, time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT / "research" / "validation_harness"))
import engine as E
from metrics import dsr_from_returns
from pbo import pbo
from splitter import cpcv

OUT = HERE / "results.json"
MATRIX = E.RAW / "grid_pnl.npz"
DISC = (pd.Timestamp("2020-07-01", tz="UTC"), pd.Timestamp("2025-03-31", tz="UTC"))
HOLD = (pd.Timestamp("2025-04-01", tz="UTC"), pd.Timestamp("2026-09-11", tz="UTC"))   # last row has no fwd
LIVE = (pd.Timestamp("2026-06-15", tz="UTC"), pd.Timestamp("2026-09-11", tz="UTC"))
BPS, BPS_HIGH = 4.4, 8.5
GRID = dict(lb=["S", "M", "L", "XL"], skip=[0, 7], k=[5, 8, 12], invvol=[False, True], resid=[False, True],
            overlay=["none", "voltarget", "funding", "gate"], structure=["ls", "long_btc", "short_btc"])


def rows(p, lo, hi):
    return np.flatnonzero((p.dates >= lo) & (p.dates <= hi))


def cfg_name(c):
    return (f"{c['lb']}|skip{c['skip']}|k{c['k']}|{'iv' if c['invvol'] else 'ew'}|{'resid' if c['resid'] else 'raw'}"
            f"|{c['overlay']}|{c['structure']}")


def build_config_pnl(p, c, bps, cache_scores={}, cache_w={}):
    fp = c["overlay"] == "funding"
    skey = (c["lb"], c["skip"], c["resid"], fp)
    if skey not in cache_scores:
        cache_scores[skey] = E.score(p, c["lb"], c["skip"], c["resid"], fp)
    wkey = skey + (c["k"], c["invvol"], c["structure"], bps)
    if wkey not in cache_w:
        W = E.weights(p, cache_scores[skey], c["k"], c["invvol"], c["structure"])
        cache_w.clear()                                   # one weight set at a time keeps memory flat
        cache_w[wkey] = E.tranched(p, W, bps)
    base = cache_w[wkey]
    if c["overlay"] == "voltarget":
        return E.vol_target(base, bps)
    if c["overlay"] == "gate":
        return E.dispersion_gate(p, base, bps)
    return base


def diagnostics(p):
    out = {}
    sc = E.score(p, "M", 0, False, False)
    W = E.weights(p, sc, 8, False, "ls")
    thu = E.weekday_mask(p, 3)
    prod = E.book(p, W, thu, BPS)
    d = rows(p, *DISC)
    out["prod_like_discovery"] = E.stats(prod[d])
    out["prod_like_full"] = E.stats(prod[rows(p, DISC[0], HOLD[1])])
    # rebalance-weekday lottery
    out["weekday_sharpe_discovery"] = {wd: E.stats(E.book(p, W, E.weekday_mask(p, wd), BPS)[d])["sharpe"] for wd in range(7)}
    # legs, funding, costs (Thursday book)
    Wl = np.where(W > 0, W, 0.0); Ws = np.where(W < 0, W, 0.0)
    T = len(p.dates)
    last = np.maximum.accumulate(np.where(thu, np.arange(T), -1))
    held = np.where(last[:, None] >= 0, W[np.maximum(last, 0)], 0.0)
    fwd = np.nan_to_num(p.fwd, nan=0.0)
    price_long = (np.where(held > 0, held, 0) * fwd).sum(1); price_short = (np.where(held < 0, held, 0) * fwd).sum(1)
    fund_long = (np.where(held > 0, held, 0) * p.accr).sum(1); fund_short = (np.where(held < 0, held, 0) * p.accr).sum(1)
    btc = fwd[:, p.btc]
    years = {}
    for y in range(2020, 2027):
        r = np.flatnonzero((p.dates.year == y) & (p.dates >= DISC[0]) & (p.dates <= HOLD[1]))
        if len(r) < 30:
            continue
        years[y] = dict(book=E.stats(prod[r])["total"], sharpe=E.stats(prod[r])["sharpe"],
                        long_leg_price=float(price_long[r].sum() * 100), short_leg_price=float(price_short[r].sum() * 100),
                        funding_long=float(fund_long[r].sum() * 100), funding_short=float(fund_short[r].sum() * 100),
                        btc=float((np.prod(1 + btc[r]) - 1) * 100))
    out["prod_like_years"] = years
    # tail: worst weeks of the prod-like book
    wk = pd.Series(prod, index=p.dates).resample("W").sum()
    out["worst_weeks"] = {str(k.date()): float(v * 100) for k, v in wk.nsmallest(8).items()}
    return out, prod


def main():
    t0 = time.time()
    p = E.build_panel()
    print(f"panel {len(p.dates)} days x {len(p.syms)} perps; eligible per day median {int(np.median(p.elig.sum(1)))}; "
          f"min {int(p.elig[rows(p, *DISC)].sum(1).min())}", flush=True)
    E.selftest(p)
    res = {"plan": "PLAN.md", "bps": BPS}

    # sanity: largest single-day moves among eligible (data glitches show up here)
    f = np.where(p.elig, p.fwd, np.nan)
    flat = np.argsort(-np.nan_to_num(np.abs(f), nan=0.0), axis=None)[:12]
    res["largest_moves"] = [(p.syms[j], str(p.dates[i].date()), float(f[i, j])) for i, j in (np.unravel_index(x, f.shape) for x in flat)]
    print("largest daily moves among eligible:", res["largest_moves"][:8], flush=True)

    diag, prod = diagnostics(p)
    res["diagnostics"] = diag
    print(json.dumps(diag, indent=1, default=float), flush=True)

    # ── grid ──
    keys = list(GRID)
    configs = [dict(zip(keys, v)) for v in itertools.product(*[GRID[k] for k in keys])]
    configs.sort(key=lambda c: (c["lb"], c["skip"], c["resid"], c["overlay"] == "funding", c["k"], c["invvol"], c["structure"]))
    names = [cfg_name(c) for c in configs]
    P = np.zeros((len(configs), len(p.dates)))
    for i, c in enumerate(configs):
        P[i] = build_config_pnl(p, c, BPS)
        if i % 100 == 0:
            print(f"grid {i}/{len(configs)}  {time.time() - t0:.0f}s", flush=True)
    np.savez_compressed(MATRIX, P=P, names=np.array(names), dates=p.dates.tz_convert(None).values)

    d = rows(p, *DISC)
    R = P[:, d].T                                          # T_disc x N
    sr_period = R.mean(0) / R.std(0, ddof=1)
    sr_ann = sr_period * np.sqrt(365)
    best = int(np.argmax(sr_ann))
    res["n_trials"] = len(configs)
    res["selected"] = dict(name=names[best], config=configs[best], discovery=E.stats(R[:, best]))
    top = np.argsort(-sr_ann)[:15]
    res["top15_discovery"] = [dict(name=names[j], **E.stats(R[:, j])) for j in top]
    base_i = names.index("M|skip0|k8|ew|raw|none|ls")
    res["baseline_tranched_discovery"] = E.stats(R[:, base_i])
    print("top discovery configs:"); [print(f"  {x['name']:<45} SR {x['sharpe']:.2f} ann {x['ann']:+.1f}% DD {x['maxdd']:.1f}") for x in res["top15_discovery"]]

    # family marginals: median Sharpe by axis value
    df = pd.DataFrame(configs); df["sr"] = sr_ann
    res["marginals"] = {k: df.groupby(k)["sr"].median().round(3).to_dict() for k in keys}
    print("median discovery Sharpe by axis:", json.dumps(res["marginals"], default=str), flush=True)
    res["share_positive_sharpe"] = float((sr_ann > 0).mean())

    # ── DSR over every trial ──
    res["dsr"] = dsr_from_returns(R[:, best], sr_period)
    print("DSR", res["dsr"], flush=True)

    # ── PBO (published, not decisive; see PLAN.md) ──
    pr = pbo(R, S=16, names=names)
    res["pbo"] = dict(pbo=pr.pbo, n_splits=pr.n_splits, median_oos_rank=pr.median_oos_rank)
    print("PBO", res["pbo"], f"{time.time() - t0:.0f}s", flush=True)

    # ── CPCV: does selecting the best on train carry to test? ──
    purge = 187
    tr = []
    for sp in cpcv(len(d), n_groups=6, k=2, purge=purge, embargo=7):
        a = R[sp.train_idx]; b = R[sp.test_idx]
        j = int(np.argmax(a.mean(0) / a.std(0, ddof=1)))
        tsr = float(b[:, j].mean() / b[:, j].std(ddof=1) * np.sqrt(365))
        tr.append(dict(test_groups=list(sp.test_groups) if hasattr(sp, "test_groups") else None, chosen=names[j],
                       train_sharpe=float(a[:, j].mean() / a[:, j].std(ddof=1) * np.sqrt(365)), test_sharpe=tsr,
                       baseline_test_sharpe=float(b[:, base_i].mean() / b[:, base_i].std(ddof=1) * np.sqrt(365))))
    ts = np.array([x["test_sharpe"] for x in tr])
    res["cpcv_transfer"] = dict(splits=tr, median_test_sharpe=float(np.median(ts)), share_positive=float((ts > 0).mean()))
    print("CPCV transfer: median test Sharpe", round(float(np.median(ts)), 2), "positive", (ts > 0).sum(), "/", len(ts), flush=True)

    # ── pre-registered verdict on discovery ──
    passed_dsr = res["dsr"]["dsr"] >= 0.95
    passed_cpcv = np.median(ts) > 0.5 and (ts > 0).mean() >= 0.8
    res["discovery_checks"] = dict(dsr=bool(passed_dsr), cpcv_transfer=bool(passed_cpcv))

    # ── HOLDOUT, opened once ──
    h = rows(p, *HOLD); lv = rows(p, *LIVE)
    sel = configs[best]
    hi_pnl = build_config_pnl(p, sel, BPS_HIGH, {}, {})
    res["holdout"] = dict(selected_4_4=E.stats(P[best, h]), selected_8_5=E.stats(hi_pnl[h]),
                          baseline_tranched=E.stats(P[base_i, h]), prod_like_thursday=E.stats(prod[h]),
                          selected_live_window=E.stats(P[best, lv]), prod_like_live_window=E.stats(prod[lv]))
    hp = res["holdout"]
    passed_hold = hp["selected_4_4"]["sharpe"] > 0 and hp["selected_4_4"]["total"] > 0 and hp["selected_8_5"]["total"] > 0
    res["holdout_check"] = bool(passed_hold)
    res["verdict"] = "PASS" if (passed_dsr and passed_cpcv and passed_hold) else "NO-GO"
    print("HOLDOUT", json.dumps(hp, indent=1), "\nVERDICT", res["verdict"], res["discovery_checks"], "holdout", passed_hold, flush=True)
    # ── robustness (not part of the verdict): prod-like universe with >= 547 listed days ──
    E._resid_cache.clear()
    p547 = E.build_panel(min_days=547)
    sel547 = build_config_pnl(p547, sel, BPS, {}, {})
    sc = E.score(p547, "M", 0, False, False); W = E.weights(p547, sc, 8, False, "ls")
    base547 = E.tranched(p547, W, BPS); prod547 = E.book(p547, W, E.weekday_mask(p547, 3), BPS)
    d5, h5 = rows(p547, *DISC), rows(p547, *HOLD)
    res["robustness_min547"] = dict(selected_discovery=E.stats(sel547[d5]), selected_holdout=E.stats(sel547[h5]),
                                    baseline_tranched_discovery=E.stats(base547[d5]), baseline_tranched_holdout=E.stats(base547[h5]),
                                    prod_like_discovery=E.stats(prod547[d5]), prod_like_holdout=E.stats(prod547[h5]))
    print("robustness >=547d:", json.dumps(res["robustness_min547"], indent=1), flush=True)
    OUT.write_text(json.dumps(res, indent=1, default=float))
    print(f"done {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
