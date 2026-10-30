"""player_stats.json の初期作成（ローカルの boats.pkl から、本番と同じ update_state で再生）と、
学習用特徴量(feats.pkl)との一致確認。
使い方: python -P build_state.py <リポジトリのパス> <最終日YYYYMMDD>
"""
import sys, io, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
REPO, UNTIL = sys.argv[1], sys.argv[2]
sys.path.insert(0, REPO)
import pandas as pd
import mbrace

B = pd.read_pickle(r"C:\Users\dash_\boatrace_data\boats.pkl")
B = B[B["date"] <= UNTIL]
t0 = time.time()
cols = ["date", "jcd", "rno", "lane", "toban", "fin_raw", "finish", "st_raw"]
rows = [dict(zip(cols, r)) for r in B[cols].itertuples(index=False)]
for r in rows:
    r["finish"] = int(r["finish"]) if r["finish"] == r["finish"] and r["finish"] is not None else None
last = UNTIL
state = {"asof": None, "p": {}}
before = [r for r in rows if r["date"] < last]
mbrace.update_state(state, before)
print(f"{last}の前日まで再生 {len(before)}行 {time.time()-t0:.0f}秒")

# 一致確認: 最終日の蒲郡レースについて、前日までの状態から作った値 vs feats.pkl
F = pd.read_pickle(r"C:\Users\dash_\boatrace_data\feats.pkl")
chk = F[(F["date"] == last) & (F["jcd"] == "07")]
keys = ["r3_sum", "r3_cnt", "r3_win", "r3_st", "r3_stn", "r3_f", "cl_n", "cl_win", "cl_t3", "cl_st", "cl_stn"]
bad = 0
for _, r in chk.iterrows():
    pf = mbrace.player_feats(state, int(r["toban"]), int(r["lane"]), last, "07")
    for k in keys:
        a, b = pf[k], (0 if pd.isna(r[k]) else r[k])
        if abs(a - b) > 1e-6:
            bad += 1
            if bad <= 15:
                print(f"  不一致 {r['rno']}R {r['lane']}号艇 {r['toban']} {k}: 本番{a} 学習{b}")
print(f"一致確認 {len(chk)}艇 × {len(keys)}項目 → 不一致 {bad}")

mbrace.update_state(state, [r for r in rows if r["date"] == last])
state["asof"] = last
mbrace.save_state(state)
print(f"保存 asof={last} 選手{len(state['p'])}人 {mbrace.STATE_FILE.stat().st_size/1e6:.2f}MB")
