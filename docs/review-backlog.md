# レビュー残課題台帳

敵対レビュー (2026-07-28, workflow run wf_fb78fdfa-246) の所見のうち、
H1 (TOCTOU) と M1-M4 は修正済み。

v0.2 は MPC + FDE 次元圧縮により **主軸 A (手数) + detector 3 個** に畳んだ。
観測されていない穴は消化しない (次元の呪い回避)。詳細: `docs/plans/2026-08-05-v0.2-mpc-plan.md`。

| # | 内容 | 状態 |
|---|---|---|
| L1 | set_state が detail を上書きし delivered の "tier3" 記録が消える | **hold** (v0.3 で履歴配列化。現状は最新 detail のみ) |
| L2 | 「空出力」が parse に合流 / OSError も parse と誤分類 | **hold** (軸 B detector = failure_stats のみ) |
| L3 | 状態機械の遷移検証なし (failed→merged の逆行が可能) | **done v0.2** (前進のみ / 終端逆行 ValueError) |
| L4 | close 二重実行で verdict 行が結合破損 / status: open 置換の topic 誤爆 | **hold** (未観測) |
| L5 | slug 未サニタイズ | **done v0.2** (`[a-z0-9]+(?:-[a-z0-9]+)*`) |
| L6 | 席出力の UTF-8 BOM | **done partial v0.2** (`utf-8-sig` 受理) |
| L7 | round 進行が指名バッチ vs frontmatter 全参加者 | **hold** (設計どおり frontmatter 参加者基準を維持) |
| L8 | watcher 経由 schema 失敗経路のテスト | **hold** (既存 e2e で一部カバー、専用は v0.3) |
| L9 | merge_opinion 単体の escape | **hold** (defense-in-depth は v0.3) |
| S1 | 背景を書く CLI が無い | **done v0.2** (`--background` / `set-background`) |
| S2 | パイプで exit code が消える | **done v0.2** (`last-result.json` + PROTOCOL 明記) |
| S3 | Tier3 貼付可否が不明 | **done partial v0.2** (Tier1 試行 + 縮退 + `tier3_paste_required` 記録) |

## v0.2 で置いた detector (修正ではなく検知)

| 軸 | Detector | 置き場所 |
|---|---|---|
| A 手数 | `human_actions` カウンタ | journal / `status` |
| B 回収頑健性 | `failure_stats` | journal / `status` |
| C 状態正しさ | 遷移検証 | `Journal.set_state` |
| D 境界 | slug 検証 | `paths.ensure_topic` |

原本: workflow 出力 (scratch には保存せず、本台帳を正とする)
