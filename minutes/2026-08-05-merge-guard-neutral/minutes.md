---
topic: merge guard が CI conclusion=NEUTRAL を failed 扱いする問題をどう直すか
status: open
round: 1
participants: [codex]
verdict:
---

# merge guard が CI conclusion=NEUTRAL を failed 扱いする問題をどう直すか

## 背景

shared/scripts/pr_merge_guarded.py の _ci_state() は statusCheckRollup を見て
conclusion が SUCCESS 以外なら一律 failed を返す。実測 (PR #1, 2026-08-05):
唯一の check である Cursor Security Agent が conclusion=NEUTRAL (実質スキップ) で
完了したため ci_state=failed となり、mergeStateStatus=CLEAN にもかかわらず
merge guard が deny した。CEO は手動でブラウザ merge した。

判断したいこと: NEUTRAL / SKIPPED をどう扱うか。
選択肢の例 — (a) 非ブロック扱い (success 相当) (b) 新カテゴリ not_required 相当として扱う
(c) そのまま failed 維持しつつ明示 override フラグを足す (d) その他。
制約: この guard は「CI 落ちてるのに merge する」事故を防ぐのが目的。緩めすぎると意味を失う。
security check がスキップされた事実自体は見逃したくない。

関連ファイル:
  <projects-root>/shared/scripts/pr_merge_guarded.py (_ci_state)
  <projects-root>/shared/scripts/pr_merge_permission_gate.py (CI_VALUES)

