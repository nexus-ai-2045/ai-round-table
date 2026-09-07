# Mac優先：CMUX・Round Table・Claude Desktopの接続

追記：ユーザーの全実装GOに基づく[Mac受渡しコマンド](mac-handoff.md)を実装しました。
以下の調査時点の未接続項目と、実席検証の状態は分けて読んでください。

2026-09-06のユーザー指定によりMac版を優先します。
Claude側の対象は**Claude Desktop内のCode画面**です。CMUX内のClaude CLIとは区別します。

現在の実装・検証状態の正本は[検証記録](collection-recovery-verification.md)です。
以下は2026-09-06時点の調査履歴で、現在の未実装項目一覧ではありません。

## 接続ごとの確認結果（調査当時）

| 接続 | 現状とMacでの進め方 | 証拠の限界 |
|---|---|---|
| CMUX ↔ ローカル制御 | インストール版0.64.22、CLIの`ping`に`PONG`。作業席の制御口は応答 | 特定ペインへの依頼送達・AI受理は試していない |
| CMUX ↔ Round Table | Round Tableのpacketとscratch契約を維持し、既存Projectsのfile-backed搬送を再利用する案 | repo内にCMUX relayは未接続。queue追加や送信成功は回答回収成功ではない |
| Claude Desktop Code ↔ Round Table | Local環境でpacketを渡し、指定した絶対パスのscratchへ回答を保存、dispatcherで回収する構成 | Desktopの自動worktreeと議事録rootを混同しない。実席往復は未実行 |
| CMUX ↔ Claude Desktop Code | Desktopを別ウィンドウの席として扱う。CMUXのClaude標準連携はCLIのsession IDを使う | Desktop画面への直接送信・継続再開APIは今回確認できていない |

[公式CMUX session restore](https://cmux.com/docs/session-restore)のClaude連携は
`claude --resume <id>`です。CMUXが捕捉したCLI sessionを復元する仕組みであり、
Desktop Code画面の会話を自動で制御できる証明にはなりません。

[Claude公式Desktop資料](https://code.claude.com/docs/en/desktop)では、CLIとDesktopは
会話履歴が別で、設定とCLAUDE.mdを共有します。CLI内の`/desktop`でDesktopへ会話を移せますが、
移動時にCLIは終了します。これは双方向の常時同期ではありません。
公式資料は共有MCP/skills/hooksも説明していますが、MCP共有だけでDesktopの会話への
外部送信口ができるとは扱いません。今回は設定を変更していません。

ローカルのClaude.appは1.46388.4。版とインストール存在だけを確認し、
対象Codeセッションの稼働・アクセス範囲・認証は調査対象外です。

## 今回のMac実装と次の順序

1. 回答回収基盤をMacで先行検証。既存の回収修正は299件の回帰試験済みです。
2. 別branchにある`pbcopy`対応を再利用し、Mac搬出を統合済みです。Windowsの`clip.exe`分岐は維持します。
3. packet内のsnapshotと出力先を絶対パスに固定済みで、Desktopや別ペインのcwdに依存させません。
4. CMUX搬送は既存`cmux_file_prompt.py` / `cmux_file_signal.py`を使う薄い接続として設計します。
   送信対象のworkspace/surface/AI、invocation、回答先を固定します。
5. 最初の実席スモークは承認済みのClaude Desktop Code 1席で、1依頼1回答。
   回答が保存されたこと、検証・回収成功、担当再開を個別に確認します。

前段のMacコード統合とオフライン試験には、Desktop操作・AI起動・hook/settings変更は不要です。
4AI同時起動や直接ペイン間相談は今回の実装へ混ぜません。

## 完了を判定する段階

`CMUX接続応答 → 正確な席へ送達 → AIが受理 → 回答ファイル完成 → ID/形式/hash検証とmerge → 必要な担当再開`

一段前の成功で次段を成功扱いしません。現在のPONGは最初の段階だけを確認しています。

## 今回の追加検証

- packet / clipboardの関連試験: 19件成功。subprocessはmockで実クリップボードを変更していません。
- CLI / 模擬往復 / pending回収の結合回帰: 17件成功。
- `git diff --check`と日本語文書検査: 成功。
- 既存`cmux_file_prompt.py --delivery send --path-only --dry-run`: コマンド生成成功。
  表示された依頼fileが作られていないことも確認。送信・Enter投入は未実行です。
- 追加コードは専用worktreeの未commit差分。元branchの他担当差分は変更していません。

CMUX専用relayの実接続、Claude Desktop実席への送達、Desktop担当の自動再開は未実施です。
