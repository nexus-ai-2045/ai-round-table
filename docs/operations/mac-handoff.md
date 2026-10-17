# Macの席への依頼受渡し

Mac先行の実装対象は、依頼の固定、CMUXの既存搬送への接続、Claude Desktop Codeへの
クリップボード受渡し、回答回収です。Desktopの非公開APIを想定した自動送信は実装しません。

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
| 製品コード | `nexus-ai-2045/ai-round-table` のmain。ローカル同期先は `$HOME/Projects/Documents/.repos/nexus_ai/ai-round-table` |
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
