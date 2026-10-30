"""
GitHub Actions から呼ばれる送信スクリプト（待機ループ方式）。

起動したら MAX_RUNTIME_MIN 分を上限に生存し、その間に各レースの
  ・締切 SNAP_EARLY 分前: オッズだけ記録
  ・締切 AHEAD_MINUTES 分前: 予想・単勝の妙味判定・Discord送信（妙味があるレースのみ）・記録
  ・締切 SNAP_LATE 分前（複数可）: オッズだけ記録
を順番に待って実行する。オッズの記録は「締切何分前に判断すれば妙味が残るか」の
検証用（判定ルール.md）。GitHub の定期実行は大幅に遅延・間引きされるため、
起動タイミングに依存しないこの方式で「実行された回」が生きている間のレースを拾う。

環境変数:
  AHEAD_MINUTES   予想・送信を締切何分前に行うか（デフォルト 5）
  SNAP_EARLY      オッズだけ記録する早い時点（デフォルト "10"）
  SNAP_LATE       オッズだけ記録する遅い時点（デフォルト "2,1"）
  MAX_RUNTIME_MIN この実行が生存する最大分数（デフォルト 25）
  HOT_ONLY        "1" なら単勝の妙味があるレースだけDiscordに送信

時刻は必ず日本時間（JST）で扱う。ランナーは UTC のため datetime.now() を
そのまま使うと締切と9時間ズレる。
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from gamagori_race import fetch_schedule

JST = ZoneInfo("Asia/Tokyo")
HERE = Path(__file__).parent


def _mins(env: str, default: str) -> list[int]:
    return [int(x) for x in os.environ.get(env, default).split(",") if x.strip()]


def main():
    ahead    = int(os.environ.get("AHEAD_MINUTES", "5"))
    early    = _mins("SNAP_EARLY", "10")
    late     = _mins("SNAP_LATE", "2,1")
    max_min  = int(os.environ.get("MAX_RUNTIME_MIN", "25"))
    hot_only = os.environ.get("HOT_ONLY", "0") == "1"

    start    = datetime.now(JST)
    end      = start + timedelta(minutes=max_min)
    date_str = start.strftime("%Y%m%d")

    print(f"ボートレース蒲郡  {date_str}  開始: {start.strftime('%H:%M')} JST"
          f"  最大稼働: {max_min}分  送信: 締切{ahead}分前  オッズ記録: {early + late}分前")

    races = fetch_schedule(date_str)
    if not races:
        print("本日の蒲郡開催なし（または取得失敗）")
        return

    events = []
    for race in races:
        h, m = map(int, race["締切"].split(":"))
        deadline = start.replace(hour=h, minute=m, second=0, microsecond=0)
        for mm in early + late:
            events.append((deadline - timedelta(minutes=mm), "snap", race, f"締切{mm}分前"))
        events.append((deadline - timedelta(minutes=ahead), "send", race, f"締切{ahead}分前"))
    events.sort(key=lambda e: e[0])

    sent, snaps, procs = 0, 0, []
    for at, kind, race, tag in events:
        now = datetime.now(JST)
        if at <= now:
            # 時刻を過ぎた処理はしない（直列実行の次の回が同じレースを再処理しないように）
            continue
        if at > end:
            print(f"  {race['race_no']}R {tag} 以降は稼働時間外 → 次の実行に任せる")
            break
        wait = (at - now).total_seconds()
        if wait > 0:
            print(f"  {race['race_no']}R {tag}（締切{race['締切']}）まで待機 {wait/60:.1f}分...", flush=True)
            time.sleep(wait)
        if kind == "snap":
            # オッズ記録は待たずに裏で走らせる（送信処理とぶつからないように）
            procs.append(subprocess.Popen([sys.executable, str(HERE / "snap_odds.py"),
                                           date_str, str(race["race_no"]), tag]))
            snaps += 1
        else:
            print(f"\n→ {race['race_no']}R を処理 (締切{race['締切']})", flush=True)
            cmd = [sys.executable, str(HERE / "gamagori_race.py"), date_str, str(race["race_no"]),
                   "--discord", "--deadline", race["締切"]]
            if hot_only:
                cmd.append("--hot-only")
            subprocess.run(cmd, check=False)
            sent += 1

    for p in procs:
        try:
            p.wait(timeout=180)
        except subprocess.TimeoutExpired:
            p.kill()
    print(f"\n処理完了: 予想{sent}レース / オッズ記録{snaps}回"
          f"（開始{start.strftime('%H:%M')} → 終了{datetime.now(JST).strftime('%H:%M')} JST）")


if __name__ == "__main__":
    main()
