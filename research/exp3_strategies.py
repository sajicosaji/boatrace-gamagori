"""オッズを見ない買い方の回収率（実際の確定払戻で計算）。
学習期間で作ったモデルを、学習に使っていない期間に当てる。
使い方: python -P exp3_strategies.py [valid|test]
"""
import sys, io, math, random, itertools
sys.path.insert(0, r"C:\Users\dash_\boatrace_data\tools")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import numpy as np, pandas as pd
import plmodel as M

PHASE = sys.argv[1] if len(sys.argv) > 1 else "valid"
F = pd.read_pickle(M.ROOT / "feats.pkl")
R = pd.read_pickle(M.ROOT / "races.pkl").set_index(["date", "jcd", "rno"])
F = F[(F["jcd"] == "07") & (F["date"] >= "20210101")].copy()
X = M.base_features(F)
if PHASE == "valid":
    tr = F["date"] <= "20240630"; ev = (F["date"] > "20240630") & (F["date"] <= "20250630")
else:
    tr = F["date"] <= "20250630"; ev = F["date"] > "20250630"
cols = sum((M.GROUPS[g] for g in ["B", "EX", "KOS", "R3", "CL", "WX"]), [])
dtr = M.Data(F[tr], X[tr], cols)
dev = M.Data(F[ev], X[ev], cols, dtr.mu, dtr.sd)
th = M.fit(dtr, 100.0, pairs=True)
p1, ex, tri = M.probs(dev, th)
n = len(dev.pos)
print(f"[{PHASE}] 学習{len(dtr.pos)}R → 評価{n}R ({dev.meta['date'].min()}〜{dev.meta['date'].max()})")

pays = []
for d, j, r in zip(dev.meta["date"], dev.meta["jcd"], dev.meta["rno"]):
    pays.append(R.loc[(d, j, r), "pay"] if (d, j, r) in R.index else {})

def payout(rr, kind, combo):
    for c, amt, _ in pays[rr].get(kind, []):
        if c == combo:
            return amt
    return 0

def boot(rows, B=2000):
    rnd = random.Random(0); m = len(rows); out = []
    by = np.array(rows)
    for _ in range(B):
        s = by[np.array([rnd.randrange(m) for _ in range(m)])]
        out.append(s[:, 1].sum() / max(s[:, 0].sum(), 1) * 100)
    out.sort(); return out[int(B * .025)], out[int(B * .975)]

def report(name, picks):
    """picks: レースごとの [(kind, combo), ...]（100円ずつ）"""
    rows = []
    for rr, lst in enumerate(picks):
        if not lst: continue
        cost = 100 * len(lst); ret = sum(payout(rr, k, c) for k, c in lst)
        rows.append((cost, ret, 1 if ret > 0 else 0))
    if not rows:
        print(f"  {name:<34} 参戦0"); return
    c = sum(x[0] for x in rows); rt = sum(x[1] for x in rows)
    lo, hi = boot([(a, b) for a, b, _ in rows])
    print(f"  {name:<34} 参戦{len(rows):>5}R 的中率{np.mean([x[2] for x in rows])*100:5.1f}% "
          f"回収率{rt/c*100:6.1f}% [{lo:5.1f},{hi:5.1f}]", flush=True)

nm = lambda *b: "-".join(str(x + 1) for x in b)
order1 = np.argsort(-p1, 1)
top = p1.max(1)

print("■単勝（◎＝モデル1位）")
report("◎ 全レース", [[("tan", nm(order1[r, 0]))] for r in range(n)])
for lo_, hi_ in [(0, .4), (.4, .5), (.5, .6), (.6, .7), (.7, 1.01)]:
    report(f"◎ 勝率{lo_:.0%}〜{hi_:.0%}", [[("tan", nm(order1[r, 0]))] if lo_ <= top[r] < hi_ else [] for r in range(n)])
report("◎が1号艇", [[("tan", nm(0))] if order1[r, 0] == 0 else [] for r in range(n)])
report("◎が2〜6号艇", [[("tan", nm(order1[r, 0]))] if order1[r, 0] != 0 else [] for r in range(n)])
report("1号艇 全レース(比較)", [[("tan", "1")] for r in range(n)])

print("■2連単")
exf = ex.reshape(n, 36); oex = np.argsort(-exf, 1)
for k in (1, 2, 3, 5):
    report(f"確率上位{k}点", [[("ex", nm(*divmod(c, 6)))] for r in range(n) for c in [None]] if False else
           [[("ex", nm(*divmod(int(c), 6))) for c in oex[r, :k]] for r in range(n)])
report("上位3艇ボックス(6点)", [[("ex", nm(a, b)) for a, b in itertools.permutations(order1[r, :3], 2)] for r in range(n)])
for lo_ in (.5, .6):
    report(f"上位3点 ◎勝率{lo_:.0%}以上", [[("ex", nm(*divmod(int(c), 6))) for c in oex[r, :3]] if top[r] >= lo_ else [] for r in range(n)])

print("■3連単")
trf = tri.reshape(n, 216); otr = np.argsort(-trf, 1)
for k in (1, 3, 5, 10, 20):
    report(f"確率上位{k}点", [[("tri", nm(*np.unravel_index(int(c), (6, 6, 6))))for c in otr[r, :k]] for r in range(n)])
report("上位3艇ボックス(6点)", [[("tri", nm(*c)) for c in itertools.permutations(order1[r, :3], 3)] for r in range(n)])
report("◎1着固定-上位2〜4艇(6点)", [[("tri", nm(order1[r, 0], b, c)) for b, c in itertools.permutations(order1[r, 1:4], 2)] for r in range(n)])
for lo_ in (.5, .6):
    report(f"上位5点 ◎勝率{lo_:.0%}以上", [[("tri", nm(*np.unravel_index(int(c), (6, 6, 6)))) for c in otr[r, :5]] if top[r] >= lo_ else [] for r in range(n)])
