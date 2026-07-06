"""
GitHub Actions から呼ばれる1回実行スクリプト。
締切時刻が「今から ahead〜ahead+interval 分後」のレースだけ送信する。

例: ahead=10, interval=15 の場合
  → 締切まで10〜25分のレースを送信
  → GitHub Actions で15分おきに実行すれば各レースを1回だけ送信できる
"""
import os
import re
import subprocess
import sys
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
    url = f"{BASE_URL}/owpc/pc/race/racelist?hd={date_str}&jcd={VENUE_CODE}&rno=1"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"スケジュール取得エラー: {e}")
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
    # GitHub Actions では環境変数で設定
    ahead    = int(os.environ.get("AHEAD_MINUTES", "10"))
    interval = int(os.environ.get("CHECK_INTERVAL", "15"))
    date_str = date.today().strftime("%Y%m%d")
    now      = datetime.now()
    script   = Path(__file__).parent / "gamagori_race.py"

    print(f"ボートレース蒲郡  {date_str}  チェック時刻: {now.strftime('%H:%M')}")
    print(f"送信対象: 締切まで {ahead}〜{ahead + interval}分 のレース")

    races = get_race_schedule(date_str)
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
