---
title: 独立レビューとproduction実装責任を二段gateで分離する
type: adr
status: accepted
created: 2026-08-30
updated: 2026-08-30
owner: production integration owner
related: docs/REVIEW_TO_IMPLEMENTATION.md
---

# ADR-0002 独立レビューとproduction実装責任を二段gateで分離する

## Context

roundtableのホストは独立性のため意見を要約・評価しない。一方、レビューfindingを実装へ
移すには採否、root cause統合、所有範囲、test、commit、fan-inの責任者が必要である。
両者を同じ「ホスト」へ載せると、独立レビュー契約とproduction採否責任が混同される。

## Decision

既存の壁打ちworkflowは変更せず、別の`review-to-implementation` workflowを追加する。
start gateでreview成果物、exact base、専用worktree/branch、所有集合、権限境界、root-cause
clusterを検査する。fan-in gateでtest、commit receipt、terminal lane、統合後再検証、単一PRを
検査し、採否責任をproduction integration ownerへ明示する。証跡は既存どおりGitへ一本化する。

## Allowed

- 既存packet/relayで独立reviewを回収する。
- 専用branch/worktreeで所有ファイルだけを編集、test、通常commitする。
- PRIVATE repoの安全gate通過後に単一PRを作成・更新する。

## Prohibited

- ホストによるreview意見の要約、評価、誘導、採否決定。
- review成果物なしの実装開始、default branch実装、所有外diffの自動採用。
- 新しいhash/witness ledger、AI実行器、既存Journal状態の流用。
- merge、release、settings、visibility、auth、secret、削除、force操作の自動化。

## Human Review Gate

gate成功後もfindingの採否、所有集合の変更、単一PRのmergeは人間レビューで停止する。
public化や公開操作はこのADRの許可範囲外である。

## Consequences

独立reviewとproduction統合の責任が機械的に分離される。manifest作成とreceipt更新の手間は
増えるが、exact base、所有外diff、未実行test、未回収laneをPR前に検出できる。

## Review Evidence

- `tests/test_review_workflow.py`: unit、adversarial、live Git、E2E mock。
- `roundtable.review_workflow`: error aggregationと二段gateの実装。
