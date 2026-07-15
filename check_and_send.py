"""
GitHub Actions から呼ばれる送信スクリプト（待機ループ方式）。

起動したら MAX_RUNTIME_MIN 分を上限に生存し、その間に「締切 AHEAD_MINUTES 分前」
を迎えるレースを順番に待って送信する。GitHub の定期実行は大幅に遅延・間引き
されるため、起動タイミングに依存する窓方式では取りこぼす。この方式なら
「実行された回」が生きている間のレースは確実に拾える。

環境変数:
  AHEAD_MINUTES   締切何分前に送信するか（デフォルト 10）
  MAX_RUNTIME_MIN この実行が生存する最大分数（デフォルト 25）
  HOT_ONLY        "1" なら勝負レース（鉄板 or 妙味あり）だけDiscordに送信

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


def main():
    ahead    = int(os.environ.get("AHEAD_MINUTES", "10"))
    max_min  = int(os.environ.get("MAX_RUNTIME_MIN", "25"))
    hot_only = os.environ.get("HOT_ONLY", "0") == "1"

    start    = datetime.now(JST)
    end      = start + timedelta(minutes=max_min)
    date_str = start.strftime("%Y%m%d")
    script   = Path(__file__).parent / "gamagori_race.py"

    print(f"ボートレース蒲郡  {date_str}  開始: {start.strftime('%H:%M')} JST"
          f"  最大稼働: {max_min}分  勝負レースのみ: {'はい' if hot_only else 'いいえ'}")

    races = fetch_schedule(date_str)
    if not races:
        print("本日の蒲郡開催なし（または取得失敗）")
        return

    sent = 0
    for race in races:
        h, m      = map(int, race["締切"].split(":"))
        deadline  = start.replace(hour=h, minute=m, second=0, microsecond=0)
        send_at   = deadline - timedelta(minutes=ahead)
        now       = datetime.now(JST)

        if send_at <= now:
            # 送信時刻を過ぎたレースは扱わない。
            # （猶予を持たせると、直列実行の次の回が同じレースを再送してしまう）
            continue
        if send_at > end:
            print(f"  {race['race_no']}R (締切{race['締切']}) は稼働時間外 → 次の実行に任せる")
            break

        wait = (send_at - now).total_seconds()
        if wait > 0:
            print(f"  {race['race_no']}R (締切{race['締切']}) まで待機 {wait/60:.1f}分...", flush=True)
            time.sleep(wait)

        print(f"\n→ {race['race_no']}R を処理 (締切{race['締切']})", flush=True)
        cmd = [sys.executable, str(script), date_str, str(race["race_no"]), "--discord"]
        if hot_only:
            cmd.append("--hot-only")
        subprocess.run(cmd, check=False)
        sent += 1

    print(f"\n処理完了: {sent}レース処理（開始{start.strftime('%H:%M')} → 終了{datetime.now(JST).strftime('%H:%M')} JST）")


if __name__ == "__main__":
    main()
