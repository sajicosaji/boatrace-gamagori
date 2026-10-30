"""モデル×確定単勝オッズの組み合わせ（Benter型）を長期間で検証。
A期間(24/7〜25/6)で組み合わせ係数とモデルを決め、B期間(25/7〜26/10)で一度だけ評価。
※確定オッズで判断した場合の「上限値」。本番は締切前オッズで判断するため、これより下がる。
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
cols = sum((M.GROUPS[g] for g in ["B", "EX", "KOS", "R3", "CL", "WX"]), [])
finraw = {(d, int(r), int(l)): f for d, r, l, f in F[["date", "rno", "lane", "fin_raw"]].itertuples(index=False)}

def build(train_end, ev_start, ev_end):
    tr = F["date"] <= train_end; ev = (F["date"] >= ev_start) & (F["date"] <= ev_end)
    d1 = M.Data(F[tr], X[tr], cols); d2 = M.Data(F[ev], X[ev], cols, d1.mu, d1.sd)
    th = M.fit(d1, 100.0, pairs=True)
    p1, ex, tri = M.probs(d2, th)
    rows = []
    for r, (d, rno) in enumerate(zip(d2.meta["date"], d2.meta["rno"])):
        o = W.get((d, int(rno)))
        if not o or d2.pos[r, 0] < 0: continue
        # 出走艇すべてのオッズが揃っているレースだけ使う（結果に依存した除外をしない）
        boats = [b for b in range(6) if d2.mask[r, b]]
        if any(o.get(b + 1, 0) < 1.0 for b in boats): continue
        odds = np.array([o[b + 1] for b in boats]); q = 1 / odds; q /= q.sum()
        p = p1[r, boats]; p = p / p.sum()
        tan = {c: a for c, a, _ in (R.loc[(d, "07", int(rno)), "pay"].get("tan", []) if (d, "07", int(rno)) in R.index else [])}
        rows.append(dict(date=d, rno=int(rno), boats=boats, p=p, q=q, odds=odds, w=boats.index(d2.pos[r, 0]), tan=tan))
    return rows

A = build("20240630", "20240701", "20250630")
B = build("20250630", "20250701", "20261029")
print(f"A期間 {len(A)}R / B期間 {len(B)}R")

def comb(x, t):
    z = t[0] * np.log(np.maximum(x["p"], 1e-9)) + t[1] * np.log(x["q"]); z = z - z.max(); e = np.exp(z); return e / e.sum()
def nll(t, S): return -sum(math.log(max(comb(x, t)[x["w"]], 1e-12)) for x in S) / len(S)
t = minimize(nll, [1.0, 1.0], args=(A,), method="Nelder-Mead").x
tq = minimize(lambda a: nll([0, a[0]], A), [1.0], method="Nelder-Mead").x[0]
print(f"A期間で決めた係数: モデル^{t[0]:.3f} × 市場^{t[1]:.3f}（市場だけ鋭くする場合 市場^{tq:.3f}）")
for name, S in (("A", A), ("B", B)):
    print(f"  {name} logloss  市場{nll([0,1],S):.4f}  市場^{tq:.2f} {nll([0,tq],S):.4f}  モデル単独{nll([1,0],S):.4f}  組合せ{nll(t,S):.4f}")

def ret_of(x, k):
    b = x["boats"][k] + 1
    fr = finraw.get((x["date"], x["rno"], b), "")
    if fr.startswith(("F", "L", "K")): return 100  # 返還
    return x["tan"].get(str(b), 0)

def boot(rows, n=3000):
    rnd = random.Random(0); a = np.array(rows); m = len(a); out = []
    if m == 0: return (0, 0)
    for _ in range(n):
        s = a[np.array([rnd.randrange(m) for _ in range(m)])]; out.append(s[:, 1].sum() / s[:, 0].sum() * 100)
    out.sort(); return out[int(n * .025)], out[int(n * .975)]

def strat(S, thr, prob, omin=1.0, omax=999, label=""):
    rows = []; byr = []
    for x in S:
        pc = prob(x); c = r_ = 0
        for k in range(len(x["boats"])):
            if pc[k] * x["odds"][k] >= thr and omin <= x["odds"][k] < omax:
                c += 100; r_ += ret_of(x, k)
        if c: byr.append((c, r_))
    if not byr: return f"{label} 0点"
    c = sum(a for a, _ in byr); rt = sum(b for _, b in byr); lo, hi = boot(byr)
    return f"{label} {len(byr):>4}R {c//100:>4}点 回収率{rt/c*100:6.1f}% [{lo:5.1f},{hi:5.1f}]"

print("\n■単勝: 組合せ確率×確定オッズ ≥ 閾値（B期間、1レース複数点あり）")
for thr in (1.0, 1.05, 1.1, 1.2, 1.3, 1.5):
    print("  " + strat(B, thr, lambda x: comb(x, t), label=f"EV≥{thr:<4}"))
print("■比較: 市場を鋭くしただけ（モデル無し）でEV判定")
for thr in (1.0, 1.1, 1.2):
    print("  " + strat(B, thr, lambda x: comb(x, [0, tq]), label=f"EV≥{thr:<4}"))
print("■オッズ帯別（組合せEV≥1.1, B期間）")
for lo_, hi_ in ((1, 2), (2, 4), (4, 8), (8, 20), (20, 999)):
    print("  " + strat(B, 1.1, lambda x: comb(x, t), lo_, hi_, label=f"{lo_}〜{hi_}倍"))
print("■B期間を前後半に分けて（組合せEV≥1.1）")
h = len(B) // 2
print("  " + strat(B[:h], 1.1, lambda x: comb(x, t), label=f"前半{B[0]['date']}〜"))
print("  " + strat(B[h:], 1.1, lambda x: comb(x, t), label=f"後半{B[h]['date']}〜"))
pickle.dump(dict(t=t, tq=tq), open(M.ROOT / "benter_win.pkl", "wb"))
