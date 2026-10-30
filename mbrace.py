"""
ボートレース公式（mbrace）の日次データ: 番組表(B)・競走成績(K)
  https://www1.mbrace.or.jp/od2/B/YYYYMM/bYYMMDD.lzh  （前日夕方〜当日朝に公開）
  https://www1.mbrace.or.jp/od2/K/YYYYMM/kYYMMDD.lzh  （当日夜に確定版）

予想モデル v5 の学習データはすべてこの公式データから作っており、本番でも
同じデータ・同じ計算式で特徴量を作る（学習と本番の定義ズレ・未来情報の混入を防ぐ）。

選手の過去成績（直近3節・艇番別通算）は player_stats.json に「前日まで」の
状態として保存し、毎日 `python mbrace.py update` で前日分の競走成績を追加する。

LZH の展開には bsdtar を使う（Windows 標準の tar.exe は bsdtar。Linux は
libarchive-tools パッケージの bsdtar）。
"""
import json
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = "https://www1.mbrace.or.jp/od2"
HERE = Path(__file__).parent
CACHE = HERE / ".cache" / "mbrace"
STATE_FILE = HERE / "player_stats.json"
JST = ZoneInfo("Asia/Tokyo")
FW = str.maketrans("０１２３４５６７８９ＲＨｍ：", "0123456789RHm:")
KEEP_MEETS = 4


# ─────────────────────────────────────────────
#  取得・展開
# ─────────────────────────────────────────────

def _tar_cmd() -> str | None:
    for c in ("bsdtar", "tar"):
        p = shutil.which(c)
        if p and (c == "bsdtar" or sys.platform.startswith("win")):
            return p
    return None


def download(kind: str, ymd: str, use_cache: bool = True) -> bytes | None:
    """kind='B'|'K'。LZHの中身（テキストのバイト列）を返す。未公開なら None。"""
    name = f"{kind.lower()}{ymd[2:]}.lzh"
    path = CACHE / name
    if not (use_cache and path.exists()):
        try:
            r = requests.get(f"{BASE}/{kind}/{ymd[:6]}/{name}", timeout=60)
        except requests.RequestException:
            return None
        if r.status_code != 200 or r.content[2:5] != b"-lh":
            return None
        CACHE.mkdir(parents=True, exist_ok=True)
        # LZH は標準入力からだと展開できない（シーク必須）ため必ずファイル経由
        if not use_cache:
            path = CACHE / f"tmp_{name}"
        path.write_bytes(r.content)
    tar = _tar_cmd()
    if not tar:
        print("  [mbrace] bsdtar が見つからないため LZH を展開できません")
        return None
    res = subprocess.run([tar, "-xOf", str(path)], capture_output=True)
    if not use_cache:
        path.unlink(missing_ok=True)
    return res.stdout if res.returncode == 0 and res.stdout else None


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────
#  番組表(B)
# ─────────────────────────────────────────────

def parse_b(raw: bytes, ymd: str) -> list[dict]:
    out, jcd, rno, deadline = [], None, None, None
    for bl in raw.split(b"\n"):
        bl = bl.rstrip(b"\r")
        m = re.match(rb"^(\d\d)BBGN", bl)
        if m:
            jcd, rno = m.group(1).decode(), None
            continue
        if re.match(rb"^\d\dBEND", bl):
            jcd = None
            continue
        if jcd is None:
            continue
        t = bl.decode("cp932", errors="replace").translate(FW)
        m = re.match(r"^\s*(\d{1,2})R\s", t)
        if m and "H" in t and "m" in t:
            rno = int(m.group(1))
            dm = re.search(r"締切予定(\d{1,2}:\d{2})", t)
            deadline = dm.group(1) if dm else None
            continue
        # 艇の行はバイト位置固定（ボートNOが3桁だと直前の数字と空白なしで連結されるため）
        if rno and len(bl) >= 40 and bl[:1] in b"123456" and bl[1:2] == b" " and bl[2:6].isdigit():
            try:
                spans = [(24, 29), (29, 35), (35, 40), (40, 46), (46, 49), (49, 55), (55, 58), (58, 64)]
                v = [_num(bl[a:b].decode("ascii", errors="replace").strip()) for a, b in spans]
                out.append(dict(date=ymd, jcd=jcd, rno=rno, lane=int(bl[0:1]), toban=int(bl[2:6]),
                                age=int(bl[14:16]), weight=int(bl[20:22]),
                                grade=bl[22:24].decode("ascii", errors="replace"),
                                nat_win=v[0], nat2=v[1], loc_win=v[2], loc2=v[3],
                                motor_no=v[4], motor2=v[5], boat_no=v[6], boat2=v[7], deadline=deadline))
            except (ValueError, IndexError):
                pass
    return out


def get_program(ymd: str, jcd: str = "07") -> dict[int, dict[int, dict]]:
    """当日の番組表 {レース番号: {艇番: 行}}。未公開・取得失敗なら空。"""
    raw = download("B", ymd)
    out: dict[int, dict[int, dict]] = {}
    if raw:
        for r in parse_b(raw, ymd):
            if r["jcd"] == jcd:
                out.setdefault(r["rno"], {})[r["lane"]] = r
    return out


# ─────────────────────────────────────────────
#  競走成績(K)
# ─────────────────────────────────────────────

K_RACE = re.compile(r"^\s+(\d{1,2})R\s+.*H\d+m")
K_ROW = re.compile(r"^\s+(\S{1,2})\s+([1-6])\s+(\d{4})\s+(\D+?)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)")


def parse_k(raw: bytes, ymd: str) -> list[dict]:
    """1行=1艇の成績（着順・スタートタイミング等）"""
    boats, jcd, rno = [], None, None
    for bl in raw.split(b"\n"):
        bl = bl.rstrip(b"\r")
        m = re.match(rb"^(\d\d)KBGN", bl)
        if m:
            jcd, rno = m.group(1).decode(), None
            continue
        if re.match(rb"^\d\dKEND", bl):
            jcd, rno = None, None
            continue
        if jcd is None:
            continue
        line = bl.decode("cp932", errors="replace")
        m = K_RACE.match(line)
        if m and "風" in line:
            rno = int(m.group(1))
            continue
        if rno is None:
            continue
        m = K_ROW.match(line)
        if m and not line.strip().startswith(("単勝", "複勝")):
            fin = m.group(1)
            boats.append(dict(date=ymd, jcd=jcd, rno=rno, lane=int(m.group(2)), toban=int(m.group(3)),
                              fin_raw=fin, finish=int(fin) if fin.isdigit() else None, st_raw=m.group(9)))
    return boats


# ─────────────────────────────────────────────
#  選手の過去成績（前日までの状態）
#   m: 直近の節 [場, 初日, 最終日, 着順合計, 走数, 1着, 2連対, ST合計, ST有効数, F数]
#   l: 艇番別通算 {艇番: [走数, 1着, 3連対, ST合計, ST有効数]}（2020年以降）
# ─────────────────────────────────────────────

def _days(a: str, b: str) -> int:
    return (datetime.strptime(b, "%Y%m%d") - datetime.strptime(a, "%Y%m%d")).days


def update_state(state: dict, rows: list[dict]) -> None:
    """1日分（以上）の成績行を時系列順に反映する。学習時の特徴量作成と同じ定義。"""
    P = state.setdefault("p", {})
    for r in sorted(rows, key=lambda x: (x["toban"], x["date"], x["jcd"], x["rno"])):
        if str(r["fin_raw"]).startswith("K"):  # 欠場
            continue
        fin = r["finish"] if r["finish"] else 6
        fin = min(fin, 6)
        st_raw = str(r["st_raw"])
        st_f = 1 if "F" in st_raw else 0
        sm = re.search(r"(\d*\.\d+)", st_raw)
        st = float(sm.group(1)) if (sm and not st_f) else None
        p = P.setdefault(str(r["toban"]), {"m": [], "l": {}})
        ms = p["m"]
        if not ms or ms[-1][0] != r["jcd"] or _days(ms[-1][2], r["date"]) > 1:
            ms.append([r["jcd"], r["date"], r["date"], 0, 0, 0, 0, 0.0, 0, 0])
            del ms[:-KEEP_MEETS]
        m = ms[-1]
        m[2] = r["date"]
        m[3] += fin; m[4] += 1; m[5] += int(fin == 1); m[6] += int(fin <= 2)
        if st is not None:
            m[7] = round(m[7] + st, 3); m[8] += 1
        m[9] += st_f
        lane = p["l"].setdefault(str(r["lane"]), [0, 0, 0, 0.0, 0])
        lane[0] += 1; lane[1] += int(fin == 1); lane[2] += int(fin <= 3)
        if st is not None:
            lane[3] = round(lane[3] + st, 3); lane[4] += 1


def player_feats(state: dict, toban: int | None, lane: int, ymd: str, jcd: str = "07") -> dict:
    """直近3節（今節を除く）と艇番別通算の集計値。データが無い選手は0。"""
    out = dict(r3_sum=0, r3_cnt=0, r3_win=0, r3_t2=0, r3_st=0.0, r3_stn=0, r3_f=0,
               cl_n=0, cl_win=0, cl_t3=0, cl_st=0.0, cl_stn=0)
    p = state.get("p", {}).get(str(toban)) if toban else None
    if not p:
        return out
    ms = p["m"]
    cur = bool(ms) and ms[-1][0] == jcd and 0 <= _days(ms[-1][2], ymd) <= 1
    prev = ms[-4:-1] if cur else ms[-3:]
    for m in prev:
        out["r3_sum"] += m[3]; out["r3_cnt"] += m[4]; out["r3_win"] += m[5]; out["r3_t2"] += m[6]
        out["r3_st"] += m[7]; out["r3_stn"] += m[8]; out["r3_f"] += m[9]
    lst = p["l"].get(str(lane))
    if lst:
        out.update(cl_n=lst[0], cl_win=lst[1], cl_t3=lst[2], cl_st=lst[3], cl_stn=lst[4])
    return out


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"asof": None, "p": {}}


def save_state(state: dict) -> None:
    # 1選手1行（差分が小さくなるように）
    lines = ["{", f' "asof": {json.dumps(state["asof"])},', ' "p": {']
    items = sorted(state["p"].items(), key=lambda kv: int(kv[0]))
    for i, (k, v) in enumerate(items):
        lines.append(f'  "{k}": {json.dumps(v, separators=(",", ":"))}' + ("," if i < len(items) - 1 else ""))
    lines += [" }", "}"]
    STATE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update(until: str | None = None) -> str | None:
    """state の翌日〜until（既定: 昨日 JST）の競走成績を取り込む。最終取り込み日を返す。"""
    state = load_state()
    if not state.get("asof"):
        print("  [mbrace] player_stats.json がありません")
        return None
    until = until or (datetime.now(JST).date() - timedelta(days=1)).strftime("%Y%m%d")
    d = datetime.strptime(state["asof"], "%Y%m%d").date() + timedelta(days=1)
    end = datetime.strptime(until, "%Y%m%d").date()
    changed = False
    while d <= end:
        ymd = d.strftime("%Y%m%d")
        raw = download("K", ymd, use_cache=False)
        if raw is None:
            # 昨日分が未公開なら次回に回す。それより古い日は開催なしとして進める
            if d >= end:
                print(f"  [mbrace] {ymd} の競走成績が未公開 → 次回取り込み")
                break
            print(f"  [mbrace] {ymd} 競走成績なし（全場休み扱い）")
        else:
            rows = parse_k(raw, ymd)
            update_state(state, rows)
            print(f"  [mbrace] {ymd} 取り込み {len(rows)}艇")
        state["asof"] = ymd
        changed = True
        d += timedelta(days=1)
    if changed:
        save_state(state)
    return state["asof"]


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "update":
        print("選手成績の更新:", update(sys.argv[2] if len(sys.argv) > 2 else None))
        # 当日の番組表を先に取得・キャッシュしておく（LZH展開が動くかの確認も兼ねる）
        today = datetime.now(JST).strftime("%Y%m%d")
        prog = get_program(today, "07")
        print(f"当日({today})の蒲郡番組表: {len(prog)}レース"
              + ("" if prog else "（非開催・未公開・展開失敗のいずれか）"))
    else:
        print("使い方: python mbrace.py update [YYYYMMDD]")
