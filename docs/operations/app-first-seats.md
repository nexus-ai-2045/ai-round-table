# アプリで見える円卓の受渡し

## 責務と範囲

Round Tableは議題・依頼ID・回答の検証・一度だけの回収・議事録を所有します。
各アプリのログイン、権限確認、モデルの利用枠はそのアプリが所有します。
アプリ操作の既定を勝手に変更せず、明示した接続と対象だけへ依頼を渡します。

2026-10-28の本人判断に従い、CLIによるモデル実行より見えるアプリの会話を優先します。
OrcaとGrok Botは後回しです。Grokはサービス名、Grok Botは別アプリであり、同じ席と扱いません。
Grokブラウザ、Grok Build CLI、Cursor経由は別経路です。Cursorの接続は今回実装していません。

## Claude Desktop

公式の `claude://code/new?q=...` は新規入力欄を開きます。送信や回答完了を意味しません。
`folder` を渡すと信頼確認が必ず発生するため、既定では渡しません。
既存のチャットは、アプリ側の `local_UUID` を指定して開きます。CLI側のUUIDとは区別します。
このリンクが同じアプリの同じウィンドウを再利用する保証はありません。
2026-10-28に本人から「アプリが2つ起動した」と指摘があったため、チャット表示の成功を
ウィンドウ再利用の成功へ読み替えません。既存画面への接続・再利用は別の未達条件です。

```bash
ai-roundtable app-link --prompt-file request.md
ai-roundtable app-link --session-id local_<UUID>
```

依頼の作成と、本文・スナップショット固定は既存のdispatchを使います。
クリップボードへ渡して回収を待つ既定動作を避ける場合は、両方の明示オプションが必要です。

```bash
ai-roundtable dispatch <topic> --participant claude --tier 3 --async --no-clipboard --root <root>
ai-roundtable handoff <topic> --invocation <id> --transport claude-link --apply --root <root>
ai-roundtable handoff-status <topic> --invocation <id> --root <root>
```

`composer-ready` は入力準備です。入力欄とIDを確認してアプリ上で送信します。
リンクの発行だけでは既存のアプリや画面を操作しません。明示的な `--open` はOSへ
リンクを渡す操作であり、新しい画面が増える場合があるため、既存画面を再利用する用途では使いません。
今回の実機試験では、Claudeアプリを前面化したうえでMac標準のキー入力を送りました。
このキー入力は汎用の自動送信APIとして提供していません。別チャットへの誤送信を避けるため、
前面化・入力先確認・送信・実会話への追加の確認を分けます。

Claudeは指定scratchにJSONを保存し、既存のcollectがID・参加者・形式・Git状態を検証します。

```bash
ai-roundtable collect <topic> --invocation <id> --timeout 0 --root <root>
```

## 実験的なGrokブラウザ経路

Macの既存Google Chromeタブを、window ID・tab ID・現在URLで明示指定します。
既に許可されているApple EventsによるDOM操作だけを使い、Chromeの再起動や、
別プロファイル、ログイン状態のコピー、CDPの有効化は行いません。
editorと送信要素はその時点の画面から解決し、既存の下書きや対象違いは停止します。
画面変更によりセレクタが成立しなければ送信成功として扱いません。

```bash
ai-roundtable handoff <topic> --invocation <id> --transport grok-browser \
  --browser-window <id> --browser-tab <id> --browser-url https://grok.com/ \
  --browser-account '<確認したアカウントメニューの表示名>' --apply --root <root>
ai-roundtable browser-collect <topic> --invocation <id> --root <root>
```

ブラウザにはローカルファイルアクセスを求めず、固定した議事録本文を渡します。
回答はJSONで返させ、依頼IDに対応した表示・形式・内容の安定を確認してから、
既存のscratchとcollectorへ渡します。新しい回収台帳やスケジューラは作りません。
送信結果が不明なら再送を止めます。ブラウザの生の本文をエラーログへ出しません。
アカウントの表示名も送り先に固定し、入力前・送信直前・回答回収時に照合します。
これは表示名の照合であり、同名の別アカウントを暗号的に識別する機構ではありません。
送達不明の場合も、届いた回答を依頼IDで検証して回収できます。再送は行いません。

この経路は今回の本人判断により追加の実席試験を中止しました。
送信要求までは試しましたが、有効なJSON回答の回収は確認できていません。
利用枠が戻った後はCodex内部ブラウザを優先して再評価します。
現時点でGrokの実往復を保証する経路として推奨しません。

## 再利用した先行実装

- [Claudeの公式ディープリンク](https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link)：新規入力欄の事前入力。
- [Claude Desktopの公式仕様](https://code.claude.com/docs/en/desktop#coming-from-the-cli)：CLIとアプリのセッション一覧・引継ぎを区別。
- [agent-browser](https://github.com/vercel-labs/agent-browser)：CDP接続時のDOM取得・要素操作。既存環境では接続口を確認できず、今回の実送信には使用していません。
- [Peekaboo](https://github.com/openclaw/Peekaboo)：Macアプリ操作のCLI/MCP候補。今回導入していません。

## 検証と改善

最小ループは1依頼・1回答です。既存のrequest、delivery receipt、journal、minutesを読み戻します。
回収後に同じcollectを再実行して重複がないことを確かめます。
アプリ更新時には新しいチャット1件で入力準備・受理・回答保存・回収を再点検します。
利用枠不足は未回収として残し、時間だけで成功へ進めません。
複数ラウンドや新しいアプリの採用は、席の同一性と送り先を検証してから進めます。
自動承認、アカウント切替、常設の監視はこの機能へ含めません。
