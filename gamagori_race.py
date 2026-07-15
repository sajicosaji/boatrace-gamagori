"""
ボートレース蒲郡 1レース分データ取得 + 予想
取得先:
  racelist   : 基本情報・FL/L0・モーター・今節成績
  beforeinfo : チルト・展示タイム・展示ST・気象水面データ
  /course    : コース別通算成績（進入率/3連対率/ST平均/スタート順）
  /back3     : 直近3節の着順
  /season    : 現在期（約半年）の総合成績

予想: 13要素スコアリング + 気象補正 + 市場オッズブレンド
買い目: Plackett-Luce モデルで 2連単/3連単 の的中確率を計算し、
        レースの自信度（鉄板/有力/接戦/混戦）に応じて点数を可変にする。
"""

import argparse
import json
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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

# 蒲郡の艇番別1着率（過去統計ベース）
LANE_WIN_RATE = {1: 0.555, 2: 0.105, 3: 0.100, 4: 0.085, 5: 0.085, 6: 0.070}

# 市場オッズを予想勝率にブレンドする比率（0で無効）
ODDS_BLEND = 0.25

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

def fetch_racelist(date: str, race_no: int) -> list[dict]:
    """
    出走表から6艇分を取得。
    セル構造:
      [0] 艇番  [1] 写真  [2] 登録番号/級別/名前/体重（混在）
      [3] FL/L0/平均ST
      [4] 全国  [5] 当地  [6] モーター  [7] ボート  [8+] 今節成績
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

        # 登録番号（toban）
        link = ic.find("a", href=re.compile(r"toban=\d+"))
        toban = int(re.search(r"toban=(\d+)", link["href"]).group(1)) if link else None

        # 級別
        gm = re.search(r"[AB][12]", ic.get_text())
        grade = gm.group(0) if gm else ""

        # 選手名
        name = next(
            (a.get_text(strip=True) for a in ic.find_all("a")
             if a.get_text(strip=True) and not re.match(r"^\d+$", a.get_text(strip=True))),
            ""
        )

        # 体重
        wm = re.search(r"([\d.]+)kg", ic.get_text())
        weight = float(wm.group(1)) if wm else None

        # FL/L0/平均ST
        fl_vals = _vals(cells[info_idx + 1])
        fl_count  = int(fl_vals[0].replace("F", "")) if fl_vals and fl_vals[0].startswith("F") else 0
        l0_count  = int(fl_vals[1].replace("L", "")) if len(fl_vals) > 1 and fl_vals[1].startswith("L") else 0
        avg_st    = float(fl_vals[2]) if len(fl_vals) > 2 else None

        # 全国 / 当地 / モーター / ボート
        zk = _vals(cells[info_idx + 2])
        tc = _vals(cells[info_idx + 3])
        mt = _vals(cells[info_idx + 4])
        bt = _vals(cells[info_idx + 5])

        # 今節成績（1〜6着順のみ）
        kosetsu = [int(cells[j].get_text(strip=True))
                   for j in range(info_idx + 6, len(cells))
                   if cells[j].get_text(strip=True).isdigit()
                   and 1 <= int(cells[j].get_text(strip=True)) <= 6]

        boats.append({
            "艇番":         i + 1,
            "登録番号":     toban,
            "選手名":       name,
            "級別":         grade,
            "体重":         weight,
            "FL回数":       fl_count,
            "L0回数":       l0_count,
            "平均ST":       avg_st,
            "全国勝率":     float(zk[0]) if zk else None,
            "全国2連率":    float(zk[1]) if len(zk) > 1 else None,
            "当地勝率":     float(tc[0]) if tc else None,
            "当地2連率":    float(tc[1]) if len(tc) > 1 else None,
            "モーター番号": int(mt[0])   if mt else None,
            "モーター2連率": float(mt[1]) if len(mt) > 1 else None,
            "ボート番号":   int(bt[0])   if bt else None,
            "ボート2連率":  float(bt[1]) if len(bt) > 1 else None,
            "今節成績":     kosetsu,
            "チルト":       None,
            "展示タイム":   None,
            "展示ST":       None,
            "展示F":        False,
            "部品交換":     None,
            "調整重量":     None,
            "単勝オッズ":   None,
            "コース別":     {},
            "直近3節":      {},
            "期別成績":     {},
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
    Table[2]: スタート展示 ST（コース順。1行1セル "1.10" "2F.08" 形式）
    div.weather1: 気象・水面データ

    進入コース: 並びはCSS描画のためテキスト取得不可。艇番=コース番号で代用。
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
                        "調整重量":   None,
                    }
                    current_bn = bn
                except (ValueError, IndexError):
                    current_bn = None
            elif len(cells) == 3 and current_bn is not None:
                # [調整重量値, 'ST', 展示ST値]
                adj_raw = cells[0].get_text(strip=True)
                st_raw  = cells[2].get_text(strip=True)
                adj = _num(adj_raw)
                if adj is not None and result[current_bn]["調整重量"] is None:
                    result[current_bn]["調整重量"] = adj
                if st_raw:
                    is_fly = "F" in st_raw
                    st_val = _num(st_raw.replace("F", ""))
                    result[current_bn]["展示ST"] = st_val
                    result[current_bn]["展示F"]  = is_fly

    # Table[2] スタート展示ST（コース順）— 艇番=コース番号として適用
    if len(tables) > 2:
        for row in tables[2].find_all("tr"):
            cells = row.find_all(["th", "td"])
            course, st_val, is_fly = None, None, False
            if len(cells) >= 3:
                try:
                    course  = int(cells[0].get_text(strip=True))
                    st_text = cells[2].get_text(strip=True)
                    is_fly  = "F" in st_text
                    st_val  = _num(st_text.replace("F", ""))
                except (ValueError, IndexError):
                    pass
            elif len(cells) == 1:
                text = cells[0].get_text(strip=True)
                # "1.10" "2F.08" 形式（コース番号＋[F]＋ST）
                m = re.match(r'^(\d)(F?)\.(\d{2})$', text)
                if m:
                    course = int(m.group(1))
                    is_fly = bool(m.group(2))
                    st_val = float(f"0.{m.group(3)}")
                else:
                    lines = [l.strip() for l in
                             cells[0].get_text(separator="\n", strip=True).split("\n") if l.strip()]
                    if len(lines) >= 2:
                        try:
                            course  = int(lines[0])
                            st_text = lines[-1]
                            is_fly  = "F" in st_text
                            st_val  = _num(st_text.replace("F", ""))
                        except ValueError:
                            pass

            if course and 1 <= course <= 6:
                bn = course  # 艇番=コース番号で代用
                if bn not in result:
                    result[bn] = {"チルト": None, "展示タイム": None, "展示ST": None,
                                  "展示F": False, "部品交換": None, "調整重量": None}
                if result[bn]["展示ST"] is None:
                    result[bn]["展示ST"] = st_val
                    result[bn]["展示F"]  = is_fly

    conditions = _parse_weather(soup)
    return result, conditions


def fetch_odds_win(date: str, race_no: int) -> dict[int, float]:
    """単勝オッズを取得。{艇番: オッズ倍率} を返す。取得失敗時は空dict。"""
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
#  選手詳細統計（コース別・直近3節・期別）
# ─────────────────────────────────────────────

def fetch_player_course_stats(toban: int) -> dict[int, dict]:
    """
    コース別通算成績（キャリア全期間）。
    Table[0] 進入率 / Table[1] 3連対率 / Table[2] ST平均 / Table[3] スタート順
    """
    url = f"{BASE_URL}/owpc/pc/data/racersearch/course?toban={toban}"
    try:
        soup = _fetch(url)
    except Exception:
        return {}

    tables = soup.find_all("table")
    result = {c: {} for c in range(1, 7)}
    key_map = {0: "進入率", 1: "3連対率", 2: "ST平均", 3: "スタート順"}

    for ti, key in key_map.items():
        if ti >= len(tables):
            break
        for row in tables[ti].find_all("tr")[1:]:
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                try:
                    c = int(cells[0].get_text(strip=True))
                    v = float(cells[1].get_text(strip=True).replace("%", ""))
                    if c in result:
                        result[c][key] = v
                except ValueError:
                    pass

    return result


def fetch_player_recent_stats(toban: int) -> dict:
    """
    直近3節の全レース着順を取得・集計。
    着順は全角数字（例：５）で記載されているため半角変換。
    落水(落)・フライング等の特殊記号は除外。
    """
    url = f"{BASE_URL}/owpc/pc/data/racersearch/back3?toban={toban}"
    try:
        soup = _fetch(url)
    except Exception:
        return {}

    tables = soup.find_all("table")
    meets, all_results = [], []

    if tables:
        for row in tables[0].find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 6:
                continue
            period = cells[0].get_text(strip=True)
            title  = cells[4].get_text(strip=True) if len(cells) > 4 else ""
            raw    = cells[5].get_text(separator="/", strip=True) if len(cells) > 5 else ""

            results = [int(v.translate(FW2HW)) for v in raw.split("/")
                       if v.strip().translate(FW2HW).isdigit()
                       and 1 <= int(v.strip().translate(FW2HW)) <= 6]

            if results:
                meets.append({"期間": period, "大会": title, "着順": results})
                all_results.extend(results)

    n = len(all_results)
    return {
        "節成績":     meets,
        "直近着順":   all_results,
        "直近出走数": n,
        "直近1着率":  all_results.count(1) / n if n else 0.0,
        "直近2連率":  sum(1 for r in all_results if r <= 2) / n if n else 0.0,
        "直近3連率":  sum(1 for r in all_results if r <= 3) / n if n else 0.0,
        "直近平均着順": sum(all_results) / n if n else None,
    }


def fetch_player_season_stats(toban: int) -> dict:
    """
    現在期（直近約6ヶ月）の成績サマリー。
    boatrace.jp は前期・後期の1期分のみ表示。
    """
    url = f"{BASE_URL}/owpc/pc/data/racersearch/season?toban={toban}"
    try:
        soup = _fetch(url)
    except Exception:
        return {}

    tables = soup.find_all("table")
    raw: dict[str, str] = {}
    if tables:
        for row in tables[0].find_all("tr"):
            cells = row.find_all(["th", "td"])
            for ki, vi in [(0, 1), (2, 3)]:
                if vi < len(cells):
                    k = cells[ki].get_text(strip=True)
                    v = cells[vi].get_text(strip=True)
                    if k:
                        raw[k] = v

    return {
        "期_勝率":     _num(raw.get("勝率")),
        "期_2連率":    _num(raw.get("2連対率")),
        "期_3連率":    _num(raw.get("3連対率")),
        "期_出走数":   _num(raw.get("出走回数")),
        "期_FL":       _num(raw.get("フライング回数")),
        "期_L0":       _num(raw.get("出遅れ回数（選手責任）")),
        "期_ST平均":   _num(raw.get("平均スタートタイミング")),
        "期_能力指数": _num(raw.get("能力指数")),
        "期_1着率":    _num(raw.get("1着")),
        "期_1着数":    _num(re.search(r"（(\d+)回）", raw.get("1着", "")).group(1)
                            if re.search(r"（(\d+)回）", raw.get("1着", "")) else None),
    }


# 選手統計のプロセス内キャッシュ（全レース予想・バックテストで同じ選手を再取得しない）
_PLAYER_STATS_CACHE: dict[int, tuple[dict, dict, dict]] = {}


def fetch_all_players_stats(boats: list[dict]) -> None:
    """
    全6選手のコース別・直近3節・期別成績を ThreadPoolExecutor で並列取得（in-place 更新）。
    1選手あたり最大3リクエスト × 6選手 = 最大18リクエスト。取得済み選手はキャッシュを使う。
    """
    boat_map = {b["艇番"]: b for b in boats}

    def _fetch_one(boat: dict) -> tuple[int, dict, dict, dict]:
        toban = boat.get("登録番号")
        if not toban:
            return boat["艇番"], {}, {}, {}
        if toban in _PLAYER_STATS_CACHE:
            c, r, s = _PLAYER_STATS_CACHE[toban]
            return boat["艇番"], c, r, s
        with ThreadPoolExecutor(max_workers=3) as ex:
            fc = ex.submit(fetch_player_course_stats, toban)
            fr = ex.submit(fetch_player_recent_stats, toban)
            fs = ex.submit(fetch_player_season_stats, toban)
            c, r, s = fc.result(), fr.result(), fs.result()
        if c or r or s:
            _PLAYER_STATS_CACHE[toban] = (c, r, s)
        return boat["艇番"], c, r, s

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(_fetch_one, b) for b in boats]
        for fut in as_completed(futures):
            try:
                bn, course, recent, season = fut.result()
                boat_map[bn]["コース別"]  = course
                boat_map[bn]["直近3節"]   = recent
                boat_map[bn]["期別成績"]  = season
            except Exception as e:
                print(f"  [警告] 選手統計取得エラー: {e}")


# ─────────────────────────────────────────────
#  データ統合
# ─────────────────────────────────────────────

def get_race_data(date: str, race_no: int, quiet: bool = False) -> tuple[list[dict], dict]:
    """1レース分の全データと気象条件を返す"""
    def _p(*a, **kw):
        if not quiet:
            print(*a, **kw)

    _p("  出走表を取得中...", end="", flush=True)
    boats = fetch_racelist(date, race_no)
    _p(f" {len(boats)}艇 OK")

    _p("  展示前情報・気象データ・単勝オッズを取得中...", end="", flush=True)
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_bi   = ex.submit(fetch_beforeinfo, date, race_no)
        f_odds = ex.submit(fetch_odds_win,   date, race_no)
    bi, conditions = f_bi.result()
    odds           = f_odds.result()
    _p(" OK")

    for b in boats:
        info = bi.get(b["艇番"], {})
        b["チルト"]     = info.get("チルト")
        b["展示タイム"] = info.get("展示タイム") or b["展示タイム"]
        b["展示ST"]     = info.get("展示ST")
        b["展示F"]      = info.get("展示F", False)
        b["部品交換"]   = info.get("部品交換")
        b["調整重量"]   = info.get("調整重量")
        b["単勝オッズ"] = odds.get(b["艇番"])

    _p("  選手統計を並列取得中（コース別通算/直近3節/現在期）...", flush=True)
    fetch_all_players_stats(boats)
    _p("  完了")

    return boats, conditions


# ─────────────────────────────────────────────
#  予想ロジック
# ─────────────────────────────────────────────

def predict(boats: list[dict], conditions: dict | None = None) -> list[dict]:
    """
    13要素による予想スコアリング。気象条件による展示タイム信頼度補正付き。

    ベース  : 蒲郡コース有利度（1コース約55%）
    補正①  : 選手スキル（全国/当地勝率）
    補正②  : 級別（A1/A2/B1/B2）
    補正③  : モーター2連率
    補正④  : 展示タイム（波高・風速による信頼度補正付き）
    補正⑤  : 体重（軽い=有利）
    補正⑥  : 展示ST（速い=有利、F=慎重）
    補正⑦  : FL歴（フライング持ち=本番慎重）
    補正⑧  : 今節成績（現節の調子）
    補正⑨  : コース別3連対率（今日の進入コースでの通算実績）← 重要
    補正⑩  : 直近3節の1着率（最近の状態）
    補正⑪  : 現在期の能力指数
    補正⑫  : コース別ST平均（当該コースでのスタート力）
    補正⑬  : 直近3節の平均着順（安定した上位着を評価）

    気象補正:
      波高 >= 10cm → 外コース（4-6）の展示タイム補正を50%に減衰（荒水面）
      波高 >=  5cm → 外コース（4-6）の展示タイム補正を75%に減衰
      風速 >=  7m  → 全艇の展示タイム補正を50%に減衰（高風速）
      風速 >=  4m  → 全艇の展示タイム補正を70%に減衰

    最後に市場（単勝オッズ）の含意確率を ODDS_BLEND の比率でブレンドし、
    モデル単独の見落としを市場の知恵で補正する（全艇のオッズが取れた時のみ）。
    """
    cond  = conditions or {}
    wave  = cond.get("波高", 0) or 0
    wind  = cond.get("風速", 0) or 0

    # 波高・風速による展示タイム信頼度係数
    wave_factor = 0.50 if wave >= 10 else (0.75 if wave >= 5 else 1.0)
    wind_factor = 0.50 if wind >= 7  else (0.70 if wind >= 4 else 1.0)

    vm  = [b["モーター2連率"] for b in boats if b["モーター2連率"] is not None]
    vex = [b["展示タイム"]   for b in boats if b["展示タイム"]   is not None]
    vw  = [b["体重"]         for b in boats if b["体重"]         is not None]
    vst = [b["展示ST"]       for b in boats if b["展示ST"] is not None and not b["展示F"]]

    avg_motor  = statistics.mean(vm)  if vm  else 33.0
    avg_ex     = statistics.mean(vex) if vex else 6.70
    avg_weight = statistics.mean(vw)  if vw  else 52.0
    avg_st     = statistics.mean(vst) if vst else 0.15

    scores = []
    for b in boats:
        base = LANE_WIN_RATE[b["艇番"]]

        # ① 選手スキル
        zk    = b["全国勝率"] or 0.0
        tc    = b["当地勝率"] or 0.0
        skill = (zk * 0.4 + tc * 0.6) if tc > 0.0 else zk
        skill_adj = (skill - 5.0) * 0.03

        # ② 級別
        grade_adj = {"A1": 0.06, "A2": 0.03, "B1": 0.0, "B2": -0.03}.get(b["級別"], 0.0)

        # ③ モーター
        m = b["モーター2連率"] if b["モーター2連率"] is not None else avg_motor
        motor_adj = (m - avg_motor) * 0.003

        # ④ 展示タイム（気象補正あり）
        ex = b["展示タイム"] if b["展示タイム"] is not None else avg_ex
        ex_adj_raw = (avg_ex - ex) * 1.0
        # 外コース（4-6）は波高の影響を受けやすい
        lane_wave = wave_factor if b["艇番"] >= 4 else 1.0
        ex_adj = ex_adj_raw * lane_wave * wind_factor

        # ⑤ 体重
        w = b["体重"] if b["体重"] is not None else avg_weight
        weight_adj = (avg_weight - w) * 0.005

        # ⑥ 展示ST
        st = b["展示ST"]
        if b["展示F"]:
            st_adj = -0.05
        elif st is not None:
            st_adj = max(-0.12, min(0.12, (avg_st - st) * 0.8))
        else:
            st_adj = 0.0

        # ⑦ FL歴
        fl_adj = -0.03 if b["FL回数"] > 0 else 0.0

        # ⑧ 今節成績
        ks = b["今節成績"]
        kosetsu_adj = (3.5 - sum(ks) / len(ks)) * 0.01 if ks else 0.0

        # ⑨ コース別3連対率（今日の進入コース = 艇番で参照）
        cd = b["コース別"].get(b["艇番"], {})
        c3 = cd.get("3連対率")
        season_races = b["期別成績"].get("期_出走数") or 60
        entry_rate   = cd.get("進入率", 10.0)
        est_races    = max(entry_rate / 100 * season_races, 1)
        # ベイズ平滑化（先験値=33%、強度=5レース）
        smoothed_c3 = (c3 * est_races + 33.0 * 5) / (est_races + 5) if c3 is not None else 33.0
        c3rate_adj  = (smoothed_c3 - 33.0) * 0.005

        # ⑩ 直近3節1着率
        r1w = b["直近3節"].get("直近1着率")
        recent_adj = max(-0.06, min(0.06, (r1w - 0.10) * 0.25)) if r1w is not None else 0.0

        # ⑪ 現在期能力指数（50が平均）
        ki = b["期別成績"].get("期_能力指数")
        season_adj = (ki - 50.0) * 0.001 if ki is not None else 0.0

        # ⑫ コース別ST平均（当該コースでのスタート力。0.17が全国平均目安）
        st_avg_course = cd.get("ST平均")
        if st_avg_course is not None and 0.01 <= st_avg_course <= 0.40:
            course_st_adj = max(-0.05, min(0.05, (0.17 - st_avg_course) * 0.4))
        else:
            course_st_adj = 0.0

        # ⑬ 直近3節の平均着順（3.5が中央値。サンプル8走以上で適用）
        r_avg = b["直近3節"].get("直近平均着順")
        r_n   = b["直近3節"].get("直近出走数", 0)
        if r_avg is not None and r_n >= 8:
            recent_avg_adj = max(-0.05, min(0.05, (3.5 - r_avg) * 0.02))
        else:
            recent_avg_adj = 0.0

        total_adj = (skill_adj + grade_adj + motor_adj + ex_adj
                     + weight_adj + st_adj + fl_adj + kosetsu_adj
                     + c3rate_adj + recent_adj + season_adj
                     + course_st_adj + recent_avg_adj)

        scores.append(max(base * (1.0 + total_adj), 0.001))

    total   = sum(scores)
    model_p = [sc / total for sc in scores]

    # 市場オッズブレンド（全艇のオッズが取得できた時のみ）
    odds_list = [b.get("単勝オッズ") for b in boats]
    if ODDS_BLEND > 0 and all(o is not None and o > 1.0 for o in odds_list):
        implied = [1.0 / o for o in odds_list]
        s_imp   = sum(implied)
        market  = [x / s_imp for x in implied]
        final_p = [(1 - ODDS_BLEND) * mp + ODDS_BLEND * mk
                   for mp, mk in zip(model_p, market)]
    else:
        final_p = model_p

    result = [{**b, "予想勝率": fp, "モデル勝率": mp}
              for b, fp, mp in zip(boats, final_p, model_p)]
    return sorted(result, key=lambda x: -x["予想勝率"])


# ─────────────────────────────────────────────
#  買い目（Plackett-Luce モデル）
# ─────────────────────────────────────────────

def _pl_exacta(p: dict[int, float]) -> dict[str, float]:
    """2連単の全組み合わせの確率。P(i→j) = p_i * p_j / (1 - p_i)"""
    out = {}
    for i in p:
        for j in p:
            if i != j and p[i] < 1.0:
                out[f"{i}-{j}"] = p[i] * p[j] / (1 - p[i])
    return out


def _pl_trifecta(p: dict[int, float]) -> dict[str, float]:
    """3連単の全組み合わせの確率。P(i→j→k) = p_i * p_j/(1-p_i) * p_k/(1-p_i-p_j)"""
    out = {}
    for i in p:
        for j in p:
            if j == i:
                continue
            for k in p:
                if k in (i, j):
                    continue
                d1 = 1 - p[i]
                d2 = 1 - p[i] - p[j]
                if d1 > 1e-9 and d2 > 1e-9:
                    out[f"{i}-{j}-{k}"] = p[i] * (p[j] / d1) * (p[k] / d2)
    return out


def recommend_bets(ranked: list[dict]) -> dict:
    """
    自信度に応じて買い目点数を可変にする。
    蒲郡は1コースの基礎勝率が55%と高いため、閾値は高めに設定。
      鉄板（◎勝率58%以上）: 2連単2点 / 3連単4点  … 厚く少点数
      有力（46%以上）      : 2連単3点 / 3連単5点
      接戦（36%以上）      : 2連単3点 / 3連単6点
      混戦（36%未満）      : 2連単4点 / 3連単7点  … 広く薄く
    妙味: モデル勝率 × 単勝オッズ（期待値）が1.2以上の艇を「市場が過小評価」として提示。
    """
    p  = {r["艇番"]: r["予想勝率"] for r in ranked}
    p1 = ranked[0]["予想勝率"]

    if p1 >= 0.58:
        conf, n2, n3 = "鉄板", 2, 4
    elif p1 >= 0.46:
        conf, n2, n3 = "有力", 3, 5
    elif p1 >= 0.36:
        conf, n2, n3 = "接戦", 3, 6
    else:
        conf, n2, n3 = "混戦", 4, 7

    ex  = sorted(_pl_exacta(p).items(),   key=lambda kv: -kv[1])[:n2]
    tri = sorted(_pl_trifecta(p).items(), key=lambda kv: -kv[1])[:n3]

    value = []
    for r in ranked:
        odds = r.get("単勝オッズ")
        mp   = r.get("モデル勝率")
        if odds and mp and mp >= 0.10:
            ev = mp * odds
            if ev >= 1.2:
                value.append({"艇番": r["艇番"], "EV": round(ev, 2), "オッズ": odds})

    return {
        "自信度":   conf,
        "2連単":    [{"組番": c, "確率": pr} for c, pr in ex],
        "3連単":    [{"組番": c, "確率": pr} for c, pr in tri],
        "2連単合成": sum(pr for _, pr in ex),
        "3連単合成": sum(pr for _, pr in tri),
        "妙味":     value,
    }


# ─────────────────────────────────────────────
#  出力ヘルパー
# ─────────────────────────────────────────────

MARKS = ["◎", "○", "▲", "△", "★"]

def _f(val, fmt="{:.2f}", fb="---"):
    return fmt.format(val) if val is not None else fb

def _pct(val, fb="---"):
    return f"{val:.1f}%" if val is not None else fb

def _rate(val, fb="---"):
    return f"{val*100:.1f}%" if val is not None else fb

def _boat_reason(b: dict, boats: list[dict], conditions: dict) -> list[str]:
    """各艇の予想根拠を生成"""
    parts = []
    cond  = conditions or {}
    wave  = cond.get("波高", 0) or 0
    lane  = b["艇番"]

    if b["級別"] in ("A1", "A2"):
        parts.append(f"【{b['級別']}】")

    if lane == 1:
        parts.append(f"1コース有利{LANE_WIN_RATE[1]*100:.0f}%")

    tc = b.get("当地勝率")
    if tc and tc >= 5.5:
        parts.append(f"当地{tc:.2f}↑")
    elif tc and tc <= 1.5:
        parts.append(f"当地{tc:.2f}↓")

    vex = [x["展示タイム"] for x in boats if x["展示タイム"] is not None]
    if vex and b["展示タイム"] is not None:
        if b["展示タイム"] == min(vex):
            parts.append("展示最速")
        elif b["展示タイム"] > statistics.mean(vex) + 0.04:
            parts.append(f"展示{b['展示タイム']:.2f}遅め")

    if b["展示F"]:
        parts.append(f"展示F{b['展示ST']:.2f}")
    elif b["展示ST"] is not None:
        vst = [x["展示ST"] for x in boats if x["展示ST"] is not None and not x["展示F"]]
        if vst and b["展示ST"] == min(vst):
            parts.append(f"展示ST最速{b['展示ST']:.2f}")

    if b["FL回数"] > 0:
        parts.append(f"FL{b['FL回数']}回（慎重）")

    cd = b["コース別"].get(lane, {})
    c3 = cd.get("3連対率")
    if c3 is not None:
        if c3 >= 50:
            parts.append(f"C{lane}三連{c3:.0f}%↑")
        elif c3 == 0:
            parts.append(f"C{lane}三連0%")

    r1w = b["直近3節"].get("直近1着率")
    rc  = b["直近3節"].get("直近出走数", 0)
    if r1w is not None and rc > 5:
        if r1w >= 0.15:
            parts.append(f"直近1着{r1w*100:.0f}%↑")
        elif r1w == 0:
            parts.append("直近1着無し")

    ki = b["期別成績"].get("期_能力指数")
    if ki is not None:
        if ki >= 55:
            parts.append(f"能力指数{ki:.0f}↑")
        elif ki <= 40:
            parts.append(f"能力指数{ki:.0f}↓")

    if wave >= 5 and lane >= 4:
        parts.append(f"波高{wave}cm注意")

    if b.get("部品交換"):
        parts.append(f"部品交換:{b['部品交換']}")

    return parts


def _fmt_bet_list(bet_items: list[dict]) -> str:
    return ", ".join(f"{b['組番']}({b['確率']*100:.0f}%)" for b in bet_items)


def is_hot_race(bets: dict) -> bool:
    """「勝負レース」判定: 自信度が鉄板、または市場が過小評価している妙味艇がある"""
    return bets["自信度"] == "鉄板" or bool(bets.get("妙味"))


def _build_discord_msg(ranked: list[dict], conditions: dict,
                       race_label: str, bets: dict) -> str:
    cond  = conditions or {}
    tenki = cond.get("天候", "不明")
    wind  = cond.get("風速", 0) or 0
    wave  = cond.get("波高", 0) or 0
    kion  = f"{cond['気温']:.1f}℃" if cond.get("気温") is not None else "---"

    lines = []
    if is_hot_race(bets):
        reason = "鉄板級の本命" if bets["自信度"] == "鉄板" else "妙味あり（市場が過小評価）"
        lines.append(f"🔥🔥 **勝負レース！**（{reason}）🔥🔥")
    lines += [
        f"🚤 **【{race_label}】**  自信度: **{bets['自信度']}**",
        f"📍 {tenki}  気温{kion}  風速{wind}m  波高{wave}cm",
        "```",
    ]
    for i, r in enumerate(ranked[:5]):
        if i >= len(MARKS):
            break
        mark  = MARKS[i]
        lane  = r["艇番"]
        name  = r["選手名"][:9]
        grade = r["級別"]
        pct   = r["予想勝率"] * 100
        tc    = r.get("当地勝率")
        c3    = r["コース別"].get(lane, {}).get("3連対率")
        r1w   = r["直近3節"].get("直近1着率")

        stats = []
        if tc  is not None: stats.append(f"当地{tc:.2f}")
        if c3  is not None: stats.append(f"C{lane}三連{c3:.0f}%")
        if r1w is not None: stats.append(f"直近{r1w*100:.0f}%")

        odds_v  = r.get("単勝オッズ")
        odds_s2 = f"  📊{odds_v:.1f}倍" if odds_v is not None else ""
        parts_v = r.get("部品交換")
        parts_s2 = f"  ⚙{parts_v}" if parts_v else ""

        lines.append(f"{mark} {lane}号艇 {name:<9} ({grade}) {pct:.0f}%{odds_s2}{parts_s2}")
        if stats:
            lines.append(f"   📎 {' / '.join(stats)}")
        lines.append("")

    lines.append("```")
    lines.append(f"📌 2連単: {_fmt_bet_list(bets['2連単'])}")
    lines.append(f"📌 3連単: {_fmt_bet_list(bets['3連単'])}  [合成{bets['3連単合成']*100:.0f}%]")
    for v in bets.get("妙味", []):
        lines.append(f"💰 妙味: {v['艇番']}号艇 単勝{v['オッズ']:.1f}倍（期待値{v['EV']:.2f}）")
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

def log_prediction(date: str, race_no: int, ranked: list[dict],
                   bets: dict, conditions: dict) -> None:
    """予想内容を logs/predictions.jsonl に追記（後日の検証・的中率集計用）"""
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    rec = {
        "記録時刻": datetime.now().isoformat(timespec="seconds"),
        "日付":     date,
        "レース":   race_no,
        "予想順":   [r["艇番"] for r in ranked],
        "予想勝率": {str(r["艇番"]): round(r["予想勝率"], 4) for r in ranked},
        "自信度":   bets["自信度"],
        "2連単":    [b["組番"] for b in bets["2連単"]],
        "3連単":    [b["組番"] for b in bets["3連単"]],
        "気象":     conditions,
    }
    with open(log_dir / "predictions.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────
#  1レース実行（表示 + Discord + ログ）
# ─────────────────────────────────────────────

W = 75

def run_race(date: str, race_no: int, do_discord: bool = False,
             detail: bool = True, hot_only: bool = False) -> bool:
    race_label = f"ボートレース蒲郡  {date[:4]}/{date[4:6]}/{date[6:]}  {race_no}R"

    print(f"\nデータ取得中... {race_label}")
    try:
        data, conditions = get_race_data(date, race_no)
    except Exception as e:
        print(f"データ取得失敗: {e}")
        return False
    if not data:
        print("データ取得失敗（出走表が見つかりません）")
        return False

    cond  = conditions
    wave  = cond.get("波高", 0) or 0
    wind  = cond.get("風速", 0) or 0
    tenki = cond.get("天候", "不明")
    kion  = f"{cond['気温']:.1f}℃" if cond.get("気温") is not None else "---"
    suion = f"{cond['水温']:.1f}℃" if cond.get("水温") is not None else "---"
    weather_str = f"天候:{tenki}  気温:{kion}  風速:{wind}m  水温:{suion}  波高:{wave}cm"

    weather_warns = []
    if wave >= 10:
        weather_warns.append(f"波高{wave}cm（荒水面）→ 外コース展示タイム信頼度 50%")
    elif wave >= 5:
        weather_warns.append(f"波高{wave}cm（やや荒れ）→ 外コース展示タイム信頼度 75%")
    if wind >= 7:
        weather_warns.append(f"風速{wind}m（強風）→ 全艇展示タイム信頼度 50%")
    elif wind >= 4:
        weather_warns.append(f"風速{wind}m（強め）→ 全艇展示タイム信頼度 70%")

    ranked = predict(data, conditions)
    bets   = recommend_bets(ranked)

    # ── サマリーヘッダー ──
    print(f'\n{"="*W}')
    print(f"【{race_label}】")
    print(weather_str)
    for ww in weather_warns:
        print(f"  ※ {ww}")
    print(f'{"="*W}')

    # ── 予想一覧テーブル ──
    has_odds = any(r["単勝オッズ"] is not None for r in ranked)
    print(f'  {"予想":^4}  {"艇":^3}  {"選手名":<12}  {"級":^3}  {"展示":^6}  {"展示ST":^7}  {"全勝率":^6}  {"地勝率":^6}  {"オッズ":^6}  今節')
    print(f'  {"─"*W}')
    for rank, r in enumerate(ranked, 1):
        st_s   = f"{'F' if r['展示F'] else ''}{r['展示ST']:.2f}" if r["展示ST"] is not None else "---"
        ks     = " ".join(str(k) for k in r["今節成績"]) if r["今節成績"] else "-"
        odds_s = f"{r['単勝オッズ']:.1f}" if r["単勝オッズ"] is not None else "---"
        parts_s = f" [換:{r['部品交換']}]" if r.get("部品交換") else ""
        print(
            f"  {rank:>2}着  "
            f"  {r['艇番']:^3}  "
            f"{r['選手名']:<12}  "
            f"{r['級別']:^3}  "
            f"{_f(r['展示タイム']):^6}  "
            f"{st_s:^7}  "
            f"{_f(r['全国勝率']):^6}  "
            f"{_f(r['当地勝率']):^6}  "
            f"{odds_s:^6}  "
            f"[{ks}]{parts_s}"
        )

    # ── 予想印 ──
    print(f'\n{"="*W}')
    print("★★★ 予想印 ★★★")
    for i, r in enumerate(ranked[:5]):
        mark   = MARKS[i]
        reason = " / ".join(_boat_reason(r, data, cond)[:4])
        print(f"  {mark}  {r['艇番']}号艇  {r['選手名']:<10}  {r['予想勝率']:>6.1%}  ({reason})")
    print(f'{"="*W}')

    # ── 各艇詳細診断 ──
    if detail:
        print(f'\n\n{"="*W}')
        print("【各艇詳細診断】")
        print(f'{"="*W}')

    for i, r in enumerate(ranked[:5] if detail else []):
        mark = MARKS[i]
        cd   = r["コース別"]
        rec  = r["直近3節"]
        sea  = r["期別成績"]
        lane = r["艇番"]

        st_s  = f"{'F' if r['展示F'] else ''}{r['展示ST']:.2f}" if r["展示ST"] is not None else "---"
        ks    = " ".join(str(k) for k in r["今節成績"]) if r["今節成績"] else "なし"
        c3own = cd.get(lane, {}).get("3連対率")
        stavu = cd.get(lane, {}).get("ST平均")

        r1w = rec.get("直近1着率")
        rc  = rec.get("直近出走数", 0)
        ki  = sea.get("期_能力指数")
        zsn = sea.get("期_勝率")

        recent_all = rec.get("直近着順", [])
        trend_s = ""
        if len(recent_all) >= 6:
            if sum(recent_all[:3]) > sum(recent_all[-3:]) + 2:
                trend_s = "  ↓直近下降傾向"
            elif sum(recent_all[-3:]) > sum(recent_all[:3]) + 2:
                trend_s = "  ↑直近上昇傾向"

        odds_val = r.get("単勝オッズ")
        odds_info = f"  単勝:{odds_val:.1f}倍" if odds_val is not None else ""
        parts_info = f"  [部品交換:{r['部品交換']}]" if r.get("部品交換") else ""
        adj_info = f"  調整重量:{r['調整重量']:.1f}kg" if r.get("調整重量") else ""

        print(f'\n{"─"*W}')
        print(f" {mark}  {lane}号艇  {r['選手名']}  （{r['級別']}）{odds_info}{parts_info}")
        print(f"   体重:{_f(r['体重'],'{:.1f}')}kg{adj_info}  チルト:{_f(r['チルト'],'{:.1f}')}  "
              f"M#{r['モーター番号'] or '---'}（2連率{_f(r['モーター2連率'],'{:.1f}')}%）")
        print(f"   展示タイム:{_f(r['展示タイム'])}  展示ST:{st_s}  今節:[{ks}]")
        print(f"   全国勝率:{_f(r['全国勝率'])} / 当地勝率:{_f(r['当地勝率'])}")
        c3s = f"{c3own:.1f}%" if c3own is not None else "---"
        sts = f"{stavu:.2f}"  if stavu is not None else "---"
        print(f"   C{lane}通算3連対率:{c3s}  ST平均:{sts}")
        r1s = f"{r1w*100:.1f}%" if r1w is not None else "---"
        print(f"   直近3節: 1着率{r1s}（{rc}本）{trend_s}")
        print(f"   現在期: 勝率{_f(zsn)} / 能力指数{int(ki) if ki is not None else '---'}")
        print(f'{"─"*W}')
        full_reason = _boat_reason(r, data, cond)
        if full_reason:
            print(f"   >> {'  '.join(full_reason)}")
        print()
        for m in rec.get("節成績", [])[:3]:
            rs = " ".join(str(k) for k in m["着順"])
            print(f"     {m['期間'][:22]}  {m['大会'][:20]}  [{rs}]")

    # ── 推奨買い目 ──
    print(f'\n{"="*W}')
    print(f"【推奨買い目】  自信度: {bets['自信度']}")
    print(f"  2連単: {_fmt_bet_list(bets['2連単'])}  [合成{bets['2連単合成']*100:.0f}%]")
    print(f"  3連単: {_fmt_bet_list(bets['3連単'])}  [合成{bets['3連単合成']*100:.0f}%]")
    for v in bets.get("妙味", []):
        print(f"  💰 妙味: {v['艇番']}号艇 単勝{v['オッズ']:.1f}倍（期待値{v['EV']:.2f}）")
    weather_note = (f"（気象補正: 波高{wave}cm・風速{wind}m）"
                    if weather_warns else "（気象補正: 適用なし）")
    print(f"\n  ※ 13要素スコアリング + 気象補正 + オッズブレンド  {weather_note}")
    print(f'{"="*W}\n')

    # ── 予想ログ記録 ──
    try:
        log_prediction(date, race_no, ranked, bets, cond)
    except Exception as e:
        print(f"  [警告] 予想ログ記録エラー: {e}")

    # ── Discord 送信 ──
    if do_discord:
        if hot_only and not is_hot_race(bets):
            print(f"Discord: 勝負レース条件を満たさないため送信スキップ（自信度: {bets['自信度']}）")
        else:
            webhook_url = load_webhook_url()
            if not webhook_url:
                print("Discord: webhook URL が見つかりません。")
                print("  discord_webhook.txt を作成するか DISCORD_WEBHOOK_URL 環境変数を設定してください。")
            else:
                msg = _build_discord_msg(ranked, cond, race_label, bets)
                print("Discord に送信中...")
                ok = send_discord(msg, webhook_url)
                print("  送信完了！" if ok else "  送信失敗。")

    return True


# ─────────────────────────────────────────────
#  メイン
# ─────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import date as _date

    parser = argparse.ArgumentParser(description="ボートレース蒲郡 予想ツール")
    parser.add_argument("date",    nargs="?", default=None,
                        help="日付 YYYYMMDD（省略=今日）")
    parser.add_argument("race_no", nargs="?", type=int, default=None,
                        help="レース番号 1-12（省略=当日全レース）")
    parser.add_argument("--all", action="store_true",
                        help="当日の全レースをまとめて予想")
    parser.add_argument("--discord", action="store_true", help="結果をDiscordに送信")
    parser.add_argument("--hot-only", action="store_true",
                        help="勝負レース（鉄板 or 妙味あり）のみDiscordに送信")
    args = parser.parse_args()

    DATE = args.date or _date.today().strftime("%Y%m%d")

    if args.all or args.race_no is None:
        # ── 全レース一括予想 ──
        label = f"{DATE[:4]}/{DATE[4:6]}/{DATE[6:]}"
        print(f"\nボートレース蒲郡  {label}  全レース一括予想")
        schedule = fetch_schedule(DATE)
        race_nos = [r["race_no"] for r in schedule] or list(range(1, 13))
        if not schedule:
            print("スケジュール取得失敗。1〜12Rを順に試します。")

        ok_count = 0
        for rno in race_nos:
            ok = run_race(DATE, rno, do_discord=args.discord, detail=False,
                          hot_only=args.hot_only)
            if ok:
                ok_count += 1
            elif not schedule:
                break  # スケジュール不明時はデータの無いレースで打ち切り

        print(f"\n{'='*W}")
        print(f"全レース予想完了: {ok_count}/{len(race_nos)}レース")
        if ok_count == 0:
            print("※ 開催日以外はデータがありません。")
            sys.exit(1)
    else:
        ok = run_race(DATE, args.race_no, do_discord=args.discord, detail=True,
                      hot_only=args.hot_only)
        if not ok:
            sys.exit(1)
