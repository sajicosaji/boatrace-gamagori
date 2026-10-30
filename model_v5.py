"""
予想モデル v5（2026-10-30〜）: 段階別 Plackett-Luce
  ・1着・2着・3着でそれぞれ別の係数（旧モデルは同じ係数を使い回していたため2・3着が不正確）
  ・特徴量の効き方を艇番ごとに別々に推定（例: 1号艇の勝率と6号艇の勝率は効き方が違う）
  ・2着・3着は「どの艇が先着したか」で並びが変わる効果（艇番ペア項）も持つ
学習: ボートレース公式（mbrace）の番組表・競走成績から、レース時点で分かっていた
情報だけで作った特徴量（未来情報なし）。蒲郡 2021/1〜の全レース。係数は model_v5.json。
学習スクリプトは research/ 配下。

ここでは numpy を使わず純 Python で計算する（Actions の依存を増やさないため）。
特徴量の式は research/plmodel.py の base_features() と完全に同じにすること。
"""
import json
import math
from pathlib import Path

_MODEL: dict | None = None
LANE_WIN = {1: .55, 2: .14, 3: .12, 4: .10, 5: .06, 6: .03}
LANE_T3 = {1: .80, 2: .55, 3: .50, 4: .45, 5: .35, 6: .25}


def load() -> dict:
    global _MODEL
    if _MODEL is None:
        _MODEL = json.loads((Path(__file__).parent / "model_v5.json").read_text(encoding="utf-8"))
    return _MODEL


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def features(boats: list[dict], wind, wave) -> dict[int, dict[str, float]]:
    """
    boats: 各艇 {lane, nat_win, nat2, loc_win, grade, motor2, boat2, weight, age, ex_time,
                 kos_sum, kos_cnt, r3_sum, r3_cnt, r3_win, r3_st, r3_stn, r3_f,
                 cl_n, cl_win, cl_t3, cl_st, cl_stn}（欠場艇は含めない）
    返り値: {艇番: {特徴量名: 値}}（標準化前）
    """
    M = load()
    exs = [b["ex_time"] for b in boats if b.get("ex_time") is not None]
    ex_mean = sum(exs) / len(exs) if exs else None
    ex_min = min(exs) if exs else None
    out = {}
    for b in boats:
        lane = b["lane"]
        nat = b["nat_win"] if b.get("nat_win") is not None else M["fill"]["nat_win"]
        loc = b.get("loc_win") or 0.0
        loc_ok = loc > 0
        ex = b.get("ex_time")
        lw, lt = LANE_WIN[lane], LANE_T3[lane]
        g = b.get("grade")
        out[lane] = {
            "nat_win": nat,
            "nat2": b["nat2"] if b.get("nat2") is not None else M["fill"]["nat2"],
            "loc_win": loc if loc_ok else nat,
            "loc_none": 0.0 if loc_ok else 1.0,
            "a1": float(g == "A1"), "a2": float(g == "A2"), "b2": float(g == "B2"),
            "motor2": b["motor2"] if b.get("motor2") is not None else 33.0,
            "boat2": b["boat2"] if b.get("boat2") is not None else 33.0,
            "weight": b["weight"] if b.get("weight") is not None else 52.0,
            "age": b["age"] if b.get("age") is not None else 40.0,
            "ex_dev": _clip(ex_mean - ex, -0.5, 0.5) if (ex is not None and ex_mean is not None) else 0.0,
            "ex_top": 1.0 if (ex is not None and ex == ex_min) else 0.0,
            "kos": 3.5 - (b.get("kos_sum", 0) + 3.5 * 2) / (b.get("kos_cnt", 0) + 2),
            "r3_avg": 3.5 - (b["r3_sum"] + 3.5 * 5) / (b["r3_cnt"] + 5),
            "r3_win": (b["r3_win"] + 0.15 * 5) / (b["r3_cnt"] + 5),
            "r3_st": (b["r3_st"] + 0.16 * 5) / (b["r3_stn"] + 5),
            "r3_f": 1.0 if b["r3_f"] > 0 else 0.0,
            "cl_win": (b["cl_win"] + lw * 10) / (b["cl_n"] + 10) - lw,
            "cl_t3": (b["cl_t3"] + lt * 10) / (b["cl_n"] + 10) - lt,
            "cl_st": (b["cl_st"] + 0.16 * 10) / (b["cl_stn"] + 10),
            "wind": _clip(wind or 0, 0, 12),
            "wave": _clip(wave or 0, 0, 20),
        }
    return out


def _base_utils(theta, feats: dict[int, dict], M) -> dict[int, float]:
    cols, mu, sd = M["cols"], M["mu"], M["sd"]
    P = len(cols)
    u = {}
    for lane, f in feats.items():
        v = theta[lane - 2] if lane >= 2 else 0.0
        off = 5 + (lane - 1) * P
        for k, c in enumerate(cols):
            v += theta[off + k] * (f[c] - mu[k]) / sd[k]
        u[lane] = v
    return u


def _softmax(u: dict[int, float]) -> dict[int, float]:
    m = max(u.values())
    e = {k: math.exp(v - m) for k, v in u.items()}
    s = sum(e.values())
    return {k: v / s for k, v in e.items()}


def predict(feats: dict[int, dict]) -> tuple[dict[int, float], dict[str, float], dict[str, float]]:
    """(勝率{艇番:p}, 2連単{"i-j":p}, 3連単{"i-j-k":p})"""
    M = load()
    th1, th2, th3 = M["theta"]
    P0 = 5 + 6 * len(M["cols"])
    lanes = sorted(feats)
    p1 = _softmax(_base_utils(th1, feats, M))
    u2 = _base_utils(th2, feats, M)
    u3 = _base_utils(th3, feats, M)
    g2 = lambda i, j: th2[P0 + (i - 1) * 6 + (j - 1)] if len(th2) > P0 else 0.0
    g3a = lambda i, k: th3[P0 + (i - 1) * 6 + (k - 1)] if len(th3) > P0 else 0.0
    g3b = lambda j, k: th3[P0 + 36 + (j - 1) * 6 + (k - 1)] if len(th3) > P0 else 0.0
    ex, tri = {}, {}
    for i in lanes:
        p2 = _softmax({j: u2[j] + g2(i, j) for j in lanes if j != i})
        for j, pj in p2.items():
            ex[f"{i}-{j}"] = p1[i] * pj
            p3 = _softmax({k: u3[k] + g3a(i, k) + g3b(j, k) for k in lanes if k not in (i, j)})
            for k, pk in p3.items():
                tri[f"{i}-{j}-{k}"] = p1[i] * pj * pk
    return p1, ex, tri


def combine_win(p1: dict[int, float], win_odds: dict[int, float]) -> dict[int, float] | None:
    """モデル勝率と単勝オッズ（市場）を組み合わせた勝率。全艇のオッズが揃わない時は None。
    p ∝ p_model^a × p_market^b（係数は学習に使っていない5,401レースで推定）"""
    M = load()
    a, b = M["combo_win"]["a"], M["combo_win"]["b"]
    if any((win_odds.get(l) or 0) < 1.0 for l in p1):
        return None
    inv = {l: 1.0 / win_odds[l] for l in p1}
    s = sum(inv.values())
    u = {l: a * math.log(max(p1[l], 1e-9)) + b * math.log(inv[l] / s) for l in p1}
    return _softmax(u)
