"""
ボートレース蒲郡 予想・配信（v5: 2026-10-30〜）

予想: 段階別 Plackett-Luce モデル v5（model_v5.py / model_v5.json）
  ボートレース公式（mbrace）の番組表・競走成績 2021/1〜の蒲郡全レースから、
  「そのレース時点で分かっていた情報だけ」で学習（旧モデルは学習データに
  未来の成績が混入しており、1号艇を大きく過小評価していた）。
  検証期間（学習に未使用）の勝率予想の精度は確定オッズ（市場）を上回る。

買い目: 単勝の「妙味」（2026-10-30〜 記録モード）
  モデル勝率と単勝オッズを組み合わせた勝率 × オッズ ≥ WIN_EV_MIN の艇。
  過去2,950レース（学習に未使用）を確定オッズで検証すると回収率124〜138%。
  ただし本番は締切前のオッズで判断するため、締切までのオッズ変動で目減りする。
  どれだけ目減りするかを測るまでは「記録モード」（実弾非推奨）とし、
  合否は 判定ルール.md の事前登録ルールで決める。
  旧方式（EV狙いの2連単/3連単・上位3艇ボックス）は 2026-10-30 に停止
  （締切前オッズでの選択バイアス・ボックスは回収率約70%と判明）。

取得先（boatrace.jp）:
  racelist   : 出走表・今節成績
  beforeinfo : 展示タイム・気象
  oddstf / odds2tf / odds3t : オッズ
取得先（mbrace）: 当日の番組表、選手の過去成績（player_stats.json を毎日更新）
"""

import argparse
import json
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import mbrace
import model_v5

# パイプ/リダイレクト先がcp932でも絵文字入り出力で落ちないようにする
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

VENUE_CODE = "07"  # 蒲郡
BASE_URL   = "https://www.boatrace.jp"
HEADERS    = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# 単勝の妙味判定: 組み合わせ勝率 × 単勝オッズ がこれ以上の艇（判定ルール.md で事前登録済み。判定期間中は変更しない）
WIN_EV_MIN = 1.10
PAPER_STAKE = 100   # 記録上の1点あたり金額（円）
PAPER_MODE = True   # True の間は「記録モード（実弾非推奨）」と明記して配信する

# 全角→半角数字変換テーブル
FW2HW = str.maketrans("０１２３４５６７８９", "0123456789")


# ─────────────────────────────────────────────
#  ユーティリティ
# ─────────────────────────────────────────────

def _make_session() -> requests.Session:
    """リトライ付き共有セッション（一時的なネットワーク断・5xxに耐える）"""
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.headers.update(HEADERS)
    return s

SESSION = _make_session()

def _vals(cell) -> list[str]:
    return [v.strip() for v in cell.get_text(separator="\n").split("\n") if v.strip()]

def _fetch(url: str) -> BeautifulSoup:
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.content, "html.parser")

def load_webhook_url() -> str:
    """Discord Webhook URL を環境変数 → discord_webhook.txt の順で探す"""
    import os
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        f = Path(__file__).parent / "discord_webhook.txt"
        if f.exists():
            url = f.read_text(encoding="utf-8-sig").strip()
    return url

def fetch_schedule(date: str) -> list[dict]:
    """当日の全レース締切予定時刻を取得。[{"race_no": 1, "締切": "15:28"}, ...]"""
    url = f"{BASE_URL}/owpc/pc/race/racelist?hd={date}&jcd={VENUE_CODE}&rno=1"
    try:
        soup = _fetch(url)
    except Exception as e:
        print(f"  スケジュール取得エラー: {e}")
        return []
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

def _num(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"[\d.]+", s)
    return float(m.group()) if m else None

def _parse_weather(soup: BeautifulSoup) -> dict:
    """beforeinfo ページの div.weather1 セクションから気象・水面データを取得"""
    cond = {"天候": "不明", "気温": None, "風速": 0, "水温": None, "波高": 0}
    for div in soup.find_all("div", class_="weather1_bodyUnit"):
        classes = div.get("class", [])
        data_s  = div.find("span", class_="weather1_bodyUnitLabelData")
        title_s = div.find("span", class_="weather1_bodyUnitLabelTitle")
        if "is-direction" in classes and data_s:
            m = re.search(r"([\d.]+)", data_s.get_text())
            if m:
                cond["気温"] = float(m.group(1))
        elif "is-weather" in classes and title_s:
            cond["天候"] = title_s.get_text(strip=True)
        elif "is-wind" in classes and data_s:
            m = re.search(r"(\d+)", data_s.get_text())
            if m:
                cond["風速"] = int(m.group(1))
        elif "is-waterTemperature" in classes and data_s:
            m = re.search(r"([\d.]+)", data_s.get_text())
            if m:
                cond["水温"] = float(m.group(1))
        elif "is-wave" in classes and data_s:
            m = re.search(r"(\d+)", data_s.get_text())
            if m:
                cond["波高"] = int(m.group(1))
    return cond


# ─────────────────────────────────────────────
#  出走表
# ─────────────────────────────────────────────

def _kosetsu_finishes(first_row) -> list[int]:
    """
    今節成績（今節の各レースの着順）。出走表は1艇4行構成で、
      1行目: 基本情報＋各レースのレース番号 / 2行目: 進入コース / 3行目: ST / 4行目: 着順（全角）
    （2026-10-30 まで1行目のレース番号を着順と誤読していたバグを修正）
    F・L・転覆・失格などは6着扱い、欠場は除外（学習データと同じ定義）。
    """
    tb = first_row.find_parent("tbody")
    trs = tb.find_all("tr", recursive=False) if tb else []
    if len(trs) < 4:
        return []
    out = []
    for td in trs[3].find_all("td"):
        s = td.get_text(strip=True).translate(FW2HW)
        if not s:
            continue
        if s.isdigit() and 1 <= int(s) <= 6:
            out.append(int(s))
        elif "欠" in s:
            continue
        else:
            out.append(6)
    return out


def fetch_racelist(date: str, race_no: int) -> list[dict]:
    """
    出走表から6艇分を取得。
    セル構造:
      [0] 艇番  [1] 写真  [2] 登録番号/級別/名前/支部/年齢/体重（混在）
      [3] FL/L0/平均ST
      [4] 全国  [5] 当地  [6] モーター  [7] ボート
    """
    url = (f"{BASE_URL}/owpc/pc/race/racelist"
           f"?hd={date}&jcd={VENUE_CODE}&rno={race_no}")
    soup = _fetch(url)

    rows = [tr for tr in soup.find_all("tr")
            if tr.find("a", href=re.compile(r"racersearch/profile"))]

    boats = []
    for i, row in enumerate(rows[:6]):
        cells = row.find_all("td")

        info_idx = next(
            (j for j, c in enumerate(cells)
             if re.search(r"\d{4}", c.get_text()) and re.search(r"[AB][12]", c.get_text())),
            None
        )
        if info_idx is None:
            continue

        ic = cells[info_idx]
        link = ic.find("a", href=re.compile(r"toban=\d+"))
        toban = int(re.search(r"toban=(\d+)", link["href"]).group(1)) if link else None
        gm = re.search(r"[AB][12]", ic.get_text())
        grade = gm.group(0) if gm else ""
        name = next(
            (a.get_text(strip=True) for a in ic.find_all("a")
             if a.get_text(strip=True) and not re.match(r"^\d+$", a.get_text(strip=True))),
            ""
        )
        wm = re.search(r"([\d.]+)kg", ic.get_text())
        weight = float(wm.group(1)) if wm else None
        am = re.search(r"(\d+)歳", ic.get_text())
        age = int(am.group(1)) if am else None

        fl_vals = _vals(cells[info_idx + 1])
        fl_count  = int(fl_vals[0].replace("F", "")) if fl_vals and fl_vals[0].startswith("F") else 0
        l0_count  = int(fl_vals[1].replace("L", "")) if len(fl_vals) > 1 and fl_vals[1].startswith("L") else 0
        avg_st    = _num(fl_vals[2]) if len(fl_vals) > 2 else None

        zk = _vals(cells[info_idx + 2])
        tc = _vals(cells[info_idx + 3])
        mt = _vals(cells[info_idx + 4])
        bt = _vals(cells[info_idx + 5])

        boats.append({
            "艇番":         i + 1,
            "登録番号":     toban,
            "選手名":       name,
            "級別":         grade,
            "年齢":         age,
            "体重":         weight,
            "FL回数":       fl_count,
            "L0回数":       l0_count,
            "平均ST":       avg_st,
            "全国勝率":     _num(zk[0]) if zk else None,
            "全国2連率":    _num(zk[1]) if len(zk) > 1 else None,
            "当地勝率":     _num(tc[0]) if tc else None,
            "当地2連率":    _num(tc[1]) if len(tc) > 1 else None,
            "モーター番号": int(mt[0]) if mt and mt[0].isdigit() else None,
            "モーター2連率": _num(mt[1]) if len(mt) > 1 else None,
            "ボート番号":   int(bt[0]) if bt and bt[0].isdigit() else None,
            "ボート2連率":  _num(bt[1]) if len(bt) > 1 else None,
            "今節成績":     _kosetsu_finishes(row),
            "チルト":       None,
            "展示タイム":   None,
            "展示ST":       None,
            "展示F":        False,
            "部品交換":     None,
            "単勝オッズ":   None,
            "番組":         None,   # mbrace 番組表の行（モデル入力はこちらを優先）
            "過去":         {},     # 直近3節・艇番別通算（player_stats.json）
        })

    return boats


# ─────────────────────────────────────────────
#  展示前情報 + 気象データ
# ─────────────────────────────────────────────

def fetch_beforeinfo(date: str, race_no: int) -> tuple[dict[int, dict], dict]:
    """
    Table[1]: 4行/艇 構造
      10セル行 : 艇番/選手名/体重/展示タイム/チルト/プロペラ/部品交換/前走ST
      3セル行  : 調整重量 / 'ST' / 展示ST値
    div.weather1: 気象・水面データ
    """
    url = (f"{BASE_URL}/owpc/pc/race/beforeinfo"
           f"?hd={date}&jcd={VENUE_CODE}&rno={race_no}")
    soup = _fetch(url)
    tables = soup.find_all("table")
    result: dict[int, dict] = {}

    if len(tables) > 1:
        current_bn = None
        for row in tables[1].find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) == 10:
                try:
                    bn    = int(cells[0].get_text(strip=True))
                    parts = cells[7].get_text(strip=True)
                    result[bn] = {
                        "チルト":     _num(cells[5].get_text(strip=True)),
                        "展示タイム": _num(cells[4].get_text(strip=True)),
                        "展示ST":     None,
                        "展示F":      False,
                        "部品交換":   parts if parts else None,
                    }
                    current_bn = bn
                except (ValueError, IndexError):
                    current_bn = None
            elif len(cells) == 3 and current_bn is not None:
                st_raw = cells[2].get_text(strip=True)
                if st_raw:
                    result[current_bn]["展示ST"] = _num(st_raw.replace("F", ""))
                    result[current_bn]["展示F"]  = "F" in st_raw

    return result, _parse_weather(soup)


# ─────────────────────────────────────────────
#  オッズ
# ─────────────────────────────────────────────

def fetch_odds_exacta(date: str, race_no: int) -> dict[str, float]:
    """
    2連単の全30通りのオッズを取得。{"1-2": 9.5, ...}
    ページ構造: 6列（1着艇ごと）× 5行、各セルは (2着艇, オッズ) のペア。
    """
    url = (f"{BASE_URL}/owpc/pc/race/odds2tf"
           f"?hd={date}&jcd={VENUE_CODE}&rno={race_no}")
    try:
        soup = _fetch(url)
    except Exception:
        return {}
    odds: dict[str, float] = {}
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        if len(rows) != 6:
            continue
        ok = True
        for row in rows[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
            if len(cells) != 12:
                ok = False
                break
            for k in range(6):
                sec_s, odd_s = cells[2 * k], cells[2 * k + 1]
                v = _num(odd_s)
                if sec_s.isdigit() and v is not None:
                    odds[f"{k + 1}-{sec_s}"] = v
        if ok and len(odds) >= 25:
            break
        odds = {}
    return odds


def fetch_odds_trifecta(date: str, race_no: int) -> dict[str, float]:
    """
    3連単の全120通りのオッズを取得。{"1-2-3": 13.5, ...}
    ページ構造: 6列（1着艇ごと）× 20行。2着ブロックの先頭行は
    (2着, 3着, オッズ) の3つ組×6列、続く行は (3着, オッズ) のペア×6列。
    """
    url = (f"{BASE_URL}/owpc/pc/race/odds3t"
           f"?hd={date}&jcd={VENUE_CODE}&rno={race_no}")
    try:
        soup = _fetch(url)
    except Exception:
        return {}
    odds: dict[str, float] = {}
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        if len(rows) != 21:
            continue
        seconds = [None] * 6
        for row in rows[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
            if len(cells) == 18:      # 新しい2着ブロックの先頭行
                for k in range(6):
                    sec_s, thr_s, odd_s = cells[3 * k], cells[3 * k + 1], cells[3 * k + 2]
                    v = _num(odd_s)
                    if sec_s.isdigit():
                        seconds[k] = sec_s
                    if sec_s.isdigit() and thr_s.isdigit() and v is not None:
                        odds[f"{k + 1}-{sec_s}-{thr_s}"] = v
            elif len(cells) == 12:    # 同じ2着ブロックの続き
                for k in range(6):
                    thr_s, odd_s = cells[2 * k], cells[2 * k + 1]
                    v = _num(odd_s)
                    if seconds[k] and thr_s.isdigit() and v is not None:
                        odds[f"{k + 1}-{seconds[k]}-{thr_s}"] = v
        if len(odds) >= 100:
            break
        odds = {}
    return odds


def fetch_odds_win(date: str, race_no: int) -> dict[int, float]:
    """単勝オッズを取得。{艇番: オッズ倍率} を返す。取得失敗時は空dict（欠場艇は含まれない）。"""
    url = (f"{BASE_URL}/owpc/pc/race/oddstf"
           f"?hd={date}&jcd={VENUE_CODE}&rno={race_no}")
    try:
        soup = _fetch(url)
    except Exception:
        return {}
    odds: dict[int, float] = {}
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            if len(cells) >= 3:
                try:
                    bn  = int(cells[0].get_text(strip=True))
                    val = float(cells[2].get_text(strip=True))
                    if 1 <= bn <= 6:
                        odds[bn] = val
                except (ValueError, IndexError):
                    pass
        if len(odds) == 6:
            break
    return odds


# ─────────────────────────────────────────────
#  データ統合
# ─────────────────────────────────────────────

_STATE: dict | None = None

def _player_state(date: str) -> dict:
    """選手の過去成績（前日まで）。古ければメモリ上でだけ追いつかせる（保存は Actions の mbrace.py update が担当）"""
    global _STATE
    if _STATE is None:
        _STATE = mbrace.load_state()
        asof = _STATE.get("asof")
        yday = (datetime.strptime(date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        if asof and asof < yday:
            d = datetime.strptime(asof, "%Y%m%d") + timedelta(days=1)
            while d.strftime("%Y%m%d") <= yday:
                raw = mbrace.download("K", d.strftime("%Y%m%d"), use_cache=False)
                if raw:
                    mbrace.update_state(_STATE, mbrace.parse_k(raw, d.strftime("%Y%m%d")))
                d += timedelta(days=1)
    return _STATE


def get_race_data(date: str, race_no: int, quiet: bool = False) -> tuple[list[dict], dict]:
    """1レース分の全データと気象条件を返す"""
    def _p(*a, **kw):
        if not quiet:
            print(*a, **kw)

    _p("  出走表・展示・単勝オッズ・番組表を取得中...", end="", flush=True)
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_rl   = ex.submit(fetch_racelist, date, race_no)
        f_bi   = ex.submit(fetch_beforeinfo, date, race_no)
        f_odds = ex.submit(fetch_odds_win, date, race_no)
        f_prog = ex.submit(mbrace.get_program, date, VENUE_CODE)
    boats = f_rl.result()
    bi, conditions = f_bi.result()
    odds = f_odds.result()
    try:
        program = f_prog.result().get(race_no, {})
    except Exception:
        program = {}
    _p(f" {len(boats)}艇 OK（番組表{'あり' if program else 'なし→出走表の値で代用'}）")

    state = _player_state(date)
    for b in boats:
        info = bi.get(b["艇番"], {})
        for k in ("チルト", "展示タイム", "展示ST", "展示F", "部品交換"):
            if k in info:
                b[k] = info[k]
        b["単勝オッズ"] = odds.get(b["艇番"])
        row = program.get(b["艇番"])
        if row and row["toban"] == b["登録番号"]:
            b["番組"] = row
        b["過去"] = mbrace.player_feats(state, b["登録番号"], b["艇番"], date, VENUE_CODE)
    return boats, conditions


# ─────────────────────────────────────────────
#  予想（モデル v5）
# ─────────────────────────────────────────────

def _model_input(b: dict) -> dict:
    """モデル入力。番組表（学習と同じ公式データ）を優先し、無ければ出走表の値で代用"""
    pg = b.get("番組") or {}
    src = lambda k_pg, k_rl: pg.get(k_pg) if pg.get(k_pg) is not None else b.get(k_rl)
    ks = b.get("今節成績") or []
    x = {
        "lane": b["艇番"],
        "nat_win": src("nat_win", "全国勝率"), "nat2": src("nat2", "全国2連率"),
        "loc_win": src("loc_win", "当地勝率"), "grade": src("grade", "級別"),
        "motor2": src("motor2", "モーター2連率"), "boat2": src("boat2", "ボート2連率"),
        "weight": src("weight", "体重"), "age": src("age", "年齢"),
        "ex_time": b.get("展示タイム"),
        "kos_sum": sum(ks), "kos_cnt": len(ks),
    }
    x.update(b.get("過去") or mbrace.player_feats({}, None, b["艇番"], "", ""))
    return x


def predict(boats: list[dict], conditions: dict | None = None) -> dict:
    """
    返り値:
      ranked   : 予想勝率順の艇リスト（各艇に 予想勝率 / 組合せ勝率 / 期待値 を付与）
      2連単確率 / 3連単確率 : {"i-j": p} / {"i-j-k": p}
      組合せ   : 単勝オッズとの組み合わせができたか
    単勝オッズが4艇以上取れていて、オッズの無い艇は欠場として除外する。
    """
    cond = conditions or {}
    n_odds = sum(1 for b in boats if b.get("単勝オッズ"))
    present = [b for b in boats if n_odds < 4 or b.get("単勝オッズ")]
    fe = model_v5.features([_model_input(b) for b in present], cond.get("風速"), cond.get("波高"))
    p1, ex, tri = model_v5.predict(fe)
    pc = model_v5.combine_win(p1, {b["艇番"]: b.get("単勝オッズ") for b in present})
    ranked = []
    for b in present:
        lane = b["艇番"]
        o = b.get("単勝オッズ")
        c = pc.get(lane) if pc else None
        ranked.append({**b, "予想勝率": p1[lane], "組合せ勝率": c,
                       "期待値": (c * o) if (c is not None and o) else None})
    ranked.sort(key=lambda r: -r["予想勝率"])
    return {"ranked": ranked, "2連単確率": ex, "3連単確率": tri, "組合せ": pc is not None}


def value_bets(pred: dict) -> list[dict]:
    """単勝の妙味（組み合わせ勝率 × オッズ ≥ WIN_EV_MIN）"""
    out = [{"艇番": r["艇番"], "オッズ": r["単勝オッズ"], "勝率": round(r["組合せ勝率"], 4),
            "期待値": round(r["期待値"], 3)}
           for r in pred["ranked"] if r["期待値"] is not None and r["期待値"] >= WIN_EV_MIN]
    return sorted(out, key=lambda x: -x["期待値"])


def confidence(p1: float) -> int:
    """自信度: 5段階（5が最高。◎の予想勝率ベース）"""
    for lv, th in ((5, 0.58), (4, 0.46), (3, 0.36), (2, 0.28)):
        if p1 >= th:
            return lv
    return 1


# ─────────────────────────────────────────────
#  出力ヘルパー
# ─────────────────────────────────────────────

MARKS = ["◎", "○", "▲", "△", "★", "　"]

def _f(val, fmt="{:.2f}", fb="---"):
    return fmt.format(val) if val is not None else fb


def _boat_notes(b: dict, boats: list[dict]) -> list[str]:
    """各艇の根拠メモ（表示用）"""
    parts = []
    pg = b.get("番組") or {}
    if b["級別"] in ("A1", "A2"):
        parts.append(b["級別"])
    tc = pg.get("loc_win", b.get("当地勝率"))
    if tc and tc >= 6.0:
        parts.append(f"当地{tc:.2f}")
    m2 = pg.get("motor2", b.get("モーター2連率"))
    if m2 is not None and m2 >= 45:
        parts.append(f"機{m2:.0f}%")
    vex = [x["展示タイム"] for x in boats if x.get("展示タイム") is not None]
    if vex and b.get("展示タイム") is not None and b["展示タイム"] == min(vex):
        parts.append("展示1位")
    h = b.get("過去") or {}
    if h.get("cl_n", 0) >= 10:
        parts.append(f"{b['艇番']}枠1着{h['cl_win'] / h['cl_n'] * 100:.0f}%")
    ks = b.get("今節成績") or []
    if ks:
        parts.append("今節" + "".join(str(k) for k in ks[-5:]))
    return parts


def _build_discord_msg(pred: dict, conditions: dict, race_label: str,
                       bets: list[dict], deadline: str | None = None) -> str:
    cond  = conditions or {}
    ranked = pred["ranked"]
    conf = confidence(ranked[0]["予想勝率"])
    lines = []
    if bets:
        lines.append(f"💡 **単勝の妙味あり**{'（📝記録モード・実弾は判定待ち）' if PAPER_MODE else ''}")
    stars = "★" * conf + "☆" * (5 - conf)
    dl = f"  締切{deadline}" if deadline else ""
    lines += [
        f"🚤 **【{race_label}】**{dl}  自信度 {conf}/5 {stars}",
        f"📍 {cond.get('天候', '不明')}  風{cond.get('風速', 0) or 0}m  波{cond.get('波高', 0) or 0}cm",
        "```",
    ]
    for i, r in enumerate(ranked[:4]):
        o = r.get("単勝オッズ")
        os_ = f" 単勝{o:.1f}倍" if o else ""
        lines.append(f"{MARKS[i]} {r['艇番']}号艇 {r['選手名'][:6]:<6}({r['級別']}) 勝率{r['予想勝率']*100:.0f}%{os_}")
        notes = _boat_notes(r, ranked)
        if notes:
            lines.append(f"   {' / '.join(notes[:4])}")
    lines.append("```")
    if bets:
        for v in bets:
            lines.append(f"💡 単勝 **{v['艇番']}号艇** {v['オッズ']:.1f}倍（勝率推定{v['勝率']*100:.0f}% → 期待値{v['期待値']:.2f}）")
    ex = sorted(pred["2連単確率"].items(), key=lambda kv: -kv[1])[:3]
    lines.append("📎 2連単の本線（確率順・参考）: " + ", ".join(f"{c}({p*100:.0f}%)" for c, p in ex))
    return "\n".join(lines)


def send_discord(message: str, webhook_url: str) -> bool:
    try:
        r = requests.post(webhook_url, json={"content": message}, timeout=10)
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"  Discord 送信エラー: {e}")
        return False


# ─────────────────────────────────────────────
#  予想ログ
# ─────────────────────────────────────────────

def log_prediction(date: str, race_no: int, pred: dict, bets: list[dict], conditions: dict,
                   exacta_odds: dict, trifecta_odds: dict, sent: bool = False,
                   deadline: str | None = None) -> None:
    """
    予想内容を日付別 jsonl に追記（夜の結果検証はこの記録と答え合わせする）。
    保存先は PREDICTION_LOG_DIR 環境変数で変更可能（GitHub Actions は sent/）。
    オッズは「記録時点」の全組番を残す（締切までのオッズ変動の検証用）。
    """
    import os
    log_dir = Path(__file__).parent / os.environ.get("PREDICTION_LOG_DIR", "logs")
    log_dir.mkdir(exist_ok=True)
    ranked = pred["ranked"]
    rec = {
        "版":       "v5",
        "記録時刻": datetime.now().astimezone().isoformat(timespec="seconds"),
        "日付":     date,
        "レース":   race_no,
        "締切":     deadline,
        "予想順":   [r["艇番"] for r in ranked],
        "予想勝率": {str(r["艇番"]): round(r["予想勝率"], 5) for r in ranked},
        "組合せ勝率": ({str(r["艇番"]): round(r["組合せ勝率"], 5) for r in ranked}
                     if pred["組合せ"] else None),
        "自信度":   confidence(ranked[0]["予想勝率"]),
        "妙味":     bets,
        "送信":     sent,
        "単勝オッズ": {str(r["艇番"]): r["単勝オッズ"] for r in ranked if r.get("単勝オッズ")},
        "2連単オッズ": exacta_odds,
        "3連単オッズ": trifecta_odds,
        "2連単確率": {c: round(p, 5) for c, p in pred["2連単確率"].items()},
        "3連単確率上位": {c: round(p, 5) for c, p in
                       sorted(pred["3連単確率"].items(), key=lambda kv: -kv[1])[:20]},
        "番組表": all(r.get("番組") for r in ranked),
        "気象":     conditions,
    }
    with open(log_dir / f"predictions_{date}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_prediction_log(date: str) -> tuple[dict[int, dict], str | None]:
    """
    日付別の予想記録を読む。({レース番号: 最後の記録}, 読み込み元) を返す。
    読み込み元は "sent"（本番の配信記録）/ "logs"（ローカルのお試し実行記録）/ None（記録なし）。
    """
    for d in ("sent", "logs"):
        f = Path(__file__).parent / d / f"predictions_{date}.jsonl"
        if not f.exists():
            continue
        out: dict[int, dict] = {}
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                out[int(rec["レース"])] = rec
            except (ValueError, KeyError):
                pass
        if out:
            return out, d
    return {}, None


# ─────────────────────────────────────────────
#  1レース実行（表示 + Discord + ログ）
# ─────────────────────────────────────────────

W = 75

def run_race(date: str, race_no: int, do_discord: bool = False,
             detail: bool = True, hot_only: bool = False, deadline: str | None = None) -> bool:
    race_label = f"蒲郡 {date[4:6]}/{date[6:]} {race_no}R"

    print(f"\nデータ取得中... {race_label}")
    try:
        data, cond = get_race_data(date, race_no)
    except Exception as e:
        print(f"データ取得失敗: {e}")
        return False
    if not data:
        print("データ取得失敗（出走表が見つかりません）")
        return False

    pred = predict(data, cond)
    ranked = pred["ranked"]

    print("  2連単・3連単オッズを取得中（記録用）...", end="", flush=True)
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_ex  = ex.submit(fetch_odds_exacta,   date, race_no)
        f_tri = ex.submit(fetch_odds_trifecta, date, race_no)
    exacta_odds, trifecta_odds = f_ex.result(), f_tri.result()
    print(f" OK（2連単{len(exacta_odds)}件 / 3連単{len(trifecta_odds)}件）")

    bets = value_bets(pred)

    print(f'\n{"="*W}')
    print(f"【{race_label}】 天候:{cond.get('天候')} 風速:{cond.get('風速')}m 波高:{cond.get('波高')}cm")
    print(f'{"="*W}')
    print(f"  印  艇  {'選手名':<10} 級  モデル勝率 組合せ勝率  単勝   期待値  展示   根拠")
    for i, r in enumerate(ranked):
        o = r.get("単勝オッズ")
        print(f"  {MARKS[i] if i < len(MARKS) else '　'}  {r['艇番']}  {r['選手名'][:8]:<10} {r['級別']:<3} "
              f"{r['予想勝率']*100:6.1f}%   "
              f"{(r['組合せ勝率'] or 0)*100:6.1f}%  {_f(o, '{:5.1f}'):>5}  "
              f"{_f(r['期待値'], '{:5.2f}'):>5}  {_f(r.get('展示タイム')):>5}  "
              f"{' / '.join(_boat_notes(r, ranked))}")
    if detail:
        ex_top = sorted(pred["2連単確率"].items(), key=lambda kv: -kv[1])[:5]
        tri_top = sorted(pred["3連単確率"].items(), key=lambda kv: -kv[1])[:5]
        print("\n  2連単 確率上位: " + ", ".join(
            f"{c}({p*100:.1f}%{'/' + format(exacta_odds[c], '.1f') + '倍' if c in exacta_odds else ''})" for c, p in ex_top))
        print("  3連単 確率上位: " + ", ".join(
            f"{c}({p*100:.1f}%{'/' + format(trifecta_odds[c], '.1f') + '倍' if c in trifecta_odds else ''})" for c, p in tri_top))
    print()
    if bets:
        for v in bets:
            print(f"  💡 単勝の妙味: {v['艇番']}号艇 {v['オッズ']:.1f}倍  勝率推定{v['勝率']*100:.1f}%  期待値{v['期待値']:.2f}")
    elif not pred["組合せ"]:
        print("  単勝オッズ未取得のため妙味判定なし（発売前など）")
    else:
        print(f"  単勝の妙味なし（期待値{WIN_EV_MIN}以上の艇なし）")
    if PAPER_MODE:
        print("  ※ 記録モード: 締切前オッズでの判断がどれだけ目減りするか検証中（判定ルール.md）")
    print(f'{"="*W}\n')

    sent = False
    if do_discord:
        if hot_only and not bets:
            print("Discord: 単勝の妙味なし → 送信スキップ（記録のみ）")
        else:
            webhook_url = load_webhook_url()
            if not webhook_url:
                print("Discord: webhook URL が見つかりません。")
            else:
                sent = send_discord(_build_discord_msg(pred, cond, race_label, bets, deadline), webhook_url)
                print("  送信完了！" if sent else "  送信失敗。")

    try:
        log_prediction(date, race_no, pred, bets, cond, exacta_odds, trifecta_odds,
                       sent=sent, deadline=deadline)
    except Exception as e:
        print(f"  [警告] 予想ログ記録エラー: {e}")
    return True


# ─────────────────────────────────────────────
#  メイン
# ─────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import date as _date

    parser = argparse.ArgumentParser(description="ボートレース蒲郡 予想ツール（v5）")
    parser.add_argument("date",    nargs="?", default=None, help="日付 YYYYMMDD（省略=今日）")
    parser.add_argument("race_no", nargs="?", type=int, default=None,
                        help="レース番号 1-12（省略=当日全レース）")
    parser.add_argument("--all", action="store_true", help="当日の全レースをまとめて予想")
    parser.add_argument("--discord", action="store_true", help="結果をDiscordに送信")
    parser.add_argument("--hot-only", action="store_true",
                        help="単勝の妙味があるレースだけDiscordに送信")
    parser.add_argument("--deadline", default=None, help="締切時刻（表示・記録用）")
    args = parser.parse_args()

    DATE = args.date or _date.today().strftime("%Y%m%d")

    if args.all or args.race_no is None:
        label = f"{DATE[:4]}/{DATE[4:6]}/{DATE[6:]}"
        print(f"\nボートレース蒲郡  {label}  全レース一括予想")
        schedule = fetch_schedule(DATE)
        race_nos = [r["race_no"] for r in schedule] or list(range(1, 13))
        deadlines = {r["race_no"]: r["締切"] for r in schedule}
        if not schedule:
            print("スケジュール取得失敗。1〜12Rを順に試します。")
        ok_count = 0
        for rno in race_nos:
            ok = run_race(DATE, rno, do_discord=args.discord, detail=False,
                          hot_only=args.hot_only, deadline=deadlines.get(rno))
            if ok:
                ok_count += 1
            elif not schedule:
                break
        print(f"\n{'='*W}")
        print(f"全レース予想完了: {ok_count}/{len(race_nos)}レース")
        if ok_count == 0:
            print("※ 開催日以外はデータがありません。")
            sys.exit(1)
    else:
        ok = run_race(DATE, args.race_no, do_discord=args.discord, detail=True,
                      hot_only=args.hot_only, deadline=args.deadline)
        if not ok:
            sys.exit(1)
