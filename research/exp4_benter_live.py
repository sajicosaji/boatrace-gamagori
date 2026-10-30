"""新モデル×確定単勝オッズ（本番期間355R）: 組み合わせると市場をどれだけ上回るか"""
import sys, io, math, pickle
sys.path.insert(0, r"C:\Users\dash_\boatrace_data\tools")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import numpy as np, pandas as pd
from scipy.optimize import minimize
import plmodel as M

F = pd.read_pickle(M.ROOT / "feats.pkl")
F = F[(F["jcd"] == "07") & (F["date"] >= "20210101")].copy()
X = M.base_features(F)
tr = F["date"] <= "20260810"; ev = F["date"] > "20260810"
cols = sum((M.GROUPS[g] for g in ["B", "EX", "KOS", "R3", "CL", "WX"]), [])
dtr = M.Data(F[tr], X[tr], cols); dev = M.Data(F[ev], X[ev], cols, dtr.mu, dtr.sd)
th = M.fit(dtr, 100.0, pairs=True)
p1, ex, tri = M.probs(dev, th)
raw = pickle.load(open(r"C:\Users\dash_\AppData\Local\Temp\claude\dfd2aacf-2b03-4b8b-baff-c5a896790cf4\scratchpad\all_races.pkl", "rb"))
rows = []
for r, (d, rno) in enumerate(zip(dev.meta["date"], dev.meta["rno"])):
    v = raw.get((d, int(rno)))
    if not v or not v["win_odds"] or dev.pos[r, 0] < 0: continue
    o = v["win_odds"]
    if any(dev.mask[r, b - 1] and (b not in o or o[b] <= 1.0) for b in range(1, 7)): continue
    boats = [b for b in range(6) if dev.mask[r, b]]
    inv = np.array([1 / o[b + 1] for b in boats]); q = inv / inv.sum()
    rows.append(dict(date=d, rno=rno, boats=boats, p=p1[r, boats], q=q, odds=np.array([o[b + 1] for b in boats]),
                     w=boats.index(dev.pos[r, 0]), pay=v["result"]["払戻金"].get("単勝", {})))
print(f"対象 {len(rows)}R")
ll = lambda P: np.mean([-math.log(max(P(x)[x['w']], 1e-9)) for x in rows])
print(f"logloss  モデル単独 {ll(lambda x: x['p']):.4f} / 市場 {ll(lambda x: x['q']):.4f}")
cut = int(len(rows) * .5); A, B = rows[:cut], rows[cut:]
def comb(x, t):
    z = t[0] * np.log(np.maximum(x['p'], 1e-9)) + t[1] * np.log(x['q']); z -= z.max(); e = np.exp(z); return e / e.sum()
def nll(t, S): return -sum(math.log(max(comb(x, t)[x['w']], 1e-12)) for x in S)
t = minimize(nll, [1.0, 1.0], args=(A,), method="Nelder-Mead").x
tq = minimize(lambda a: nll([0, a[0]], A), [1.0], method="Nelder-Mead").x
print(f"前半で学習: モデル^{t[0]:.2f}×市場^{t[1]:.2f}  後半logloss 市場{np.mean([-math.log(x['q'][x['w']]) for x in B]):.4f}"
      f" / 市場^{tq[0]:.2f} {nll([0,tq[0]],B)/len(B):.4f} / 組合せ {nll(t,B)/len(B):.4f}")
tf = minimize(nll, [1.0, 1.0], args=(rows,), method="Nelder-Mead").x
print(f"全期間で学習: モデル^{tf[0]:.2f}×市場^{tf[1]:.2f}")
for thr in (1.0, 1.1, 1.2, 1.3):
    cost = ret = hits = nb = 0
    for x in B:
        pc = comb(x, t)
        for k, b in enumerate(x['boats']):
            if pc[k] * x['odds'][k] >= thr:
                nb += 1; cost += 100
                if k == x['w']:
                    hits += 1; ret += x['pay'].get(str(b + 1), 0)
    print(f"  後半: 組合せ確率×確定オッズ≥{thr}: {nb}点 的中{hits} 回収率{ret/max(cost,1)*100:.0f}%")
