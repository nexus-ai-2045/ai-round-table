<!-- repo-preflight:review-record -->

# 公開準備レビュー記録

repo-preflight の検査結果と、それに対する人間の判断を記録します。
**この記録は「検査を通した」証拠であり、「安全である」ことの保証ではありません。**

## 対象

- repository: `nexus-ai-2045/ai-round-table`
- 公開意図: public（時期未定）
- ライセンス: MIT

## 開発保証ゲート

検査ロジックは本リポジトリへコピーしない。上流を直接呼ぶ。`engineering-brain` は埋め込まない。

| 契約 | 上流 | 設定 | 扱い |
|---|---|---|---|
| 文書・実装の宣言整合 | `nexus-ai-2045/repo-preflight`（pin SHA） | `.repo-preflight-consistency.json`（`shadow`） | `consistency_gate` + `readiness_scan`。shadow 所見は止めない。`tool_error` は fail-closed |
| tracked ∧ ignored の新規悪化 | `nexus-ai-2045/ai-ratchet-gate` v0.1.1（wheel + SHA-256） | `.ai-ratchet-gate/baseline.txt` | 既存分は grandfather。baseline に無い新規だけ deny |

古い `.repo-preflight.json` は preferences 用途の旧名だったため、整合契約は `.repo-preflight-consistency.json` へ canonicalize し、旧ファイルは残さない。

### トリガ方針（Actions 課金）

- 開発保証 workflow（`repository-guarantees.yml`）は **`workflow_dispatch` のみ**。`pull_request` / `push` では起動しない
- `workflow_dispatch` で BASE と HEAD が同一（空 diff）のときは差分検査を緑にしない（fail-closed）
- 既存の `test.yml`（pytest）は別契約。本節の開発保証とは混ぜない

### 手元同等の確認手順

feature 枝で `origin/main` との差分がある状態で実行する（`BASE==HEAD` は意図的に失敗させる）。

```bash
# 1) ai-ratchet-gate（tracked∧ignored の新規悪化だけ deny）
python -m pip install --require-hashes -r requirements-tools.txt
python -m ai_ratchet_gate --repo .

# 2) repo-preflight（上流 clone。検査ロジックはコピーしない）
REPO_PREFLIGHT_SHA=f825268978228a3cfb2f5ecba16a74d424134b1a
git clone --no-checkout https://github.com/nexus-ai-2045/repo-preflight.git /tmp/repo-preflight
git -C /tmp/repo-preflight checkout --detach "$REPO_PREFLIGHT_SHA"
test "$(git -C /tmp/repo-preflight rev-parse HEAD)" = "$REPO_PREFLIGHT_SHA"

git fetch origin main
BASE="$(git rev-parse origin/main)"
HEAD="$(git rev-parse HEAD)"
test "$BASE" != "$HEAD"  # 空diff fail-closed

python /tmp/repo-preflight/scripts/consistency_gate.py \
  --repo . --base-ref "$BASE" --require-config --require-mode shadow --json

python /tmp/repo-preflight/scripts/readiness_scan.py \
  --repo . --release --consistency-base-ref origin/main
```

Actions で同等確認する場合は、feature 枝を選んで `repository-guarantees` を `workflow_dispatch` する（default branch 直上だと空 diff で fail-closed）。

## 検査記録

### 2026-08-14 / intent=publish

| 検査 | 結果 | 判断 |
|---|---|---|
| `secret_scan` | pass（0 件） | — |
| `origin` | pass | — |
| `dependency_configuration` | pass（pyproject.toml） | — |
| `commit_identity` | pass（mismatch 0） | — |
| `clean_worktree` | **fail** | 未追跡の議事録ディレクトリが残っていた。公開前に整理する |
| `required_documents` | **fail** | README / LICENSE / SECURITY / CONTRIBUTING / PREFLIGHT が不在 → 起草（commit は公開準備 PR で実施） |
| `personal_path_scan` | **fail** | 下表 |
| `ci_configuration` | unknown（workflow 0） | → GitHub Actions を追加 |
| `dependency_vulnerability_audit` | unknown | 生態系固有の監査が別途必要 |
| `human_visual_review` | unknown | 人間にしか埋められない欄。公開判断時に実施 |

### personal_path_scan の内訳（実測で分類）

検出を機械的に「全部消す」のではなく、1 件ずつ中身を見て分類しました。

| 分類 | 対象 | 対応 |
|---|---|---|
| 実パス | `docs/plans/2026-07-27-v0.1-implementation.md`（2 行） | 置換する |
| 実パス | `minutes/2026-08-05-merge-guard-neutral/` の議事録と snapshot（各 2 行） | 置換する |
| 実パス | `scratch/gpt-plan-review-packet.md`（2 行） | 置換する |
| **原典の例示** | `references/codex-app-server-README.ja.md` / `.orig.md`（Users 配下の例示パス 30 行超） | **触らない**。OpenAI の README に含まれる例であり、個人情報ではない |
| 私的参照 | `docs/DESIGN.md` 冒頭の非公開 repo への参照（username は含まない） | 「非公開の内部調査」の明示に書き換える |
| 履歴 | 上記の過去版 8 ファイル分 | 公開前に履歴書き換えが必要 |

### 2026-08-30 / 再検査（公開準備 PR 時点）

| 検査 | 結果 | 判断 |
|---|---|---|
| `personal_path_scan` | pass | 実パス 8 行（6 箇所）をプレースホルダへ置換。追跡ファイルの残存 0 件を `git grep` で実測（原典例示の references/ は方針どおり除外） |
| `required_documents` | pass | README / LICENSE / SECURITY / CONTRIBUTING / PREFLIGHT を本 PR で commit |
| `clean_worktree` | pass | 議事録の状態 3 ファイル（minutes.md / journal.json / seats.json）を commit し、ランタイム生成物（`minutes/*/scratch/` / `minutes/*/snapshot/` / `minutes/*/last-result.json`）は gitignore 化。commit なしで毎回上書きされる実装（cli / make_snapshot の atomic_write 直書き）のため、追跡すると恒久 dirty になることをコードで確認済み |
| `ci_configuration` | 追加 | `.github/workflows/test.yml` を本 PR で追加（windows + ubuntu）。初実走 (run 33312907180) は両 OS とも **job 起動前に billing ブロックで fail**（→ 2026-09-01 の記録で訂正。恒久条件ではなかった） |
| `consistency_gate` | shadow 採用 | PR #9 で `.repo-preflight-consistency.json` を shadow モード採用済み。再走で `repository_consistency` pass |
| `readme_release_design` | pass | 見出しを 目的と仕組み / 使い方（できること）/ 制約 に整え、情報設計ゲート pass |

再走 (readiness_scan --intent publish) の残 blocked 要因は次の 3 種のみ:
**git 履歴の書き換え**（personal_path_scan の history 15 commit + 原典例示 2 件は容認判断済み）/
**CI 実走**（→ 09-01 に解消）/ **human_visual_review**（人間欄）。いずれも下の未了リストと対応。

### 2026-09-01 / CI 実走と記録の訂正

**08-30 の「private repo の Actions は billing で起動しない」は誤りだった。**
月次無料枠が枯渇していただけで、月初のリセット後は同じ repo・同じ workflow が普通に走った。
課金設定は変更していない。「今日動かない」を構成上の恒久条件として書いてしまった記録ミス。

| 検査 | 結果 | 判断 |
|---|---|---|
| `ci_runtime_result` | **pass** | PR #14 / #13 / #15 で **windows-latest / ubuntu-latest とも success**。この repo で初めての CI 実走緑 |
| POSIX 経路 | **実測済み** | ubuntu-latest で全件走行。起草時「未実測」としていた前提が解消（README / CONTRIBUTING を更新） |

初実走は同時に実バグを 1 件検出した。windows runner は `TEMP` が 8.3 短縮名
（`C:\Users\RUNNER~1\...`）で、`ledger.work_tree_root` が git 出力を正規化せず返していたため
path 比較が壊れ、**既存 repo の中にネスト repo を init しうる**状態だった（D13 違反の経路）。
PR #14 で修正。開発機では 8.3 名が出ないため、ローカル 247 件緑のまま素通りしていた。
**「ローカルで緑」は「実行環境で緑」ではない**ことの実例として残す。

### 2026-09-01 / 目視レビューの下調べ（human_visual_review の材料）

公開すると **94 ファイル**が外から読める。機械検査（secret_scan / worktree の
personal_path_scan）はすべて pass しているので、残るのは「読まれて困らないか」の人間判断だけ。
下調べとして、判断が要る箇所を実測で 3 点に絞った。
**この節は下調べであって `human_visual_review` の完了ではない。**

| # | 対象 | 実測 | 判断 |
|---|---|---|---|
| A | `minutes/space-civilization-annual-design-20260830/` | 4 files / 225 行。**別 private repo (space-civilization-choice) の 2026–2040 年シミュレーター設計**が丸ごと入っている | 未定 |
| B | `scratch/` の 6 ファイル | 489 行。外部 AI へのレビュー依頼文と返答そのもの | 未定 |
| C | 「CEO」という社内呼称 | **35 ファイル**（tests 9 / docs 9 / roundtable 8 / scratch 6 / scripts・minutes・adapters 各 1） | 未定 |

補足:

- **A** は構造上は正しい置き場所。`packet.build` は席へ `snapshot/minutes.snapshot.md` しか
  渡さないので、席が読むレビュー対象文書は本 repo 内に無いと到達できない（file-backed 収集契約）。
  ただし**この repo を公開すると別プロジェクトの設計が一緒に出る**。分離するなら
  「席に届ける仕組み」の方を先に決める必要がある
- **B** は依頼の書き方がそのまま残る。秘匿情報は無いが、作業の生々しさは残る
- **C** は外部読者には文脈のない語として映る。残す判断でも実害は無いが、出現数は多い
- `cmux` は `docs/DESIGN.md` の「cmux 非依存」1 箇所のみ。無害
- `minutes/` のうち席の生の発話が載っているのは `delivery-safety-smoke-20260819` と
  `tier1-final` の 2 件

### 2026-09-01 / gitleaks の誤検知（履歴 scan 限定）

履歴全体を scan すると `references/codex-app-server-README.*` で `generic-api-key` が
2 箇所ヒットする。中身は OpenAI の README にある JSON 例の `idempotencyKey` で
資格情報ではない。差分 scan では出ないため公開準備の最終段まで気付かなかった。
**公開後は第三者が全履歴を scan できる**ので、判断を `.gitleaks.toml` の allowlist として
repo に残した（PR #17）。除外は path と regex の AND 条件に絞ってある。

## 未了（公開前に閉じること）

- [x] 実パス 6 箇所（8 行）の置換 — 2026-08-30 完了。`git grep` で残存 0 件を実測
- [ ] git 履歴の書き換え（過去版 8 ファイルに残るため、ファイル修正だけでは消えない。
      force push を伴うため人間の明示判断で実施）
- [x] `clean_worktree` を pass させる — 2026-08-30 完了（状態 3 ファイル commit + gitignore 3 行）
- [x] CI の緑を確認する — 2026-09-01 完了。windows-latest / ubuntu-latest とも success
      （PR #14 / #13 / #15）。08-30 の billing ブロックは月次無料枠の枯渇で、恒久条件では
      なかった。初実走で Windows 固有の実バグを 1 件検出し PR #14 で修正済み
- [x] 版番号の確定 — 2026-09-01 完了。**0.3.0** に統一（v0.3 Phase 1–4 が着地済みの実体に
      合わせた。起草時の「公開タグ希望 0.1.0」は 2 版ずれのため撤回）。数字は
      `roundtable/__version__` の 1 箇所に寄せ、pyproject との一致と relay の直書き禁止を
      `tests/test_version_consistency.py` が機械検査する
- [ ] `human_visual_review` の実施 — 下調べは 2026-09-01 に完了（上記の A / B / C の 3 判断に
      絞り込み済み）。残るのは人間が A・B・C を決めることだけ
- [ ] 検査を再走して blocked が解けることを確認
- [ ] 公開時に GitHub の Private Vulnerability Reporting を有効化（SECURITY.md の報告導線）

## 判断の記録

**公開時期は未定です。** 準備を整えたうえで private のまま保持し、公開の実行は
別途の明示判断で行います。visibility の変更はツールでは行いません。
