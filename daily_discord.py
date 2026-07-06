"""
当日の蒲郡ボートレース全レースをDiscordに自動送信

使い方:
  python daily_discord.py              # 締切10分前に自動送信
  python daily_discord.py --ahead 5   # 締切5分前に送信
  python daily_discord.py --date 20260706  # 日付指定
"""
import argparse
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

VENUE_CODE = "07"
BASE_URL   = "https://www.boatrace.jp"
HEADERS    = {"User-Agent": (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)}


def get_race_schedule(date_str: str) -> list[dict]:
    """
    racelist ページから当日の全レース締切予定時刻を取得。
    テーブル構造:
      行0: レース | 1R | 2R | ... | 12R
      行1: 締切予定時刻 | HH:MM | ...
    """
    url = f"{BASE_URL}/owpc/pc/race/racelist?hd={date_str}&jcd={VENUE_CODE}&rno=1"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"  スケジュール取得エラー: {e}")
        return []

    soup  = BeautifulSoup(r.content, "html.parser")
    races = []

    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue
        header = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if header and header[0] == "レース" and "1R" in header:
            times = [c.get_text(strip=True) for c in rows[1].find_all(["th", "td"])][1:]
            for i, t in enumerate(times):
                if re.match(r"\d{1,2}:\d{2}", t):
                    races.append({"race_no": i + 1, "締切": t})
            break

    return races


def main():
    parser = argparse.ArgumentParser(description="ボートレース蒲郡 全レース自動Discord送信")
    parser.add_argument("--ahead", type=int, default=10,
                        help="締切何分前に送信するか（デフォルト: 10）")
    parser.add_argument("--date", default=None,
                        help="日付 YYYYMMDD（デフォルト: 今日）")
    args = parser.parse_args()

    date_str = args.date or date.today().strftime("%Y%m%d")
    script   = Path(__file__).parent / "gamagori_race.py"
    label    = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"

    print(f"\nボートレース蒲郡  {label}  スケジュール取得中...")
    races = get_race_schedule(date_str)

    if not races:
        print("本日の蒲郡レーススケジュールが取得できませんでした。")
        print("ヒント: 開催日以外はデータがありません。")
        return

    now = datetime.now()

    print(f"\n本日 {len(races)}レース を検出:")
    for r in races:
        h, m   = map(int, r["締切"].split(":"))
        dl_dt  = now.replace(hour=h, minute=m, second=0, microsecond=0)
        snd_dt = dl_dt - timedelta(minutes=args.ahead)
        status = "（スキップ）" if (snd_dt - now).total_seconds() < -60 else ""
        print(f"  {r['race_no']:2d}R  締切:{r['締切']}  送信予定:{snd_dt.strftime('%H:%M')}{status}")

    print(f"\n締切 {args.ahead}分前 に予測→Discord送信します。")
    print("Ctrl+C で中断できます。\n")

    skipped = 0
    for race in races:
        h, m      = map(int, race["締切"].split(":"))
        dl_dt     = now.replace(hour=h, minute=m, second=0, microsecond=0)
        send_dt   = dl_dt - timedelta(minutes=args.ahead)
        remaining = (send_dt - datetime.now()).total_seconds()

        if remaining < -60:
            print(f"スキップ（時間切れ）: {race['race_no']:2d}R  締切{race['締切']}")
            skipped += 1
            continue

        if remaining > 0:
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            print(f"待機中...  {race['race_no']:2d}R  締切{race['締切']}  "
                  f"送信予定:{send_dt.strftime('%H:%M')}  "
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
