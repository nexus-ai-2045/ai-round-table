# 独立レビューから実装へ進む workflow

> SSOT: 本ファイル。機械契約は `roundtable.review_workflow` の
> `roundtable.review-to-implementation/v1`。壁打ちの司会契約は `docs/PROTOCOL.md`。

## 目的と責務

この workflow は、複数席の独立レビューを、別worktreeの実装開始条件と単一PRの
fan-in証跡へ接続する。既存roundtableの議事録、packet、relay、schema検証、Git証跡を
再利用し、新しいAI実行器やhash ledgerは作らない。

- 壁打ちホスト: 意見を要約・評価・採否判断しない。各席のraw成果物を回収する。
- 実装lane owner: 指定されたexact base、branch、worktree、所有ファイル、権限境界内で修正し、
  root-cause clusterごとの失敗経路testとcommit receiptを返す。
- production integration owner（`fan_in.owner`）: findingの採用・棄却、重複・矛盾の統合、
  統合後再検証、単一PR closeoutを所有する。レビュー席や壁打ちホストの自己申告を採否証明にしない。
- 人間: merge、release、公開、settings、visibility、auth、secret、削除、force操作を判断する。

## 二段gate

### start

実装前に次をすべて要求する。

1. `base_commit` は40桁のexact SHAで、live `HEAD` と一致する。
2. 専用worktreeと非default branchがlive状態と一致し、worktreeがcleanである。
   「専用worktree」は path 一致だけでなく **linked worktree であること**を検査する
   (`git rev-parse --git-dir` と `--git-common-dir` の相違)。primary checkout を
   指定した場合は deny する。`git worktree add` で作ること。
3. review成果物が存在し、同じ`base_commit`、`complete: true`、`roundtable.review/v1`、
   stable finding IDを持つ。
4. 全findingが1つ以上のroot-cause clusterへ割り当てられ、clusterに単一ownerと失敗経路testがある。
   **cluster が参照する finding は `<reviewer>:<id>` 形式**で書く。独立レビューは席同士が
   採番を相談しないため、素の id では grok の `R1` と claude の `R1` が 1 件に潰れ、
   片方だけを参照していても「全件割当済み」に見えてしまう。
   `reviewer` と finding `id` 自体には区切り文字 `:` を許さない。
5. `owned_files` は重複のないrepo相対pathである。区切りは `/` のみ (`\` は deny)。
   綴りが git の出力と食い違うと fan-in の所有照合が必ず外れるため、schema 段階で止める。
6. 権限境界は少なくとも `merge/release/settings/visibility/auth/secret/delete/force` を禁止する。
   これらを `allowed` にも書いた矛盾した境界は deny する。
7. default branchは`main`/`master`という名前の推測ではなく、remoteの`origin/HEAD`から判定する。
   判定不能なら安全側に停止する。

```powershell
python -m roundtable.cli workflow-gate workflow.json --phase start --repo <worktree>
```

### fan-in

start契約に加え、exact commit SHA、cluster IDに結び付いた成功test receipt、全cluster ownerのterminal化、
統合後再検証、production integration owner、`single_pr: true` を要求する。live Gitでbaseから
commitまでの変更ファイルが`owned_files`内だけかを照合する。

照合は `git log -m --format= --no-renames -z --name-only base..commit` で、range内の
各commitが触った全pathを取る。終端treeだけを比較すると、所有外ファイルを途中で変更して
後のcommitで戻した履歴が消えるため。

- `--no-renames`: 既定の rename 検出は改名後の名前しか返さないため、所有外ファイルを
  owned な名前へ改名すると所有権検査が**素通り**する (fail-open)。
- `-z`: 非 ASCII path が `core.quotepath` でクォート化され、照合が全部外れるのを避ける。

worktree の clean 検査は start だけでなく **fan-in でも行い**、
`--untracked-files=all` を明示する。repo設定で未追跡を隠した状態でも、所有外の未commit/未追跡を
残したまま「所有ファイルしか触っていない」と通ってしまうため。
`commit_sha` が `base_commit` と同一 (実装 commit ゼロ) の receipt も deny する
(`merge-base --is-ancestor` は自分自身を祖先と判定するため、これだけでは弾けない)。
空の子commitも変更pathが0件なのでdenyする。

```powershell
python -m roundtable.cli workflow-gate workflow.json --phase fan-in --repo <worktree>
```

gate成功は「安全に採用済み」「merge可」を意味しない。構造・Git・receiptがfan-in審査へ
進める形になったことだけを示す。

## manifest例

```json
{
  "schema": "roundtable.review-to-implementation/v1",
  "workflow_id": "topic-implementation-1",
  "base_commit": "0123456789abcdef0123456789abcdef01234567",
  "implementation": {
    "worktree": "C:/work/topic-implementation-1",
    "branch": "feat/topic-implementation-1",
    "owner": "implementation-lane",
    "owned_files": ["roundtable/example.py", "tests/test_example.py"]
  },
  "review_artifacts": [
    {"reviewer": "grok", "path": "C:/packets/grok-review.json", "schema": "roundtable.review/v1"},
    {"reviewer": "claude", "path": "C:/packets/claude-review.json", "schema": "roundtable.review/v1"}
  ],
  "permission_boundary": {
    "allowed": ["edit-owned-files", "test", "commit"],
    "prohibited": ["merge", "release", "settings", "visibility", "auth", "secret", "delete", "force"]
  },
  "root_cause_clusters": [
    {"id": "RC1", "finding_ids": ["grok:G-1", "claude:C-2"], "owner": "implementation-lane", "test": "tests/test_example.py"}
  ],
  "fan_in": {
    "owner": "production-integration-owner",
    "single_pr": true,
    "commit_sha": "89abcdef0123456789abcdef0123456789abcdef",
    "tests": [{"command": "python -m pytest", "status": "passed", "cluster_ids": ["RC1"]}],
    "integration_reverified": true,
    "terminal_lanes": ["implementation-lane"]
  }
}
```

review成果物は少なくとも次の形を持つ。

```json
{"schema":"roundtable.review/v1","base_commit":"0123456789abcdef0123456789abcdef01234567","complete":true,"findings":[{"id":"G-1"}]}
```

## 失敗時のrepair path

- review欠落/schema不一致: 同じ席・同じinvocationの成果物を回収し、自動再送しない。
- base/worktree/branch不一致: 実装を開始せず、production integration ownerへ差し戻す。
- finding未割当: root causeを決め、単一ownerと失敗経路testを付ける。
- 所有外diff: 自動採用しない。所有集合の変更か差分分離を人間レビューへ戻す。
- test/commit/fan-in receipt欠落: PR closeoutを行わず、未実行と残務を明示する。
