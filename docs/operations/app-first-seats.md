# アプリで見える円卓の受渡し

## 責務と範囲

Round Tableは議題・依頼ID・回答の検証・一度だけの回収・議事録を所有します。
各アプリのログイン、権限確認、モデルの利用枠はそのアプリが所有します。
アプリ操作の既定を勝手に変更せず、明示した接続と対象だけへ依頼を渡します。

2026-10-28の本人判断に従い、CLIによるモデル実行より見えるアプリの会話を優先します。
OrcaとGrok Botは後回しです。Grokはサービス名、Grok Botは別アプリであり、同じ席と扱いません。
Grokブラウザ、Grok Build CLI、Cursor経由は別経路です。Cursorの接続は今回実装していません。

## Claude Desktop

既存チャットへの受渡しは `claude-socket` を使います。新しいウィンドウ、画面操作、
クリップボード、再起動は使いません。macOS/Linuxの同一OSユーザー内に限定し、
Windowsのnamed pipeや別端末は未対応です。送信先には `local_UUID` ではなく、
稼働中のClaude CodeセッションUUIDを明示します。レジストリ・プロセス開始時刻・
ソケット・会話記録を照合し、名前だけでは選びません。

```bash
ai-roundtable dispatch <topic> --participant claude --tier 3 --async --no-clipboard --root <root>
ai-roundtable handoff <topic> --invocation <id> --transport claude-socket --claude-session <UUID> --root <root>
ai-roundtable handoff <topic> --invocation <id> --transport claude-socket --claude-session <UUID> --apply --root <root>
ai-roundtable socket-collect <topic> --invocation <id> --root <root>
```

`--apply` のないhandoffは依頼の固定のみです。`submitted` は通信フレームを書いた状態で、
Claudeの受信・承認・回答を証明しません。受信側の保留・拒否を設定変更で回避しません。
送信前に `sending` を記録し、途中失敗でも同じ依頼は再送しません。
socket-collectは送信後の対象チャット記録から依頼番号とメッセージ番号を照合し、
回答JSON・参加者・schemaを検証します。最初の `answer-observed` 後、もう一度collectし、
同じ回答が確認された時だけ既存のscratch検証・議事録mergeへ進みます。
不明・未回収・別入力の割込みは完了にしません。回収の再実行でも再送はしません。
既存の出力フックによる差し戻しは、同じ会話のネイティブなメタ通知だけを識別します。
差し戻された回答は破棄し、訂正後の最終回答を待ちます。通常の人間入力や他セッションの
割込みとは区別し、フック自体の無効化・承認の代行は行いません。
会話全体は複製せず、今回の回答と照合情報だけを既存の仕組みに記録します。

公式文書 [Cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging) は
2026-10-28に全節を確認しました。通知購読、別端末、cloud、受信制御、非対話セッション、
容量・連投制限も含みます。今回の範囲は同じMacの既存Desktop Codeチャットだけです。
認証値・子プロセス識別・権限モードを偽装せず、受信設定や認証ファイルは変更しません。
ソケットの存在と受信制御は公式仕様ですが、NDJSONの全フィールドや会話記録形式は
公開された安定APIではありません。
[先行実装](https://github.com/PeterSR/claude-code-socket-transport) と実機試験に基づく
互換アダプターとして、照合できない場合は停止します。

### 表示用リンク・新規入力欄（別経路）

公式の `claude://code/new?q=...` は新規入力欄を開きます。送信や回答完了を意味しません。
`folder` を渡すと信頼確認が必ず発生するため、既定では渡しません。
既存のチャットは、アプリ側の `local_UUID` を指定して開きます。CLI側のUUIDとは区別します。
取り違えを避けるため、リンク生成器は接頭辞のないCLI形式のUUIDを受け付けません。
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

`composer-requested` はOSへ入力準備を依頼した状態です。画面表示の確認ではありません。
`app-link --open` も `open_requested` だけを返し、`window_reuse_verified` はfalseです。
入力欄とIDを確認してアプリ上で送信します。
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
- [Peekaboo](https://github.com/openclaw/Peekaboo)：Macアプリ操作のCLI/MCP候補。
  4.9.7をnpm execで起動し、既存Claudeプロセスとウィンドウ一覧、利用許可を確認しました。
  ただし対象ウィンドウのAX要素が見つからず、focusは操作前に拒否されました。
  取得できた部分ツリーも操作可能なsnapshotではなく、このホストでの既存画面の再利用は未達です。
  PIDを固定したアプリの前面化は成功しましたが、続くウィンドウ固定の再観測は失敗し、
  操作先の確かな記録を得られませんでした。アプリの前面化成功も、チャットやボタンの操作成功とは区別します。
  新しいウィンドウの作成や権限変更でこの失敗を迂回していません。

## 検証と改善

最小ループは1依頼・1回答です。既存のrequest、delivery receipt、journal、minutesを読み戻します。
回収後に同じcollectを再実行して重複がないことを確かめます。
アプリ更新時には新しいチャット1件で入力準備・受理・回答保存・回収を再点検します。
利用枠不足は未回収として残し、時間だけで成功へ進めません。
複数ラウンドや新しいアプリの採用は、席の同一性と送り先を検証してから進めます。
自動承認、アカウント切替、常設の監視はこの機能へ含めません。
