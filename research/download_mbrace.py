"""mbrace（ボートレース公式）の競走成績(K)・番組表(B)の日次LZHを取得する。
既に取得済みのファイルはスキップ。時間予算を超えたら新規投入を止めて終了（再実行で続きから）。
使い方: python -I download_mbrace.py 20210101 20261029 [予算秒]
"""
import sys, time
from datetime import date, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter

ROOT = Path(r"C:\Users\dash_\boatrace_data\raw")
BASE = "https://www1.mbrace.or.jp/od2"

def targets(d0, d1):
    d = d0
    while d <= d1:
        for kind in ("K", "B"):
            yy, mm, dd = d.strftime("%y"), d.strftime("%m"), d.strftime("%d")
            name = f"{kind.lower()}{yy}{mm}{dd}.lzh"
            url = f"{BASE}/{kind}/{d.strftime('%Y%m')}/{name}"
            yield url, ROOT / kind / name
        d += timedelta(days=1)

def main():
    d0 = date(int(sys.argv[1][:4]), int(sys.argv[1][4:6]), int(sys.argv[1][6:]))
    d1 = date(int(sys.argv[2][:4]), int(sys.argv[2][4:6]), int(sys.argv[2][6:]))
    budget = float(sys.argv[3]) if len(sys.argv) > 3 else 480
    (ROOT / "K").mkdir(parents=True, exist_ok=True); (ROOT / "B").mkdir(parents=True, exist_ok=True)
    miss_file = ROOT / "missing.txt"
    missing = set(miss_file.read_text().split()) if miss_file.exists() else set()
    todo = [(u, p) for u, p in targets(d0, d1) if not p.exists() and p.name not in missing]
    print(f"未取得 {len(todo)} 件", flush=True)
    s = requests.Session()
    s.mount("https://", HTTPAdapter(pool_connections=32, pool_maxsize=32))
    t0 = time.time(); ok = ng = 0; new_missing = []

    def get(u, p):
        r = s.get(u, timeout=60)
        if r.status_code == 404:
            return p, None
        r.raise_for_status()
        if r.content[2:5] != b"-lh":  # LZHヘッダ確認（HTMLのエラーページ等を弾く）
            return p, None
        return p, r.content

    it = iter(todo); running = {}
    with ThreadPoolExecutor(max_workers=24) as ex:
        while True:
            while len(running) < 24 and time.time() - t0 < budget:
                nxt = next(it, None)
                if nxt is None: break
                running[ex.submit(get, *nxt)] = nxt
            if not running: break
            done = next(as_completed(running))
            u, p = running.pop(done)
            try:
                p2, content = done.result()
                if content is None:
                    new_missing.append(p.name); ng += 1
                else:
                    tmp = p.with_suffix(".part"); tmp.write_bytes(content); tmp.replace(p); ok += 1
            except Exception as e:
                ng += 1
    if new_missing:
        with open(miss_file, "a") as f: f.write("\n".join(new_missing) + "\n")
    rest = sum(1 for u, p in targets(d0, d1) if not p.exists()) - len(missing) - len(new_missing)
    print(f"取得 {ok} / 欠番・失敗 {ng} / 残り {rest} / {time.time()-t0:.0f}秒", flush=True)

if __name__ == "__main__":
    main()
