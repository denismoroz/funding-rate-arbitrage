"""Part 3: the PRIMARY verdict axis is 'OOS CPCV distribution + PBO' (DSR was
demoted to informational after the carry/FRAB calibration). How much does the
OOS-distribution axis actually discriminate?"""
import numpy as np
rng = np.random.default_rng(3)
ANN, M = np.sqrt(252), 2000

print("OOS CPCV axis: 'median segment Sharpe > 0' and 'fraction of segments > 0'")
print("Segments are SHORT, so per-segment Sharpe is extremely noisy.\n")
print(f"{'trueSR':>7} | {'seg=44d':>22} | {'seg=88d':>22}")
print(f"{'':>7} | {'med frac>0':>11}{'P(med>0)':>11} | {'med frac>0':>11}{'P(med>0)':>11}")
print("-"*58)
for sr in (-0.5, 0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0):
    out=[]
    for seg_len in (44, 88):
        n_seg = 25
        fracs=[]; medpos=[]
        for _ in range(M):
            # each segment: mean of seg_len daily returns with true per-day sharpe
            seg_sr = rng.standard_normal(n_seg)/np.sqrt(seg_len) + sr/ANN
            # per-segment annualized sharpe estimate
            s = seg_sr*ANN
            fracs.append((s>0).mean()); medpos.append(np.median(s)>0)
        out.append((np.median(fracs), np.mean(medpos)))
    print(f"{sr:>7.2f} | {out[0][0]*100:>10.0f}%{out[0][1]*100:>10.0f}% | "
          f"{out[1][0]*100:>10.0f}%{out[1][1]*100:>10.0f}%")

print("\n=> the axis separates sign, not size: it cannot tell SR 0.3 from SR 1.5,")
print("   and it passes anything with a positive mean. It is a WEAK gate, biased")
print("   toward GO — the opposite failure mode from PBO.\n")

print("="*66)
print("Consistency check: two candidates that got the SAME PBO, opposite calls")
print("="*66)
print("  XSMOM        PBO = 0.605  -> launched live")
print("  token_unlock PBO = 0.605  -> cited as 'choice does not carry forward'")
print("  simulated baseline PBO on a no-edge menu = 0.605-0.612")
print("\n=> 0.605 is the NULL value of PBO for this menu shape. It distinguished")
print("   nothing; the two opposite decisions were made on the same non-signal.")
