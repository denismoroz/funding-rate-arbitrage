"""Power test of the validation harness itself.

Question: if a strategy has a KNOWN true Sharpe, how often does the harness say GO?
That is the false-negative rate — i.e. how many real edges the graveyard may hold.

Verdict rule from validation_harness/README.md: GO iff DSR > 0.95 AND PBO < 0.2.
"""
import sys, itertools, numpy as np
sys.path.insert(0, "/Users/d/prj/funding-rate-arbitrage/research/validation_harness")
from pbo import pbo as pbo_ref
from metrics import dsr_from_returns

# ---- fast, exact re-implementation of CSCV PBO (validated vs pbo_ref below) --
def pbo_fast(R, S=16):
    T, N = R.shape
    chunks = np.array_split(np.arange(T), S)
    n_k = np.array([len(c) for c in chunks])
    s1 = np.stack([R[c].sum(axis=0) for c in chunks])        # S x N
    s2 = np.stack([(R[c]**2).sum(axis=0) for c in chunks])   # S x N
    combos = list(itertools.combinations(range(S), S//2))
    idx = np.zeros((len(combos), S), dtype=bool)
    for i, cb in enumerate(combos): idx[i, list(cb)] = True
    def sharpe(mask):                      # mask: M x S  -> M x N
        n = mask @ n_k
        a = mask.astype(float) @ s1
        b = mask.astype(float) @ s2
        mu = a / n[:, None]
        var = (b - n[:, None]*mu**2) / (n[:, None]-1)
        sd = np.sqrt(np.maximum(var, 0))
        out = np.zeros_like(mu); nz = sd > 0
        out[nz] = mu[nz]/sd[nz]
        return out
    is_p, oos_p = sharpe(idx), sharpe(~idx)
    n_star = is_p.argmax(axis=1)
    # rank of n_star among oos (1=worst .. N=best), average ranks on ties
    order = oos_p.argsort(axis=1)
    ranks = np.empty_like(order)
    rows = np.arange(oos_p.shape[0])[:, None]
    ranks[rows, order] = np.arange(1, N+1)[None, :]
    r_star = ranks[rows[:, 0], n_star]
    omega = r_star / (N + 1)
    lam = np.log(omega/(1-omega))
    return float((lam <= 0).mean())

rng = np.random.default_rng(0)
_R = rng.standard_normal((600, 5)); _R[:, 0] += 0.05
a, b = pbo_ref(_R, S=8).pbo, pbo_fast(_R, S=8)
assert abs(a-b) < 1e-9, f"fast PBO mismatch {a} vs {b}"
print(f"[check] fast PBO == reference PBO ({a:.4f})  ok\n")

# ---- the experiment ---------------------------------------------------------
T = 1101          # same length as the real crypto XSEC run (daily)
N_CFG = 5         # same menu size
S = 16
M = 300           # repetitions per scenario
ANN = np.sqrt(252)

def run(true_sharpe_ann, rho, m=M):
    """menu of N_CFG correlated variants of ONE idea, all sharing the true edge."""
    mu = true_sharpe_ann / ANN          # per-period mean, unit vol
    go = dsr_ok = pbo_ok = 0
    dsrs, pbos = [], []
    for _ in range(m):
        common = rng.standard_normal((T, 1))
        idio = rng.standard_normal((T, N_CFG))
        R = np.sqrt(rho)*common + np.sqrt(1-rho)*idio + mu
        p = pbo_fast(R, S=S)
        srs = R.mean(axis=0)/R.std(axis=0, ddof=1)
        best = int(np.argmax(srs))
        d = dsr_from_returns(R[:, best], srs)["dsr"]
        dsrs.append(d); pbos.append(p)
        if d > 0.95: dsr_ok += 1
        if p < 0.2: pbo_ok += 1
        if d > 0.95 and p < 0.2: go += 1
    return dict(sr=true_sharpe_ann, rho=rho, go=go/m, dsr_ok=dsr_ok/m, pbo_ok=pbo_ok/m,
                med_dsr=float(np.median(dsrs)), med_pbo=float(np.median(pbos)))

print(f"T={T} daily obs (~4.4y), menu={N_CFG} correlated variants, S={S}, {M} reps/scenario")
print(f"GO rule: DSR>0.95 AND PBO<0.2\n")
print(f"{'trueSR':>7} {'rho':>5} | {'medDSR':>7} {'medPBO':>7} | {'passDSR':>8} {'passPBO':>8} {'GO':>7}")
print("-"*62)
rows=[]
for rho in (0.0, 0.5, 0.9):
    for sr in (0.0, 0.5, 1.0, 1.5, 2.0):
        r = run(sr, rho)
        rows.append(r)
        print(f"{sr:>7.1f} {rho:>5.1f} | {r['med_dsr']:>7.3f} {r['med_pbo']:>7.3f} | "
              f"{r['dsr_ok']*100:>7.0f}% {r['pbo_ok']*100:>7.0f}% {r['go']*100:>6.0f}%")
    print("-"*62)
import json
json.dump(rows, open("research/harness_power/power.json","w"), indent=1)
