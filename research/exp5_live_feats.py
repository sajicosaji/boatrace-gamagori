"""本番で再現しやすい特徴量セットだけでどこまで行けるか（検証期間・本番期間）"""
import sys, io, math
sys.path.insert(0, r"C:\Users\dash_\boatrace_data\tools")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import numpy as np, pandas as pd
import plmodel as M
exec(open(r"C:\Users\dash_\boatrace_data\tools\exp1_groups.py", encoding="utf-8").read().split("cols_all = []")[0].split("import plmodel as M")[1])

sets = {
    "B+EX+KOS+WX": ["B", "EX", "KOS", "WX"],
    "+R3": ["B", "EX", "KOS", "WX", "R3"],
    "+CL": ["B", "EX", "KOS", "WX", "CL"],
    "全部": ["B", "EX", "KOS", "WX", "R3", "CL"],
}
periods = {"検証(24/7-25/6)": (tr, va),
           "本番期間(26/8/11-10/29)": (F["date"] <= "20260810", F["date"] > "20260810")}
for pname, (a, b) in periods.items():
    for sname, gs in sets.items():
        cols = sum((M.GROUPS[g] for g in gs), [])
        d1 = M.Data(F[a], X[a], cols); d2 = M.Data(F[b], X[b], cols, d1.mu, d1.sd)
        th = M.fit(d1, 100.0, pairs=True)
        l1, l2, l3 = evaluate(d2, th)
        print(f"{pname} {sname:<12} 単勝{np.nanmean(l1):.4f} 2連単{np.nanmean(l2):.4f} 3連単{np.nanmean(l3):.4f} n={len(l1)}", flush=True)
    mk = (market_ll(d2.meta, "tan"), market_ll(d2.meta, "ex"), market_ll(d2.meta, "tri"))
    print(f"{pname} 市場(確定)    単勝{np.nanmean(mk[0]):.4f} 2連単{np.nanmean(mk[1]):.4f} 3連単{np.nanmean(mk[2]):.4f}")
