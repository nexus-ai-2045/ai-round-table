# 回収修正の検証記録

初回検証: 2026-09-06、macOS / Python 3.14.5。
更新: 2026-09-07。過去の合格記録と、追加変更後の検証を分けて記載します。

- branch: `fix/collection-recovery-clean-20260906`
- base: `baaeeeff4da575e7bdf68d7bbfa88556c44a0048`
- 状態: ローカル差分。コードcommit、push、PR、mergeは未実施。
- 全体試験: `python3 -m pytest -q --basetemp=.pytest-tmp-final` → **299 passed in 104.88s**。
- `git diff --check`: 成功。
- 独立Pythonレビュー: 生存ロックのmtime失効問題を検出・修正後、残P1/P2なし。
- 日本語文書検査: 回収運用・CMUX提案の2文書で成功。
- Ruff: 当該Python環境にmoduleがなく未実行。追加インストールは行っていない。

別プロセスのCLIスモークでは、一時git rootへ`new-topic`、
`dispatch --no-clipboard --async`、模擬回答のtmp→正式名、`collect-pending`を実行。
同じコマンドをもう一度実行して、議事録の掲載1件・内容不変・`resume_executed:false`を確認しました。
AIを起動した試験ではありません。

元のレビュー回答も読取だけで確認し、journalはmerged、ID・参加者一致、議事録掲載1回、
last-resultはok/mergedでした。元の回答原文・議事録・製品成果物の書換えは行っていません。

## 残る境界

- 休止中Codex座長タスクの自動再開adapterと常設定期実行は未接続。正式機構・対象・権限の判断が必要です。
- Windows実機とCIは未実施。macOSの生存ロック維持・子プロセス死亡後解放は実測済みです。
- CMUX4AI案は読取調査とスモーク案まで。席追加・直接相談の設計採否は未実施です。
- 旧版collectorと新版を同一topicで並走させない切替手順が必要です。

初回CLIテストのbasetemp指定ミスで、模擬議事録がdispatcherにより
`fix/collection-recovery-20260906`へ機械commitされました。このbranchは保全しています。
提出対象はmain起点の上記clean branchへソース差分を保持して切り替え済みで、
模擬議事録commitを含みません。残った未追跡fixtureはOSの一時ディレクトリへ移動しました。
旧branchの削除や履歴改変はしていません。

## Mac受渡し実装時の検証（2026-09-06）

ユーザーのMac優先・全実装GOを受け、`handoff` / `handoff-status`を追加しました。
既存CMUX file-signal wrapper、Mac clipboard、回答回収をつなぎ、依頼/snapshotの固定と
送達不明時の再送禁止を実装しました。詳細は[Mac受渡し手順](mac-handoff.md)を参照してください。

- `python3 -m pytest -q --basetemp=.pytest-tmp-mac-handoff-final`: **345 passed**。
- 当時の`--collect-only`も345件。後述の通知機能追加後の全体試験とは別の記録です。
- `git diff --check`: 成功。
- 独立security reviewの最終確認: 残P1/P2なし。
- 実CLI別プロセスで実在のCMUX wrapperを指定し、依頼固定・prepare・statusを確認。
  packet/snapshot hashが保存され、prepareのみでは送信されていないことを確認。
- 日本語文書検査: Mac受渡し手順とPROTOCOLで成功。

## 通知機能追加と通常CLIへの導入（2026-09-07）

`followup` の `prepare` / `claim` / `record` / `status` を実装しました。
検証済み回答への通知を元担当のタスクIDへ固定し、二重claimや対象違いの成功記録を拒否します。
詳細は[元担当への返却手順](coordinator-followup.md)を参照してください。

- `uv` によるeditable導入後、固定wheelへ切り替えました。作業フォルダの参照ではなくPython 3.13のsite-packagesから起動することを確認しました。
- 配布wheelのPythonファイル20件と作業ソースの一致を確認しました。リポジトリ外からのCLI起動、模擬回答の回収、二重掲載防止、重複claim拒否のスモークが成功しました。
- 通知機能の既存独立レビューでは、残P1/P2なしを確認しました。
- 追加変更を含む全体pytest: **355 passed in 724.10s**。Python 3.14で、`--basetemp=.pytest-tmp-delivery-final` とJUnit XML保存を指定して実行しました。
- 配布したPython 3.13環境でも、リポジトリ外から模擬回答の回収・二重掲載防止・重複claim拒否を確認しました。
- 変更した文書8件の日本語ガード、利用者向け文書ガード、差分の空白検査が成功しました。
- コードの保存・配布状況と、CLIへのローカル導入は別です。commit以降の結果は主担当の確定記録に従います。

## Claude Desktop実席の確認と残務

指定席は `AI Round Table 完成` です。前回はリモート接続不能表示と無効な送信ボタンを確認しました。
今回、CUAによるClaudeアプリの画面読取りはできましたが、指定席への検索操作のクリックで `noWindowsAvailable` が返りました。
今回もメッセージは送信していません。以前の「Mac Localの新規席作成を質問中」という記載を、
現在進行中の確認待ちとして引き継ぎません。

Desktopの実席での依頼・回答の往復と、休止中の座長の自動再開は未完了です。
通知outboxの実装・通常CLIの起動確認だけでは、これらの成立を証明できません。
GUIの読取りを、製品の自動Desktop送信・再開adapterの実装とは扱いません。
