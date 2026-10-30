"""単勝妙味（確定オッズ判断）の較正: 予測期待値 vs 実際の回収率、300点での振れ幅"""
import sys, io, random
sys.path.insert(0, r"C:\Users\dash_\boatrace_data\tools")
import numpy as np
exec(open(r"C:\Users\dash_\boatrace_data\tools\exp6_benter_hist.py", encoding="utf-8").read().split("■単勝")[0].rsplit("print(", 1)[0])

picks = []
for x in B:
    pc = comb(x, t)
    for k in range(len(x["boats"])):
        ev = pc[k] * x["odds"][k]
        if ev >= 1.1:
            picks.append((ev, x["odds"][k], ret_of(x, k) / 100))
picks = np.array(picks)
print(f"B期間 妙味{len(picks)}点 / {len(B)}R（1Rあたり{len(picks)/len(B):.2f}点）")
for lo, hi in ((1.1, 1.2), (1.2, 1.4), (1.4, 1.7), (1.7, 99)):
    s = picks[(picks[:, 0] >= lo) & (picks[:, 0] < hi)]
    print(f"  予測期待値{lo}〜{hi}: {len(s):>4}点 平均予測{s[:,0].mean():.2f} → 実際{s[:,2].mean():.2f}  平均オッズ{s[:,1].mean():.1f}")
print(f"  全体: 平均予測{picks[:,0].mean():.2f} → 実際{picks[:,2].mean():.2f}")
rnd = random.Random(0)
sims = [np.mean(picks[[rnd.randrange(len(picks)) for _ in range(300)], 2]) for _ in range(4000)]
print(f"300点の回収率の振れ幅（95%）: {np.percentile(sims,2.5)*100:.0f}%〜{np.percentile(sims,97.5)*100:.0f}%  標準偏差{np.std(sims)*100:.0f}pt")
for cap in (10, 20, 30):
    s = picks[picks[:, 1] <= cap]
    sims = [np.mean(s[[rnd.randrange(len(s)) for _ in range(300)], 2]) for _ in range(2000)]
    print(f"  オッズ{cap}倍以下に限ると: {len(s)}点 予測{s[:,0].mean():.2f}→実際{s[:,2].mean():.2f}  300点の振れ幅±{np.std(sims)*196:.0f}pt")
