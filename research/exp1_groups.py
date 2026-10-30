"""特徴量グループの効き目を検証（蒲郡・時系列分割）。市場（確定配当から逆算した的中組の確率）と比較。"""
import sys, io, math
sys.path.insert(0, r"C:\Users\dash_\boatrace_data\tools")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import numpy as np, pandas as pd
import plmodel as M

F = pd.read_pickle(M.ROOT / "feats.pkl")
R = pd.read_pickle(M.ROOT / "races.pkl")
JCD = sys.argv[1] if len(sys.argv) > 1 else "07"
F = F[(F["jcd"] == JCD) & (F["date"] >= "20210101")].copy()
X = M.base_features(F)
tr = F["date"] <= "20240630"
va = (F["date"] > "20240630") & (F["date"] <= "20250630")
R = R.set_index(["date", "jcd", "rno"])

def market_ll(meta, kind):
    out = []
    for d, j, r in zip(meta["date"], meta["jcd"], meta["rno"]):
        pay = R.loc[(d, j, r), "pay"].get(kind) if (d, j, r) in R.index else None
        out.append(-math.log(min(75.0 / pay[0][1], 0.95)) if pay and pay[0][1] > 0 else np.nan)
    return np.array(out)

def evaluate(data, thetas):
    p1, ex, tri = M.probs(data, thetas)
    n = len(data.pos); i = np.arange(n); w, s, t = data.pos.T
    ok1 = w >= 0; ok2 = ok1 & (s >= 0); ok3 = ok2 & (t >= 0)
    l1 = np.where(ok1, -np.log(np.maximum(p1[i, w], 1e-9)), np.nan)
    l2 = np.where(ok2, -np.log(np.maximum(ex[i, w, s], 1e-9)), np.nan)
    l3 = np.where(ok3, -np.log(np.maximum(tri[i, w, s, t], 1e-9)), np.nan)
    return l1, l2, l3

cols_all = []
dv_cache = {}
order = ["LANE", "B", "EX", "KOS", "R3", "CL", "WX"]
for gname in order:
    if gname != "LANE":
        cols_all = cols_all + M.GROUPS[gname]
    cols = cols_all if cols_all else ["age"]  # LANEのみ: ダミー1列（正則化で0に）
    dtr = M.Data(F[tr], X[tr], cols)
    dva = M.Data(F[va], X[va], cols, dtr.mu, dtr.sd)
    best = None
    for lam in ([1e6] if gname == "LANE" else [1.0, 10.0, 30.0]):
        th = M.fit(dtr, lam)
        l1, l2, l3 = evaluate(dva, th)
        r = (np.nanmean(l1), np.nanmean(l2), np.nanmean(l3), lam)
        if best is None or r[0] + r[1] + r[2] < best[0] + best[1] + best[2]:
            best = r
    if "mk" not in dv_cache:
        dv_cache["mk"] = (market_ll(dva.meta, "tan"), market_ll(dva.meta, "ex"), market_ll(dva.meta, "tri"))
    print(f"+{gname:<4} 検証logloss 単勝{best[0]:.4f} 2連単{best[1]:.4f} 3連単{best[2]:.4f} (λ={best[3]})", flush=True)
mk = dv_cache["mk"]
print(f"市場(確定) 検証logloss 単勝{np.nanmean(mk[0]):.4f} 2連単{np.nanmean(mk[1]):.4f} 3連単{np.nanmean(mk[2]):.4f}  n={len(mk[0])}")
