"""mbrace の番組表(B)・競走成績(K) LZH を解析して pandas の pickle にまとめる。
LZH は Windows 標準の tar(bsdtar) で標準出力に展開し、ディスクには書き出さない。
出力:
  boats.pkl : 1行=1艇（番組表の情報 + 成績の着順・展示タイム等）
  races.pkl : 1行=1レース（気象・払戻金）
使い方: python -P parse_mbrace.py
"""
import re, subprocess, sys, pickle
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
import pandas as pd

ROOT = Path(r"C:\Users\dash_\boatrace_data")
FW = str.maketrans("０１２３４５６７８９ＲＨｍ：", "0123456789RHm:")


def read_lzh(p: Path) -> bytes:
    r = subprocess.run(["tar", "-xOf", str(p)], capture_output=True)
    return r.stdout if r.returncode == 0 else b""


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ───────────── 番組表 ─────────────
B_RACE = re.compile(r"^\s*(\d{1,2})R\s")

def parse_b(p: Path) -> list[dict]:
    raw = read_lzh(p)
    if not raw:
        return []
    ymd = "20" + p.stem[1:]
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
        line = bl.decode("cp932", errors="replace")
        t = line.translate(FW)
        m = B_RACE.match(t)
        if m and "H" in t and "m" in t:
            rno = int(m.group(1))
            dm = re.search(r"締切予定(\d{1,2}:\d{2})", t)
            deadline = dm.group(1) if dm else None
            continue
        # 艇の行（バイト位置固定）: 艇番 登番 名前(8B) 年齢 支部(4B) 体重 級別 …
        if rno and len(bl) >= 40 and bl[:1] in b"123456" and bl[1:2] == b" " and bl[2:6].isdigit():
            try:
                lane = int(bl[0:1]); toban = int(bl[2:6])
                age = int(bl[14:16]); weight = int(bl[20:22])
                grade = bl[22:24].decode("ascii", errors="replace")
                # 数値欄も固定幅（ボートNOが3桁だと直前のモーター2率と空白なしで連結される）
                spans = [(24, 29), (29, 35), (35, 40), (40, 46), (46, 49), (49, 55), (55, 58), (58, 64)]
                v = [num(bl[a:b].decode("ascii", errors="replace").strip()) for a, b in spans]
                kos = bl[64:76].decode("cp932", errors="replace")
                out.append(dict(date=ymd, jcd=jcd, rno=rno, lane=lane, toban=toban, age=age,
                                weight=weight, grade=grade, nat_win=v[0], nat2=v[1], loc_win=v[2],
                                loc2=v[3], motor_no=v[4], motor2=v[5], boat_no=v[6], boat2=v[7],
                                kosetsu_b=kos, deadline=deadline))
            except (ValueError, IndexError):
                pass
    return out


# ───────────── 競走成績 ─────────────
K_RACE = re.compile(r"^\s+(\d{1,2})R\s+.*H\d+m")
K_ROW = re.compile(r"^\s+(\S{1,2})\s+([1-6])\s+(\d{4})\s+(\D+?)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)")
K_PAY = re.compile(r"^\s+(単勝|複勝|２連単|２連複|拡連複|３連単|３連複)?\s+((?:[1-6](?:-[1-6]){0,2}\s+\d+\s*(?:人気\s+\d+)?\s*)+)$")
KIND = {"単勝": "tan", "複勝": "fuku", "２連単": "ex", "２連複": "qn", "拡連複": "wide",
        "３連単": "tri", "３連複": "trio"}

def parse_k(p: Path) -> tuple[list[dict], list[dict]]:
    raw = read_lzh(p)
    if not raw:
        return [], []
    ymd = "20" + p.stem[1:]
    boats, races = [], []
    jcd, cur, kind = None, None, None
    for bl in raw.split(b"\n"):
        bl = bl.rstrip(b"\r")
        m = re.match(rb"^(\d\d)KBGN", bl)
        if m:
            jcd, cur = m.group(1).decode(), None
            continue
        if re.match(rb"^\d\dKEND", bl):
            jcd, cur = None, None
            continue
        if jcd is None:
            continue
        line = bl.decode("cp932", errors="replace")
        m = K_RACE.match(line)
        if m and "風" in line:
            rno = int(m.group(1))
            wm = re.search(r"風\s+(\S+)\s+(\d+)m", line)
            vm = re.search(r"波\s+(\d+)cm", line)
            tm = re.search(r"H\d+m\s+(\S+)", line)
            cur = dict(date=ymd, jcd=jcd, rno=rno, weather=tm.group(1) if tm else None,
                       wind_dir=wm.group(1) if wm else None, wind=int(wm.group(2)) if wm else None,
                       wave=int(vm.group(1)) if vm else None, pay={})
            races.append(cur)
            kind = None
            continue
        if cur is None:
            continue
        m = K_ROW.match(line)
        if m and not line.strip().startswith(("単勝", "複勝")):
            fin, lane, toban = m.group(1), int(m.group(2)), int(m.group(3))
            ex, course, st = m.group(7), m.group(8), m.group(9)
            boats.append(dict(date=ymd, jcd=jcd, rno=cur["rno"], lane=lane, toban=toban, fin_raw=fin,
                              finish=int(fin) if fin.isdigit() else None, ex_time=num(ex),
                              course=int(course) if course.isdigit() else None, st_raw=st))
            continue
        m = K_PAY.match(line)
        if m:
            if m.group(1):
                kind = KIND[m.group(1)]
            if kind is None:
                continue
            body = m.group(2)
            for cm in re.finditer(r"([1-6](?:-[1-6]){0,2})\s+(\d+)(?:\s*人気\s+(\d+))?", body):
                cur["pay"].setdefault(kind, []).append((cm.group(1), int(cm.group(2)),
                                                        int(cm.group(3)) if cm.group(3) else None))
        elif line.strip() == "":
            pass
        else:
            kind = None if not line.startswith("        ") else kind
    return boats, races


def main():
    bfiles = sorted((ROOT / "raw" / "B").glob("b*.lzh"))
    kfiles = sorted((ROOT / "raw" / "K").glob("k*.lzh"))
    with ProcessPoolExecutor(max_workers=8) as ex:
        b_rows = [r for rows in ex.map(parse_b, bfiles, chunksize=16) for r in rows]
        k_out = list(ex.map(parse_k, kfiles, chunksize=16))
    k_boats = [r for b, _ in k_out for r in b]
    k_races = [r for _, rs in k_out for r in rs]
    B = pd.DataFrame(b_rows)
    K = pd.DataFrame(k_boats)
    R = pd.DataFrame(k_races)
    key = ["date", "jcd", "rno", "lane"]
    B = B.drop_duplicates(key, keep="last")
    K = K.drop_duplicates(key, keep="last")
    R = R.drop_duplicates(["date", "jcd", "rno"], keep="last")
    D = B.merge(K, on=key, how="inner", suffixes=("", "_k"))
    D = D[D["toban"] == D["toban_k"]].drop(columns=["toban_k"])
    print(f"番組表 {len(B)}艇 / 成績 {len(K)}艇 / 結合 {len(D)}艇 / レース {len(R)}")
    D.to_pickle(ROOT / "boats.pkl")
    R.to_pickle(ROOT / "races.pkl")


if __name__ == "__main__":
    main()
