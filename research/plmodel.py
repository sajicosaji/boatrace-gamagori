"""段階別 Plackett-Luce（1着・2着・3着で別々の係数を持つ条件付きロジット）。
特徴量は艇番ごとに別係数（艇番×特徴量の交互作用）。学習・予測・評価の共通部品。
"""
import math
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.optimize import minimize

ROOT = Path(r"C:\Users\dash_\boatrace_data")
GRADE = {"A1": 3, "A2": 2, "B1": 1, "B2": 0}


def base_features(F: pd.DataFrame) -> pd.DataFrame:
    """生データ→特徴量（すべてレース時点で既知の値のみ）。live 側でも同じ式を使う。"""
    X = pd.DataFrame(index=F.index)
    nat = F["nat_win"].fillna(F["nat_win"].median())
    loc_ok = F["loc_win"].fillna(0) > 0
    X["nat_win"] = nat
    X["nat2"] = F["nat2"].fillna(F["nat2"].median())
    X["loc_win"] = np.where(loc_ok, F["loc_win"], nat)
    X["loc_none"] = (~loc_ok).astype(float)
    X["a1"] = (F["grade"] == "A1").astype(float)
    X["a2"] = (F["grade"] == "A2").astype(float)
    X["b2"] = (F["grade"] == "B2").astype(float)
    X["motor2"] = F["motor2"].fillna(33.0)
    X["boat2"] = F["boat2"].fillna(33.0)
    X["weight"] = F["weight"].fillna(52.0)
    X["age"] = F["age"].fillna(40.0)
    X["ex_dev"] = F["ex_dev"].fillna(0.0)
    X["ex_top"] = (F["ex_rank"] == 1).astype(float)
    # 今節（同節の前走まで）平均着順。少数レースは3.5に縮める
    X["kos"] = 3.5 - (F["kos_sum"] + 3.5 * 2) / (F["kos_cnt"] + 2)
    # 直近3節
    X["r3_avg"] = 3.5 - (F["r3_sum"].fillna(0) + 3.5 * 5) / (F["r3_cnt"].fillna(0) + 5)
    X["r3_win"] = (F["r3_win"].fillna(0) + 0.15 * 5) / (F["r3_cnt"].fillna(0) + 5)
    X["r3_st"] = (F["r3_st"].fillna(0) + 0.16 * 5) / (F["r3_stn"].fillna(0) + 5)
    X["r3_f"] = (F["r3_f"].fillna(0) > 0).astype(float)
    # コース別（艇番別）通算: 1着率・3連対率・平均ST（艇番の平均値に縮める）
    lane_win = F["lane"].map({1: .55, 2: .14, 3: .12, 4: .10, 5: .06, 6: .03})
    lane_t3 = F["lane"].map({1: .80, 2: .55, 3: .50, 4: .45, 5: .35, 6: .25})
    X["cl_win"] = (F["cl_win"] + lane_win * 10) / (F["cl_n"] + 10) - lane_win
    X["cl_t3"] = (F["cl_t3"] + lane_t3 * 10) / (F["cl_n"] + 10) - lane_t3
    X["cl_st"] = (F["cl_st"] + 0.16 * 10) / (F["cl_stn"] + 10)
    # 気象（レース単位。艇番との交互作用でのみ効く）
    X["wind"] = F["wind"].fillna(0).clip(0, 12)
    X["wave"] = F["wave"].fillna(0).clip(0, 20)
    return X


GROUPS = {
    "B": ["nat_win", "nat2", "loc_win", "loc_none", "a1", "a2", "b2", "motor2", "boat2", "weight", "age"],
    "EX": ["ex_dev", "ex_top"],
    "KOS": ["kos"],
    "R3": ["r3_avg", "r3_win", "r3_st", "r3_f"],
    "CL": ["cl_win", "cl_t3", "cl_st"],
    "WX": ["wind", "wave"],
}


class Data:
    """レース×6艇のパディング済み配列"""

    def __init__(self, F: pd.DataFrame, X: pd.DataFrame, cols: list[str], mu=None, sd=None):
        F = F.sort_values(["date", "jcd", "rno", "lane"])
        X = X.loc[F.index, cols].astype(float)
        self.mu = X.mean() if mu is None else mu
        self.sd = X.std().replace(0, 1) if sd is None else sd
        Z = ((X - self.mu) / self.sd).to_numpy()
        key = F["date"].astype(str) + F["jcd"].astype(str) + F["rno"].astype(str).str.zfill(2)
        codes, uniq = pd.factorize(key, sort=False)
        n = len(uniq)
        lane = F["lane"].to_numpy() - 1
        P = len(cols)
        self.races = uniq
        self.mask = np.zeros((n, 6), bool)
        self.mask[codes, lane] = True
        self.Z = np.zeros((n, 6, P))
        self.Z[codes, lane] = Z
        fin = F["finish"].to_numpy()
        self.pos = np.full((n, 3), -1)
        for k in (1, 2, 3):
            sel = fin == k
            self.pos[codes[sel], k - 1] = lane[sel]
        self.cols = cols
        meta = F.groupby(key, sort=False).agg(date=("date", "first"), jcd=("jcd", "first"), rno=("rno", "first"))
        self.meta = meta.loc[uniq].reset_index(drop=True)

    def design(self):
        """艇番 one-hot(2〜6) + 特徴量×艇番 one-hot → (n, 6, 5 + 6P)"""
        n, _, P = self.Z.shape
        D = np.zeros((n, 6, 5 + 6 * P))
        for l in range(1, 6):
            D[:, l, l - 1] = 1.0
        for l in range(6):
            D[:, l, 5 + l * P: 5 + (l + 1) * P] = self.Z[:, l, :]
        return D


def _stage_loss(theta, D, avail, chosen, lam):
    U = D @ theta
    U = np.where(avail, U, -1e9)
    m = U.max(1, keepdims=True)
    E = np.exp(U - m) * avail
    S = E.sum(1, keepdims=True)
    Pm = E / S
    idx = np.arange(len(chosen))
    ll = (U[idx, chosen] - m[:, 0] - np.log(S[:, 0])).sum()
    Y = np.zeros_like(Pm); Y[idx, chosen] = 1
    pen = theta.copy(); pen[:5] = 0  # 艇番の切片は正則化しない
    grad = -np.einsum("ri,rik->k", Y - Pm, D) + lam * pen
    return -ll + 0.5 * lam * pen @ pen, grad


def _pair_cols(n, prev_lanes):
    """先着艇の艇番×自艇の艇番の one-hot（2着・3着段で「誰が勝ったか」で並びが変わる効果）"""
    k = len(prev_lanes)
    E = np.zeros((n, 6, 36 * k))
    r = np.arange(n)
    for t, pl in enumerate(prev_lanes):
        for j in range(6):
            E[r, j, 36 * t + pl * 6 + j] = 1.0
    return E


def fit(data: Data, lam: float = 1.0, pairs: bool = True) -> list[np.ndarray]:
    D = data.design()
    thetas = []
    avail = data.mask.copy()
    for s in range(3):
        ok = data.pos[:, s] >= 0
        for t in range(s):
            ok &= data.pos[:, t] >= 0
        a = avail[ok].copy()
        for t in range(s):
            a[np.arange(ok.sum()), data.pos[ok, t]] = False
        Ds = D[ok]
        if pairs and s > 0:
            Ds = np.concatenate([Ds, _pair_cols(ok.sum(), [data.pos[ok, t] for t in range(s)])], axis=2)
        res = minimize(_stage_loss, np.zeros(Ds.shape[2]), args=(Ds, a, data.pos[ok, s], lam),
                       jac=True, method="L-BFGS-B", options={"maxiter": 1000})
        thetas.append(res.x)
    return thetas


def probs(data: Data, thetas):
    """勝率(n,6)・2連単(n,6,6)・3連単(n,6,6,6)。thetas の長さで艇番ペア項の有無を判定"""
    D = data.design()
    P0 = D.shape[2]
    th1, th2, th3 = thetas
    neg = np.where(data.mask, 0.0, -1e9)
    U1 = D @ th1 + neg
    E1 = np.exp(U1 - U1.max(1, keepdims=True)) * data.mask
    p1 = E1 / E1.sum(1, keepdims=True)
    G2 = th2[P0:].reshape(6, 6) if len(th2) > P0 else np.zeros((6, 6))
    G3a = th3[P0:P0 + 36].reshape(6, 6) if len(th3) > P0 else np.zeros((6, 6))
    G3b = th3[P0 + 36:].reshape(6, 6) if len(th3) > P0 else np.zeros((6, 6))
    idx = np.arange(6)
    # 2着: U2[r,i,j] = base_j + G2[i,j]
    U2 = (D @ th2[:P0])[:, None, :] + G2[None] + neg[:, None, :]
    U2[:, idx, idx] = -1e9
    E2 = np.exp(U2 - U2.max(2, keepdims=True))
    p2 = E2 / E2.sum(2, keepdims=True)
    ex = p1[:, :, None] * p2
    # 3着: U3[r,i,j,k] = base_k + G3a[i,k] + G3b[j,k]
    U3 = ((D @ th3[:P0])[:, None, None, :] + G3a[None, :, None, :] + G3b[None, None, :, :]
          + neg[:, None, None, :])
    U3[:, idx, :, idx] = -1e9
    U3[:, :, idx, idx] = -1e9
    E3 = np.exp(U3 - U3.max(3, keepdims=True))
    p3 = E3 / E3.sum(3, keepdims=True)
    tri = ex[:, :, :, None] * p3
    tri[:, idx, idx, :] = 0
    return p1, ex, tri
