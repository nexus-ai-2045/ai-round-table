<!-- repo-preflight:review-record -->

# 公開準備レビュー記録

repo-preflight の検査結果と、それに対する人間の判断を記録します。
**この記録は「検査を通した」証拠であり、「安全である」ことの保証ではありません。**

## 対象

- repository: `nexus-ai-2045/ai-round-table`
- 公開意図: public（時期未定）
- ライセンス: MIT

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
| **原典の例示** | `references/codex-app-server-README.ja.md` / `.orig.md`（30 行超の `/Users/me/...`） | **触らない**。OpenAI の README に含まれる例であり、個人情報ではない |
| 私的参照 | `docs/DESIGN.md` 冒頭の非公開 repo への参照（username は含まない） | 「非公開の内部調査」の明示に書き換える |
| 履歴 | 上記の過去版 8 ファイル分 | 公開前に履歴書き換えが必要 |

### 2026-08-30 / 再検査（公開準備 PR 時点）

| 検査 | 結果 | 判断 |
|---|---|---|
| `personal_path_scan` | pass | 実パス 8 行（6 箇所）をプレースホルダへ置換。追跡ファイルの残存 0 件を `git grep` で実測（原典例示の references/ は方針どおり除外） |
| `required_documents` | pass | README / LICENSE / SECURITY / CONTRIBUTING / PREFLIGHT を本 PR で commit |
| `clean_worktree` | pass | 議事録の状態 3 ファイル（minutes.md / journal.json / seats.json）を commit し、ランタイム生成物（`minutes/*/scratch/` / `minutes/*/snapshot/` / `minutes/*/last-result.json`）は gitignore 化。commit なしで毎回上書きされる実装（cli / make_snapshot の atomic_write 直書き）のため、追跡すると恒久 dirty になることをコードで確認済み |
| `ci_configuration` | 追加 | `.github/workflows/test.yml` を本 PR で追加（windows + ubuntu、初実走）。緑の確認は下の未了リストで追跡 |
| `consistency_gate` | shadow 採用 | PR #9 で `.repo-preflight-consistency.json` を shadow モード採用済み |

## 未了（公開前に閉じること）

- [x] 実パス 6 箇所（8 行）の置換 — 2026-08-30 完了。`git grep` で残存 0 件を実測
- [ ] git 履歴の書き換え（過去版 8 ファイルに残るため、ファイル修正だけでは消えない。
      force push を伴うため人間の明示判断で実施）
- [x] `clean_worktree` を pass させる — 2026-08-30 完了（状態 3 ファイル commit + gitignore 3 行）
- [ ] CI を追加して緑を確認する（workflow は本 PR で追加済み。緑の確認は CI 実走後にチェック）
- [ ] 版番号の確定（`pyproject.toml` は 0.2.0。v0.3 Phase 1–4 が着地済みのため、
      公開タグ希望 0.1.0 とは 2 版ずれ。要・人間判断）
- [ ] `human_visual_review` の実施
- [ ] 検査を再走して blocked が解けることを確認
- [ ] 公開時に GitHub の Private Vulnerability Reporting を有効化（SECURITY.md の報告導線）

## 判断の記録

**公開時期は未定です。** 準備を整えたうえで private のまま保持し、公開の実行は
別途の明示判断で行います。visibility の変更はツールでは行いません。
