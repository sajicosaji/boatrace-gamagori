"""全特徴量で 艇番ペア項の有無・正則化の強さを比較（蒲郡・検証期間）"""
import sys, io
sys.path.insert(0, r"C:\Users\dash_\boatrace_data\tools")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import numpy as np, pandas as pd
import plmodel as M
exec(open(r"C:\Users\dash_\boatrace_data\tools\exp1_groups.py", encoding="utf-8").read().split("cols_all = []")[0].split("import plmodel as M")[1])

cols = sum((M.GROUPS[g] for g in ["B", "EX", "KOS", "R3", "CL", "WX"]), [])
dtr = M.Data(F[tr], X[tr], cols)
dva = M.Data(F[va], X[va], cols, dtr.mu, dtr.sd)
for pairs in (False, True):
    for lam in (10.0, 30.0, 100.0, 300.0):
        th = M.fit(dtr, lam, pairs=pairs)
        l1, l2, l3 = evaluate(dva, th)
        print(f"pairs={pairs!s:<5} λ={lam:<5} 単勝{np.nanmean(l1):.4f} 2連単{np.nanmean(l2):.4f} 3連単{np.nanmean(l3):.4f}", flush=True)
