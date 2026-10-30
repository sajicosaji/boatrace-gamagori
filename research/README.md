# research/ — モデル v5 の学習・検証スクリプト（ローカル専用）

データは `C:\Users\dash_\boatrace_data`（リポジトリ外・約160MB）に置く。すべて `python -P <スクリプト>` で実行。

| 順 | スクリプト | 内容 |
|---|---|---|
| 1 | download_mbrace.py | 公式 mbrace の番組表(B)・競走成績(K) 日次LZHを取得（2020/1〜） |
| 2 | parse_mbrace.py | LZH を解析して boats.pkl / races.pkl |
| 3 | build_features.py | レース時点の情報だけで特徴量を作る → feats.pkl |
| 4 | scrape_win_odds.py / scrape_ex_odds.py | boatrace.jp から蒲郡の確定単勝・2連単オッズ（2024/7〜） |
| 5 | train_production.py | 本番用 model_v5.json を出力 |
| 6 | build_state.py | player_stats.json の初期作成＋学習特徴量との一致確認 |
| 7 | check_parity.py | 本番の model_v5.py と学習側 plmodel.py の予測一致確認 |

検証（2026-10-30 時点の結果）:
- exp1/exp2/exp5: 特徴量グループ・艇番ペア項・正則化の比較（検証期間 2024/7〜2025/6）
- exp3: オッズを見ない買い方の回収率（すべて100%未満: 70〜94%）
- exp4/exp6: モデル×確定単勝オッズの組み合わせ。B期間（2025/7〜2026/10, 2,950R）で
  期待値1.0以上 124%・1.1以上 138%（確定オッズで判断した場合の上限値）
- exp7: 期待値の較正と300点での振れ幅

注意: exp6 で一度「結果に依存したレース除外」（1.0倍の本命が勝ったレースだけ落ちる）バグがあり、
修正前は回収率156〜268%と過大に出ていた。オッズが欠けたレースの除外は必ず結果と無関係に行うこと。
