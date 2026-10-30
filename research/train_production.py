"""本番用モデル（model_v5.json）を作る。
1) 段階別PL（艇番ペア項あり、λ=100）を 2021/1〜最新の蒲郡全レースで学習
2) モデル×確定単勝オッズの組み合わせ係数を、学習に使っていない予測（A:〜24/6学習→24/7〜25/6、
   B:〜25/6学習→25/7〜最新）で推定
使い方: python -P train_production.py <出力先model_v5.json>
"""
import sys, io, json, math, pickle
sys.path.insert(0, r"C:\Users\dash_\boatrace_data\tools")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import numpy as np, pandas as pd
from scipy.optimize import minimize
import plmodel as M

OUT = sys.argv[1]
F = pd.read_pickle(M.ROOT / "feats.pkl")
F = F[(F["jcd"] == "07") & (F["date"] >= "20210101")].copy()
X = M.base_features(F)
W = pickle.load(open(M.ROOT / "win_odds_07.pkl", "rb"))
cols = sum((M.GROUPS[g] for g in ["B", "EX", "KOS", "R3", "CL", "WX"]), [])
LAM = 100.0

def oos(train_end, s, e):
    tr = F["date"] <= train_end; ev = (F["date"] >= s) & (F["date"] <= e)
    d1 = M.Data(F[tr], X[tr], cols); d2 = M.Data(F[ev], X[ev], cols, d1.mu, d1.sd)
    p1, _, _ = M.probs(d2, M.fit(d1, LAM, pairs=True))
    rows = []
    for r, (d, rno) in enumerate(zip(d2.meta["date"], d2.meta["rno"])):
        o = W.get((d, int(rno)))
        boats = [b for b in range(6) if d2.mask[r, b]]
        if not o or d2.pos[r, 0] < 0 or any(o.get(b + 1, 0) < 1.0 for b in boats): continue
        q = np.array([1 / o[b + 1] for b in boats]); q /= q.sum()
        p = p1[r, boats] / p1[r, boats].sum()
        rows.append((np.log(np.maximum(p, 1e-9)), np.log(q), boats.index(d2.pos[r, 0])))
    return rows

rows = oos("20240630", "20240701", "20250630") + oos("20250630", "20250701", "20261029")
def nll(t):
    s = 0.0
    for lp, lq, w in rows:
        z = t[0] * lp + t[1] * lq; z -= z.max(); s -= z[w] - math.log(np.exp(z).sum())
    return s / len(rows)
t = minimize(nll, [0.7, 0.4], method="Nelder-Mead").x
print(f"組み合わせ係数（{len(rows)}R）: モデル^{t[0]:.4f} × 市場^{t[1]:.4f}  logloss {nll(t):.4f} / 市場 {nll([0,1]):.4f}")

dall = M.Data(F, X, cols)
th = M.fit(dall, LAM, pairs=True)
model = {
    "version": "v5",
    "trained": {"from": F["date"].min(), "to": F["date"].max(), "races": int(len(dall.pos)), "lambda": LAM,
                "note": "段階別Plackett-Luce（1着・2着・3着で別係数、艇番×特徴量、艇番ペア項）。mbrace公式データ・レース時点の情報のみ"},
    "cols": cols,
    "mu": [float(v) for v in dall.mu[cols]],
    "sd": [float(v) for v in dall.sd[cols]],
    "fill": {"nat_win": float(F["nat_win"].median()), "nat2": float(F["nat2"].median())},
    "theta": [[float(v) for v in x] for x in th],
    "combo_win": {"a": float(t[0]), "b": float(t[1]), "races": len(rows)},
}
json.dump(model, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"保存: {OUT}  学習 {model['trained']}")
