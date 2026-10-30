"""
ボートレース蒲郡 結果検証（v5 記録モード対応・2026-10-30〜）

その日の送信記録（sent/predictions_日付.jsonl）と実際の結果を照合する。
  ・◎の1着的中
  ・単勝の妙味（締切5分前に判断した買い目）の記録上の収支（1点100円）
  ・同じ買い目を「確定オッズ」で見直した期待値（締切までのオッズ変動でどれだけ目減りしたか）
  ・締切10分前/2分前/1分前のオッズで判断していたら（sent/odds_日付.jsonl のスナップショット）
を集計し、判定ルール.md の事前登録ルールに対する進捗を出す。
日別の集計は sent/eval_日付.json に保存し、累計はそこから計算する。

使い方:
  python results.py                      # 今日
  python results.py 20261031            # 指定日
  python results.py 20261031 20261105   # 期間（日ごとに検証）
  python results.py --discord           # サマリーをDiscordに送信
"""

import argparse
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import model_v5
from gamagori_race import (
    BASE_URL, VENUE_CODE, FW2HW, WIN_EV_MIN, PAPER_STAKE,
    _fetch, fetch_schedule, fetch_odds_win, fetch_odds_exacta,
    load_prediction_log, load_webhook_url, send_discord,
)

JST = ZoneInfo("Asia/Tokyo")
HERE = Path(__file__).parent
# 書き込み先（Actions は sent/、ローカル実行は logs/ に書いて本番記録を汚さない）。読むときは両方見る
LOG_DIR = HERE / os.environ.get("PREDICTION_LOG_DIR", "logs")
READ_DIRS = [LOG_DIR] + ([HERE / "sent"] if LOG_DIR != HERE / "sent" else [])
FW_RANK = {"１": 1, "２": 2, "３": 3, "４": 4, "５": 5, "６": 6}

# 判定ルール.md（事前登録・判定期間中は変更しない）
RULE_START = "20261031"
RULE_PICKS = 300          # 判定に必要な記録点数（締切5分前判断の単勝妙味）
RULE_PASS_EV = 1.05       # 確定オッズで見直した期待値の平均がこれ以上
RULE_PASS_ROI = 95.0      # かつ記録上の回収率がこれ以上（%）
RULE_FAIL_EV = 1.00       # 確定オッズで見直した期待値の平均がこれ未満なら不合格
RULE_MID = 100            # 中間チェックの点数
RULE_MID_FAIL_EV = 0.95   # 中間チェックで確定期待値がこれ未満なら早期に不合格
SEND_TAG = "締切5分前"
SNAP_TAGS = ["締切10分前", "締切2分前", "締切1分前"]


# ─────────────────────────────────────────────
#  結果取得
# ─────────────────────────────────────────────

def fetch_race_result(date: str, race_no: int) -> dict | None:
    """
    raceresult ページから着順・払戻金・決まり手・返還艇を取得。未確定・非開催なら None。
      {"着順": [5, 1, 6, 2], "返還艇": [3], "決まり手": "まくり差し",
       "払戻金": {"3連単": {"5-1-6": 7380}, "単勝": {"5": 1200}, ...}}
    返還艇 = フライング・出遅れ・欠場（その艇の舟券は返還される）
    """
    url = (f"{BASE_URL}/owpc/pc/race/raceresult"
           f"?hd={date}&jcd={VENUE_CODE}&rno={race_no}")
    try:
        soup = _fetch(url)
    except Exception:
        return None
    tables = soup.find_all("table")
    if len(tables) < 4:
        return None

    result_table = None
    for t in tables:
        rows = t.find_all("tr")
        if not rows:
            continue
        header = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if header[:2] == ["着", "枠"]:
            result_table = t
            break
    if result_table is None:
        return None

    ranks, refund = [], []
    for row in result_table.find_all("tr")[1:]:
        cells = row.find_all(["th", "td"])
        if len(cells) < 3:
            continue
        rank_s = cells[0].get_text(strip=True)
        lane_s = cells[1].get_text(strip=True).translate(FW2HW)
        if not lane_s.isdigit():
            continue
        rank = FW_RANK.get(rank_s)
        if rank is None:
            hw = rank_s.translate(FW2HW)
            rank = int(hw) if hw.isdigit() and 1 <= int(hw) <= 6 else None
        if rank is not None:
            ranks.append((rank, int(lane_s)))
        elif any(m in rank_s for m in ("Ｆ", "F", "Ｌ", "L", "欠")):
            refund.append(int(lane_s))
    ranks.sort()
    finish = [lane for _, lane in ranks]
    if not finish:
        return None

    payouts: dict[str, dict[str, int]] = {}
    for t in tables:
        rows = t.find_all("tr")
        header = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])] if rows else []
        if not header or header[0] != "勝式":
            continue
        current_kind = None
        for row in rows[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
            if not cells or not any(cells):
                continue
            if len(cells) >= 4 and cells[0]:
                current_kind = cells[0]
                combo_s, amount_s = cells[1], cells[2]
            elif len(cells) >= 2 and current_kind:
                combo_s, amount_s = cells[0], cells[1]
            else:
                continue
            combo = combo_s.replace(" ", "").replace("　", "")
            m = re.search(r"([\d,]+)", amount_s.replace("¥", ""))
            if combo and m:
                payouts.setdefault(current_kind, {})[combo] = int(m.group(1).replace(",", ""))
        break

    kimarite = ""
    for t in tables:
        rows = t.find_all("tr")
        header = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])] if rows else []
        if header and header[0] == "決まり手":
            if len(rows) > 1:
                kimarite = rows[1].get_text(strip=True)
            break

    return {"着順": finish, "返還艇": refund, "決まり手": kimarite, "払戻金": payouts}


# ─────────────────────────────────────────────
#  オッズ記録（スナップショット・確定）
# ─────────────────────────────────────────────

def load_odds_snaps(date: str) -> dict[int, dict[str, dict]]:
    """{レース: {時点: {"単勝": {...}, "2連単": {...}}}}"""
    out: dict[int, dict[str, dict]] = {}
    for f in [d / f"odds_{date}.jsonl" for d in READ_DIRS]:
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                out.setdefault(int(r["レース"]), {})[r["時点"]] = r
            except (ValueError, KeyError):
                pass
    return out


def record_final_odds(date: str, race_no: int, snaps: dict) -> dict:
    """確定オッズ（単勝・2連単）を取得して記録ファイルに追記（取得済みなら再取得しない）"""
    if "確定" in snaps.get(race_no, {}):
        return snaps[race_no]["確定"]
    rec = {"日付": date, "レース": race_no, "時点": "確定",
           "記録時刻": datetime.now(JST).isoformat(timespec="seconds"),
           "単勝": {str(k): v for k, v in fetch_odds_win(date, race_no).items()},
           "2連単": fetch_odds_exacta(date, race_no)}
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / f"odds_{date}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    snaps.setdefault(race_no, {})["確定"] = rec
    return rec


# ─────────────────────────────────────────────
#  1レースの評価
# ─────────────────────────────────────────────

def _picks_at(p1: dict[int, float], win_odds: dict[str, float]) -> list[dict]:
    """そのオッズで判断した場合の単勝妙味（本番と同じ判定式）"""
    odds = {int(k): float(v) for k, v in (win_odds or {}).items()}
    pc = model_v5.combine_win(p1, odds)
    if not pc:
        return []
    return [{"艇番": l, "オッズ": odds[l], "勝率": pc[l], "期待値": pc[l] * odds[l]}
            for l in p1 if pc[l] * odds[l] >= WIN_EV_MIN]


def _settle(picks: list[dict], result: dict, final_win: dict[str, float], p1: dict[int, float]) -> dict:
    """記録上の収支（1点100円）と、確定オッズで見直した期待値"""
    tan = result["払戻金"].get("単勝", {})
    winner = result["着順"][0]
    fodds = {int(k): float(v) for k, v in (final_win or {}).items()}
    pcf = model_v5.combine_win(p1, fodds) if fodds else None
    cost = ret = hits = 0
    ev_final = []
    for v in picks:
        l = int(v["艇番"])
        cost += PAPER_STAKE
        if l in result["返還艇"]:
            ret += PAPER_STAKE
        elif l == winner:
            ret += tan.get(str(l), 0) * PAPER_STAKE // 100
            hits += 1
        if pcf and l in fodds:
            ev_final.append(pcf[l] * fodds[l])
    return {"点数": len(picks), "的中": hits, "投資": cost, "払戻": ret,
            "判断時期待値": [round(v["期待値"], 3) for v in picks],
            "確定期待値": [round(x, 3) for x in ev_final],
            "判断時オッズ": [v["オッズ"] for v in picks],
            "確定オッズ": [fodds.get(int(v["艇番"])) for v in picks]}


def evaluate_race(date: str, rno: int, rec: dict | None, snaps: dict) -> dict | None:
    result = fetch_race_result(date, rno)
    if result is None:
        return None
    out = {"レース": rno, "着順": result["着順"], "決まり手": result["決まり手"],
           "単勝払戻": result["払戻金"].get("単勝", {}), "v5": False}
    if not rec:
        return out
    yosou = rec.get("予想順") or []
    out["本命"] = yosou[0] if yosou else None
    out["本命的中"] = bool(yosou) and yosou[0] == result["着順"][0]
    if rec.get("版") != "v5":
        return out
    out["v5"] = True
    p1 = {int(k): float(v) for k, v in rec["予想勝率"].items()}
    final = record_final_odds(date, rno, snaps)
    out["送信"] = rec.get("送信", False)
    out[SEND_TAG] = _settle(rec.get("妙味") or [], result, final["単勝"], p1)
    out[SEND_TAG]["艇番"] = [v["艇番"] for v in rec.get("妙味") or []]
    for tag in SNAP_TAGS:
        s = snaps.get(rno, {}).get(tag)
        if s:
            picks = _picks_at(p1, s["単勝"])
            out[tag] = _settle(picks, result, final["単勝"], p1)
            out[tag]["艇番"] = [v["艇番"] for v in picks]
    return out


# ─────────────────────────────────────────────
#  集計
# ─────────────────────────────────────────────

def _agg(evals: list[dict], tag: str) -> dict:
    a = {"点数": 0, "的中": 0, "投資": 0, "払戻": 0, "判断時期待値": [], "確定期待値": [], "レース": 0}
    for e in evals:
        s = e.get(tag)
        if not s:
            continue
        a["レース"] += 1
        for k in ("点数", "的中", "投資", "払戻"):
            a[k] += s[k]
        a["判断時期待値"] += s["判断時期待値"]
        a["確定期待値"] += s["確定期待値"]
    return a


def _fmt_agg(name: str, a: dict) -> str:
    if not a["点数"]:
        return f"{name}: 0点"
    roi = a["払戻"] / a["投資"] * 100 if a["投資"] else 0
    e0 = sum(a["判断時期待値"]) / len(a["判断時期待値"]) if a["判断時期待値"] else 0
    e1 = sum(a["確定期待値"]) / len(a["確定期待値"]) if a["確定期待値"] else 0
    return (f"{name}: {a['点数']}点 的中{a['的中']} 回収率{roi:.0f}%({a['払戻'] - a['投資']:+,}円) "
            f"期待値 判断時{e0:.2f}→確定{e1:.2f}")


def save_eval(date: str, evals: list[dict]) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    (LOG_DIR / f"eval_{date}.json").write_text(
        json.dumps({"日付": date, "レース": evals}, ensure_ascii=False, indent=1), encoding="utf-8")


def cumulative() -> dict[str, dict]:
    evals = []
    files = {}
    for d in reversed(READ_DIRS):  # 同じ日付は書き込み先（LOG_DIR）を優先
        for f in d.glob("eval_*.json"):
            files[f.stem] = f
    for stem, f in sorted(files.items()):
        if stem.split("_")[1] < RULE_START:
            continue
        try:
            evals += [e for e in json.loads(f.read_text(encoding="utf-8"))["レース"] if e.get("v5")]
        except (ValueError, KeyError):
            pass
    return {tag: _agg(evals, tag) for tag in [SEND_TAG] + SNAP_TAGS}


def judgment(c: dict) -> str:
    a = c[SEND_TAG]
    n = a["点数"]
    e1 = sum(a["確定期待値"]) / max(len(a["確定期待値"]), 1)
    if RULE_MID <= n < RULE_PICKS and e1 < RULE_MID_FAIL_EV:
        return f"❌ 中間チェックで不合格（{n}点・確定期待値{e1:.2f}<{RULE_MID_FAIL_EV}）→ 判定ルール.md の次の手順へ"
    if n < RULE_PICKS:
        return f"判定まで あと{RULE_PICKS - n}点（{n}/{RULE_PICKS}）"
    roi = a["払戻"] / a["投資"] * 100 if a["投資"] else 0
    if e1 >= RULE_PASS_EV and roi >= RULE_PASS_ROI:
        return f"✅ 合格（確定期待値{e1:.2f}≥{RULE_PASS_EV} かつ 回収率{roi:.0f}%≥{RULE_PASS_ROI:.0f}%）→ 少額の実弾を提案"
    if e1 < RULE_FAIL_EV:
        return f"❌ 不合格（確定期待値{e1:.2f}<{RULE_FAIL_EV}）→ 判定ルール.md の次の手順へ"
    return f"⏸ 保留（確定期待値{e1:.2f} / 回収率{roi:.0f}%）→ 判定ルール.md の次の手順へ"


def summarize_day(date: str, verbose: bool = True) -> tuple[list[dict], str] | None:
    label = f"{date[4:6]}/{date[6:]}"
    schedule = fetch_schedule(date)
    race_nos = [r["race_no"] for r in schedule] or list(range(1, 13))
    log, _ = load_prediction_log(date)
    snaps = load_odds_snaps(date)
    evals = []
    for rno in race_nos:
        if verbose:
            print(f"  {rno}R を検証中...", flush=True)
        ev = evaluate_race(date, rno, log.get(rno), snaps)
        if ev:
            evals.append(ev)
        elif not schedule:
            break
    if not evals:
        return None
    save_eval(date, evals)

    pred = [e for e in evals if e.get("本命") is not None]
    hit = sum(1 for e in pred if e["本命的中"])
    v5 = [e for e in evals if e.get("v5")]
    lines = [f"📊 **【蒲郡 {label} 結果】** {len(evals)}レース  ◎1着 {hit}/{len(pred)}"
             + (f"（{hit / len(pred) * 100:.0f}%）" if pred else "")]
    if v5:
        lines.append("```")
        lines.append(_fmt_agg(f"💡単勝妙味({SEND_TAG}判断)", _agg(v5, SEND_TAG)))
        for tag in SNAP_TAGS:
            a = _agg(v5, tag)
            if a["レース"]:
                lines.append("  " + _fmt_agg(f"{tag}判断なら", a))
        lines.append("```")
        for e in v5:
            s = e.get(SEND_TAG) or {}
            if not s.get("点数"):
                continue
            res = "-".join(map(str, e["着順"][:3]))
            mark = "🎯" if s["的中"] else "❌"
            od = " ".join(f"{b}号艇{o0:.1f}→{o1 if o1 is None else format(o1, '.1f')}倍"
                          for b, o0, o1 in zip(s["艇番"], s["判断時オッズ"], s["確定オッズ"]))
            lines.append(f"{mark} {e['レース']:2d}R 単勝{od}  結果{res}  ({s['払戻'] - s['投資']:+,}円)")
        c = cumulative()
        lines.append(f"📈 **累計（{RULE_START[4:6]}/{RULE_START[6:]}〜・記録モード）**")
        lines.append("```")
        lines.append(_fmt_agg(SEND_TAG, c[SEND_TAG]))
        for tag in SNAP_TAGS:
            if c[tag]["レース"]:
                lines.append(_fmt_agg(tag, c[tag]))
        lines.append(judgment(c))
        lines.append("```")
    else:
        for e in evals:
            lines.append(f"　 {e['レース']:2d}R 結果{'-'.join(map(str, e['着順'][:3]))}")
    return evals, "\n".join(lines)


# ─────────────────────────────────────────────
#  メイン
# ─────────────────────────────────────────────

def _daterange(start: str, end: str):
    d0 = datetime.strptime(start, "%Y%m%d").date()
    d1 = datetime.strptime(end, "%Y%m%d").date()
    d = d0
    while d <= d1:
        yield d.strftime("%Y%m%d")
        d += timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(description="ボートレース蒲郡 結果検証（v5）")
    parser.add_argument("start", nargs="?", default=None, help="日付 YYYYMMDD（省略=今日）")
    parser.add_argument("end",   nargs="?", default=None, help="終了日")
    parser.add_argument("--discord", action="store_true", help="サマリーをDiscordに送信")
    args = parser.parse_args()

    start = args.start or datetime.now(JST).strftime("%Y%m%d")
    end = args.end or start
    summaries = []
    for d in _daterange(start, end):
        print(f"\n===== {d[:4]}/{d[4:6]}/{d[6:]} =====")
        out = summarize_day(d)
        if out is None:
            print("  開催なし（またはレース未確定）")
            continue
        summaries.append(out[1])
        print()
        print(out[1])

    if not summaries:
        print("\n検証できたレースがありません（非開催日、または結果未確定）。")
        return
    if args.discord:
        webhook = load_webhook_url()
        if not webhook:
            print("Discord: webhook URL が見つかりません。")
        else:
            ok = all(send_discord(s[:1990], webhook) for s in summaries)
            print("  送信完了！" if ok else "  送信失敗。")


if __name__ == "__main__":
    main()
