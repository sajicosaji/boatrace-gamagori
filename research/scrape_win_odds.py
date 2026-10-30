"""蒲郡の過去レースの確定単勝オッズを boatrace.jp から取得（取得済みはスキップ、時間予算で打ち切り）
使い方: python -P scrape_win_odds.py 20240701 20261029 [予算秒] [並列数]
"""
import sys, time, pickle
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
sys.path.insert(0, r"C:\Users\dash_\OneDrive\Desktop\デスクトップ整理_2026-10-04\プロジェクト・開発\競艇_蒲郡")
import gamagori_race as G

OUT = Path(r"C:\Users\dash_\boatrace_data\win_odds_07.pkl")

def main():
    d0, d1 = sys.argv[1], sys.argv[2]
    budget = float(sys.argv[3]) if len(sys.argv) > 3 else 500
    workers = int(sys.argv[4]) if len(sys.argv) > 4 else 16
    F = pd.read_pickle(r"C:\Users\dash_\boatrace_data\feats.pkl")
    races = sorted(set(map(tuple, F[(F["jcd"] == "07") & (F["date"] >= d0) & (F["date"] <= d1)][["date", "rno"]]
                           .drop_duplicates().itertuples(index=False))))
    got = pickle.load(open(OUT, "rb")) if OUT.exists() else {}
    todo = [(d, int(r)) for d, r in races if (d, int(r)) not in got]
    print(f"対象 {len(races)}R / 未取得 {len(todo)}R", flush=True)
    t0 = time.time(); ok = ng = 0; it = iter(todo); running = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        while True:
            while len(running) < workers and time.time() - t0 < budget:
                nxt = next(it, None)
                if nxt is None: break
                running[ex.submit(G.fetch_odds_win, *nxt)] = nxt
            if not running: break
            fut = next(as_completed(running)); key = running.pop(fut)
            try:
                o = fut.result()
                if o: got[key] = o; ok += 1
                else: ng += 1
            except Exception:
                ng += 1
    pickle.dump(got, open(OUT, "wb"))
    print(f"取得 {ok} / 失敗 {ng} / 累計 {len(got)} / {time.time()-t0:.0f}秒", flush=True)

if __name__ == "__main__":
    main()
