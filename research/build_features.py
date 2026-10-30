"""boats.pkl / races.pkl から「そのレース時点で分かっていた情報だけ」で特徴量を作る。
過去成績系は必ず当該レースより前のレースだけから計算する（未来情報リーク防止）。
出力: feats.pkl
"""
import re
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(r"C:\Users\dash_\boatrace_data")


def main():
    D = pd.read_pickle(ROOT / "boats.pkl")
    R = pd.read_pickle(ROOT / "races.pkl")
    D["dt"] = pd.to_datetime(D["date"])
    D["absent"] = D["fin_raw"].str.startswith("K")
    D = D[~D["absent"]].copy()
    # 着外（F/L/転覆/失格等）は6着扱いで成績計算に入れる
    D["fin_num"] = D["finish"].fillna(6.0).clip(upper=6)
    D["is_win"] = (D["finish"] == 1).astype(float)
    D["is_top2"] = (D["finish"] <= 2).astype(float)
    D["is_top3"] = (D["finish"] <= 3).astype(float)
    st = D["st_raw"].astype(str)
    D["st_f"] = st.str.contains("F").astype(float)
    D["st_num"] = pd.to_numeric(st.str.extract(r"(\d*\.\d+)")[0], errors="coerce")
    D.loc[D["st_f"] == 1, "st_num"] = np.nan
    D["st_ok"] = D["st_num"].notna().astype(float)
    D["st0"] = D["st_num"].fillna(0)

    # ── 節（同じ選手が同じ場で連続出場している期間）──
    D = D.sort_values(["toban", "dt", "jcd", "rno"]).reset_index(drop=True)
    g = D.groupby("toban")
    prev_dt, prev_j = g["dt"].shift(), g["jcd"].shift()
    new = prev_dt.isna() | (D["jcd"] != prev_j) | ((D["dt"] - prev_dt).dt.days > 1)
    D["meet"] = new.astype(int).groupby(D["toban"]).cumsum()

    # 今節成績（当該レースより前の同節レースのみ）
    gm = D.groupby(["toban", "meet"])
    D["kos_cnt"] = gm.cumcount()
    D["kos_sum"] = gm["fin_num"].cumsum() - D["fin_num"]
    D["kos_win"] = gm["is_win"].cumsum() - D["is_win"]

    # 直近3節（現在の節より前の3節）
    M = gm.agg(m_sum=("fin_num", "sum"), m_cnt=("fin_num", "size"), m_win=("is_win", "sum"),
               m_t2=("is_top2", "sum"), m_st=("st0", "sum"), m_stn=("st_ok", "sum"),
               m_f=("st_f", "sum")).reset_index()
    M = M.sort_values(["toban", "meet"])
    for c in ["m_sum", "m_cnt", "m_win", "m_t2", "m_st", "m_stn", "m_f"]:
        sh = M.groupby("toban")[c].shift(1).fillna(0)
        M["r3_" + c[2:]] = sh.groupby(M["toban"]).rolling(3, min_periods=1).sum().reset_index(level=0, drop=True)
    D = D.merge(M[["toban", "meet"] + [c for c in M.columns if c.startswith("r3_")]], on=["toban", "meet"], how="left")

    # コース(=艇番)別の通算成績（当該レースより前のみ、2020年以降の記録で計算）
    D = D.sort_values(["toban", "lane", "dt", "rno"]).reset_index(drop=True)
    gl = D.groupby(["toban", "lane"])
    D["cl_n"] = gl.cumcount()
    D["cl_win"] = gl["is_win"].cumsum() - D["is_win"]
    D["cl_t3"] = gl["is_top3"].cumsum() - D["is_top3"]
    D["cl_st"] = gl["st0"].cumsum() - D["st0"]
    D["cl_stn"] = gl["st_ok"].cumsum() - D["st_ok"]

    # ── レース内の相対値 ──
    key = ["date", "jcd", "rno"]
    D = D.sort_values(key + ["lane"]).reset_index(drop=True)
    gr = D.groupby(key)
    D["n_boats"] = gr["lane"].transform("size")
    D["ex_mean"] = gr["ex_time"].transform("mean")
    D["ex_dev"] = (D["ex_mean"] - D["ex_time"]).fillna(0).clip(-0.5, 0.5)
    D["ex_rank"] = gr["ex_time"].rank(method="min")
    D = D.merge(R[key + ["wind", "wave", "weather", "wind_dir"]], on=key, how="left")

    keep = ["date", "dt", "jcd", "rno", "lane", "toban", "age", "weight", "grade", "nat_win", "nat2",
            "loc_win", "loc2", "motor_no", "motor2", "boat_no", "boat2", "deadline", "fin_raw", "finish",
            "ex_time", "ex_dev", "ex_rank", "course", "st_num", "st_f", "kos_cnt", "kos_sum", "kos_win",
            "r3_sum", "r3_cnt", "r3_win", "r3_t2", "r3_st", "r3_stn", "r3_f", "cl_n", "cl_win", "cl_t3",
            "cl_st", "cl_stn", "n_boats", "wind", "wave", "weather", "wind_dir"]
    D[keep].to_pickle(ROOT / "feats.pkl")
    print(len(D), "rows", D["date"].min(), D["date"].max())


if __name__ == "__main__":
    main()
