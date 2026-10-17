# 回答の発見・回収と担当再開

対象は回答ファイルを保存した後に座長側の待機が切れる場合と、座長プロセスの再起動です。
既存dispatcherのjournal、回収、議事録mergeを使用します。追加daemonやOS設定は不要です。

## 守る条件

- 席の誤出力から回答の帰属と議事録を守ります。ID・参加者・形式検証を通った回答だけを採用します。
- 同じinvocationを複数回回収しても議事録に重複させません。正規化JSONのSHA256で採用内容を結び付けます。
- 回答の内容の真偽、ローカルgit履歴を改変できる攻撃者、AIの実行停止はこの機構の保証対象外です。
- `last-result.json`は最新結果の表示です。未回収の正本はgitで検証される`journal.json`です。
- 依頼packet、scratchの回答原文、議事録、製品repoの成果物を別々に扱います。回答を製品成果物へ自動採用しません。

## 実行

以下の`ROOT`は`minutes/<topic>`の二つ上です。既存運用が`ROOT/minutes/<topic>`を
生成するため、`ROOT`自体の名前が`minutes`なら`minutes/minutes/<topic>`になります。
推測で移動せず、発行済みinvocationのjournalの所在に合わせてください。

```bash
python3 -m roundtable.cli collect-pending TOPIC --root ROOT
python3 -m roundtable.cli collect-pending TOPIC --root ROOT --timeout 30 --poll-interval 2
python3 -m roundtable.cli collect TOPIC --root ROOT --invocation INVOCATION --timeout 30
python3 -m roundtable.cli cancel TOPIC --root ROOT --invocation INVOCATION
```

`collect-pending`の既定は1回の走査です。`--timeout`は追加観測の待機枠です。
ファイル検証、tmp安定確認、git、ロック処理の実行時間は別途かかります。
終了コードは0=未回収なし、1=失敗記録あり、2=未回収ありです。取消済み一覧も必ず出します。
0は担当再開やレビュー採用の成功を意味しません。

通常の回収待機が切れた場合は`waiting`として残し、後着回答を再回収できます。
旧版の`failed: timeout` / `failed: stalled-tmp`は自動走査では再開せず、対象を確認して
明示的な`collect --invocation`を使用します。形式不正などの終端失敗を勝手に再採用しません。
`cancel`は回収を取り消します。席のAIプロセスは止めません。取消後の後着回答は残して不採用にします。

`.json.tmp`は安定確認とID・形式検証を通った場合だけ既存の救済機構で回収します。
元ファイルを残し、`recovered-from-tmp`を記録します。正式名へrenameしていない事実は消しません。

## 休止中の担当を再開する境界

回収した回答を元担当のCodexタスクへ返す[followup契約](coordinator-followup.md)があります。
稼働中のCodexが通知を固定・claimし、正式な`send_message_to_thread`の結果を記録します。
CLI単独では停止中のCodexを起動できず、常設定期実行も提供しません。
`collect-pending`は`follow_up.resume_executed: false`と
`coordinator-resume-adapter-unavailable`を返します。既にmergedの回答も一覧に残すため、
回収直後に座長が停止しても再起動後の確認候補を失いません。通知済みかはfollowup記録で照合し、
元担当が回答を読んで作業したかは正式タスクAPIの実結果で別に確認します。

定期化する場合は、製品が提供する正式なタスク定期実行機構に、対象root/topic、
この有限コマンド、結果の照合、再開対象の正確なタスクIDを登録する判断が別途必要です。
任意shell hook、無断daemon、座長再開の代わりの新規AI席生成を追加しません。
再開は正式ツールのreceiptで確認し、回答発見、回収、再開要求の受理、担当の実作業完了を区別します。

## 検証の入口

```bash
python3 -m pytest -q tests/test_pending_collection.py tests/test_watcher.py tests/test_tmp_recovery.py tests/test_concurrency_high_findings.py
```

回帰試験は模擬回答と一時git repoで行います。実AI起動やDesktopタスク再開の実証ではありません。

## ロック切替と保証範囲

invocation単位の回収・取消はOS標準の排他ロックを使用します。生存中の所有者から
mtime経過だけでロックを奪いません。ファイルはunlinkせず、プロセス終了時にOSが解放します。
POSIXは`flock`、Windowsは`msvcrt.locking`です。今回の実プロセス試験はmacOS上で行っています。
Windowsの検証はCIでも実行します。最新結果は[検証記録](collection-recovery-verification.md)から確認してください。
利用者のWindows実機での動作確認は別であり、自動運用へ反映済みとは扱いません。

同じtopicで旧版と新版のcollectorを同時実行しないでください。切替前に旧collectorの終了を
確認してください。旧版にはmtimeでlockファイルを削除する経路があり、新版と排他方式が異なります。
既存journal/minutesの短い更新用FileLockは今回変更していません。
