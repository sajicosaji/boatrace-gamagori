"""
ボートレース蒲郡 結果検証・バックテストツール

予想（gamagori_race.py と同じロジック）と実際のレース結果を照合し、
的中率と回収率（100円/点 で買った場合）を集計する。

使い方:
  python results.py                      # 今日の全レースを検証
  python results.py 20260706            # 指定日の全レースを検証
  python results.py 20260701 20260706   # 期間バックテスト
  python results.py 20260706 --discord  # 検証結果をDiscordに送信

注意:
  展示・オッズ・結果はレース後もサイトに残るため、過去日でも検証可能。
  ただし選手統計（コース別/直近3節/現在期）は「現在の値」なので、
  過去日に遡るほど当時の予想とズレが生じる（直近数日なら実用上ほぼ同一）。
"""

import argparse
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

from gamagori_race import (
    BASE_URL, VENUE_CODE, FW2HW,
    _fetch, fetch_schedule, get_race_data, predict, recommend_bets,
    fetch_odds_exacta, fetch_odds_trifecta, is_hot_race,
    load_prediction_log, load_webhook_url, send_discord,
)

FW_RANK = {"１": 1, "２": 2, "３": 3, "４": 4, "５": 5, "６": 6}


# ─────────────────────────────────────────────
#  結果取得
# ─────────────────────────────────────────────

def fetch_race_result(date: str, race_no: int) -> dict | None:
    """
    raceresult ページから着順・払戻金・決まり手を取得。
    レース未確定・非開催日は None を返す。

    返り値例:
      {
        "着順":   [5, 1, 6, 2],          # 完走艇の枠番（1着から順）
        "決まり手": "まくり差し",
        "払戻金": {"3連単": {"5-1-6": 7380}, "2連単": {"5-1": 3000}, ...},
      }
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

    # ── 着順テーブル（ヘッダー: 着/枠/ボートレーサー/レースタイム）──
    finish: list[int] = []
    result_table = None
    for t in tables:
        header = [c.get_text(strip=True) for c in t.find_all("tr")[0].find_all(["th", "td"])]
        if header[:2] == ["着", "枠"]:
            result_table = t
            break
    if result_table is None:
        return None

    ranks: list[tuple[int, int]] = []
    for row in result_table.find_all("tr")[1:]:
        cells = row.find_all(["th", "td"])
        if len(cells) < 3:
            continue
        rank_s = cells[0].get_text(strip=True)
        lane_s = cells[1].get_text(strip=True)
        rank = FW_RANK.get(rank_s)
        if rank is None:
            hw = rank_s.translate(FW2HW)
            rank = int(hw) if hw.isdigit() and 1 <= int(hw) <= 6 else None
        if rank is not None and lane_s.isdigit():
            ranks.append((rank, int(lane_s)))
    ranks.sort()
    finish = [lane for _, lane in ranks]
    if not finish:
        return None  # 全艇異常などレース不成立

    # ── 払戻金テーブル（勝式/組番/払戻金/人気）──
    payouts: dict[str, dict[str, int]] = {}
    payout_table = None
    for t in tables:
        header = [c.get_text(strip=True) for c in t.find_all("tr")[0].find_all(["th", "td"])]
        if header and header[0] == "勝式":
            payout_table = t
            break

    if payout_table is not None:
        current_kind = None
        for row in payout_table.find_all("tr")[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
            if not cells or not any(cells):
                continue
            # 4セル行: [勝式, 組番, 払戻金, 人気] / 3セル行: 同一勝式の続き
            if len(cells) >= 4 and cells[0]:
                current_kind = cells[0]
                combo_s, amount_s = cells[1], cells[2]
            elif len(cells) >= 2 and current_kind:
                combo_s, amount_s = cells[0], cells[1]
            else:
                continue

            combo = combo_s.replace(" ", "").replace("　", "")
            m = re.search(r"([\d,]+)", amount_s.replace("¥", ""))
            if not combo or not m:
                continue
            amount = int(m.group(1).replace(",", ""))
            payouts.setdefault(current_kind, {})[combo] = amount

    # ── 決まり手 ──
    kimarite = ""
    for t in tables:
        header = [c.get_text(strip=True) for c in t.find_all("tr")[0].find_all(["th", "td"])]
        if header and header[0] == "決まり手":
            rows = t.find_all("tr")
            if len(rows) > 1:
                kimarite = rows[1].get_text(strip=True)
            break

    return {"着順": finish, "決まり手": kimarite, "払戻金": payouts}


# ─────────────────────────────────────────────
#  1レースの照合
# ─────────────────────────────────────────────

def _evaluate_common(result: dict, race_no: int, conf, hot: bool,
                     honmei, yosou_jun: list,
                     ex_bets: list[str], tri_bets: list[str]) -> dict:
    """着順・払戻と買い目リストを突き合わせて収支を出す共通部"""
    finish  = result["着順"]
    winner  = finish[0] if finish else None
    ex_hit_combo  = f"{finish[0]}-{finish[1]}" if len(finish) >= 2 else None
    tri_hit_combo = f"{finish[0]}-{finish[1]}-{finish[2]}" if len(finish) >= 3 else None

    ex_hit  = ex_hit_combo in ex_bets if ex_hit_combo else False
    tri_hit = tri_hit_combo in tri_bets if tri_hit_combo else False

    pay_ex  = result["払戻金"].get("2連単", {})
    pay_tri = result["払戻金"].get("3連単", {})

    return {
        "レース":    race_no,
        "自信度":    conf,
        "勝負":      hot,
        "本命":      honmei,
        "予想順":    yosou_jun,
        "着順":      finish,
        "決まり手":  result["決まり手"],
        "本命的中":  honmei == winner if honmei else False,
        "2連単的中": ex_hit,
        "3連単的中": tri_hit,
        "2連単買い目": ex_bets,
        "3連単買い目": tri_bets,
        "2連単収支": (100 * len(ex_bets), pay_ex.get(ex_hit_combo, 0) if ex_hit else 0),
        "3連単収支": (100 * len(tri_bets), pay_tri.get(tri_hit_combo, 0) if tri_hit else 0),
    }


def evaluate_race_from_log(date: str, race_no: int, rec: dict) -> dict | None:
    """送信記録（実際に配信した買い目）と結果を照合する。追加の予想計算はしない"""
    result = fetch_race_result(date, race_no)
    if result is None:
        return None
    hot = bool(rec.get("送信"))  # 実際に配信したレースだけを「勝負」扱いにする
    ex_bets  = [b["組番"] for b in rec.get("2連単", [])] if hot else []
    tri_bets = [b["組番"] for b in rec.get("3連単", [])] if hot else []
    yosou = rec.get("予想順", [])
    return _evaluate_common(result, race_no, rec.get("自信度"), hot,
                            yosou[0] if yosou else None, yosou, ex_bets, tri_bets)


def evaluate_race(date: str, race_no: int, quiet: bool = True) -> dict | None:
    """予想を再計算し、結果と照合して的中・収支を返す（記録が無い日の検証用）"""
    result = fetch_race_result(date, race_no)
    if result is None:
        return None

    try:
        boats, conditions = get_race_data(date, race_no, quiet=quiet)
    except Exception as e:
        print(f"  {race_no}R: 予想データ取得失敗 ({e})")
        return None
    if not boats:
        return None

    ranked = predict(boats, conditions)
    bets   = recommend_bets(ranked,
                            fetch_odds_exacta(date, race_no),
                            fetch_odds_trifecta(date, race_no))

    return _evaluate_common(result, race_no, bets["自信度"], is_hot_race(bets),
                            ranked[0]["艇番"], [r["艇番"] for r in ranked],
                            [b["組番"] for b in bets["2連単"]],
                            [b["組番"] for b in bets["3連単"]])


# ─────────────────────────────────────────────
#  日次サマリー
# ─────────────────────────────────────────────

def summarize_day(date: str, verbose: bool = True) -> tuple[list[dict], str] | None:
    """
    当日全レースを照合し、(各レース結果, サマリー文字列) を返す。
    送信記録（sent/ または logs/ の predictions_日付.jsonl）があれば
    「実際に配信した買い目」と答え合わせし、無ければ予想を再計算して検証する。
    """
    label = f"{date[:4]}/{date[4:6]}/{date[6:]}"
    schedule = fetch_schedule(date)
    race_nos = [r["race_no"] for r in schedule] or list(range(1, 13))

    log = load_prediction_log(date)
    # 実際に配信した記録が1件も無い日は、ローカルのお試し実行記録に引きずられず再計算検証する
    if log and not any(r.get("送信") for r in log.values()):
        log = {}
    if verbose and log:
        print(f"  送信記録 {len(log)}レース分を使用（実配信ベースの検証）")

    evals = []
    for rno in race_nos:
        if verbose:
            print(f"  {rno}R を検証中...", flush=True)
        if log:
            rec = log.get(rno)
            if rec:
                ev = evaluate_race_from_log(date, rno, rec)
            else:
                # 記録が無いレース（稼働の谷間など）は結果だけ載せる
                result = fetch_race_result(date, rno)
                ev = (_evaluate_common(result, rno, None, False, None, [], [], [])
                      if result else None)
        else:
            ev = evaluate_race(date, rno)
        if ev:
            evals.append(ev)
        elif not schedule:
            break

    if not evals:
        return None

    n        = len(evals)
    bet_ev   = [e for e in evals if e["勝負"]]
    nb       = len(bet_ev)
    pred_ev  = [e for e in evals if e["本命"] is not None]
    np_      = max(len(pred_ev), 1)
    win_n    = sum(1 for e in pred_ev if e["本命的中"])
    ex_n     = sum(1 for e in bet_ev if e["2連単的中"])
    tri_n    = sum(1 for e in bet_ev if e["3連単的中"])
    ex_cost  = sum(e["2連単収支"][0] for e in evals)
    ex_ret   = sum(e["2連単収支"][1] for e in evals)
    tri_cost = sum(e["3連単収支"][0] for e in evals)
    tri_ret  = sum(e["3連単収支"][1] for e in evals)

    def roi(ret, cost):
        return ret / cost * 100 if cost else 0.0

    lines = [
        f"📊 **【蒲郡 {label} 結果検証】** ({n}レース / 🔥勝負{nb} / 見送り{n-nb})",
        "```",
        f"◎1着的中 : {win_n}/{len(pred_ev)}  ({win_n/np_*100:.0f}%)",
        f"2連単的中: {ex_n}回  回収率 {roi(ex_ret, ex_cost):.0f}%  ({ex_ret:,}円/{ex_cost:,}円)",
        f"3連単的中: {tri_n}回  回収率 {roi(tri_ret, tri_cost):.0f}%  ({tri_ret:,}円/{tri_cost:,}円)",
        f"合計収支 : {'+' if ex_ret+tri_ret >= ex_cost+tri_cost else ''}{ex_ret+tri_ret-ex_cost-tri_cost:,}円"
        f"  (回収率 {roi(ex_ret+tri_ret, ex_cost+tri_cost):.0f}%)",
        "```",
    ]

    hit_lines = []
    for e in evals:
        finish_s = "-".join(map(str, e["着順"][:3]))
        conf_s = f"{e['自信度']}/5" if isinstance(e["自信度"], int) else "-"
        if not e["勝負"]:
            hit_lines.append(f"　 {e['レース']:2d}R [見送り] 結果{finish_s}")
            continue
        cost = e["2連単収支"][0] + e["3連単収支"][0]
        gain = e["2連単収支"][1] + e["3連単収支"][1]
        marks = []
        if e["2連単的中"]: marks.append(f"2単¥{e['2連単収支'][1]:,}")
        if e["3連単的中"]: marks.append(f"3単¥{e['3連単収支'][1]:,}")
        if marks:
            hit_lines.append(f"🎯 {e['レース']:2d}R [🔥{conf_s}] 結果{finish_s}  {' / '.join(marks)}  ({gain-cost:+,}円)")
        else:
            hit_lines.append(f"❌ {e['レース']:2d}R [🔥{conf_s}] 結果{finish_s}  外れ ({-cost:,}円)")
    lines.extend(hit_lines)

    return evals, "\n".join(lines)


# ─────────────────────────────────────────────
#  メイン
# ─────────────────────────────────────────────

def _daterange(start: str, end: str):
    d0 = datetime.strptime(start, "%Y%m%d").date()
    d1 = datetime.strptime(end, "%Y%m%d").date()
    d  = d0
    while d <= d1:
        yield d.strftime("%Y%m%d")
        d += timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(description="ボートレース蒲郡 結果検証・バックテスト")
    parser.add_argument("start", nargs="?", default=None, help="日付 YYYYMMDD（省略=今日）")
    parser.add_argument("end",   nargs="?", default=None, help="終了日（期間バックテスト時）")
    parser.add_argument("--discord", action="store_true", help="サマリーをDiscordに送信")
    args = parser.parse_args()

    start = args.start or datetime.now(JST).strftime("%Y%m%d")
    end   = args.end or start

    all_evals = []
    summaries = []
    for d in _daterange(start, end):
        print(f"\n===== {d[:4]}/{d[4:6]}/{d[6:]} =====")
        out = summarize_day(d)
        if out is None:
            print("  開催なし（またはレース未確定）")
            continue
        evals, summary = out
        all_evals.extend(evals)
        summaries.append(summary)
        print()
        print(summary)

    if not all_evals:
        print("\n検証できたレースがありません。")
        sys.exit(1)

    # 期間合計（複数日のとき）
    if len(summaries) > 1:
        n        = len(all_evals)
        win_n    = sum(1 for e in all_evals if e["本命的中"])
        ex_n     = sum(1 for e in all_evals if e["2連単的中"])
        tri_n    = sum(1 for e in all_evals if e["3連単的中"])
        cost     = sum(e["2連単収支"][0] + e["3連単収支"][0] for e in all_evals)
        ret      = sum(e["2連単収支"][1] + e["3連単収支"][1] for e in all_evals)
        print(f"\n{'='*60}")
        print(f"【期間合計】 {n}レース")
        print(f"  ◎1着 {win_n/n*100:.0f}% / 2連単 {ex_n/n*100:.0f}% / 3連単 {tri_n/n*100:.0f}%")
        print(f"  総回収率 {ret/cost*100:.0f}%  ({ret:,}円/{cost:,}円  収支{ret-cost:+,}円)")
        print(f"{'='*60}")

    if args.discord and summaries:
        webhook = load_webhook_url()
        if not webhook:
            print("Discord: webhook URL が見つかりません。")
        else:
            print("\nDiscord に送信中...")
            ok = all(send_discord(s, webhook) for s in summaries)
            print("  送信完了！" if ok else "  送信失敗。")


if __name__ == "__main__":
    main()
