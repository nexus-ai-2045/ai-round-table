# Macの席への依頼受渡し

Mac先行の実装対象は、依頼の固定、CMUXの既存搬送への接続、Claude Desktop Codeへの
クリップボード受渡し、回答回収です。加えて、既存`Desktop`チャットを明示する実験的な
`claude-socket` 搬送を用意します。既存の`CMUX`・`clipboard`経路はそのまま使えます。

## 設計の不変条件

- 発行済みinvocationに対して一つの依頼・snapshot・搬送先を固定します。
- 製品成果物、回答原文、議事録、搬出receiptは別々に保存します。
- 依頼変更・hash不一致・別の搬送先への再利用は拒否します。
- 外部投入直前にsendingを記録します。投入後の例外・timeoutは送達不明にし、自動再送しません。
- CMUXでのsubmittedは搬出だけ、Desktopのclipboard-readyは貼付前です。回答完成とは区別します。
- 取消・回収と同じinvocation排他を使い、終了した依頼へ送信しません。
- 休止中Desktopの会話をCLIのresumeや新規席にすり替えません。

## 実行環境の前提

配布パッケージは共通wrapperに必要なPyYAMLを依存として導入します。cmuxアプリが起動し、
`cmux`コマンドがPATH上に必要です。送信前に同じPythonでwrapperの`--help`を実行し、
起動できない場合は`prepared`のまま理由を返します。環境修復後はこの準備済み依頼を再実行できます。
送信処理を開始した後の`delivery-unknown`は、この再実行対象に含みません。

## 使用順序

`ROOT`は絶対パスを使います。最初に既存dispatcherで議題と依頼を作ります。

```bash
ai-roundtable new-topic TOPIC --topic '議題' --participants cc --root ROOT
ai-roundtable dispatch TOPIC --participant cc --no-clipboard --async --root ROOT
```

返されたinvocationを`INV`に使います。`handoff`の既定はローカル準備のみです。
`--apply`が搬出の明示指定です。準備済みの同じ依頼と送り先を使用します。

Claude Desktop Codeの場合：

```bash
ai-roundtable handoff TOPIC --invocation INV --transport claude-desktop --root ROOT
ai-roundtable handoff TOPIC --invocation INV --transport claude-desktop --apply --root ROOT
```

既存の対象CodeセッションのLocal環境・読取権限・保存先を確認し、準備されたpacketを貼り付けます。
送信が無効なオフラインのリモート席へは投入しません。別セッション作成は対象変更として明示します。
GUIで入力欄に置いただけの状態を送信済みとせず、画面の受理と回答の生成を別に確認します。

CMUXで既に動いているCLI席の場合：

```bash
ai-roundtable handoff TOPIC --invocation INV --transport cmux \
  --workspace WORKSPACE_UUID --surface SURFACE_UUID \
  --cmux-script /absolute/path/to/Projects/shared/scripts/cmux_file_signal.py --root ROOT
```

準備結果で対象と依頼を確認した後、同じ引数に`--apply`を付けて搬出します。
workspace/surfaceのUUIDと期待AIプロセスを照合する既存wrapperを使用します。
CMUXの選択中ペインを暗黙の宛先にしません。

回収と状態確認：

```bash
ai-roundtable handoff-status TOPIC --invocation INV --root ROOT
ai-roundtable collect-pending TOPIC --timeout 30 --root ROOT
ai-roundtable cancel TOPIC --invocation INV --root ROOT
```

搬出失敗を新規invocationの自動発行やTier3への自動縮退で隠しません。
送達不明の場合は同じinvocationで回収を確認し、席への再送を繰り返しません。

## `Claude socket`による既存チャットへの受渡し

対象は`macOS/Linux`の同一`OS`ユーザー（`UID`）で稼働する、既存の`Claude Desktop Code`チャットです。
`Windows`の`named pipe`、別端末、新規チャット作成は非対応です。
画面操作、新規ウィンドウ、クリップボードは使いません。
セッション`UUID`を明示し、レジストリの `claude-desktop`、`PID`とプロセス開始時刻、
`socket`と会話記録の所有者・`device/inode`を照合します。送信直前・回収時にも再照合します。
認証・受信制御・設定・セキュリティポリシーを変更して通す処理はありません。

`ROOT` は議事録を保存する絶対パスです。既存の対象チャットの`UUID`を `SESSION_UUID`
に指定します（`local_` 接頭辞は付けません）。`dispatch`の結果を `INV` に使います。

```bash
ai-roundtable new-topic TOPIC --topic '議題' --participants claude --root ROOT
ai-roundtable dispatch TOPIC --participant claude --tier 3 --async --no-clipboard --root ROOT
ai-roundtable handoff TOPIC --invocation INV --transport claude-socket \
  --claude-session SESSION_UUID --root ROOT
# 準備結果を確認してから、同じ宛先へ一度だけ送信
ai-roundtable handoff TOPIC --invocation INV --transport claude-socket \
  --claude-session SESSION_UUID --apply --root ROOT
ai-roundtable handoff-status TOPIC --invocation INV --root ROOT
ai-roundtable socket-collect TOPIC --invocation INV --root ROOT
# answer-observed の後、同じ回答が安定していることをもう一度照合
ai-roundtable socket-collect TOPIC --invocation INV --root ROOT
```

`--apply` のない`handoff`はローカル準備だけです。送信前に `sending` と宛先を保存し、
送信後の `submitted` は通信フレームを書いた状態を示します。受信・回答完成の証明ではなく、
照合した回答がない間は `ok=false`、`model_receipt_verified=false` です。
拒否・中断・送達不明でも同じ`invocation`は再送しません。
宛先解決など送信前の失敗で `prepared` のままなら、原因の解消後に送信できます。

回収は送信後の対象会話記録にある依頼番号・メッセージ番号・参加者と、
最終返答の`JSON/schema`を照合します。初回は `answer-observed` として保留し、
回答の`UUID`と本文`hash`が続けて一致した時だけ既存の`collector`で議事録へ取り込みます。
`answer-observed` の `ok=false` は回収未完了を示します。
再回収は `already_collected` を返し、議事録を重複更新しません。
別入力の割込み、取消、未知の会話記録や不完全な返答は未回収のままです。
待機中は `socket-collect` を再実行し、`handoff --apply` は繰り返しません。

送るのは固定済みの役割指定と議事録本文のプレーンテキストです。
ファイル・画像添付は扱いません。`JSON`を含む送信フレーム全体は1 `MiB`、
回収する会話記録の増分は8 `MiB`、レジストリ1件は64 `KiB`までです。
相手アプリ側の受信・容量制限も受け、超過時に分割・再送する処理はありません。
`NDJSON`通信と会話記録の形式は安定した公開`API`として保証されない実験的な互換形式です。
変更や照合不一致を検出した場合は、成功扱いせず停止します。

既存の`Mac`実機スモークでは3件の`invocation`で送信・返答`JSON`の照合・議事録回収を確認しています。
これはその3依頼の観測で、今回の変更で実機送信を再実行した記録ではありません。
`Linux`の実席往復、常設の自動継続、すべての`Desktop`バージョンでの互換性は未確認です。

## 作業範囲を固定した実装依頼

コードの実装を依頼する場合は、通常のレビュー依頼と分けて、`handoff`に
`--implementation-manifest <workflow.json>`を明示します。対象は`claude-socket`のみです。
新しい契約を作らず、[既存の実装開始・結果回収契約](../REVIEW_TO_IMPLEMENTATION.md)の
`roundtable.review-to-implementation/v1`を使います。

```bash
ai-roundtable handoff TOPIC --root ROOT --invocation INV \
  --transport claude-socket --claude-session SESSION_UUID \
  --implementation-manifest workflow.json
```

実装前に、同じ基準コミットに対するレビュー、作業ツリー・ブランチ・担当者・所有ファイル、
許可操作と禁止操作を検証します。準備した契約を固定し、送信直前にも照合します。
検証に失敗した契約を、レビュー依頼や別の送信先へ自動的に切り替えません。
この指定がない場合のレビュー用の動作は変えません。

相手側の権限や確認待ちはそのまま有効です。別のエージェントから届いた依頼で、
権限設定の変更や確認待ちの承認を代行できるわけではありません。
許可された範囲で既存コードを確認し、不足分だけを実装・テストした結果を、既存の回答形式で返します。

`socket-collect`で回答が`merged`になっても、実装完了の証明ではありません。
依頼元が実際の変更・コミット・テスト結果を独立して確認し、既存の
`workflow-gate --phase fan-in`を通してから実装結果を受け入れます。
テスト失敗・承認待ち・未コミット・所有範囲外の変更を成功扱いにしません。

受信側でローカルコミットを毎回確認する設定がある場合、契約に`commit`が含まれていても
アプリ自身の確認は省略されません。「今回許可」は次の依頼への恒久許可ではありません。
確認回数の見直しは、送信側の契約変更とは別に、受信側の既存権限設定を対象とします。

並列に回帰試験を実行する場合は、それぞれ異なる`--basetemp=.pytest-tmp-<用途>`を指定します。
既定の一時ディレクトリを共有すると、互いの試験用ファイルを消して誤失敗になります。

## Desktopの担当再開

[Claude公式資料](https://code.claude.com/docs/en/desktop#work-across-sessions)は
Desktop内部のCodeセッション間の読取・送信を説明しています。これは外部CLIからの制御APIではありません。
実席へ最初の依頼を渡した後にDesktop内部で担当へ連絡する場合も、正確なセッションを指定し、
再開要求の受理と相手の作業結果を別々に確認します。クリップボードやファイル作成だけでは再開成功にしません。

無人で全Desktop会話を起動する機能は保証範囲外です。今回の製品機能は既存席への受渡しと回収までで、
GUI接続はユーザー指定の実席による検証を必要とします。定期化やhook/settingsの自動登録は行いません。

## 正本と更新・切替

| 対象 | 正本・役割 |
|---|---|
| 製品コード | `nexus-ai-2045/ai-round-table` のmain。ローカル同期先は `$HOME/Projects/Documents/.repos/<INTERNAL_ROOT>/ai-round-table` |
| 通常CLI | `$HOME/.local/bin/ai-roundtable`。固定wheelから導入し、開発worktreeのeditable参照を残さない |
| cmux搬送 | `Projects/shared/scripts/cmux_file_signal.py`。共通基盤担当が更新し、製品側へ複製しない |
| 議題の進行・回答 | 指定ROOT配下のGit検証付きjournal/minutes。last-resultは最新表示であり正本の代用にしない |
| 作業・試験の保全 | 完了worktreeは利用先がないことを確認し、必要な差分・履歴・試験証拠を検証付きarchiveへ移して整理 |

更新は小さく行います。mainの更新を確認し、変更範囲のCI・レビューを確認してから新しいwheelを作り、
同じ通常CLIへ導入します。旧wheelと旧SHAは復元用に保持します。未commit差分のあるcheckoutには上書きせず、
担当・差分・保全先を照合してから同期します。自動pullやdirty差分の自動破棄は行いません。

切替前に旧collectorの終了を確認し、同じtopicで旧版と新版を並走させません。
導入後はcheckout外でCLI起動と同じPythonによるwrapper起動を確認し、専用topicで1依頼・1回答を実測します。
回答1件、再回収不変、元担当への通知受理と実処理を確認してから運用へ戻します。
失敗時はjournalとreceiptを読み、preparedの事前失敗だけを環境修復後に再実行します。
送達不明・通知claim済みは再送せず、対象の実状態を確認します。

新しい修復を採用したら[検証記録](collection-recovery-verification.md)へ対象SHA・結果・残件を追記し、
検知した不具合には再発を捉える試験を対応させます。コード、導入、実往復、担当の採用を一つの成功に混ぜません。
