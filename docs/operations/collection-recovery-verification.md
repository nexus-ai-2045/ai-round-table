# 回収修正の検証記録

初回検証: 2026-09-06、macOS / Python 3.14.5。
更新: 2026-09-08。過去の合格記録と、追加変更後の検証を分けて記載します。

## 現在の受入状況

現在の状態はこの表を正本とし、後続の日付別記録はその時点の証拠として残します。
今回の完了条件は、Macの依頼受渡し・重複しない回答回収・元担当への返却を、
提出コードと通常の配布環境で確認することです。候補構成の成功と正式反映は分けます。

| 対象 | 状態と次の行動 |
|---|---|
| 製品修正と配布CLI | PR #22へ提出済み。固定wheelを通常CLIへ導入し、ソース一致とcheckout外の起動・回収を確認 |
| CI | Ubuntu成功。WindowsはCLI起動成功、テスト隔離ガードの2件を修復中。最新HEADの両OS成功を受入条件にする |
| Mac cmuxの実往復・本部返却 | 共通候補で成功。回答1件・再回収不変・本部の読取と記録反映まで確認 |
| 共通基盤の正式反映 | Projects PR #725。既存の本部担当が保持し、承認後のmerge・限定したroot反映・実測を残す |
| Claude Desktop指定席 | 操作接続障害で未検証。指定席の復旧後に1依頼・1回答を検証する |
| 休止中座長の常設自動再開 | 未接続。正式タスク送信とその後の実処理は確認済みだが、常設の自動再開とは扱わない |

## 初回検証の記録

- branch: `fix/collection-recovery-clean-20260906`
- base: `baaeeeff4da575e7bdf68d7bbfa88556c44a0048`
- 提出先: [PR #22](https://github.com/nexus-ai-2045/ai-round-table/pull/22)。mergeは未実施。以下の日付別試験は当時の差分に対する記録です。
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

## 初回検証時に残っていた境界

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

## 配布環境の事前検査と実席スモーク（2026-09-08）

固定wheelからの初回cmux実行で、共通wrapperが使うPyYAMLの配布依存漏れを検出しました。
パッケージ依存へ追加し、同じPythonでのwrapper起動とcmuxのPATHを送信前に検査するよう修復しました。
事前検査失敗はpreparedを維持し、実送信開始後の送達不明・再送禁止を維持します。

- 関連テスト: **26 passed**。独立レビュー: 重大指摘なし。
- wheelを再構築し、通常CLIへPyYAMLとともに再導入しました。
- cmux公式アプリへの利用者用CLIリンクを作り、`cmux ping`のPONGを確認しました。
- 専用Astra実席を起動し、入力待ちを確認しました。
- 次の送信試行は共通resolverのUUID解決失敗により停止しました。回答往復は未成立です。
- 共通resolverの修復・回帰検証・runtime反映は既存の共通基盤担当へ依頼済みです。
  製品側でUUID照合を緩めたり、送達不明の依頼を再送したりしていません。

355件の全体試験は上記事前検査追加前の結果です。PRのCI結果と実席の往復成立は別に判定します。

## 共通候補での実席往復と本部返却（2026-09-08）

共通側の修復確定commit `d661e13e39a34712055bc3ada5bcfeb16899dd75` のwrapperを明示して検証しました。
UUID解決とTTY欠損時の実プロセス確認を共通側で修復し、製品側の照合条件は緩めていません。

- 専用Astra実席への搬送がsubmitted/終了コード0となりました。
- AIが指定snapshotを読み、指定scratchへ回答JSONを保存しました。
- 回答ID・形式・hash検証後に議事録へ1件統合し、再回収で内容が変わらないことを確認しました。
- followupの固定通知を一度claimし、正式なタスク送信で本部へ返却、実tool callに対応するreceiptを記録しました。
- 本部が回答と受渡し記録を読み、実席証拠として採用・記録した返答を確認しました。
  これは稼働中のCodexによる返却と担当の処理で、停止中Codexの自動起動の証明ではありません。
- Windows CIで判明した3件のテスト文字コード不整合を、UTF-8明示で修復しました。関連47件成功、独立レビューに重大指摘なし。

共通候補の正式runtime反映は本部の残務です。候補worktreeを明示した成功を、通常環境への採用済みと扱いません。
Claude Desktop指定席の往復も未完了です。PR #22の最新CIはPR側で確認してください。

## PR追加レビューの収束（2026-09-08）

送信中の並行回収、通知先UUID表記差、不正な取消ID、採用commit後の復旧の4指摘を修復した。
busyな送信を待たず別回答を回収する巡回と、scratch欠落時の採用済み復旧も確認した。
統合後の関連pytestは**68 passed in 44.81s**。テストはリポジトリ外の一時領域で実行し、製品HEADの不変を確認した。
詳細と再発防止は[レビュー記録](../review-backlog.md)にまとめる。
