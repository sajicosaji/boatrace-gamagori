"""
当日の蒲郡ボートレース全レースをDiscordに自動送信

使い方:
  python daily_discord.py              # 締切10分前に自動送信
  python daily_discord.py --ahead 5   # 締切5分前に送信
  python daily_discord.py --date 20260706  # 日付指定
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gamagori_race import fetch_schedule

JST = ZoneInfo("Asia/Tokyo")


def main():
    parser = argparse.ArgumentParser(description="ボートレース蒲郡 全レース自動Discord送信")
    parser.add_argument("--ahead", type=int, default=10,
                        help="締切何分前に送信するか（デフォルト: 10）")
    parser.add_argument("--date", default=None,
                        help="日付 YYYYMMDD（デフォルト: 今日）")
    args = parser.parse_args()

    now      = datetime.now(JST)
    date_str = args.date or now.strftime("%Y%m%d")
    script   = Path(__file__).parent / "gamagori_race.py"
    label    = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"

    print(f"\nボートレース蒲郡  {label}  スケジュール取得中...")
    races = fetch_schedule(date_str)

    if not races:
        print("本日の蒲郡レーススケジュールが取得できませんでした。")
        print("ヒント: 開催日以外はデータがありません。")
        return

    print(f"\n本日 {len(races)}レース を検出:")
    for r in races:
        h, m   = map(int, r["締切"].split(":"))
        dl_dt  = now.replace(hour=h, minute=m, second=0, microsecond=0)
        snd_dt = dl_dt.timestamp() - args.ahead * 60
        status = "（スキップ）" if (snd_dt - now.timestamp()) < -60 else ""
        snd_s  = datetime.fromtimestamp(snd_dt, JST).strftime("%H:%M")
        print(f"  {r['race_no']:2d}R  締切:{r['締切']}  送信予定:{snd_s}{status}")

    print(f"\n締切 {args.ahead}分前 に予測→Discord送信します。")
    print("Ctrl+C で中断できます。\n")

    skipped = 0
    for race in races:
        h, m      = map(int, race["締切"].split(":"))
        dl_dt     = datetime.now(JST).replace(hour=h, minute=m, second=0, microsecond=0)
        send_ts   = dl_dt.timestamp() - args.ahead * 60
        remaining = send_ts - datetime.now(JST).timestamp()

        if remaining < -60:
            print(f"スキップ（時間切れ）: {race['race_no']:2d}R  締切{race['締切']}")
            skipped += 1
            continue

        if remaining > 0:
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            snd_s = datetime.fromtimestamp(send_ts, JST).strftime("%H:%M")
            print(f"待機中...  {race['race_no']:2d}R  締切{race['締切']}  "
                  f"送信予定:{snd_s}  "
                  f"(あと {mins}分{secs:02d}秒)")
            try:
                time.sleep(remaining)
            except KeyboardInterrupt:
                print("\n中断しました。")
                return

        print(f"\n→ 予測送信: {race['race_no']}R  (締切{race['締切']})")
        subprocess.run(
            [sys.executable, str(script), date_str, str(race["race_no"]), "--discord"],
            check=False
        )
        print(f"送信完了\n")

    done = len(races) - skipped
    print(f"本日の処理完了: {done}レース送信 / {skipped}レーススキップ")


if __name__ == "__main__":
    main()
