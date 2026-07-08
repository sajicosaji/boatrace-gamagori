"""
GitHub Actions から呼ばれる1回実行スクリプト。
締切時刻が「今から ahead〜ahead+interval 分後」のレースだけ送信する。

例: ahead=10, interval=15 の場合
  → 締切まで10〜25分のレースを送信
  → GitHub Actions で15分おきに実行すれば各レースを1回だけ送信できる

時刻は必ず日本時間（JST）で扱う。GitHub Actions のランナーは UTC のため、
datetime.now() をそのまま使うと締切と9時間ズレて一切送信されない。
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gamagori_race import fetch_schedule

JST = ZoneInfo("Asia/Tokyo")


def main():
    # GitHub Actions では環境変数で設定
    ahead    = int(os.environ.get("AHEAD_MINUTES", "10"))
    interval = int(os.environ.get("CHECK_INTERVAL", "15"))
    now      = datetime.now(JST)
    date_str = now.strftime("%Y%m%d")
    script   = Path(__file__).parent / "gamagori_race.py"

    print(f"ボートレース蒲郡  {date_str}  チェック時刻: {now.strftime('%H:%M')} JST")
    print(f"送信対象: 締切まで {ahead}〜{ahead + interval}分 のレース")

    races = fetch_schedule(date_str)
    if not races:
        print("本日の蒲郡開催なし（または取得失敗）")
        return

    sent = 0
    for race in races:
        h, m  = map(int, race["締切"].split(":"))
        dl_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        mins_to_deadline = (dl_dt - now).total_seconds() / 60

        if ahead <= mins_to_deadline < ahead + interval:
            print(f"\n→ {race['race_no']}R を送信 (締切{race['締切']} / あと{mins_to_deadline:.0f}分)")
            subprocess.run(
                [sys.executable, str(script), date_str, str(race["race_no"]), "--discord"],
                check=False
            )
            sent += 1

    if sent == 0:
        print("  このタイミングで送信対象のレースなし")
    else:
        print(f"\n{sent}レース送信完了")


if __name__ == "__main__":
    main()
