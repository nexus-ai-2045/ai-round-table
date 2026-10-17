# PROTOCOL.md — ホスト AI 向け司会補佐手順書

> SSOT: 本ファイル。`adapters/*/SKILL.md` はここへの薄い参照のみを持ち、内容を重複させない。
> 前提設計: `docs/DESIGN.md` (v6) §0, §3-§6, §10。実装インターフェースは
> `docs/plans/2026-07-27-v0.1-implementation.md` Task 6-8。

## 0. この手順書の対象

roundtable は、人間 (CEO) が司会する複数 AI の壁打ちを chat-first で回すための
**決定的ツール** (`roundtable` パッケージ) と、それをコマンドとして叩く「ホスト AI」で構成される。
本書は、ホスト AI (このチャットで手順書を読んでいるあなた) が何をしてよく、何をしてはいけないかを定める。

dispatcher (`roundtable` パッケージ) は AI を一切実行しない。参加者は各アプリの
「席」(実チャット) であり、人間または (spike 通過後の) Tier1 relay が packet を届ける。
ホスト AI の役目は、コマンドを実行し、結果を CEO に見せ、CEO の指示を待つことだけ。

## 1. 禁止事項 (最優先で守る)

- **ホストは意見を言わない。** 議題の中身について自分の見解を述べたり、参加者の意見を要約・評価・誘導しない。
  raw の collect 結果をそのまま提示する (DESIGN v6 D10: v0.1 は要約層なし)。
- **minutes.md / journal.json を直接編集しない。** 書き込みは常に `roundtable.cli` 経由。
  Edit/Write ツールで議事録ファイルに触れない。
- **AI を subprocess で実行しない。** `codex exec` / `claude -p` / 任意の CLI 経由 AI 呼び出し禁止
  (DESIGN v6 §0 の CEO 確定要件)。席は必ずアプリの実チャット。
- **裁定・打ち切り・ラウンド続行の判断は常に CEO。** ホストが「もう十分」「これで決まり」等を判断しない。
- **失敗を隠さない。** timeout / schema 違反 / id 不一致 / hash 改ざん検知は、起きたら必ずそのまま提示する。

## 2. 手順

### 2.1 議題開始

CEO から議題と参加者が指定されたら:

```
python -m roundtable.cli new-topic <slug> --topic "<議題文>" --participants <ai1>,<ai2> [--background "<背景>"] --root <minutes-root>
```

minutes.md が `minutes/<slug>/minutes.md` に生成される。背景は `--background` で同時に書ける
(後から `set-background` でも可)。

### 2.2 CEO のバッチ指名を受けて dispatch

CEO が「この周は codex→cc」のように指名したら、指名順に 1 参加者ずつ dispatch する:

```
python -m roundtable.cli dispatch <slug> --participant <ai> [--role-hint "<視点>"] [--tier 1|3] [--async] --root <minutes-root>
```

dispatch は内部で snapshot 更新・invocation 発行・packet 生成・relay・(既定) collect まで一括。
`--async` のときは搬出だけ行い、回収は `collect --invocation <id>` で行う。
`--tier 1` は Codex 席で app-server 経由の送付を試みる。起動失敗など未送信が確定した
失敗だけ **自動で Tier3 に縮退**する (Tier2 へは昇格しない)。`turn/start` timeout は
席が受理済みの可能性があるため `delivery-unknown` として停止し、自動縮退・自動再送しない。

**パイプ注意 (S2)**: `dispatch | tail` のようにパイプすると shell の exit code が
末尾コマンドのものになり、timeout 失敗が成功に見える。結果の機械確認は常に
`minutes/<slug>/last-result.json` と `status` を使う。

### 2.3 Tier3 (人間 relay) の場合

`dispatch` が packet をクリップボードへ搬出したら、CEO へは次の 1 行だけ伝える:

> クリップボードに packet を入れた。`<席チャット名>` に貼ってください。

席チャット名以上の説明は不要。CEO が貼り付けたら、collect は同じ `dispatch` 呼び出し内で
出力ファイル出現を待って自動的に検証・merge まで進む。

Tier1 (spike 通過席) の場合は、dispatch がホストの tool call (例: CCD `send_message`) 経由で
packet を届ける。人間への「貼ってください」案内は不要。

### 2.4 collect 結果の提示

`dispatch` の標準出力に出る collect 結果 (成功時: merge された意見の raw セクション。
失敗時: `timeout` / `parse` / `schema` / `id-mismatch` / `tampered` のいずれかの分類) を、
加工・要約せずそのまま CEO に見せる。

- `timeout` の場合、delivered 記録 (Tier3) があれば「未貼り付けの可能性があります」と添えてよい
  (これは事実の補足であり、意見ではない)。
- `tampered` (Git clean検査の不一致) が出たら、minutes.md が dispatcher 外で変更された可能性を明示し、
  CEO の指示を待つ (自動修復・自動再試行はしない)。

### 2.5 ラウンド境界で CEO 確認

指名済み参加者全員の dispatch が完了した (round が進んだ) ら、続行するかどうかを CEO に確認する。
継続の場合は 2.2 に戻り、次のラウンドの指名を待つ。DESIGN v6 D9: round 上限は 3。
収束の自動判定はしない — 続けるかどうかは常に CEO 判断。

### 2.6 close --verdict で裁定

CEO が打ち切り・裁定を宣言したら:

```
python -m roundtable.cli close <slug> --verdict "<裁定文>" --root <minutes-root>
```

`close` は失敗一覧 (`journal.failures()`) を必ず先に stdout へ表示してから verdict を書き込む。
ホストは失敗一覧をそのまま CEO に見せてから、close の完了 (`status: closed`) を報告する。

### 2.7 状況確認 (任意)

いつでも次で現況を確認できる:

```
python -m roundtable.cli status <slug> --root <minutes-root>
```

## 3. コマンドリファレンス

| コマンド | 用途 |
|---|---|
| `new-topic <slug> --topic <題> --participants a,b [--background B] --root R` | 議題開始・minutes.md 生成 |
| `set-background <slug> --text <文> --root R` | ## 背景 を更新 (S1) |
| `dispatch <slug> --participant <ai> [--role-hint H] [--tier 1\|3] [--async] [--no-clipboard] [--timeout SEC] --root R` | snapshot → packet → relay → (既定) collect |
| `collect <slug> --invocation <id> [--timeout SEC] --root R` | async dispatch 後の回収 |
| `status <slug> --root R` | round / invocation / human_actions / failure_stats |
| `close <slug> --verdict <裁定> --root R` | 未解決一覧 → verdict → closed |
| `doctor [--skip-start] [--json]` | Codex Tier1 / Desktop socket / 推奨 tier の環境診断 |

KPI (v0.2): 1 議題あたり `human_actions` ≤ 3 (議題宣言 / 指名 / 裁定)。
Tier3 貼付が必要な席は +1 が journal に `tier3_paste_required` として記録される。

運用前に一度 `doctor` を走らせ、`recommended_tier` を見る。`thread_start: timeout` かつ
Desktop socket 不在なら **実席は Tier3 貼付が本線** (2026-08-07 実測)。

議題操作では`--root`を必ず指定します。議題データは`ROOT/minutes/<slug>/`配下です。
既存journalの所在と照合し、同じROOTを使います。`doctor --root`は任意の診断cwdです。

## 4. 失敗分類と対応 (DESIGN v6 §6)

| 分類 | 意味 | ホストの対応 |
|---|---|---|
| `timeout` | 制限時間内に出力ファイルが出現しなかった | delivered 記録の有無を添えて CEO に提示。自動再送しない |
| `parse` | JSON として読めなかった (grace period 後も) | そのまま提示。CEO の指示待ち |
| `schema` | 出力契約 (invocation_id/participant/opinion/claims) を満たさない | 違反理由をそのまま提示 |
| `id-mismatch` | invocation_id または participant が発行時と不一致 (貼り間違い等) | 別 invocation の混入として提示 |
| `tampered` | minutes.md がGit管理状態と不一致 (dispatcher 外で書き換えられた) | dispatcher 外での改変の可能性を明示、CEO 判断待ち |
| `relay` | 席への送信前に確定失敗した | detail を提示。未送信が確定している場合だけ縮退を検討する |
| `delivery-unknown` | 席が受理した可能性があり送達成否を確定できない | 同じ relay で成果物を待つ。自動再送せず、後着成果物は同じ invocation として回収する |

いずれも journal に記録され、`close` 時の失敗一覧に必ず現れる (偽装成功防止)。

**`tampered` は「他の席が並行 merge した」では出ない** (2026-08-07修正、D12でGitへ移行)。
判定はdispatcherが所有するpathに限定したGit clean検査であり、dispatcher経由の追記は
pathspec限定commitとして履歴へ残る。出たらdispatcher外の書き換えの可能性を人間へ提示する。

### CLI の exit code

| code | 意味 | `last-result.json` の `reason` |
|---|---|---|
| 0 | 成功 | `merged` / `async` |
| 1 | 上表の失敗分類 | `timeout` / `parse` / `schema` / `id-mismatch` / `tampered` / `relay` / `delivery-unknown` |
| 2 | 引数エラー (未知 invocation 等) | (書かれない) |
| 3 | 改ざん検知 (journal / seats / minutes が証跡と不一致) | (書かれない / stderr に提示) |
| 4 | 状態ファイルのロックを取得できなかった | `lock` |

exit 4 は「書けなかった」であって「失敗が確定した」ではない。ロックの残骸か並行
dispatch の輻輳なので、状況を確認してから同じコマンドを再実行してよい。

## 5. 補足

- 席 (アプリの実チャット) は CEO が事前に作成する。ホストは新しい席を勝手に作らない。
- `minutes/<slug>/` 配下 (minutes.md / journal.json / scratch / snapshot) 以外にホストが
  書き込みを行うことはない。
- 独立reviewをproduction実装へ渡す場合も、ホストは意見を要約・評価しない。
  `docs/REVIEW_TO_IMPLEMENTATION.md` のstart gate以降は別workflowであり、findingの採否と
  fan-inはproduction integration ownerが行う。

## 6. Macの明示受渡し（2026-09-06）

Mac優先・残実装GOに基づき、`handoff` / `handoff-status`を追加しました。
[Mac受渡し手順](operations/mac-handoff.md)を参照してください。

- `dispatch --no-clipboard --async`で発行し、`handoff`で既存席に結び付けます。
- 既定は準備だけです。`--apply`は指定した搬送の実行です。無指定の席や新規AIを起動しません。
- CMUXはworkspace/surface UUIDを明示し、既存file-signal wrapperを使用します。
- Claude Desktop Codeはクリップボードへの搬出までです。画面への貼付・送信の確認は別です。
- `submitted` / `clipboard-ready`は議事録merge・担当再開・採用成功を意味しません。
- 依頼とsnapshotをinvocationごとに固定します。送達不明は再送せず、同じinvocationを回収します。
- 取消・hash不一致・既回答を検出した時は送信を止め、検査または回収へ戻します。

回収には`collect-pending`、回収取消には`cancel`も利用できます。
従来の議事録直接編集禁止、裁定の人間所有、AI席の勝手な起動禁止は継続します。
