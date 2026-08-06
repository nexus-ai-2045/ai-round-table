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

## v0.2 Tier1 ハードニング (2026-08-06)

PR #3 (別セッション実装) が main に入った後、そちらの Tier1 経路を敵対レビュー観点で
検査して見つかった穴。**設計フォーク (PR #4) は superseded として閉じ、main を土台に
安全性だけ移植した**。

| # | severity | 内容 | 対応 |
|---|---|---|---|
| T1 | HIGH | `subprocess.Popen(text=True)` に encoding 指定がなく、Windows では cp932。**日本語 packet が壊れる** | `encoding="utf-8"` / `errors="replace"` を明示 |
| T2 | HIGH | `thread/start` に sandbox / approvalPolicy が無く**サーバ既定依存**。実測で Codex の既定は `danger-full-access` になりうる | `sandbox="workspace-write"` + `approvalPolicy="never"` を明示 |
| T3 | HIGH | `cwd` が未配線で app-server が CLI の実行ディレクトリを継承。書込範囲が不定 | `cli` → `get_relay(cwd=str(tp.root))` → spawn/thread の両方へ。**議題ディレクトリに限定** |
| T4 | HIGH | `close()` が `proc.kill()` のみ。孫プロセスが孤児化 | stdin close → terminate → wait → `taskkill /T /F` |
| T5 | MED | `ephemeral=True` にしていた | 削除。一時席にすると会話がアプリに残らず「席を CEO が読める」要件 (DESIGN v6 §0) を壊す |

検証: `tests/test_v02_tier1_hardening.py` (6 本)。実 CLI を起動せず、
危険な既定に戻ったら落ちる形にしてある。全 suite 49 passed。

**未対応で残す穴 (v0.3)**: `journal.json` / `seats.json` に `minutes.md` 相当の hash 保護がない。
Tier1 の書込範囲を議題配下に絞ったので露出は減ったが、同一議題内では席が journal.json を書ける。

## PR #4 のテスト資産を main へ移植 (2026-08-06)

設計フォークで PR #4 を閉じた際、そこにあった**守備範囲まで一緒に失われていた**ことに
気づいたため、`tests/test_v02_invariants.py` として main の API に合わせて持ち込んだ。
実装の内部構造ではなく**守るべき性質**でまとめてある (API が変わっても生き残る形)。

移植前後の守備範囲:

| 対象 | main (移植前) | 移植後 |
|---|---|---|
| 状態遷移の逆行・終端 | 部分的 | 6 本 (merged/failed 終端・逆行拒否・非永続化・失敗分岐) |
| slug パストラバーサル | 1 本 | 24 本 (`..` `/abs` `C:/abs` `\server\share` `nul\x00` 等) |
| KPI 計測の性質 | なし | 2 本 (再ロード永続・status が観測専用) |
| 失敗集計 | 部分的 | 2 本 (schema 畳み込み・空 detail を落とさない) |
| 議事録防御 | 11 本 | +4 本 (改ざん fail-closed・見出し注入全経路・Round 抑止攻撃) |

**この移植で main の実バグを 1 件検出した**: `topic_dir()` が slug を検証しておらず、
`ensure_topic()` 経由でしか軸 D が効いていなかった。`topic_dir` を直接使う経路
(将来の CLI・ツール) は root 外を指せる状態だった → 合成と検証を同じ場所に置くよう修正。

全 suite 49 → **113 passed**。

## Codex bot レビュー (PR #5, 2026-08-06) — 対応

| # | 判定 | 内容 | 対応 |
|---|---|---|---|
| P1-a | **却下 (指摘が誤り)** | 「sandbox は camelCase `workspaceWrite` を送れ」 | スキーマで確定: `thread/start.sandbox` の型は **`SandboxMode` = `["read-only", "workspace-write", "danger-full-access"]`** で **hyphen が正**。camelCase は `turn/start.sandboxPolicy` (型 `SandboxPolicy` のオブジェクト `{"type": "workspaceWrite"}`) の話で、bot が README の turn 例を thread と混同している。変更しない |
| P1-b | 採用 | 非 Windows で子孫が回収されない | `_ProcessTree` を新設。POSIX は `start_new_session=True` + `killpg` |
| P1-c | 採用 | Windows で terminate 成功時に木の掃除を飛ばす | **Job Object + `KILL_ON_JOB_CLOSE`** に変更。ハンドルを閉じた時点で木ごと終わるので、terminate が成功した経路でも取りこぼさない |
| P2 | 採用 | 相対 `--root` だと cwd が二重解決される | `str(tp.root.resolve())` を渡す |

検証: `tests/test_v02_tier1_hardening.py` に 3 本追加 (POSIX グループ分離 / terminate 成功時の木回収 / 相対 root の絶対化)。全 suite **115 passed**。

一次情報: `codex app-server generate-json-schema` の `ClientRequest.json` →
`definitions.SandboxMode` / `definitions.SandboxPolicy` / `definitions.AskForApproval`。

## 実往復スモークで判明した致命的ブロッカー (2026-08-06)

**Tier1 は実環境で 100% 失敗していた。** 実議題 `2026-08-06-tier1-live` を `--tier 1` で
dispatch したところ `thread/start: timeout after 5s` で Tier3 に縮退した。

素の app-server を計測した結果:

| 段階 | 実測 | 修正前の既定 | 判定 |
|---|---|---|---|
| `initialize` | **6.66s** | 8s | ギリギリ (環境次第で落ちる) |
| `thread/start` | **20.9s** | **5s** | **必ず失敗** |

→ `TIMEOUT_INITIALIZE=60` / `TIMEOUT_THREAD_START=120` / `TIMEOUT_TURN_START=120` に修正し、
実測値を根拠としてコードに残した。回帰防止テスト
`test_timeouts_have_margin_over_measured_latency` で数値を不変条件として固定。

**学び**: fake stdio のユニットテストは即答するので、この種の穴を構造的に検出できない。
「実装済み・単体テスト green」と「運用で効いている」は別物であることの実例。
実往復スモークを受け入れ基準に入れていた判断が正しかった。

**副産物**: 実サーバが `sandbox: "workspace-write"` (hyphen) を受理した。Codex bot の
camelCase 指摘 (PR #5 P1-a) が誤りであることが、schema に加えて**実測でも裏付けられた**。
