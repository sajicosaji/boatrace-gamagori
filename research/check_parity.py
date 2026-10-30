"""本番の model_v5.py（純Python）と学習側 plmodel.py の予測が一致するか確認。
入力は本番と同じ経路（番組表B・player_stats相当・展示タイム）で組み立てる。
使い方: python -P check_parity.py <リポジトリ> <日付>
"""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
REPO, DAY = sys.argv[1], sys.argv[2]
sys.path.insert(0, REPO); sys.path.insert(1, r"C:\Users\dash_\boatrace_data\tools")
import numpy as np, pandas as pd
import model_v5, plmodel as M

mj = model_v5.load()
F = pd.read_pickle(M.ROOT / "feats.pkl")
F = F[(F["jcd"] == "07") & (F["date"] == DAY)].copy()
X = M.base_features(F)
cols = mj["cols"]
d = M.Data(F, X, cols, pd.Series(mj["mu"], index=cols), pd.Series(mj["sd"], index=cols))
th = [np.array(t) for t in mj["theta"]]
p1, ex, tri = M.probs(d, th)
worst = 0.0
for r, rno in enumerate(d.meta["rno"]):
    g = F[F["rno"] == rno]
    boats = []
    for _, b in g.iterrows():
        x = {k: (None if pd.isna(b[k]) else b[k]) for k in ["nat_win", "nat2", "loc_win", "grade", "motor2", "boat2", "weight", "age", "ex_time"]}
        x.update(lane=int(b["lane"]), kos_sum=b["kos_sum"], kos_cnt=b["kos_cnt"])
        for k in ["r3_sum", "r3_cnt", "r3_win", "r3_st", "r3_stn", "r3_f", "cl_n", "cl_win", "cl_t3", "cl_st", "cl_stn"]:
            x[k] = 0 if pd.isna(b[k]) else b[k]
        boats.append(x)
    fe = model_v5.features(boats, g["wind"].iloc[0], g["wave"].iloc[0])
    q1, qex, qtri = model_v5.predict(fe)
    for l, v in q1.items(): worst = max(worst, abs(v - p1[r, l - 1]))
    for c, v in qex.items():
        i, j = map(int, c.split("-")); worst = max(worst, abs(v - ex[r, i - 1, j - 1]))
    for c, v in qtri.items():
        i, j, k = map(int, c.split("-")); worst = max(worst, abs(v - tri[r, i - 1, j - 1, k - 1]))
    if r == 0:
        print(f"{rno}R 本番版 勝率", {l: round(v, 4) for l, v in q1.items()})
        print(f"{rno}R 学習版 勝率", {l + 1: round(float(p1[r, l]), 4) for l in range(6)})
print(f"{len(d.meta)}レース 最大差 {worst:.2e}")
