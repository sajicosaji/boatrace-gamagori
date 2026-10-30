"""
オッズのスナップショットを記録する（締切までのオッズ変動の検証用）。
  python snap_odds.py 20261031 5 締切2分前
単勝・2連単の全組番を sent/odds_YYYYMMDD.jsonl（PREDICTION_LOG_DIR）に1行追記する。
夜の結果検証（results.py）が確定オッズも同じファイルに追記し、
「締切何分前に判断すれば妙味が残るか」を判定ルール.md に沿って集計する。
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from gamagori_race import fetch_odds_exacta, fetch_odds_win


def snap(date: str, race_no: int, tag: str) -> dict:
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_w = ex.submit(fetch_odds_win, date, race_no)
        f_e = ex.submit(fetch_odds_exacta, date, race_no)
    return {
        "日付": date, "レース": race_no, "時点": tag,
        "記録時刻": datetime.now().astimezone().isoformat(timespec="seconds"),
        "単勝": {str(k): v for k, v in f_w.result().items()},
        "2連単": f_e.result(),
    }


def append(rec: dict) -> None:
    log_dir = Path(__file__).parent / os.environ.get("PREDICTION_LOG_DIR", "logs")
    log_dir.mkdir(exist_ok=True)
    with open(log_dir / f"odds_{rec['日付']}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    d, r, tag = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    rec = snap(d, r, tag)
    append(rec)
    print(f"  オッズ記録 {r}R {tag}: 単勝{len(rec['単勝'])} / 2連単{len(rec['2連単'])}")
