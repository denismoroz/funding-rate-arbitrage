"""Part 2: is PBO broken, or was it MISAPPLIED? And is DSR itself calibrated on
fat-tailed, autocorrelated crypto returns (not the gaussian ideal)?"""
import sys, itertools, numpy as np
sys.path.insert(0, "/Users/d/prj/funding-rate-arbitrage/research/validation_harness")
sys.path.insert(0, "/Users/d/prj/funding-rate-arbitrage/research/harness_power")
from metrics import dsr_from_returns
from power import pbo_fast          # validated against reference impl

rng = np.random.default_rng(7)
T, S, ANN, M = 1101, 16, np.sqrt(252), 300

def verdict(R):
    p = pbo_fast(R, S=S)
    srs = R.mean(axis=0)/R.std(axis=0, ddof=1)
    d = dsr_from_returns(R[:, int(np.argmax(srs))], srs)["dsr"]
    return d, p

print("=== A. PBO on a HETEROGENEOUS menu (what PBO is actually designed for) ===")
print("   one config carries the edge, the rest are duds -> the CHOICE is meaningful\n")
print(f"{'trueSR(best)':>12} {'medPBO':>8} {'passPBO<0.2':>12}")
print("-"*36)
for sr in (0.0, 0.5, 1.0, 1.5, 2.0):
    ps=[]
    for _ in range(M):
        R = rng.standard_normal((T, 5))
        R[:, 0] += sr/ANN                  # only cfg0 has the edge
        ps.append(pbo_fast(R, S=S))
    ps=np.array(ps)
    print(f"{sr:>12.1f} {np.median(ps):>8.3f} {(ps<0.2).mean()*100:>11.0f}%")

print("\n=== B. HOMOGENEOUS menu (variants of ONE idea = how it was actually used) ===")
print("   all configs share the same edge -> picking the IS-best is pure noise\n")
print(f"{'trueSR(all)':>12} {'medPBO':>8} {'passPBO<0.2':>12}")
print("-"*36)
for sr in (0.0, 1.0, 2.0, 3.0):
    ps=[]
    for _ in range(M):
        c = rng.standard_normal((T,1)); i = rng.standard_normal((T,5))
        R = np.sqrt(0.9)*c + np.sqrt(0.1)*i + sr/ANN
        ps.append(pbo_fast(R, S=S))
    ps=np.array(ps)
    print(f"{sr:>12.1f} {np.median(ps):>8.3f} {(ps<0.2).mean()*100:>11.0f}%")

print("\n=== C. DSR calibration under CRYPTO-like returns (fat tails + autocorr) ===")
print("   true edge = 0. A well-calibrated gate should fire ~5% of the time at DSR>0.95\n")
print(f"{'returns'::>0} {'':>2}{'medDSR':>10} {'falseGO(DSR>0.95)':>20}")
def ar1_t(T, n, phi, df):
    x = rng.standard_t(df, size=(T+200, n)) / np.sqrt(df/(df-2))
    for k in range(1, T+200): x[k] = phi*x[k-1] + np.sqrt(1-phi**2)*x[k]
    return x[200:]
for label, gen in (("gaussian iid      ", lambda: rng.standard_normal((T,5))),
                   ("fat tails t(4)    ", lambda: ar1_t(T,5,0.0,4)),
                   ("t(4) + AR(1) .15  ", lambda: ar1_t(T,5,0.15,4)),
                   ("t(4) + AR(1) .30  ", lambda: ar1_t(T,5,0.30,4))):
    ds=[]
    for _ in range(M):
        R = gen()
        srs = R.mean(axis=0)/R.std(axis=0, ddof=1)
        ds.append(dsr_from_returns(R[:, int(np.argmax(srs))], srs)["dsr"])
    ds=np.array(ds)
    print(f"{label} {np.median(ds):>10.3f} {(ds>0.95).mean()*100:>19.0f}%")

print("\n=== D. what DSR value corresponds to what TRUE Sharpe (homogeneous menu) ===")
print(f"{'trueSR':>7} {'medDSR':>8} {'p10':>8} {'p90':>8}")
print("-"*34)
for sr in (0.0,0.25,0.5,0.75,1.0,1.25,1.5):
    ds=[]
    for _ in range(M):
        c=rng.standard_normal((T,1)); i=rng.standard_normal((T,5))
        R=np.sqrt(0.9)*c+np.sqrt(0.1)*i+sr/ANN
        srs=R.mean(axis=0)/R.std(axis=0,ddof=1)
        ds.append(dsr_from_returns(R[:,int(np.argmax(srs))],srs)["dsr"])
    ds=np.array(ds)
    print(f"{sr:>7.2f} {np.median(ds):>8.3f} {np.percentile(ds,10):>8.3f} {np.percentile(ds,90):>8.3f}")
