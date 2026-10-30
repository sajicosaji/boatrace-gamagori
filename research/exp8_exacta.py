"""2連単の妙味: モデル×市場（確定2連単オッズ）。取得できた範囲（2024/7〜）を前半で係数決定・後半で評価。
方式1: 組合せ単勝勝率 × モデルの2着条件付き確率
方式2: 2連単確率そのものを モデル^a × 市場^b で組み合わせ
※確定オッズで判断した場合の上限値
"""
import sys, io, math, pickle, random
sys.path.insert(0, r"C:\Users\dash_\boatrace_data\tools")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import numpy as np, pandas as pd
from scipy.optimize import minimize
import plmodel as M

F = pd.read_pickle(M.ROOT / "feats.pkl")
F = F[(F["jcd"] == "07") & (F["date"] >= "20210101")].copy()
X = M.base_features(F)
R = pd.read_pickle(M.ROOT / "races.pkl").set_index(["date", "jcd", "rno"])
W = pickle.load(open(M.ROOT / "win_odds_07.pkl", "rb"))
E = pickle.load(open(M.ROOT / "ex_odds_07.pkl", "rb"))
bt = pickle.load(open(M.ROOT / "benter_win.pkl", "rb"))["t"]
keys = sorted(E); print(f"2連単オッズ {len(E)}R  {keys[0]}〜{keys[-1]}")
cols = sum((M.GROUPS[g] for g in ["B", "EX", "KOS", "R3", "CL", "WX"]), [])
tr = F["date"] <= "20240630"; ev = (F["date"] >= "20240701") & (F["date"] <= keys[-1][0])
d1 = M.Data(F[tr], X[tr], cols); d2 = M.Data(F[ev], X[ev], cols, d1.mu, d1.sd)
p1, ex, _ = M.probs(d2, M.fit(d1, 100.0, pairs=True))
fr = {(d, int(r), int(l)): f for d, r, l, f in F[["date", "rno", "lane", "fin_raw"]].itertuples(index=False)}

rows = []
for r, (d, rno) in enumerate(zip(d2.meta["date"], d2.meta["rno"])):
    k = (d, int(rno)); eo, wo = E.get(k), W.get(k)
    boats = [b for b in range(6) if d2.mask[r, b]]
    if not eo or not wo or d2.pos[r, 0] < 0 or d2.pos[r, 1] < 0: continue
    if any(wo.get(b + 1, 0) < 1.0 for b in boats): continue
    combos = [(i, j) for i in boats for j in boats if i != j]
    if any(eo.get(f"{i+1}-{j+1}", 0) < 1.0 for i, j in combos): continue
    q = np.array([1 / wo[b + 1] for b in boats]); q /= q.sum()
    pw = np.array([p1[r, b] for b in boats]); pw /= pw.sum()
    z = bt[0] * np.log(pw) + bt[1] * np.log(q); pcw = np.exp(z - z.max()); pcw /= pcw.sum()
    pcw = dict(zip(boats, pcw))
    om = np.array([eo[f"{i+1}-{j+1}"] for i, j in combos]); qm = 1 / om; qm /= qm.sum()
    pm = np.array([ex[r, i, j] for i, j in combos]); pm /= pm.sum()
    cond2 = np.array([ex[r, i, j] / p1[r, i] for i, j in combos])
    m1 = np.array([pcw[i] for i, j in combos]) * cond2
    pay = {c: a for c, a, _ in R.loc[(d, "07", int(rno)), "pay"].get("ex", [])}
    act = (int(d2.pos[r, 0]), int(d2.pos[r, 1]))
    refund = {b for b in boats if str(fr.get((d, int(rno), b + 1), "")).startswith(("F", "L"))}
    rows.append(dict(date=d, combos=combos, odds=om, q=qm, pm=pm, m1=m1, w=combos.index(act), pay=pay, refund=refund))
print(f"評価対象 {len(rows)}R")
h = len(rows) // 2; A, B = rows[:h], rows[h:]
print(f"前半 {A[0]['date']}〜 / 後半 {B[0]['date']}〜{B[-1]['date']}")

def m2(x, t):
    z = t[0] * np.log(np.maximum(x["pm"], 1e-12)) + t[1] * np.log(x["q"]); z -= z.max(); e = np.exp(z); return e / e.sum()
def nll(t, S): return -np.mean([math.log(max(m2(x, t)[x["w"]], 1e-12)) for x in S])
t2 = minimize(nll, [0.5, 0.5], args=(A,), method="Nelder-Mead").x
ll = lambda f, S: -np.mean([math.log(max(f(x)[x["w"]], 1e-12)) for x in S])
print(f"係数 モデル^{t2[0]:.3f}×市場^{t2[1]:.3f}")
print(f"後半logloss 市場{ll(lambda x: x['q'], B):.4f} / モデル{ll(lambda x: x['pm'], B):.4f} / "
      f"方式1 {ll(lambda x: x['m1'] / x['m1'].sum(), B):.4f} / 方式2 {ll(lambda x: m2(x, t2), B):.4f}")

def ret(x, c):
    i, j = x["combos"][c]
    if i in x["refund"] or j in x["refund"]: return 100
    return x["pay"].get(f"{i+1}-{j+1}", 0)

def strat(S, prob, thr, omax=1e9):
    byr = []
    for x in S:
        p = prob(x); evs = p * x["odds"]
        sel = [c for c in range(len(evs)) if evs[c] >= thr and x["odds"][c] <= omax]
        if sel: byr.append((100 * len(sel), sum(ret(x, c) for c in sel)))
    if not byr: return "0点"
    a = np.array(byr); rnd = random.Random(0); bs = []
    for _ in range(2000):
        s = a[[rnd.randrange(len(a)) for _ in range(len(a))]]; bs.append(s[:, 1].sum() / s[:, 0].sum() * 100)
    return (f"{len(a):>4}R {int(a[:,0].sum()/100):>5}点 回収率{a[:,1].sum()/a[:,0].sum()*100:6.1f}% "
            f"[{np.percentile(bs,2.5):5.1f},{np.percentile(bs,97.5):5.1f}]")

for name, prob in (("方式1", lambda x: x["m1"]), ("方式2", lambda x: m2(x, t2))):
    print(f"■{name}（後半）")
    for thr in (1.1, 1.2, 1.3, 1.5):
        print(f"  EV≥{thr}: {strat(B, prob, thr)}")
    print(f"  EV≥1.2 かつ 50倍以下: {strat(B, prob, 1.2, 50)}")
