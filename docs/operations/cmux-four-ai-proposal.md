# CMUXの4席案：入口調査と最小スモーク

現在の進め方は[Mac優先のDesktop接続](mac-desktop-connection.md)を参照してください。
ユーザー指定のClaude席はDesktop内Code画面です。以下は当初のCLI入口調査として残します。

2026-09-06の読取調査です。4AIの起動、GUI操作、認証・設定変更、外部送信は行っていません。
採用案はCMUXを作業席・搬送、Round Tableを依頼契約・回答検証・議事録、製品repoを成果物正本と分ける形です。

## 確認した入口

| 対象 | ローカルの入口 | 公式資料と制限 |
|---|---|---|
| CMUX | `/Applications/cmux.app/Contents/Resources/bin/cmux` | [公式CLI](https://cmux.com/docs/api)に分割とsurface指定送信。画面・socket接続は調査対象外 |
| Codex | PATHの公式npm package、既存`relay_codex.py` | 既存app-server経路を優先。`exec --help`はモデル実行なしで確認。Desktop座長タスクの再開と席のresumeは別 |
| Claude Code | `.local/bin/claude`のversion 2.1.260へのsymlink | [公式CLI](https://code.claude.com/docs/en/cli-reference)に対話・headless・session再開。認証・実行は調査対象外 |
| Gemini | PATHの`@google/gemini-cli/bundle/gemini.js` | [公式headless](https://geminicli.com/docs/cli/headless/)にprompt・JSON出力。Round Table adapterは未実装 |
| Grok | `.local/bin/grok` → `.grok/bin/grok` | [公式仕様](https://docs.x.ai/build/cli/headless-scripting)に`grok agent stdio` ACP。名称からの推測ではない |

Grok既存adapterの`grok agent --no-leader stdio`はWindows実測に基づきます。
公式入口が存在することは、現在のMac版でその引数が通る証明ではありません。
各AIの認証状態、利用枠、課金方式、実席往復は本調査の保証に含めません。

## 現行契約と採否事項

[PROTOCOL](../PROTOCOL.md)は任意CLIでのAI直接実行を禁止し、
[README](../../README.md)は席間の直接接続を避け、Claude Codeをホストとして扱います。
したがって4分割できることだけを理由に4席を起動しません。
Codexが座長の場合のClaude席追加と、ペイン間直接相談は現行設計の変更判断が必要です。

Projectsには`shared/scripts/cmux_file_prompt.py`と`cmux_file_signal.py`の
ファイル本文＋短いpointerの搬送経路が既存です。採用時はそれを再利用し、
過去のsurface番号ではなく実行直前の対象とAIプロセスを照合します。
送信後の画面確認だけを回答完成・merge成功と扱いません。

## 採否後の1依頼1回答スモーク案

1. 承認された既存1席と、専用scratch、依頼1件、回答1件、期限を固定する。
2. dispatcherでinvocationとpacketを発行し、既存relayを優先する。CMUX搬送は別途対象surfaceを確定する。
3. 席は所定scratchにtmp→正式名で回答を保存する。
4. `collect`でID・形式・hashを検査し、journalと議事録をreadbackする。再collectで重複しないことを確認する。
5. 担当再開が必要なら正式な再開ツールのreceiptを別に確認する。

このスモークが通った後、4席への拡大を判断します。調査中にスモークは実行していません。
