# 回収した結果を元担当へ返す

`collect-pending` は回答を検証し議事録へ統合します。元担当への返却は、
稼働中のCodexが正式な `send_message_to_thread` を使う別の工程です。
`followup` は送り先・回答hash・通知文を固定し、二重送信を防ぐ記録を持ちます。

## 操作

```bash
ai-roundtable followup TOPIC --root ROOT --invocation INV --action prepare --target-thread TASK_UUID
ai-roundtable followup TOPIC --root ROOT --invocation INV --action claim
```

claimの成功結果にある送り先と通知文を、そのままnative task送信へ渡します。
CLIは外部送信しません。送信前にclaimを永続化し、同じclaimの再実行からは再送しません。
送信後は実toolの結果を根拠に、次の情報をJSONへ記録します。

- `action`: `send_message_to_thread`
- `accepted`: 実toolで受理された場合のみtrue
- `thread_id`: 実toolが返した対象task
- `message_sha256`: claimされた通知文のSHA256
- `source_call_id`: 実際のtool call ID

```bash
ai-roundtable followup TOPIC --root ROOT --invocation INV --action record --target-thread TASK_UUID --message-sha256 HASH --receipt /absolute/path/native-receipt.json
ai-roundtable followup TOPIC --root ROOT --invocation INV --action status
```

このreceiptは呼出し側による実tool結果の対応記録であり、独立した認証証明ではありません。
送信結果が不明なら成功receiptを作らず、元のtaskとtool結果を読み返します。
送り先や通知hashを変えて成功を記録できません。

## 完了の境界

`submitted` は再開要求の受理までです。元担当が回答を読み、作業したことは
`read_thread` / `wait_threads` の実結果で別に確認します。`resume_executed` を
ファイル作成だけでtrueにしません。

Codex自身が停止している間の自動発見・起動は、このCLIだけでは行えません。
常設の起動主体や他アプリへの自動送信を追加する場合は、その接続を別に検証します。

## 現在の確認状況

導入・試験結果とClaude Desktop実席の状況は、
[検証記録](collection-recovery-verification.md)にまとめています。
