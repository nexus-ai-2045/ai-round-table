> 取得日: 2026-07-27 / 原典: https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
> 版: commit 3016671bb077c43448b8fa88f3edfa9772e17058 / ライセンス: Apache-2.0 / 種別: B (全文訳)
> 原文 snapshot: codex-app-server-README.orig.md

# codex-app-server

`codex app-server` は、[Codex VS Code 拡張機能](https://marketplace.visualstudio.com/items?itemName=openai.chatgpt) のようなリッチなインターフェースを Codex が動かすために使うインターフェースである。

## Table of Contents

- [Protocol](#protocol)
- [Message Schema](#message-schema)
- [Core Primitives](#core-primitives)
- [Lifecycle Overview](#lifecycle-overview)
- [Initialization](#initialization)
- [API Overview](#api-overview)
- [Events](#events)
- [Approvals](#approvals)
- [Skills](#skills)
- [Apps](#apps)
- [Auth endpoints](#auth-endpoints)
- [Experimental API Opt-in](#experimental-api-opt-in)

## Protocol

[MCP](https://modelcontextprotocol.io/) と同様に、`codex app-server` は JSON-RPC 2.0 メッセージ(ワイヤー上では `"jsonrpc":"2.0"` ヘッダーを省略)を使った双方向通信をサポートする。

サポートされるトランスポート:

- stdio (`--stdio` または `--listen stdio://`、デフォルト): 改行区切りの JSON (JSONL)
- websocket (`--listen ws://IP:PORT`): 1 つの websocket テキストフレームにつき 1 つの JSON-RPC メッセージ (**実験的 / 未サポート**)
- unix socket (`--listen unix://` または `--listen unix://PATH`): `$CODEX_HOME/app-server-control/app-server-control.sock` またはカスタムのソケットパス経由で、標準の HTTP Upgrade ハンドシェイクを使った websocket 接続
- off (`--listen off`): ローカルトランスポートを公開しない

`--listen ws://IP:PORT` で実行する場合、同じリスナーが基本的な HTTP ヘルスプローブも提供する:

- `GET /readyz` は、リスナーが新しい接続を受け付けるようになると `200 OK` を返す。
- `GET /healthz` は `Origin` ヘッダーが存在しない場合に `200 OK` を返す。
- `Origin` ヘッダーを伴うリクエストはすべて `403 Forbidden` で拒否される。

Websocket トランスポートは現在実験的であり、未サポートである。本番ワークロードでの利用に依存してはならない。

`--code-mode-host wss://HOST/PATH` を渡すと、この app-server プロセスをローカルの code-mode ホストを起動する代わりに、リモートの code-mode ホストへ接続する。この outbound 接続は `--listen` とは独立しており、プロセスのスレッド間で共有される。ローカルの code-mode ホストには `ws://` を使う。

unix socket トランスポートは、ローカルの app-server control-plane クライアント向けに意図されている。`codex app-server proxy` は、デフォルトでは `$CODEX_HOME/app-server-control/app-server-control.sock` へ、`--sock PATH` が指定された場合はそのパスへ、正確に 1 つの raw stream 接続を開き、そのソケットと stdin/stdout の間でバイトをプロキシする。プロキシされたストリームは websocket HTTP Upgrade ハンドシェイクに続いて websocket フレームを運ぶ。

トレーシング / ログ出力:

- `RUST_LOG` がログのフィルタリング/詳細度を制御する。
- `LOG_FORMAT=json` を設定すると、app-server のトレーシングログを JSON として `stderr` に出力する (1 行に 1 イベント)。

バックプレッシャーの挙動:

- サーバーは、トランスポートの ingress、リクエスト処理、outbound 書き込みの間に有界キューを使用する。
- リクエストの ingress が飽和すると、新しいリクエストは JSON-RPC エラーコード `-32001`、メッセージ `"Server overloaded; retry later."` で拒否される。
- クライアントはこれをリトライ可能として扱い、ジッター付きの指数バックオフを使うべきである。

## Message Schema

現在、`codex app-server generate-ts` を使ってスキーマの TypeScript 版を、または `codex app-server generate-json-schema` を使って JSON Schema バンドルをダンプできる。各出力は、それを実行した Codex のバージョンに固有であり、生成される成果物はそのバージョンに一致することが保証される。

```
codex app-server generate-ts --out DIR
codex app-server generate-json-schema --out DIR
```

## Core Primitives

API は、ユーザーと Codex の間のインタラクションを表す 3 つのトップレベルプリミティブを公開する:

- **Thread (スレッド、会話)**: ユーザーと Codex エージェントの間の会話。各スレッドは複数のターンを含む。
- **Turn (ターン)**: 会話の 1 ターン。通常はユーザーメッセージで始まり、エージェントメッセージで終わる。各ターンは複数のアイテムを含む。
- **Item (アイテム)**: ターンの一部としてのユーザー入力とエージェント出力を表し、永続化され、将来の会話のコンテキストとして使われる。アイテムの例には、ユーザーメッセージ、エージェントの推論、エージェントメッセージ、シェルコマンド、ファイル編集などがある。

会話を作成・一覧・アーカイブするには thread API を使う。turn API で会話を進行させ、turn の通知経由で進捗をストリーミングする。

## Lifecycle Overview

- 接続ごとに 1 回初期化する: トランスポート接続を開いた直後に、クライアントのメタデータを添えて `initialize` リクエストを送信し、その後 `initialized` 通知を発行する。このハンドシェイク以前にその接続上で送られた他のリクエストは拒否される。
- スレッドを開始 (または再開) する: `thread/start` を呼んで新しい会話を開く。レスポンスは thread オブジェクトを返し、`thread/started` 通知も受け取る。既存の会話を継続する場合は、代わりにその ID を指定して `thread/resume` を呼ぶ。既存の会話から分岐したい場合は、`thread/fork` を呼んで履歴をコピーした新しい thread id を作成する。`thread/start` と同様に、`thread/fork` もインメモリの一時的なスレッドのために `ephemeral: true` を受け付ける。
  返される `thread.ephemeral` フラグは、そのセッションが意図的にインメモリのみであるかどうかを示す。`true` の場合、`thread.path` は `null` になる。
- ターンを開始する: ユーザー入力を送るには、対象の `threadId` とユーザーの入力を指定して `turn/start` を呼ぶ。オプションのフィールドで、モデル、cwd、サンドボックスポリシー、または実験的な `permissions` プロファイル選択、承認ポリシー、承認レビューアーなどを上書きできる。これは新しい turn オブジェクトを即座に返す。そのターンが実際に開始されると、app-server は `turn/started` を発行する。
- イベントをストリーミングする: `turn/start` の後、stdout 上の JSON-RPC 通知を読み続ける。`item/started`、`item/completed`、`item/agentMessage/delta` のようなデルタ、ツールの進捗などが見えるはずである。これらは、ストリーミングされるモデル出力と、あらゆる副作用 (コマンド、ツール呼び出し、推論メモ) を表す。
- ターンを終了する: モデルが完了した (またはターンが `turn/interrupt` 呼び出しによって中断された) 場合、サーバーは最終的な turn 状態とトークン使用量を伴う `turn/completed` を送信する。

## Initialization

クライアントは、その接続上で他のメソッドを呼び出す前に、トランスポート接続ごとに 1 回の `initialize` リクエストを送信し、その後 `initialized` 通知で応答しなければならない。サーバーは、上流サービスに提示するユーザーエージェント文字列、サーバーの Codex home ディレクトリを示す `codexHome`、および app-server ランタイムのターゲットを記述する `platformFamily` と `platformOs` 文字列を返す。初期化前に発行された後続のリクエストは `"Not initialized"` エラーを受け取り、同じ接続上での `initialize` の繰り返し呼び出しは `"Already initialized"` エラーを受け取る。

`initialize.params.capabilities` は、`optOutNotificationMethods` によるコネクション単位の通知オプトアウトもサポートする。これは、その接続に対して抑制する正確なメソッド名のリストである。マッチングは完全一致 (ワイルドカード/プレフィックスなし) である。未知のメソッド名は受け入れられ、無視される。

未サポートのフィールドタイプのフォールバックを含め、OpenAI 拡張 MCP フォームを処理できるクライアントは、`initialize.params.capabilities.mcpServerOpenaiFormElicitation` を `true` に設定する。すると app-server は、その接続によって開始・再開・フォークされたスレッドに対して、下流の `openai/form` MCP 拡張を通知する。リクエストエンベロープを処理できないクライアントは、このフィールドを省略するか `false` に設定する。

`codex app-server` の上に構築するアプリケーションは、`clientInfo` パラメータで自身を識別すべきである。

**重要**: `clientInfo.name` は、OpenAI Compliance Logs Platform 向けにクライアントを識別するために使われる。エンタープライズ用途を意図した新しい Codex 統合を開発している場合は、既知クライアント一覧への追加のため連絡してほしい。詳細: https://chatgpt.com/admin/api-reference#tag/Logs:-Codex

例 (OpenAI 公式の VSCode 拡張機能から):

```json
{
  "method": "initialize",
  "id": 0,
  "params": {
    "clientInfo": {
      "name": "codex_vscode",
      "title": "Codex VS Code Extension",
      "version": "0.1.0"
    }
  }
}
```

通知オプトアウトを伴う例:

```json
{
  "method": "initialize",
  "id": 1,
  "params": {
    "clientInfo": {
      "name": "my_client",
      "title": "My Client",
      "version": "0.1.0"
    },
    "capabilities": {
      "experimentalApi": true,
      "optOutNotificationMethods": ["thread/started", "item/agentMessage/delta"]
    }
  }
}
```

## API Overview

- `thread/start` — 新しいスレッドを作成する。`thread/started` (現在の `thread.status` を含む) を発行し、そのスレッドの turn/item イベントに自動購読させる。実験的な `historyMode: "paginated"` は、projection ベースの永続的な履歴を選択する。リクエストに `cwd` が含まれ、解決されたサンドボックスが `workspace-write` またはフルアクセスの場合、app-server はそのプロジェクトをユーザーの `config.toml` で信頼済みとしてマークする。現在のセッションをクリアした後に代替スレッドを開始する際は `sessionStartSource: "clear"` を渡すと、`SessionStart` フックはデフォルトの `"startup"` の代わりに `source: "clear"` を受け取る。実験的な `allowProviderModelFallback` は、権威あるスタティックモデルカタログを持つプロバイダーが、利用不可のリクエスト済み `model` をカタログのデフォルトに置き換えることを許可する。動的またはキャッシュされたカタログはリクエストされたモデルを保持する。実験的な `runtimeWorkspaceRoots` は、app-server がデフォルトの環境選択を作成する際に使われるランタイムワークスペースルートを提供する。パスは絶対パスでなければならない。パーミッションについては、id によるプロファイル選択である実験的な `permissions` を優先する。レガシーな `sandbox` 省略形は引き続き受け付けられるが、`permissions` と組み合わせることはできない。非推奨の実験的な `multiAgentMode` は無視される。積極的なマルチエージェント動作には Ultra 推論努力度を使うこと。実験的な `environments` は、そのスレッド上のターンに対して固定 (sticky) の実行環境を選択する。省略するとサーバーのデフォルトを使用し、`[]` を渡すと環境を無効化し、環境ごとの `cwd` とオプションの環境固有の `runtimeWorkspaceRoots` を伴う明示的な環境 id を渡すこともできる。明示的な環境はトップレベルのルートを無視する。省略された環境ごとのルートはその環境の `cwd` をデフォルトとし、空のリストは明示的にルートなしを選択する。実験的な `selectedCapabilityRoots` は、環境ネイティブの絶対パスを使って、環境が所有するプラグインまたはスタンドアロンスキルのルートを選択する。それらのルート配下で見つかったスキルは、それを所有する環境を通じて一覧・読取される。選択されたプラグインによって宣言された stdio MCP サーバーはその環境で起動され、HTTP MCP 接続はその環境の HTTP クライアントを使用する。
- `thread/resume` — 既存のスレッドを id で再オープンし、以降の `turn/start` 呼び出しがそこに追記されるようにする。`thread/start` と同じパーミッション上書きルールを受け付ける。
- `thread/fork` — 保存されている履歴をコピーして、既存のスレッドを新しい thread id にフォークする。オプションの `lastTurnId` を渡すと、そのターン (境界を含む) までの履歴のみをコピーし、それ以降のターンはフォークから除外する。進行中のターンを `lastTurnId` の境界にすることは拒否される。実験的な `beforeTurnId` は、代わりに参照ターンより厳密に前の履歴をコピーする (そのターンが進行中の場合を含む) が、`lastTurnId` と組み合わせることはできない。両方の境界が `null` で、ソーススレッドがターンの途中である場合、フォークは、マークなしの部分的なターンの続きを継承する代わりに、`turn/interrupt` と同じ中断マーカーを記録する。返される `thread.forkedFromId` は、既知の場合、ソーススレッドを指す。インメモリの一時的なフォークには `ephemeral: true` を受け付け、`thread/started` (現在の `thread.status` を含む) を発行し、新しいスレッドの turn/item イベントに自動購読させる。実験的なクライアントは、フォーク履歴全体をすぐに受け取る代わりに `thread/turns/list` でページングする予定の場合は `excludeTurns: true` を、ソーススレッドの現在のゴールをフォークに引き継ぎ、自動継続が再開する前に明示的なターンを実行する場合は `deferGoalContinuation: true` を渡すことができる。遅延ゴール継続は、そのターンが開始するまで永続化され、`ephemeral: true` と組み合わせることはできない。`thread/start` と同じパーミッション上書きルールを受け付ける。
- `thread/start`、`thread/resume`、`thread/fork` のレスポンスには、レガシーな `sandbox` 互換 projection が含まれる。`instructionSources` は、リモート環境から読み込まれたファイルを含め、各ソース環境のネイティブな絶対パス構文を使って読み込まれた命令ファイルを一覧する。実験的なクライアントは、スレッドスコープのランタイムルートについては `runtimeWorkspaceRoots` を、既知の場合は名前付きまたは暗黙のビルトインプロファイルの identity/provenance については `activePermissionProfile` を読み取れる。それらの非推奨の実験的な `multiAgentMode` フィールドおよび対応するスレッド設定は、常に `explicitRequestOnly` を報告する。Ultra 推論努力度が積極的なマルチエージェント動作のソースである。
- `thread/list` — 保存されているスレッドをページングする。カーソルベースのページネーションと、オプションの `modelProviders`、`sourceKinds`、`archived`、`sectionId`、`cwd`、`searchTerm` フィルタをサポートする。セクションをその永続化された手動順序で一覧するときは `sortKey` を `"section_position"` に設定する。実験的なクライアントは、直接の spawn された子については `parentThreadId` を、任意の深さの spawn された子孫については `ancestorThreadId` を使用できる。この 2 つのフィルタは互いに排他的である。Review スレッドと Guardian スレッドは、この spawn-edge のライフサイクルに参加しないため含まれない。返される各 `thread` は `status` (`ThreadStatus`) を含み、スレッドが現在ロードされていない場合は `notLoaded` がデフォルトになる。サブエージェントスレッドは、直接の親が既知の場合、`parentThreadId` も含む。
- `threadSection/list` — 独立して永続化されたスレッドセクションとその表示名をページングする。現在スレッドを含まないセクションも含む。
- `thread/loaded/list` — 現在メモリにロードされているスレッド id を一覧する。
- `thread/read` — 保存されているスレッドを id で読み取り、再開はしない。オプションで `includeTurns` によりターンを含める。返される `thread` は `status` (`ThreadStatus`) を含み、スレッドが現在ロードされていない場合は `notLoaded` がデフォルトになる。ロード済みのスレッドについては、実験的なクライアントは `canAcceptDirectInput` を使って `turn/start` と `turn/steer` が受け付けられるかどうかを判定できる。未ロードの保存済みスレッドは、その能力が利用できない場合 `null` を報告する。
- `thread/turns/list` — 実験的。スレッドを再開せずに、保存されているスレッドのターン履歴をページングする。`sortDirection`、`itemsView`、`nextCursor`、`backwardsCursor` を使ったカーソルベースのページネーションをサポートする。
- `thread/items/list` — 実験的。スレッドを再開せずに、永続化されたスレッドアイテムをページングする。結果を 1 つのターンに限定するには `turnId` を渡す。省略するとスレッド全体にわたってアイテムをページングする。アクティブなスレッドストアはアイテムページネーションをサポートしている必要がある。
- `thread/searchOccurrences` — 実験的。1 つのページング対応スレッド内で、可視のユーザーメッセージとサマリー選択された最終アシスタントメッセージから、リテラルな大文字小文字を区別しないマッチを見つける。
- `thread/metadata/update` — sqlite 内の保存済みスレッドメタデータにパッチを当てる。永続化された `gitInfo` フィールドの更新をサポートし、更新後の `thread` を返す。
- `thread/section/move` — スレッドを、`sectionId` で識別されるセクションへ、別のスレッドの前 (または `beforeThreadId` が `null` の場合は末尾) にアトミックに移動する。同じセクション内での並べ替えは `sectionEnteredAt` を保持する。別のセクションに入るとリセットされる。`sectionId` を `null` に設定すると、スレッドをそのセクションから除去する。成功時は `{}` を返す。
- `thread/settings/update` — 実験的。ターンを開始したり transcript アイテムを追加したりせずに、ロード済みスレッドの次ターン設定への部分的な更新をキューに入れる。省略されたフィールドは変更されない。`serviceTier: null` はティアをクリアする。非推奨の `multiAgentMode` は無視され、Ultra 推論努力度が積極的なマルチエージェント動作を有効にする。`sandboxPolicy` と `permissions` は組み合わせることができない。更新が受け入れられると `{}` を返し、実際に変化した場合のみ、完全な有効設定を伴う `thread/settings/updated` を発行する。`turn/start` の設定上書きは、保存されている設定を変更する場合、同じ通知を発行する。
- `thread/memoryMode/set` — 実験的。ロード済みのスレッドまたは保存済みの rollout に対して、スレッドの永続化されたメモリ適格性を `"enabled"` または `"disabled"` に設定する。成功時は `{}` を返す。
- `memory/reset` — 実験的。既存のスレッドメモリモードを保持したまま、現在の `CODEX_HOME/memories` ディレクトリをクリアし、sqlite 内の永続化されたメモリステージデータをリセットする。成功時は `{}` を返す。
- `thread/goal/set` — マテリアライズされたスレッドに対する単一の永続化されたゴールを作成または更新する。現在のゴールを返し、`thread/goal/updated` を発行する。
- `thread/goal/get` — マテリアライズされたスレッドの現在の永続化されたゴールを取得する。ゴールが存在しない場合は `goal: null` を返す。
- `thread/goal/clear` — マテリアライズされたスレッドの現在の永続化されたゴールをクリアする。ゴールが除去されたかどうかを返し、状態が変化した場合は `thread/goal/cleared` を発行する。
- `thread/goal/updated` — スレッドのゴールが変化するたびに発行される通知。現在の完全なゴールを含む。
- `thread/goal/cleared` — スレッドのゴールが除去されるたびに発行される通知。
- `thread/settings/updated` — 実験的。ロード済みスレッドの有効な次ターン設定が変化したとき、購読しているクライアントに発行される通知。`threadId` と完全な `threadSettings` を含む。
- `thread/status/changed` — ロード済みスレッドのステータスが変化したときに発行される通知 (`threadId` + 新しい `status`)。
- `thread/archive` — スレッドの rollout ファイルをアーカイブディレクトリに移動し、spawn された子孫スレッドの rollout ファイルもあれば移動を試みる。成功時は `{}` を返し、アーカイブされた各スレッドについて `thread/archived` を発行する。
- `thread/delete` — アクティブまたはアーカイブ済みのスレッドと、spawn された子孫スレッドをハード削除する。成功時は `{}` を返し、削除された各スレッドについて `thread/deleted` を発行する。
- `thread/unsubscribe` — この接続の、スレッドの turn/item イベントへの購読を解除する。これが最後の購読者だった場合、サーバーはスレッドをロードされたままにし、購読者もスレッドアクティビティもない状態が 30 分続いた後にのみアンロードし、`SessionEnd` フックを実行してから `thread/closed` を発行する。
- `thread/name/set` — ロード済みスレッドまたは永続化された rollout に対して、スレッドのユーザー向け名前を設定または更新する。成功時は `{}` を返し、初期化済みでオプトインしたクライアントに `thread/name/updated` を発行する。スレッド名は一意である必要はない。名前検索は最も最近更新されたスレッドに解決される。
- `thread/unarchive` — アーカイブ済みの rollout ファイルを sessions ディレクトリに戻す。成功時は復元された `thread` を返し、`thread/unarchived` を発行する。
- `thread/compact/start` — スレッドの会話履歴圧縮 (compaction) をトリガーする。標準の turn/item 通知経由で進捗がストリーミングされる間、即座に `{}` を返す。
- `thread/shellCommand` — スレッドに対してユーザーが開始した `!` シェルコマンドを実行する。これは、スレッドのサンドボックスポリシーを継承するのではなく、フルアクセスでサンドボックスなしに実行される。標準の turn/item 通知経由で進捗がストリーミングされ、アクティブなターンがあればそのメッセージストリームにフォーマット済み出力が届く間、即座に `{}` を返す。
- `thread/backgroundTerminals/clean` — スレッドについて実行中のすべてのバックグラウンドターミナルを終了させる (実験的。`capabilities.experimentalApi` が必要)。クリーンアップリクエストが受け入れられると `{}` を返す。
- `thread/backgroundTerminals/list` — ロード済みスレッドについて実行中のバックグラウンドターミナルを一覧する (実験的。`capabilities.experimentalApi` が必要)。実行中のターミナル id を含む `data` を返す。
- `thread/backgroundTerminals/terminate` — app-server の `processId` で 1 つの実行中バックグラウンドターミナルを終了させる (実験的。`capabilities.experimentalApi` が必要)。プロセスが終了されたかどうかを返す。
- `thread/rollback` — 非推奨。まもなく削除される。エージェントのインメモリコンテキストから最後の N ターンを削除し、rollback マーカーを rollout に永続化することで、将来の再開でプルーニングされた履歴が見えるようにする。成功時は更新された `thread` (`turns` が入力された状態) を返す。ページング対応スレッドは rollback をサポートしない。
- `turn/start` — スレッドにユーザー入力を追加し、Codex の生成を開始する。最初の `turn` オブジェクトで応答し、`turn/started`、`item/*`、`turn/completed` の通知をストリーミングする。`clientUserMessageId` はオプションで、指定すると対応する `userMessage` アイテムがそれを `clientId` としてエコーバックする。実験的な `runtimeWorkspaceRoots` は、新しく解決される環境選択のデフォルトルートを提供する。明示的な `environments[].runtimeWorkspaceRoots` は、環境ネイティブの絶対パスでそのフォールバックを上書きする。パーミッションの上書きには、id によるプロファイル選択である実験的な `permissions` を優先する。レガシーな `sandboxPolicy` フィールドは引き続き受け付けられるが、`permissions` と組み合わせることはできない。`collaborationMode` については、`settings.developer_instructions: null` は「選択されたモードの組み込み命令を使う」ことを意味する。非推奨の実験的な `multiAgentMode` は無視される。Ultra 推論努力度が積極的な動作を選択する。
- `thread/inject_items` — ユーザーターンを開始せずに、生の Responses API アイテムをロード済みスレッドのモデル可視な履歴に追記する。成功時は `{}` を返す。
- `turn/steer` — 新しいターンを開始せずに、既にインフライトな通常のターンにユーザー入力を追加する。入力を受け付けたアクティブな `turnId` を返す。`clientUserMessageId` はオプションで、指定すると対応する `userMessage` アイテムがそれを `clientId` としてエコーバックする。Review と手動 compaction のターンは `turn/steer` を拒否する。
- `turn/interrupt` — `(thread_id, turn_id)` によってインフライトなターンのキャンセルを要求する。成功は空の `{}` レスポンスで、ターンは `status: "interrupted"` で終了する。
- `thread/realtime/start` — スレッドスコープのリアルタイムセッションを開始する (実験的)。モデル出力を選ぶには `outputModality: "text"` または `outputModality: "audio"` を渡す。オプションで `model` と `version` を渡すと、このセッションに限り設定済みのリアルタイム選択を上書きできる。`includeStartupContext: false` を渡すと Codex が生成するスタートアップコンテキストを省略でき、オプションで `initialItems` を渡すと、セッション作成時に完全なロール付きテキストメッセージで V3 をシードできる。バージョン `"v1"` はレガシーの Bidi `conversation.handoff.*` を使い、`"v2"` は Realtime Voice API を使い、`"v3"` は Frameless Bidi `delegation.*` を使いながら V1 の Codex Voice の挙動を保持する。V3 の自動 Codex テキストについて、`codexResponseHandoffMode` は `"thinking"` (デフォルト。すべての出力がチャンネルなしの thinking append を使う)、`"commentary"` (すべての出力が commentary チャンネルを使う)、または `"bemTags"` (生の BEM エンベロープが使用する API チャンネルを選択する: BEM の `analysis` と `commentary` は `commentary` を使い、BEM の `final` とパース不能な出力は `speakable` を使う) を受け付ける。BEM エンベロープは、フロントエンドモデルが解釈できるよう append されたテキスト内に残る。V1 と V2 はこの設定を無視する。V3 のハンドオフはレガシーな `"Agent Final Message"` ラベルを前置しない。`clientManagedHandoffs: true` を渡すと、自動的な Codex 応答配信を無効化し、クライアントの明示的な append 呼び出しのみがハンドオフを生成する。`codexResponsesAsItems: true` を渡すと、自動的な Codex 応答を代わりにリアルタイム会話アイテムとして送信し、オプションで `codexResponseItemPrefix` を渡すとそれらのアイテムに実験用の指示を前置できる。`{}` を返し、`thread/realtime/*` 通知をストリーミングする。`transport` を省略すると websocket トランスポートになり、`{ "type": "webrtc", "sdp": "..." }` を渡すと、ブラウザが生成した SDP オファーから Bidi WebRTC セッションを作成する。リモートのアンサー SDP は `thread/realtime/sdp` として発行される。会話の `version: "v2"` リクエストは WebRTC では引き続きサポートされない。
- `thread/realtime/appendAudio` — アクティブなリアルタイムセッションに入力音声チャンクを追加する (実験的)。`{}` を返す。
- `thread/realtime/appendText` — アクティブなリアルタイムセッションに、必須の `role` (`user`、`developer`、または `assistant`) を伴うテキスト入力を追加する (実験的)。`{}` を返す。`role` を省略する古いクライアントは `user` をデフォルトとする。
- `thread/realtime/appendSpeech` — リアルタイムモデルがユーザーに向けて話すべきテキストを追加する (実験的)。`{}` を返す。
- `thread/realtime/stop` — スレッドのアクティブなリアルタイムセッションを停止する (実験的)。`{}` を返す。
- `review/start` — スレッドに対して Codex の自動レビューアーを起動する。`turn/start` と同様に応答する。インラインレビューは、`enteredReviewMode` と `exitedReviewMode` アイテムを伴う `item/started`/`item/completed` 通知、そしてレビューを含む最終的なアシスタントの `agentMessage` を発行する。分離 (detached) レビューは、新しいレビュースレッド上で通常の turn アイテムをストリーミングする。
- `command/exec` — スレッド/ターンを開始せずに、サーバーサンドボックス下で単一のコマンドを実行する (ユーティリティや検証に便利)。
- `command/exec/write` — 実行中の `command/exec` セッションに base64 デコードされた stdin バイトを書き込む、または stdin を閉じる。`{}` を返す。
- `command/exec/resize` — `processId` によって、実行中の PTY バックの `command/exec` セッションをリサイズする。`{}` を返す。
- `command/exec/terminate` — `processId` によって、実行中の `command/exec` セッションを終了させる。`{}` を返す。
- `command/exec/outputDelta` — ストリーミングされる `command/exec` セッションからの base64 エンコードされた stdout/stderr チャンクについて発行される通知。
- `process/spawn` — 実験的。app server が動いているホスト上で、Codex サンドボックスなしにスタンドアロンのプロセスを起動する。プロセスが起動した後に返答し、`process/outputDelta` と `process/exited` の通知を発行する。
- `process/writeStdin` — 実験的。実行中の `process/spawn` セッションに base64 デコードされた stdin バイトを書き込む、または stdin を閉じる。`{}` を返す。
- `process/resizePty` — 実験的。`processHandle` によって、実行中の PTY バックの `process/spawn` セッションをリサイズする。`{}` を返す。
- `process/kill` — 実験的。`processHandle` によって、実行中の `process/spawn` セッションを終了させる。`{}` を返す。
- `process/outputDelta` — 実験的。ストリーミングされる `process/spawn` セッションからの base64 エンコードされた stdout/stderr チャンクについて発行される通知。
- `process/exited` — 実験的。`process/spawn` セッションが終了したときに発行される通知。
- `fs/readFile` — 絶対ファイルパスを読み取り、`{ dataBase64 }` を返す。
- `fs/writeFile` — base64 エンコードされた `{ dataBase64 }` から絶対ファイルパスに書き込む。`{}` を返す。
- `fs/createDirectory` — 絶対ディレクトリパスを作成する。`recursive` のデフォルトは `true`。
- `fs/getMetadata` — 絶対パスのメタデータを返す: `isDirectory`、`isFile`、`isSymlink`、`createdAtMs`、`modifiedAtMs`。
- `fs/readDirectory` — 絶対ディレクトリパスの直下の子エントリを一覧する。各エントリは `fileName`、`isDirectory`、`isFile` を含み、`fileName` はパスではなく子の名前のみである。
- `fs/remove` — 絶対ファイルまたはディレクトリツリーを削除する。`recursive` と `force` のデフォルトは `true`。
- `fs/copy` — 絶対パス間でコピーする。ディレクトリコピーには `recursive: true` が必要。
- `fs/watch` — この接続を、絶対ファイルまたはディレクトリパスと呼び出し元が指定した `watchId` に対するファイルシステム変更通知に購読させる。正規化された `path` を返す。
- `fs/unwatch` — 以前の `fs/watch` の通知送信を停止する。`{}` を返す。
- `fs/changed` — 監視されているパスが変化したときに発行される通知。`watchId` と `changedPaths` を含む。
- `model/list` — 利用可能なモデルを一覧する (`hidden: true` のエントリも含めるには `includeHidden: true` を設定)。カタログが意図する進行順序でのモデル提示の文字列推論努力度オプション、`additionalSpeedTiers`、`serviceTiers`、オプションの `defaultServiceTier`、オプションのレガシー `upgrade` モデル id、オプションの `upgradeInfo` メタデータ (`model`、`upgradeCopy`、`modelLink`、`migrationMarkdown`)、オプションの `availabilityNux` メタデータを伴う。クライアントは、努力度名から順序を導出するのではなく、`supportedReasoningEfforts` 配列の順序を保持すべきである。
- `modelProvider/capabilities/read` — 現在設定されているモデルプロバイダーのプロバイダーレベルの能力を読み取る。
- `experimentalFeature/list` — ステージメタデータ (`beta`、`underDevelopment`、`stable` など)、有効/デフォルト有効状態、カーソルページネーションを伴う機能フラグを一覧する。既存のロード済みスレッドの機能状態を表示する場合は `threadId` を渡すと、`enabled` はそのスレッドの cwd 用のプロジェクトローカル設定を含む、更新済みの設定から計算される。省略された場合、サーバーはデフォルトの設定解決コンテキストを使う。beta でないフラグについては、`displayName`/`description`/`announcement` は `null` になる。
- `permissionProfile/list` — beta。効果的な要件を反映するオプションの表示用 `description` テキストと `allowed` フラグを伴う、利用可能なパーミッションプロファイル id をカーソルページネーションで一覧する。呼び出し元がプロジェクトローカルの `[permissions.<id>]` エントリを現在のカタログビューに含める必要がある場合は `cwd` を渡す。
- `experimentalFeature/enablement/set` — 現在サポートされている機能キーに対する、プロセス全体のインメモリなランタイム機能有効化にパッチを当てる。各機能について、優先順位は: cloud requirements > `--enable <feature_name>` > config.toml > `experimentalFeature/enablement/set` (新) > コードのデフォルト、の順である。無効なキーは無視される。
- `environment/add` — 実験的。後で `thread/start` や `turn/start` から選択できるよう、`environmentId` と `execServerUrl` によって名前付きのリモート環境を追加または置換する。オプションの `connectTimeoutMs` は WebSocket 接続タイムアウトを上書きする。`{}` を返し、デフォルト環境は変更しない。
- `environment/info` — 実験的。`environmentId` によって設定済みの環境に接続し、検出された `shell` とデフォルトの `cwd` を正規の環境ネイティブな `file:` URI として返す。接続失敗はリクエストエラーとして返される。
- `environment/status` — 実験的。1 つの設定済み `environmentId` の現在のステータスを読み取る。準備済みのリモート環境は、環境を起動・再接続せずに、既存の exec-server 接続経由でプローブされる。レスポンスは `ready`、`pending`、`disconnected`、`unknown` のいずれかを報告する。
- `thread/environment/connected` と `thread/environment/disconnected` — 実験的。選択された環境について、スレッド起動後に観測された exec-server 接続遷移を報告する。現在の接続状態はリプレイされない。
- `collaborationMode/list` — 利用可能なコラボレーションモードのプリセットを一覧する (実験的。ページネーションなし)。組み込みのプリセットはモデルを選択しない。Plan プリセットは medium 推論努力度を選択する。このレスポンスは組み込みの開発者向け指示を省略する。クライアントは、モードを設定する際に Codex の組み込み命令を使うために `settings.developer_instructions: null` を渡すか、独自の命令を明示的に提供すべきである。
- `skills/list` — 1 つ以上の `cwd` 値についてスキルを一覧する (オプションの `forceReload`)。
- `skills/extraRoots/set` — app-server プロセスのランタイムの追加スタンドアロンスキルルートを置き換える。ルートは永続化されない。存在しないディレクトリは受け入れられ、単にスキルをロードしない。
- `hooks/list` — 1 つ以上の `cwd` 値について検出されたフックを一覧する。
- `marketplace/add` — HTTP(S) の Git URL、SSH の Git URL、または GitHub の `owner/repo` 省略形から、リモートのプラグインマーケットプレイスを追加し、ユーザーのマーケットプレイス設定に永続化する。インストール済みのルートパスと、そのマーケットプレイスが既に存在していたかどうかを返す。
- `marketplace/remove` — ユーザーのマーケットプレイス設定から名前で設定済みのマーケットプレイスを削除し、インストール済みのマーケットプレイスルートが存在する場合はそれも削除する。
- `marketplace/upgrade` — 設定済みのすべての Git プラグインマーケットプレイス、または `marketplaceName` が指定された場合は 1 つの名前付きマーケットプレイスをアップグレードする。選択されたマーケットプレイス名、アップグレードされたルート、マーケットプレイスごとのエラーを返す。
- `plugin/list` — 検出されたプラグインマーケットプレイスとプラグインの状態を一覧する。有効なマーケットプレイスのインストール/認証ポリシーメタデータ、`installPolicySource` (`WORKSPACE_SETTING` または `IMPLICIT_CANONICAL_APP`) の null 許容のリモートインストールポリシー由来情報、利用可能な場合のリモートマーケットプレイスの `version` とローカルでマテリアライズされた `localVersion`、プラグインの `availability` (デフォルトは `AVAILABLE`、上流でブロックされたリモートプラグインは `DISABLED_BY_ADMIN`)、パースまたはロードできなかったマーケットプレイスファイルに対する fail-open な `marketplaceLoadErrors` エントリ、公式のキュレーション済みマーケットプレイスに対するベストエフォートな `featuredPluginIds` を含む。plugin list、installed、read、share-list メソッドが返すすべての `PluginSummary` は、null 許容の `disabledReason` と `eligiblePlanTypes` を含み、リモートプラグインについてはプラグインサービスの可用性メタデータと生のプラン識別子を保持し、ローカルプラグインまたは古いリモートレスポンスについては `null` を返す。同じサマリーは `mustShowInstallationInterstitial` も含む: リモートサービスの値は `true` または `false` を保持し、ローカルプラグインとポリシーを省略するリモートレスポンスは `null` になる。クライアントは値が `null` のとき fail closed (安全側に倒す) すべきである。クライアントは、リモートの `workspace-directory`、`shared-with-me`、`created-by-me-remote` のマーケットプレイス種別を明示的にリクエストできる。`forceRefetch: true` を設定すると、リクエストされたマーケットプレイスについて TTL バックのリモートカタログキャッシュをバイパスし、新しいデータを待つ。キャッシュエントリは、取得が成功した場合のみ置き換えられる。ローカルのマーケットプレイスが含まれる場合、リクエストは、マーケットプレイスのサマリーが返される前に、設定済みのプラグインキャッシュが調和 (reconcile) するのも待つ。app-server の起動時、既存のキャッシュ済みカタログはバックグラウンドで更新される間も `plugin/list` から利用可能なままである。`interface.category` は、存在する場合マーケットプレイスのカテゴリを使い、そうでなければプラグインマニフェストのカテゴリにフォールバックする (**開発中。本番クライアントからはまだ呼び出さないこと**)。
- `plugin/installed` — より広いリモートカタログをフェッチせずに、インストール済みのプラグイン行と、明示的にリクエストされたローカルのインストール提案プラグイン名を一覧する。リモートの行は、null 許容の `installPolicySource` と、Unix 秒単位のバックエンドインストールタイムスタンプである `installedAt` を含む。`installedAt` は `plugin/list`、`plugin/read`、`plugin/share/list` からも返される。ローカルプラグイン、未インストールのプラグイン、デフォルトでインストールされるプラグイン、インストールタイムスタンプを含まない古いバックエンドレスポンスでは `null` になる。メンション表示サーフェスは、プラグインページの発見データではなく、プラグインメンションのペイロードが必要な場合、この狭いビューを使える (**開発中。本番クライアントからはまだ呼び出さないこと**)。
- `plugin/read` — `marketplacePath` と `pluginName` によって 1 つのプラグインを読み取り、マーケットプレイス情報、list 形式の `summary`、マニフェストの説明/インターフェースメタデータ、同梱されているスキル/フック/アプリ/MCP サーバー名を返す。リモートプラグインの詳細には、カタログからのスケジュール済みタスクのサマリーを含められる。`scheduledTasks: null` はメタデータが利用不可であることを意味し、空配列はカタログがスケジュール済みタスクを見つけなかったことを意味する。リモートプラグインの詳細は、リモートカタログが提供する場合、正規の `shareUrl` を公開する。利用不可の場合、またはカタログがそれを省略する場合は `null` になる。このフィールドは、引き続きユーザーとワークスペースの共有状態を記述する `summary.shareContext` とは別である。所有しているワークスペースのプラグインについて、`summary.shareContext.canPublishToWorkspace` は、現在のユーザーがプラグインをワークスペースディレクトリに追加できるかどうかを報告する。`plugin/share/save` は、共有を作成または更新した後に同じ能力を返す。クライアントは、いずれかの値が `null` のときは fail closed すべきである。リモートのスキルインターフェースは、カタログがアイコン URL を提供する場合、`iconSmallUrl` と `iconLargeUrl` を公開する。返されるプラグインスキルは、ローカル設定によるフィルタリング後の現在の `enabled` 状態を含む。同梱されているフックは、`hooks/list` との相関付けのためにキー付けされた軽量な宣言サマリーとして返される。インストール後の認証を促すには `plugin/install` の `appsNeedingAuth` を、現在のコネクタのアクセス可能性を判定するには `app/list` の `isAccessible` を使う (**開発中。本番クライアントからはまだ呼び出さないこと**)。
- `plugin/skill/read` — `remoteMarketplaceName`、`remotePluginId`、`skillName` によって、オンデマンドでリモートプラグインのスキル markdown を読み取る。これにより、クライアントはプラグインバンドルをダウンロードせずに、未インストールのリモートプラグインのスキルをプレビューできる。
- `skills/changed` — 監視されているローカルのスキルファイルが変化したときに発行される通知。
- `app/installed` — 最後にコミットされたスナップショットから、インストール済みのコネクタランタイム状態を読み取る。オプションで先にリフレッシュする。
- `app/list` — 利用可能なアプリを一覧する。
- `remoteControl/enable` — 実験的。現在の app-server プロセスのリモートコントロールを有効化し、現在のリモートコントロールステータススナップショットを返す。デフォルトでは、不足している登録はレスポンス前に完了し、その設定は現在の app-server クライアントスコープに対して永続化される。プロセスに対してのみリモートコントロールを有効化し、永続化された設定を変更しないようにするには `ephemeral: true` を渡す。
- `remoteControl/disable` — 実験的。現在の app-server プロセスのリモートコントロールを無効化し、現在のリモートコントロールステータススナップショットを返す。デフォルトでは、無効化された設定は現在の app-server クライアントスコープに対して永続化される。プロセスに対してのみ無効化し、永続化された設定を変更しないようにするには `ephemeral: true` を渡す。これは、既に登録済みのコントローラーデバイスを取り消すものではない。
- `remoteControl/status/read` — 実験的。現在のリモートコントロールステータススナップショットを読み取る。`status` は `disabled`、`connecting`、`connected`、`errored` のいずれか。`serverName` はこの app-server プロセスが使うローカルマシン名。`environmentId` は、app-server に現在の登録がある場合は文字列、その登録がクリア・無効化されている場合やリモートコントロールが無効な場合は `null` になる。
- `remoteControl/pairing/start` — 実験的。現在の app-server プロセスのために、短命なリモートコントロールペアリングアーティファクトを開始する。手動ペアリングコードも要求するには `manualCode: true` を渡す。`pairingCode`、`manualPairingCode`、`environmentId`、Unix 秒の `expiresAt` を返す。app-server は意図的にバックエンドの `serverId` を公開しない。
- `remoteControl/pairing/status` — 実験的。リモートコントロールの `pairingCode` または `manualPairingCode` が claim (受諾) されたかどうかをポーリングする。2 つのフィールドのうち正確に 1 つを渡す。`claimed` を返す。
- `remoteControl/client/list` — 実験的。環境へのアクセスを許可されたコントローラーデバイスを一覧する。`environmentId` とオプションの `cursor`、`limit`、`order` を渡す。ピッカー向けのクライアントメタデータと `nextCursor` を返す。この署名済みアカウント管理操作は、ローカルリレーが無効または未登録の間も機能する。
- `remoteControl/client/revoke` — 実験的。環境への 1 つのコントローラーデバイスの許可を取り消す。`environmentId` と `clientId` を渡す。空オブジェクトを返す。この署名済みアカウント管理操作は、ローカルリレーが無効または未登録の間も機能する。
- `remoteControl/status/changed` — リモートコントロールのステータスまたはクライアントに見える環境 id が変化したときに発行される通知。`status` は `disabled`、`connecting`、`connected`、`errored` のいずれか。`serverName` はこの app-server プロセスが使うローカルマシン名。`environmentId` は、app-server に現在の登録がある場合は文字列、その登録がクリア・無効化されている場合やリモートコントロールが無効な場合は `null` になる。新しく初期化された app-server クライアントは、常に現在のステータススナップショットを受け取る。
- `skills/config/write` — 名前または絶対パスによって、ユーザーレベルのスキル設定を書き込む。
- `plugin/install` — 発見されたマーケットプレイスエントリからプラグインをインストールする。インストール不可とマークされたマーケットプレイスエントリは拒否し、あれば MCP をインストールし、有効なプラグイン認証ポリシーと認証がまだ必要なアプリを返す (**開発中。本番クライアントからはまだ呼び出さないこと**)。
- `plugin/uninstall` — `<plugin>@<marketplace>` 形式の `pluginId` によってローカルプラグインをアンインストールする (キャッシュされたファイルを削除し、ユーザーレベルの設定エントリをクリアすることで)。または、バックエンドの `pluginId` によってリモートの ChatGPT プラグインをアンインストールする (アンインストールを ChatGPT プラグインバックエンドに転送し、ダウンロード済みのリモートプラグインキャッシュを削除することで) (**開発中。本番クライアントからはまだ呼び出さないこと**)。
- `mcpServer/oauth/login` — 設定済みの MCP サーバーの OAuth ログインを開始する。そのスレッドが選択したプラグインとエグゼキューターからサーバーを解決するには `threadId` を渡す。`authorization_url` を受け取り、続いてブラウザフローが完了すると `mcpServer/oauthLogin/completed` を受け取る。
- `tool/requestUserInput` — ツール呼び出しについて 1〜3 個の短い質問でユーザーにプロンプトを出し、その回答を返す (実験的)。
- `config/mcpServer/reload` — ディスクから MCP サーバー設定を再読み込みし、ロード済みスレッドのリフレッシュをキューに入れる (各スレッドの次のアクティブなターンで適用される)。`{}` を返す。サーバーを再起動せずに `config.toml` を編集した後にこれを使う。
- `mcpServerStatus/list` — 設定済みの MCP サーバーを、そのツール、認証ステータス、サーバー情報、`full` 詳細についてはリソース/リソーステンプレートとともに列挙する。オプションの `threadId` とカーソル+limit のページネーションをサポートする。`threadId` が省略された場合、サーバーは最新のグローバル設定から直接読み取る。`detail` が省略された場合、サーバーは `full` をデフォルトとする。`unknown` の認証ステータスは、OAuth サポートを判定できなかったことを意味する。`unsupported` は、OAuth がサポートされていないことが既知であることを意味する。
- `mcpServer/resource/read` — オプションの `threadId`、`server`、`uri` によって、設定済みの MCP サーバーからリソースを読み取り、テキスト/ブロブのリソース `contents` を返す。`threadId` が省略された場合、サーバーは最新の MCP 設定から直接読み取る。
- `mcpServer/tool/call` — `threadId`、`server`、`tool`、オプションの `arguments`、オプションの `_meta` によって、スレッドの設定済み MCP サーバー上のツールを呼び出し、MCP のツール結果を返す。
- `windowsSandbox/setupStart` — 選択されたモード (`elevated` または `unelevated`) で Windows サンドボックスのセットアップを開始する。特定のワークスペースをターゲットにするためのオプションの絶対 `cwd` を受け付ける。即座に `{ started: true }` を返し、後で `windowsSandbox/setupCompleted` を発行する。
- `feedback/upload` — フィードバックレポート (分類 + オプションの理由/ログ、conversation_id、オプションの `extraLogFiles` 添付配列) を送信する。追跡用スレッド id を返す。
- `config/read` — 設定の階層化と管理要件を解決した後の、`config.toml` に保存されている opaque な `desktop` 値を含む、ランタイムで有効な設定を取得する。
- `externalAgentConfig/detect` — `includeHome`、オプションの `cwds`、オプションの `migrationSource` セレクターによって、移行可能な外部エージェントのアーティファクトを検出する。省略、`null`、または未認識の migration-source 値はデフォルトの挙動を保持する。非推奨のオプションの `source` フィールドは互換性のため引き続き受け付けられるが、移行ソースは選択しない。検出された各アイテムは `cwd` (home の場合は `null`) を含み、複数アイテムの移行はさらに、プラグイン id、スキル名、メモリ、セッションメタデータ、その他のアーティファクト名を伴う構造化された `details` を含めてもよい。レスポンスには、検出されたソースセッションから推測されたコネクタ候補も含まれ、正規化された表示 `name`、そのコネクタを使用した検出済みセッションの数、検出に使われたソースメタデータフィールドを伴う。
- `externalAgentConfig/import` — `cwd` (home の場合は `null`) と detect が返した任意の `details` を伴う明示的な `migrationItems` を渡すことで、選択された外部エージェント移行アイテムを適用する。読み取るソースが一致するよう、detect で使ったのと同じオプションの `migrationSource` を渡す。省略、`null`、または未認識の値はデフォルトの挙動を保持する。オプションの `source` は、インポートを開始した製品を識別し、オプションの opaque な `providerId` は、移行ソースの選択に影響を与えずに、その製品が選択したプロバイダーに分析情報を帰属させる。レスポンスは、同期インポートフェーズを `importId` で確認する。予想される移行の失敗は、JSON-RPC エラーではなくアイテムごとの失敗として報告されるため、サーバーはその `importId` を引き続き返し、すべての同期・バックグラウンド作業が終わると同じ ID で `externalAgentConfig/import/completed` を発行する。完了通知には、成功と失敗を含む型レベルの `itemTypeResults` が含まれ、クライアントが別途報告できるよう生の失敗メッセージも含む。
- `externalAgentConfig/import/readHistories` — 完了したインポート履歴と、正常にインポートされたセッション履歴から検出されたコネクタ候補を読み取る。成功したセッションエントリは、利用可能だった場合、元のインポート時のタイトルを含む。コネクタ候補は、正規化された表示 `name`、そのコネクタを使用したインポート済みセッションの数、検出に使われたソースメタデータフィールドを含む。
- `config/value/write` — 単一の設定キー/値をユーザーのディスク上の config.toml に書き込む。`desktop.someKey` のようなドット区切りパスは、同じ汎用的な書き込みサーフェスを使う。管理要件と重複する書き込みは `configRequirementReadonly` で拒否される。
- `config/batchWrite` — 複数の `desktop.*` 編集を含め、複数の設定編集をユーザーのディスク上の config.toml にアトミックに適用する。ロード済みスレッドをホットリロードするオプションの `reloadUserConfig: true` を伴う。セッション静的なモデル、推論努力度、Plan モードの推論努力度、サービスティア、パーソナリティのデフォルトは既存のスレッドをリロードしない。
- `configRequirements/read` — `requirements.toml` および/または MDM からロードされた要件制約 (何も設定されていない場合は `null`) を取得する。正確な管理値 (`sqliteHome`、`logDir`、`modelCatalogJson`、`checkForUpdateOnStartup`、`allowLoginShell`、`feedback.enabled`、`windowsSandboxPrivateDesktop`)、許可リスト (`allowedApprovalPolicies`、`allowedSandboxModes`、`allowedWebSearchModes`)、階層化されたパーミッションプロファイルの許可マップ (`allowedPermissionProfiles`)、管理対象のパーミッションプロファイルのデフォルト (`defaultPermissions`)、ライフサイクルフックのロックダウン (`allowManagedHooksOnly`)、リモートコントロールポリシー (`allowRemoteControl`。`false` はリモートコントロールを強制無効化し、`true` または `null` は既存の挙動を保持する)、computer use ポリシー (`computerUse`)、Browser Use ポリシー (`browserUse.disableAutoReview`)、固定された機能値 (`featureRequirements`。管理者が `false` に設定できるデフォルト許可の `in_app_updates` ポリシーを含む)、管理対象のライフサイクルフック (`hooks`。各コマンドハンドラーのオプションの `additionalContextLimit` を含む)、`enforceResidency`、管理対象の新規スレッドデフォルト (`models.newThread.model`、`models.newThread.modelReasoningEffort`、`models.newThread.serviceTier`)、正規のドメイン/ソケットパーミッションと `managedAllowedDomainsOnly`・`dangerFullAccessDenylistOnly` のような `network` 制約を含む。

### Example: Start or resume a thread

新しい Codex 会話が必要なときは、新しいスレッドを開始する。

```json
{ "method": "thread/start", "id": 10, "params": {
    // Optionally set config settings. If not specified, will use the user's
    // current config settings.
    "model": "gpt-5.1-codex",
    "cwd": "/Users/me/project",
    "approvalPolicy": "never",
    "sandbox": "workspaceWrite",
    // Prefer experimental profile selection:
    // "permissions": ":workspace"
    // Experimental runtime roots for :workspace_roots materialization:
    // "runtimeWorkspaceRoots": ["/Users/me/project", "/Users/me/openai"],
    // Experimental capability roots selected by the hosting platform:
    "selectedCapabilityRoots": [
        {
            "id": "github@openai",
            "location": {
                "type": "environment",
                "environmentId": "workspace",
                "path": "/opt/cca/plugins/github"
            }
        }
    ],
    // Do not send both "sandbox" and "permissions".
    "personality": "friendly",
    "serviceName": "my_app_server_client", // optional metrics tag (`service_name`)
    "sessionStartSource": "startup", // optional: "startup" (default) or "clear"
    // Experimental: requires opt-in
    "dynamicTools": [
        {
            "type": "namespace",
            "name": "tickets",
            "description": "Ticket management tools",
            "tools": [
                {
                    "type": "function",
                    "name": "lookup_ticket",
                    "description": "Fetch a ticket by id",
                    "deferLoading": true,
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "id": { "type": "string" }
                        },
                        "required": ["id"]
                    }
                }
            ]
        }
    ],
} }
{ "id": 10, "result": {
    "thread": {
        "id": "thr_123",
        "preview": "",
        "modelProvider": "openai",
        "createdAt": 1730910000
    }
} }
{ "method": "thread/started", "params": { "thread": { … } } }
```

有効な `personality` の値は `"friendly"`、`"pragmatic"`、`"none"` である。`"none"` が選択されると、personality のプレースホルダーは空文字列に置き換えられる。

保存されているセッションを継続するには、以前に記録した `thread.id` を指定して `thread/resume` を呼ぶ。レスポンスの形は `thread/start` と一致する。保存されているセッションに永続化されたトークン使用量が含まれる場合、サーバーはレスポンスの直後に `thread/tokenUsage/updated` を発行するため、クライアントは次のターンが始まる前に復元された使用量を描画できる。`approvalsReviewer` を含む、`thread/start` がサポートするのと同じ設定上書きも渡すことができる。

デフォルトでは、`thread/resume` は再構築されたターン履歴を `thread.turns` に含む。実験的なクライアントは、`excludeTurns: true` を渡して、スレッドのメタデータとライブの再開状態のみを返し、ネットワーク経由でターン履歴をページングしたい場合は別途 `thread/turns/list` を呼ぶことができる。そのモードでは、サーバーは復元された `thread/tokenUsage/updated` のリプレイもスキップする。これにより、履歴的な使用量を帰属させるためだけにターンを再構築することを避けられる。

ページング対応スレッドは、レガシースレッドと同じ再開契約を保持する。デフォルトの再開は、投影された履歴全体を `thread.turns` にマテリアライズする。`excludeTurns: true` はその配列を空のままにし、再開境界で可視な永続的な履歴のための `turnsBackwardsCursor` と `itemsBackwardsCursor` を含む。各カーソルは、`sortDirection: "desc"` を伴ってそれぞれに対応する list API に直接渡す。最初のページはカーソルの先頭行を含み、より新しいレコードはライブ通知経由で届く。いずれのカーソルも、まだ永続的な行がない場合は `null` になる。

1 回のラウンドトリップでライブの再開購読とターンのページの両方が欲しい実験的なクライアントは、`initialTurnsPage` を渡すことができる。これは、`thread/turns/list` と同じ `limit`、`sortDirection`、`itemsView` の制御を受け付ける。省略された制御はそのデフォルトを使う。レスポンスは、フォローアップのページネーション用の `nextCursor` と `backwardsCursor` を伴う `initialTurnsPage` を含む。

デフォルトでは、再開はそのスレッドに関連付けられている最新の永続化された `model` と `reasoningEffort` の値を使う。`model`、`modelProvider`、`config.model`、`config.model_reasoning_effort` のいずれかを指定すると、その永続化されたフォールバックは無効化され、代わりに明示的な上書きと通常の設定解決が使われる。

例:

```json
{ "method": "thread/resume", "id": 11, "params": {
    "threadId": "thr_123",
    "personality": "friendly"
} }
{ "id": 11, "result": { "thread": { "id": "thr_123", … } } }

{ "method": "thread/resume", "id": 12, "params": {
    "threadId": "thr_123",
    "excludeTurns": true
} }
{ "id": 12, "result": {
    "thread": { "id": "thr_123", "turns": [], … },
    "turnsBackwardsCursor": "turn-head-cursor-or-null",
    "itemsBackwardsCursor": "item-head-cursor-or-null"
} }

{ "method": "thread/resume", "id": 13, "params": {
    "threadId": "thr_123",
    "excludeTurns": true,
    "initialTurnsPage": {
        "limit": 20,
        "sortDirection": "desc",
        "itemsView": "summary"
    }
} }
{ "id": 13, "result": {
    "thread": { "id": "thr_123", "turns": [], … },
    "initialTurnsPage": {
        "data": [ ... ],
        "nextCursor": "older-turns-cursor-or-null",
        "backwardsCursor": "newer-turns-cursor-or-null"
    }
} }
```

保存されているセッションから分岐するには、`thread.id` を指定して `thread/fork` を呼ぶ。これは新しい thread id を作成し、それに対する `thread/started` 通知を発行する。返される `thread.sessionId` は、現在のライブなセッションツリーのルートを識別する。ルートスレッドは自身の `thread.id` を `thread.sessionId` として使う。ロードされていない保存済みスレッドも自身の `thread.id` を報告する。これは、あるスレッドを再開するとそれが新しいライブなセッションツリーのルートになるためである。ソース履歴に永続化されたトークン使用量が含まれる場合、サーバーはレスポンスの直後に新しいスレッド向けの `thread/tokenUsage/updated` も発行する。ソーススレッドが実行中の場合、フォークは、あたかも現在のターンが先に中断されたかのようにそれをスナップショットする。フォークをインメモリのみにとどめるべき場合は `ephemeral: true` を渡す:

```json
{ "method": "thread/fork", "id": 12, "params": { "threadId": "thr_123", "ephemeral": true } }
{ "id": 12, "result": { "thread": { "id": "thr_456", "sessionId": "thr_456", … } } }
{ "method": "thread/started", "params": { "thread": { … } } }
```

`thread/resume` と同様に、実験的なクライアントは `thread/fork` に `excludeTurns: true` を渡して、`thread.turns` にはメタデータのみを返し、`thread/turns/list` で履歴をページングできる。このモードでは、サーバーは復元された `thread/tokenUsage/updated` のリプレイをスキップする。これにより、履歴的な使用量を帰属させるためだけにフォークパスがターンを再構築することを避けられる。ページング対応スレッドのエフェメラルなフォークには `excludeTurns: true` が必須である。

### Example: List threads (with pagination & filters)

`thread/list` を使うと履歴 UI を描画できる。結果はデフォルトで `createdAt` (新しい順) の降順になる。

ロード済みの spawn されたスレッドについて、実験的な `canAcceptDirectInput` は、V1 エージェントが直接入力を受け付ける場合は `true`、V2 エージェントがその親に所有されている場合は `false` になる。この能力が利用不可または適用不能な場合 (未ロードのスレッドや通常の CLI スレッドを含む) は `null` になる。`thread/list` と `thread/search` はいずれも、永続化されたメタデータではなく、ロード済みのスレッド状態からこの能力を導出する。

以下の任意の組み合わせを渡せる:

- `cursor` — 以前のレスポンスからの opaque な文字列。最初のページでは省略する。
- `limit` — 未設定の場合、サーバーは妥当なページサイズをデフォルトとする。
- `sortKey` — `created_at` (デフォルト)、`updated_at`、`recency_at`、またはセクションの永続化された手動順序のための `section_position`。
- `recencyAt` は、スレッドが作成されたときに初期化され、ターンが開始するたびに進む。`updatedAt` と異なり、バックグラウンド出力やその他の永続化された変更はこれを進めない。
- `sortDirection` — タイムスタンプソートでは `desc` (デフォルト)、`section_position` では `asc` (デフォルト)。
- `modelProviders` — 特定のプロバイダーに結果を制限する。未設定、null、空配列の場合はすべてのプロバイダーを含む。
- `sourceKinds` — 特定のソースに結果を制限する。省略するか `[]` を渡すと、対話的なセッションのみ (`cli`、`vscode`)。
- `archived` — `true` の場合、アーカイブ済みのスレッドのみを一覧する。`false` または `null` の場合、非アーカイブのスレッドを一覧する (デフォルト)。
- `sectionId` — `threadSection/list` からの ID を指定すると、そのセクションのスレッドを返す。セクションを持たないスレッドのみを返すには `null` を渡す。すべてのセクションとセクションを持たないスレッドを含めるには省略する。
- `cwd` — セッションの cwd がこのパスに正確に一致するスレッド、または配列が渡された場合はこれらのパスのいずれかに一致するスレッドに結果を制限する。相対パスは、マッチング前に app-server プロセスの cwd に対して解決される。
- `useStateDbOnly` — `true` の場合、メタデータを修復するために JSONL rollout をスキャンせずに state DB から返す。省略するか `false` を渡すと、デフォルトのスキャン+修復の挙動を保持する。
- `searchTerm` — 抽出されたタイトルにこの部分文字列を含むスレッド (大文字小文字を区別する) に結果を制限する。
- レスポンスには、同じ方向で続けるための `nextCursor` と、`sortDirection` を反転する際に `cursor` として渡す `backwardsCursor` が含まれる。
- レスポンスには、利用可能な場合、AgentControl が spawn したスレッドのサブエージェント向けの `agentNickname` と `agentRole` が含まれる。

例:

```json
{ "method": "thread/list", "id": 20, "params": {
    "cursor": null,
    "limit": 25,
    "cwd": ["/Users/me/project", "/Users/me/project-worktree"],
    "sortKey": "created_at"
} }
{ "id": 20, "result": {
    "data": [
        { "id": "thr_a", "preview": "Create a TUI", "modelProvider": "openai", "createdAt": 1730831111, "updatedAt": 1730831111, "recencyAt": 1730831111, "status": { "type": "notLoaded" }, "agentNickname": "Atlas", "agentRole": "explorer" },
        { "id": "thr_b", "preview": "Fix tests", "modelProvider": "openai", "createdAt": 1730750000, "updatedAt": 1730750000, "recencyAt": 1730750000, "status": { "type": "notLoaded" } }
    ],
    "nextCursor": "opaque-token-or-null",
    "backwardsCursor": "opaque-token-or-null"
} }
```

`nextCursor` が `null` のとき、最終ページに到達している。

### Example: List descendant threads

初期化時に `capabilities.experimentalApi` を有効にし、`ancestorThreadId` を伴う `thread/list` を使って、永続化された spawn-edge 状態から、あるスレッドの spawn されたすべての子孫をページングする。祖先自身は除外され、各結果の `parentThreadId` はその直接の親のままである。直接の子のみが欲しい場合は代わりに `parentThreadId` を使う。両方のフィルタを送るのは無効である。Review スレッドと Guardian スレッドは、spawn-edge のライフサイクルに参加しないため含まれない。`modelProviders` または `sourceKinds` が省略された場合、関係性でフィルタされたリクエストはそれぞれすべてのプロバイダーまたはソース種別を含む。明示的なフィルタは、空の `sourceKinds` リストに対する対話的のみのデフォルトを含め、通常の `thread/list` の挙動を保持する。

```json
{ "method": "thread/list", "id": 21, "params": {
    "ancestorThreadId": "00000000-0000-0000-0000-000000000100",
    "limit": 25
} }
{ "id": 21, "result": {
    "data": [
        { "id": "00000000-0000-0000-0000-000000000101", "parentThreadId": "00000000-0000-0000-0000-000000000100", "status": { "type": "notLoaded" } },
        { "id": "00000000-0000-0000-0000-000000000102", "parentThreadId": "00000000-0000-0000-0000-000000000101", "status": { "type": "notLoaded" } }
    ],
    "nextCursor": null,
    "backwardsCursor": null
} }
```

### Example: List loaded threads

`thread/loaded/list` は、現在メモリにロードされているスレッド id を返す。ディスク上の rollout をスキャンせずに、どのセッションがアクティブかを確認したいときに便利である。

```json
{ "method": "thread/loaded/list", "id": 21 }
{ "id": 21, "result": {
    "data": ["thr_123", "thr_456"]
} }
```

### Example: Track thread status changes

`thread/status/changed` は、既にクライアントに紹介された後にロード済みスレッドのステータスが変化するたびに発行される:

- `threadId` と新しい `status` を含む。
- ステータスは `notLoaded`、`idle`、`systemError`、または (`activeFlags` を伴う。`active` は実行中を意味する) `active` になり得る。
- `thread/start`、`thread/fork`、分離レビュースレッドは、別個の初期の `thread/status/changed` を発行しない。それらの `thread/started` 通知は既に現在の `thread.status` を運んでいる。

```json
{
  "method": "thread/status/changed",
  "params": {
    "threadId": "thr_123",
    "status": { "type": "active", "activeFlags": [] }
  }
}
```

### Example: Unsubscribe from a loaded thread

`thread/unsubscribe` は、現在の接続の、スレッドへの購読を除去する。レスポンスの status は次のいずれかである:

- `unsubscribed` — 接続が購読していて、今除去された場合。
- `notSubscribed` — 接続がそのスレッドを購読していなかった場合。
- `notLoaded` — スレッドがロードされていない場合。

これが最後の購読者だった場合、サーバーは即座にスレッドをアンロードしない。スレッドに購読者もアクティビティもない状態が 30 分続いた後にアンロードし、`SessionEnd` フックを実行してから `thread/closed` と `notLoaded` への `thread/status/changed` 遷移を発行する。

`SessionEnd` は、アーカイブ、削除、正常な app-server シャットダウンの前にも実行される。ルートスレッドに対してのみ実行され、`ThreadSpawn` の子や内部サブエージェントに対しては実行されない。フックはあくまで助言的であり、その出力はティアダウンをブロックできない。デフォルトのタイムアウトは 1 秒、設定されたタイムアウトは最大 3 秒にキャップされ、`async: true` は設定警告付きで同期的に実行され、フック入力は常に `reason: "other"` を報告する。`SessionEnd` のマッチャーは、その理由に対して評価される。

```json
{ "method": "thread/unsubscribe", "id": 22, "params": { "threadId": "thr_123" } }
{ "id": 22, "result": { "status": "unsubscribed" } }
```

その後、アイドルアンロードタイムアウトの後:

```json
{ "method": "thread/status/changed", "params": {
    "threadId": "thr_123",
    "status": { "type": "notLoaded" }
} }
{ "method": "thread/closed", "params": { "threadId": "thr_123" } }
```

### Example: Read a thread

`thread/read` を使って、保存されているスレッドを id で取得し、再開はしない。スレッド履歴を `thread.turns` にロードしたい場合は `includeTurns` を渡す。返されるスレッドは、利用可能な場合、サブエージェントスレッド向けの `parentThreadId`、`agentNickname`、`agentRole` を含む。

ページング対応スレッドはメタデータのみの読み取りをサポートする。`includeTurns: true` はそれらに対してはサポートされない。

```json
{ "method": "thread/read", "id": 22, "params": { "threadId": "thr_123" } }
{ "id": 22, "result": {
    "thread": { "id": "thr_123", "status": { "type": "notLoaded" }, "turns": [] }
} }
```

```json
{ "method": "thread/read", "id": 23, "params": { "threadId": "thr_123", "includeTurns": true } }
{ "id": 23, "result": {
    "thread": { "id": "thr_123", "status": { "type": "notLoaded" }, "turns": [ ... ] }
} }
```

### Example: List thread turns (experimental)

`capabilities.experimentalApi = true` を伴う `thread/turns/list` を使って、保存されているスレッドのターン履歴を再開せずにページングする。デフォルトでは、結果は降順にソートされるため、クライアントは現在から始めて `nextCursor` でより古いターンを取得できる。レスポンスには `backwardsCursor` も含まれる。これを、より前のページから見て最初のアイテムより新しいターンを取得するために、後のリクエストで `sortDirection: "asc"` を伴う `cursor` として渡す。

返されるすべての `Turn` は `itemsView` を含み、`items` 配列が意図的に省略されたか (`notLoaded`)、サマリーアイテムのみを含むか (`summary`)、永続化された app-server 履歴から利用可能なすべてのアイテムを含むか (`full`) をクライアントに伝える。返される詳細レベルを選ぶには `itemsView` を渡す。省略された `itemsView` はデフォルトで `"summary"` になる。

ページング対応スレッドも同じビューをサポートする。それらの `full` ビューは、app-server がターンページを返す前に、ページング対応のアイテム投影からマテリアライズされる。

```json
{ "method": "thread/turns/list", "id": 24, "params": {
    "threadId": "thr_123",
    "limit": 50,
    "sortDirection": "desc",
    "itemsView": "summary"
} }
{ "id": 24, "result": {
    "data": [ ... ],
    "nextCursor": "older-turns-cursor-or-null",
    "backwardsCursor": "newer-turns-cursor-or-null"
} }
```

`thread/items/list` は、オプションで 1 つのターンにフィルタして、スレッド全体の永続化されたアイテムをページングする:

```json
{ "method": "thread/items/list", "id": 25, "params": {
    "threadId": "thr_123",
    "turnId": "turn_456",
    "limit": 100,
    "sortDirection": "asc"
} }
```

各返されるエントリは、含まれる `turnId` とその完全な `item` を含むため、クライアントはフィルタされていないページをターンにグループ化できる。`turnId` を省略するか `null` を渡すと、スレッド全体にわたってアイテムをページングする。アイテムのカーソルは `turnId` の有無にかかわらず再利用できる。フィルタはカーソルのスコープを変更しない。アイテムページネーションを実装していないスレッドストアは、メッセージ `thread/items/list is not supported yet` を伴う JSON-RPC の `-32601` を返す。

`thread/searchOccurrences` は、1 つのページング対応スレッドを、その rollout をリプレイせずに検索する。ステアリングメッセージを含むすべての可視のユーザーメッセージと最終アシスタントメッセージから、時系列順に出現箇所を返す。`snippetMatchRange` は `snippet` 内の UTF-16 オフセットを使い、`turnCursor` は含まれるターンをロードするために `thread/turns/list` に直接渡すことができる。

```json
{ "method": "thread/searchOccurrences", "id": 26, "params": {
    "threadId": "thr_123",
    "searchTerm": "needle",
    "limit": 50
} }
{ "id": 26, "result": {
    "data": [{
        "turnId": "turn_456",
        "itemId": "item_789",
        "snippet": "The needle is here.",
        "snippetMatchRange": { "start": 4, "end": 10 },
        "turnCursor": "opaque-inclusive-turn-cursor"
    }],
    "nextCursor": null
} }
```

### Example: Update stored thread metadata

`thread/metadata/update` を使って、スレッドを再開せずに sqlite バックの `gitInfo` にパッチを当てる。省略されたフィールドは変更されない。一方、明示的な `null` は保存されている値をクリアする。セクションに入る・並べ替える・出るには `thread/section/move` を使う。セクションの位置は引き続きサーバーが所有し、`sortKey` が `section_position` のとき `thread/list` はスレッドを手動順序で返す。

```json
{ "method": "thread/metadata/update", "id": 24, "params": {
    "threadId": "thr_123",
    "gitInfo": { "branch": "feature/sidebar-pr" }
} }
{ "id": 24, "result": {
    "thread": {
        "id": "thr_123",
        "gitInfo": { "sha": null, "branch": "feature/sidebar-pr", "originUrl": null }
    }
} }

{ "method": "thread/metadata/update", "id": 25, "params": {
    "threadId": "thr_123",
    "gitInfo": { "branch": null }
} }
{ "id": 25, "result": {
    "thread": {
        "id": "thr_123",
        "gitInfo": null
    }
} }

{ "method": "thread/section/move", "id": 26, "params": {
    "threadId": "thr_123",
    "sectionId": "01984de2-8f74-7c91-a3b2-5c5e937cf318",
    "beforeThreadId": null
} }
{ "id": 26, "result": {} }

{ "method": "thread/list", "id": 27, "params": {
    "sectionId": "01984de2-8f74-7c91-a3b2-5c5e937cf318",
    "sortKey": "section_position",
    "limit": 100
} }

{ "method": "thread/section/move", "id": 28, "params": {
    "threadId": "thr_123",
    "sectionId": "01984de2-8f74-7c91-a3b2-5c5e937cf318",
    "beforeThreadId": "thr_456"
} }
{ "id": 28, "result": {} }

{ "method": "thread/section/move", "id": 29, "params": {
    "threadId": "thr_123",
    "sectionId": null,
    "beforeThreadId": null
} }
{ "id": 29, "result": {} }
```

実験的: `thread/memoryMode/set` を使って、スレッドが将来のメモリ生成に対して適格なままかどうかを変更する。

```json
{ "method": "thread/memoryMode/set", "id": 26, "params": {
    "threadId": "thr_123",
    "mode": "disabled"
} }
{ "id": 26, "result": {} }
```

実験的: `memory/reset` を使って、現在の Codex home のローカルメモリアーティファクトと sqlite バックのメモリステージデータをクリアする。これは既存のスレッドメモリモードを保持する。スレッドの将来のメモリ適格性を変更したい場合は、別途 `thread/memoryMode/set` を使う。

```json
{ "method": "memory/reset", "id": 27 }
{ "id": 27, "result": {} }
```

### Example: Set and update a thread goal

`thread/goal/set` を使って、マテリアライズされたスレッドの現在のゴールを作成または更新する。クライアントは、トークン予算が枯渇または枯渇に近づいて停止する場合は `budgetLimited` を、外部からの介入待ちで進行が止まっている場合は `blocked` を、使用量の可用性がそれ以上の作業を止める場合は `usageLimited` を設定できる。システムはまた、会計が設定されたトークン予算を超えた場合に `budgetLimited` を、ターンがハードな使用量制限エラーで終わった場合に `usageLimited` を設定する。

```json
{ "method": "thread/goal/set", "id": 27, "params": {
    "threadId": "thr_123",
    "objective": "Keep improving the benchmark until p95 latency is under 120ms",
    "tokenBudget": 200000
} }
{ "id": 27, "result": { "goal": {
    "threadId": "thr_123",
    "objective": "Keep improving the benchmark until p95 latency is under 120ms",
    "status": "active",
    "tokenBudget": 200000,
    "tokensUsed": 0,
    "timeUsedSeconds": 0,
    "createdAt": 1776272400,
    "updatedAt": 1776272400
} } }
{ "method": "thread/goal/updated", "params": { "threadId": "thr_123", "goal": {
    "threadId": "thr_123",
    "objective": "Keep improving the benchmark until p95 latency is under 120ms",
    "status": "active",
    "tokenBudget": 200000,
    "tokensUsed": 0,
    "timeUsedSeconds": 0,
    "createdAt": 1776272400,
    "updatedAt": 1776272400
} } }
```

```json
{ "method": "thread/goal/set", "id": 28, "params": {
    "threadId": "thr_123",
    "status": "blocked"
} }
{ "id": 28, "result": { "goal": {
    "threadId": "thr_123",
    "objective": "Keep improving the benchmark until p95 latency is under 120ms",
    "status": "blocked",
    "tokenBudget": 200000,
    "tokensUsed": 10000,
    "timeUsedSeconds": 60,
    "createdAt": 1776272400,
    "updatedAt": 1776272460
} } }
```

`thread/goal/get` を使って、変更せずに現在のゴールを読み取る。

```json
{ "method": "thread/goal/get", "id": 29, "params": { "threadId": "thr_123" } }
{ "id": 29, "result": { "goal": null } }
```

`thread/goal/clear` を使って、現在のゴールを除去する。

```json
{ "method": "thread/goal/clear", "id": 30, "params": { "threadId": "thr_123" } }
{ "id": 30, "result": { "cleared": true } }
{ "method": "thread/goal/cleared", "params": { "threadId": "thr_123" } }
```

### Example: Archive a thread

`thread/archive` を使って、永続化された rollout (ディスク上に JSONL ファイルとして保存されている) をアーカイブ済みセッションディレクトリに移動し、spawn された子孫スレッドの rollout の移動を試みる。

```json
{ "method": "thread/archive", "id": 21, "params": { "threadId": "thr_b" } }
{ "id": 21, "result": {} }
{ "method": "thread/archived", "params": { "threadId": "thr_b" } }
```

アーカイブされたスレッドは、`archived` が `true` に設定されない限り `thread/list` に現れない。

### Example: Delete a thread

`thread/delete` を使って、スレッドと spawn された子孫スレッドをハード削除する。リクエストが成功する前に、既存の rollout ファイルと関連メタデータを削除しなければならない。存在しない rollout ファイルは既に削除済みとして扱われる。

```json
{ "method": "thread/delete", "id": 23, "params": { "threadId": "thr_b" } }
{ "id": 23, "result": {} }
{ "method": "thread/deleted", "params": { "threadId": "thr_b" } }
```

### Example: Unarchive a thread

`thread/unarchive` を使って、アーカイブ済みの rollout を sessions ディレクトリに戻す。

```json
{ "method": "thread/unarchive", "id": 24, "params": { "threadId": "thr_b" } }
{ "id": 24, "result": { "thread": { "id": "thr_b" } } }
{ "method": "thread/unarchived", "params": { "threadId": "thr_b" } }
```

### Example: Trigger thread compaction

`thread/compact/start` を使って、スレッドの手動履歴圧縮をトリガーする。リクエストは即座に `{}` で返る。

進捗は、同じ `threadId` 上で標準の `turn/*` と `item/*` 通知として発行される。クライアントは単一の圧縮アイテムを期待すべきである:

- `item/started` with `item: { "type": "contextCompaction", ... }`
- 同じ `contextCompaction` アイテム id を伴う `item/completed`

圧縮が実行されている間、スレッドは実質的にターンの中にあるため、クライアントは通知に基づいて進捗 UI を表示すべきである。

```json
{ "method": "thread/compact/start", "id": 25, "params": { "threadId": "thr_b" } }
{ "id": 25, "result": {} }
```

### Example: Run a thread shell command

TUI の `!` ワークフロー向けに `thread/shellCommand` を使う。リクエストは即座に `{}` で返る。
この API はサンドボックスなしでフルアクセスで実行される。スレッドのサンドボックスポリシーは継承しない。

そのスレッドに既にアクティブなターンがある場合、コマンドはそのターンの補助アクションとして実行される。この場合、進捗は既存のターン上で標準の `item/*` 通知として発行され、フォーマット済み出力はそのターンのメッセージストリームに注入される:

- `item/started` with `item: { "type": "commandExecution", "source": "userShell", ... }`
- ゼロ個以上の `item/commandExecution/outputDelta`
- 同じ `commandExecution` アイテム id を伴う `item/completed`

そのスレッドにまだアクティブなターンがない場合、サーバーはシェルコマンドのためのスタンドアロンのターンを開始する。この場合、クライアントは以下を期待すべきである:

- `turn/started`
- `item/started` with `item: { "type": "commandExecution", "source": "userShell", ... }`
- ゼロ個以上の `item/commandExecution/outputDelta`
- 同じ `commandExecution` アイテム id を伴う `item/completed`
- `turn/completed`

```json
{ "method": "thread/shellCommand", "id": 26, "params": { "threadId": "thr_b", "command": "git status --short" } }
{ "id": 26, "result": {} }
```

### Example: Start a turn (send user input)

ターンは、ユーザー入力 (テキスト、画像、または音声) をスレッドに紐づけ、Codex の生成をトリガーする。`input` フィールドは判別可能なユニオンのリストである:

- `{"type":"text","text":"Explain this diff"}`
- `{"type":"image","url":"data:image/png;base64,…"}`
- `{"type":"localImage","path":"/tmp/screenshot.png"}`
- `{"type":"audio","url":"data:audio/wav;base64,…"}`
- `{"type":"localAudio","path":"/tmp/recording.mp3"}`

`image` バリアントはインラインの data URL を受け付ける。リモートの HTTP(S) 画像 URL は拒否される。代わりに data URL か `localImage` を使う。
`audio` バリアントは data URL を受け付ける。他の URL スキームは拒否される。`localAudio` はローカルの wav、mp3、m4a、webm、ogg ファイルを読み取り、Responses API リクエスト前に data URL に変換する。

新しいターンにオプションで設定の上書きを指定できる。指定した場合、それらの設定は同じスレッド上の後続のターンのデフォルトになる。`outputSchema` は現在のターンにのみ適用される。実験的な `environments` はターンスコープである: 省略するとスレッドの固定 (sticky) 環境を継承し、`[]` を渡すと環境なしでターンを実行し、明示的な環境 id を渡すとこのターンに限り sticky な選択を上書きできる。

`approvalsReviewer` は以下を受け付ける:

- `"user"` — デフォルト。承認リクエストをクライアントで直接レビューする。
- `"auto_review"` — 承認リクエストを、慎重にプロンプトされたサブエージェントにルーティングする。このサブエージェントは、承認または拒否の判断を下す前に、関連するコンテキストを収集し、リスクベースの決定フレームワークを適用する。レガシーな値 `"guardian_subagent"` は互換性のため引き続き受け付けられる。

```json
{ "method": "turn/start", "id": 30, "params": {
    "threadId": "thr_123",
    "clientUserMessageId": "client_msg_123",
    "input": [ { "type": "text", "text": "Run tests" } ],
    // Below are optional config overrides
    "cwd": "/Users/me/project",
    // Experimental: turn-scoped environment selection.
    "environments": [
        { "environmentId": "local", "cwd": "/Users/me/project" }
    ],
    "approvalPolicy": "unlessTrusted",
    "sandboxPolicy": {
        "type": "workspaceWrite",
        "writableRoots": ["/Users/me/project"],
        "networkAccess": true
    },
    // Prefer experimental profile selection:
    // "permissions": ":workspace"
    // Experimental runtime roots for :workspace_roots materialization:
    // "runtimeWorkspaceRoots": ["/Users/me/project", "/Users/me/openai"],
    // Do not send both "sandboxPolicy" and "permissions".
    "model": "gpt-5.1-codex",
    "effort": "medium",
    "summary": "concise",
    "personality": "friendly",
    // Optional JSON Schema to constrain the final assistant message for this turn.
    "outputSchema": {
        "type": "object",
        "properties": { "answer": { "type": "string" } },
        "required": ["answer"],
        "additionalProperties": false
    }
} }
{ "id": 30, "result": { "turn": {
    "id": "turn_456",
    "status": "inProgress",
    "items": [],
    "error": null
} } }
```

### Example: Start a turn (invoke a skill)

テキスト入力に `$<skill-name>` を含めることで、明示的にスキルを呼び出す。

```json
{ "method": "turn/start", "id": 33, "params": {
    "threadId": "thr_123",
    "input": [
        { "type": "text", "text": "$skill-creator Add a new skill for triaging flaky CI and include step-by-step usage." },
        { "type": "skill", "name": "skill-creator", "path": "/Users/me/.codex/skills/skill-creator/SKILL.md" }
    ]
} }
{ "id": 33, "result": { "turn": {
    "id": "turn_457",
    "status": "inProgress",
    "items": [],
    "error": null
} } }
```

### Example: Start a turn (invoke an app)

テキスト入力に `$<app-slug>` を含め、`app://<connector-id>` 形式のアプリ id を伴う `mention` 入力アイテムを追加することで、アプリを呼び出す。

```json
{ "method": "turn/start", "id": 34, "params": {
    "threadId": "thr_123",
    "input": [
        { "type": "text", "text": "$demo-app Summarize the latest updates." },
        { "type": "mention", "name": "Demo App", "path": "app://demo-app" }
    ]
} }
{ "id": 34, "result": { "turn": {
    "id": "turn_458",
    "status": "inProgress",
    "items": [],
    "error": null
} } }
```

### Example: Start a turn (invoke a plugin)

テキスト入力に `@sample` のような UI メンショントークンを含め、`plugin/installed` または `plugin/list` が返す正確な `plugin://<plugin-name>@<marketplace-name>` パスを伴う `mention` 入力アイテムを追加することで、プラグインを呼び出す。

```json
{ "method": "turn/start", "id": 35, "params": {
    "threadId": "thr_123",
    "input": [
        { "type": "text", "text": "@sample Summarize the latest updates." },
        { "type": "mention", "name": "Sample Plugin", "path": "plugin://sample@test" }
    ]
} }
{ "id": 35, "result": { "turn": {
    "id": "turn_459",
    "status": "inProgress",
    "items": [],
    "error": null
} } }
```

### Example: Inject raw history items

`thread/inject_items` を使って、ユーザーターンを開始せずに、あらかじめ組み立てた Responses API アイテムを、ロード済みスレッドのプロンプト履歴に追記する。これらのアイテムは rollout に永続化され、後続のモデルリクエストに含まれる。`input_image` アイテムはインラインの data URL を使わなければならない。リモートの HTTP(S) 画像 URL は拒否される。

```json
{ "method": "thread/inject_items", "id": 36, "params": {
    "threadId": "thr_123",
    "items": [
        {
            "type": "message",
            "role": "assistant",
            "content": [{ "type": "output_text", "text": "Previously computed context." }]
        }
    ]
} }
{ "id": 36, "result": {} }
```

### Example: Start realtime with WebRTC

ブラウザや webview が `RTCPeerConnection` を所有し、app-server がサーバー側のリアルタイムセッションを作成すべき場合は、`transport.type: "webrtc"` を伴う `thread/realtime/start` を使う。トランスポートの `sdp` は、手書きや最小限の SDP 文字列ではなく、`RTCPeerConnection.createOffer()` が生成したオファー SDP でなければならない。

オファーには、クライアントがネゴシエートしたいメディアセクションを含めるべきである。標準的なリアルタイム UI フローでは、`createOffer()` を呼ぶ前に音声トラック/トランシーバーと `oai-events` データチャンネルを作成する:

```javascript
const pc = new RTCPeerConnection();

audioElement.autoplay = true;
pc.ontrack = (event) => {
  audioElement.srcObject = event.streams[0];
};

const mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
pc.addTrack(mediaStream.getAudioTracks()[0], mediaStream);
pc.createDataChannel("oai-events");

const offer = await pc.createOffer();
await pc.setLocalDescription(offer);
```

そして `offer.sdp` を app-server に送る。Core は、バックエンド命令には `experimental_realtime_ws_backend_prompt` を、デフォルトの Realtime API セッション識別子にはスレッドの会話 id を使う。この `realtimeSessionId` 値は、Codex のセッション/スレッドグループ id ではなく、上流の Realtime API セッションを指す。開始レスポンスは `{}` であり、リモートのアンサー SDP は後で `thread/realtime/sdp` として届き、`setRemoteDescription()` に渡すべきである:

```json
{ "method": "thread/realtime/start", "id": 40, "params": {
    "threadId": "thr_123",
    "outputModality": "audio",
    "prompt": "You are on a call.",
    "realtimeSessionId": null,
    "transport": { "type": "webrtc", "sdp": "v=0\r\no=..." }
} }
{ "id": 40, "result": {} }
{ "method": "thread/realtime/sdp", "params": {
    "threadId": "thr_123",
    "sdp": "v=0\r\no=..."
} }
```

`prompt` を省略すると Codex のデフォルトのリアルタイムバックエンドプロンプトが使われる。そのデフォルトのバックエンドプロンプトなしでセッションを開始すべき場合は `prompt: null` または `prompt: ""` を送る。
クライアントは、スレッドやユーザー設定を変更せずに別のリアルタイムセッション設定を選ぶために、`thread/realtime/start` に `model` を渡すこともできる。
クライアントは、このセッションに限りリアルタイムプロトコルを選ぶために `version` を渡せる。WebRTC は AVAS を使い、レガシーの Bidi `"v1"` または Frameless Bidi `"v3"` をサポートする。Realtime Voice `"v2"` は WebRTC では拒否される。
`includeStartupContext: false` を渡すと、選択されたバックエンドプロンプトは使いつつ、このセッションについては Codex のスタートアップコンテキストをスキップする。
V3 では、ライブ入力が始まる前に、クライアントは完全なテキストメッセージでセッションをシードするために `initialItems` を渡せる:

```json
{
  "initialItems": [
    {
      "role": "developer",
      "text": "Relevant user memory: prefers concise technical answers."
    },
    {
      "role": "user",
      "text": "Continue from the prior discussion."
    }
  ]
}
```

各アイテムは `"user"`、`"developer"`、`"assistant"` のいずれかの `role` と、`text` 文字列を必要とする。Core はこれらを、初期セッションのブートストラップ (WebRTC のコール作成を含む) の間、Frameless Bidi の `session.initial_items` としてシリアライズする。
リクエストは、128 アイテム、アイテムあたり推定 8,192 テキストトークン、すべてのアイテムで合計推定 8,192 テキストトークンに制限される。
`initialItems` を省略するか空リストを渡すと、以前のセッションペイロードとスタートアップの挙動が保持される。V1 と V2 は空でない `initialItems` を拒否する。
`clientManagedHandoffs: true` を渡すと、自動的な Codex 応答のハンドオフとアイテムが抑制される。その場合クライアントは、`thread/realtime/appendText` または `thread/realtime/appendSpeech` でどの更新を配信するかを選べる。
`codexResponsesAsItems: true` を渡すと、プロトコルのデフォルトの speakable 出力パスの代わりに、自動的な Codex 応答が `conversation.item.create` で注入される。このモードを使う場合、`codexResponseItemPrefix` は各自動 Codex 応答アイテムに短い実験用命令を前置できる。`codexResponsesAsItems` を省略するか `false` を渡すと、デフォルトの speakable の挙動が保持される。V3 では、自動ハンドオフはデフォルトで `codexResponseHandoffMode: "thinking"` になり、これはすべての自動応答についてコンテキスト append の `channel` を省略する。`"commentary"` を渡すとすべての応答が commentary にルーティングされ、`"bemTags"` を渡すと BEM の commentary タグは `commentary` に、final タグは `speakable` に、analysis タグは `commentary` にルーティングされる。パース不能な BEM 出力は `speakable` にフォールバックする。BEM ルーティングは生のエンベロープを読み取り、フロントエンドモデルのために append されたテキスト内にそれを保持する。`"bemTags"` では、クライアントは個々のチャンネルの受理プレフィックスを上書きするために `codexResponseHandoffChannelPrefixes` を渡せる。例えば `{"analysis":["[THINKING]"],"commentary":["[PROGRESS]","[UPDATE]"],"final":["[DONE]"]}`。省略されたチャンネルは、ハードコードされた `[ANALYSIS]`、`[COMMENTARY]`、`[FINAL]` のデフォルトを保持する。この設定は V1 や V2 には影響しない。V3 のハンドオフは、レガシーな `"Agent Final Message"` ラベルを決して前置しない。古いクライアントは、削除された `codexResponseHandoffPrefix` フィールドを引き続き送るかもしれないが、サーバーは未知のリクエストフィールドを無視する。
アプリ提供のリアルタイムテキストアイテムを追加するには `thread/realtime/appendText` を、アプリがリアルタイムの更新を話すべきだと判断した場合は `thread/realtime/appendSpeech` を呼ぶ。

```javascript
await pc.setRemoteDescription({
  type: "answer",
  sdp: notification.params.sdp,
});
```

### Example: Interrupt an active turn

`turn/interrupt` で実行中のターンをキャンセルできる。

```json
{ "method": "turn/interrupt", "id": 31, "params": {
    "threadId": "thr_123",
    "turnId": "turn_456"
} }
{ "id": 31, "result": {} }
```

サーバーはアクティブなターンのキャンセルを要求し、その後 `status: "interrupted"` を伴う `turn/completed` イベントを発行する。これはバックグラウンドターミナルを終了させない。それらのシェルを明示的に停止したい場合は `thread/backgroundTerminals/clean` を使う。ターンの中断が完了したことを知るには `turn/completed` イベントに頼る。

### Example: Clean background terminals

スレッドに関連付けられたすべての実行中バックグラウンドターミナルを終了させるには `thread/backgroundTerminals/clean` を使う。このメソッドは実験的であり、`capabilities.experimentalApi = true` が必要である。

```json
{ "method": "thread/backgroundTerminals/clean", "id": 35, "params": {
    "threadId": "thr_123"
} }
{ "id": 35, "result": {} }
```

### Example: List and terminate background terminals

ロード済みスレッドに関連付けられた実行中のバックグラウンドターミナルを調べるには `thread/backgroundTerminals/list` を使う。`backgroundTerminals` セグメントは、意図的に既存の `thread/backgroundTerminals/clean` メソッドに続いている。返される `processId` は app-server のプロセス id であり、ホスト OS のメタデータは null 許容である。リクエストは標準の `cursor` と `limit` のページネーションフィールドを受け付ける。`nextCursor` が非 null のとき、それを `cursor` として渡して次のページを取得する。

```json
{ "method": "thread/backgroundTerminals/list", "id": 36, "params": { "threadId": "thr_123" } }
{ "id": 36, "result": { "data": [
    {
        "itemId": "item_456",
        "processId": "42",
        "command": "python3 -m http.server",
        "cwd": "/workspace",
        "osPid": null,
        "cpuPercent": null,
        "rssKb": null
    }
], "nextCursor": null } }
```

その `processId` によって 1 つの実行中バックグラウンドターミナルを終了させるには `thread/backgroundTerminals/terminate` を使う。

```json
{ "method": "thread/backgroundTerminals/terminate", "id": 37, "params": { "threadId": "thr_123", "processId": "42" } }
{ "id": 37, "result": { "terminated": true } }
```

### Example: Steer an active turn

現在アクティブな通常のターンに追加のユーザー入力を追記するには `turn/steer` を使う。これは `turn/started` を発行せず、スレッド設定の上書きを受け付けない。

```json
{ "method": "turn/steer", "id": 32, "params": {
    "threadId": "thr_123",
    "clientUserMessageId": "client_msg_124",
    "input": [ { "type": "text", "text": "Actually focus on failing tests first." } ],
    "expectedTurnId": "turn_456"
} }
{ "id": 32, "result": { "turnId": "turn_456" } }
```

`expectedTurnId` は必須である。アクティブなターンがない場合、`expectedTurnId` がアクティブなターンと一致しない場合、またはアクティブなターンの種類が同一ターン内でのステアリングを受け付けない場合 (例えば review や手動 compaction)、リクエストは `invalid request` エラーで失敗する。

### Example: Request a code review

現在チェックアウトされているプロジェクトに対して Codex のレビューアーを実行するには `review/start` を使う。リクエストは、スレッド id と、何がレビューされるべきかを記述する `target` を受け取る:

- `{"type":"uncommittedChanges"}` — ステージ済み、未ステージ、未追跡のファイル。
- `{"type":"baseBranch","branch":"main"}` — 指定されたブランチの upstream に対する diff (Codex が実行する正確な `git merge-base`/`git diff` の指示についてはプロンプトを参照)。
- `{"type":"commit","sha":"abc1234","title":"Optional subject"}` — 特定のコミットをレビューする。
- `{"type":"custom","instructions":"Free-form reviewer instructions"}` — レガシーな手動レビューリクエストと同等のフォールバックプロンプト。
- `delivery` (`"inline"` または `"detached"`、デフォルトは `"inline"`) — レビューがどこで実行されるか:
  - `"inline"`: 既存のスレッド上で新しいターンとしてレビューを実行する。レスポンスの `reviewThreadId` は元の `threadId` と等しく、新しい `thread/started` 通知は発行されない。
  - `"detached"`: 親会話から新しいレビュースレッドをフォークし、そこでレビューを実行する。レスポンスの `reviewThreadId` はこの新しいレビュースレッドの id であり、サーバーはレビューアイテムのストリーミング前にそれに対する `thread/started` 通知を発行する。

リクエスト/レスポンスの例:

```json
{ "method": "review/start", "id": 40, "params": {
    "threadId": "thr_123",
    "delivery": "inline",
    "target": { "type": "commit", "sha": "1234567deadbeef", "title": "Polish tui colors" }
} }
{ "id": 40, "result": {
    "turn": {
        "id": "turn_900",
        "status": "inProgress",
        "items": [
            { "type": "userMessage", "id": "turn_900", "content": [ { "type": "text", "text": "Review commit 1234567: Polish tui colors" } ] }
        ],
        "error": null
    },
    "reviewThreadId": "thr_123"
} }
```

分離 (detached) レビューには `"delivery": "detached"` を使う。レスポンスは同じ形だが、`reviewThreadId` は (元の `threadId` とは異なる) 新しいレビュースレッドの id になる。サーバーは、レビューターンのストリーミング前に、その新しいスレッドに対する `thread/started` 通知も発行する。内部的には、これは通常のフォークされたスレッドとターンであり、そのプロンプトは同梱の `$review-agent` スキルに言及するため、通常のターンステアリング、ツール、パーミッション、アイテムストリームの挙動が適用される。

親スレッドがページング対応の場合、分離レビューはサポートされない。

インラインレビューでは、Codex は通常の `turn/started` 通知に続いて、進捗を表示できるよう `enteredReviewMode` アイテムを伴う `item/started` をストリーミングする:

```json
{
  "method": "item/started",
  "params": {
    "item": {
      "type": "enteredReviewMode",
      "id": "turn_900",
      "review": "current changes"
    }
  }
}
```

レビューアーが終了すると、サーバーは最終的なレビューテキストを含む `exitedReviewMode` アイテムを伴う `item/started` と `item/completed` を発行する:

```json
{
  "method": "item/completed",
  "params": {
    "item": {
      "type": "exitedReviewMode",
      "id": "turn_900",
      "review": "Looks solid overall...\n\n- Prefer Stylize helpers — app.rs:10-20\n  ..."
    }
  }
}
```

`review` 文字列はプレーンテキストであり、全体の説明と、構造化された各所見に対する箇条書きリスト (生成されたスキーマの `ThreadItem::ExitedReviewMode` に一致) を既にまとめている。この通知を使って、クライアントでレビューアー出力を描画する。

### Example: One-off command execution

スレッドやターンを作成せずに、サーバーのサンドボックス内でスタンドアロンのコマンド (argv ベクター) を実行する:

```json
{ "method": "command/exec", "id": 32, "params": {
    "command": ["ls", "-la"],
    "processId": "ls-1",                           // optional string; required for streaming and ability to terminate the process
    "cwd": "/Users/me/project",                    // optional; defaults to server cwd
    "env": { "FOO": "override" },                  // optional; merges into the server env and overrides matching names
    "size": { "rows": 40, "cols": 120 },           // optional; PTY size in character cells, only valid with tty=true
    "permissionProfile": ":workspace",             // optional profile id; defaults to user config
    "outputBytesCap": 1048576,                     // optional; per-stream capture cap
    "disableOutputCap": false,                     // optional; cannot be combined with outputBytesCap
    "timeoutMs": 10000,                            // optional; ms timeout; defaults to server timeout
    "disableTimeout": false                        // optional; cannot be combined with timeoutMs
} }
{ "id": 32, "result": {
    "exitCode": 0,
    "stdout": "...",
    "stderr": ""
} }
```

- 明示的にサンドボックスなしのプロセス実行 API を、即座の spawn 確認・ハンドルベースの制御・出力通知・終了通知とともに使いたい場合は `process/spawn` を優先する。
- 既に外部でサンドボックス化されているクライアントについては、レガシーな `sandboxPolicy` を `{"type":"externalSandbox","networkAccess":"enabled"}` に設定する (`networkAccess` を省略すると制限されたままになる)。Codex はこのモードで独自のサンドボックスを強制しない。モデルにはフルなファイルシステムアクセス権があると伝え、`environment_context` を通じて `networkAccess` の状態を渡す。

補足:

- 空の `command` 配列は拒否される。
- コマンドのパーミッション上書きには `permissionProfile` を優先する。これは、低レベルのファイルシステム/ネットワークパーミッションを受け付けるのではなく、id (例えば `:read-only`、`:workspace`、ユーザー定義の `[permissions.<id>]` プロファイル) によってアクティブなプロファイルを選択する。レガシーな `sandboxPolicy` フィールドは `turn/start` と同じ形 (例えば `dangerFullAccess`、`readOnly`、フラグ付きの `workspaceWrite`、`networkAccess` が `restricted|enabled` の `externalSandbox`) を受け付けるが、`permissionProfile` と組み合わせることはできない。
- `env` は、サーバーのシェル環境ポリシーが生成する環境にマージされる。一致する名前は上書きされ、指定されていない変数はそのまま残る。
- 省略された場合、`timeoutMs` はサーバーのデフォルトにフォールバックする。
- 省略された場合、`outputBytesCap` はサーバーのデフォルトであるストリームあたり 1 MiB にフォールバックする。
- `disableOutputCap: true` は、その `command/exec` リクエストについて stdout/stderr のキャプチャの切り詰めを無効化する。`outputBytesCap` と組み合わせることはできない。
- `disableTimeout: true` は、その `command/exec` リクエストについてタイムアウトを完全に無効化する。`timeoutMs` と組み合わせることはできない。
- `processId` はバッファ実行についてはオプションである。省略すると、Codex はライフサイクル追跡のために内部的な id を生成するが、`tty`、`streamStdin`、`streamStdoutStderr` は無効のままでなければならず、そのコマンドについて後続の `command/exec/write` / `command/exec/terminate` 呼び出しは利用できない。
- `size` は `tty: true` の場合にのみ有効である。文字セルでの初期 PTY サイズを設定する。
- バッファ形式の Windows サンドボックス実行は相関のために `processId` を受け付けるが、それらのリクエストについて `command/exec/write` と `command/exec/terminate` は依然として未サポートである。
- バッファ形式の Windows サンドボックス実行はまた、デフォルトの出力キャップを必要とする。カスタムの `outputBytesCap` と `disableOutputCap` はそこではサポートされない。
- `tty`、`streamStdin`、`streamStdoutStderr` はオプションのブール値である。これらを省略するレガシーなリクエストは、引き続きバッファ実行を使う。
- `tty: true` は PTY モードに加えて `streamStdin: true` と `streamStdoutStderr: true` を暗示する。
- `tty` と `streamStdin` はそれ自体ではタイムアウトを無効化しない。サーバーのデフォルトタイムアウトを使うには `timeoutMs` を省略するか、プロセスが終了するか明示的に終了させられるまで生存させ続けるには `disableTimeout: true` を設定する。
- `outputBytesCap` は `stdout` と `stderr` に独立して適用され、ストリーミングされたバイトは最終レスポンスに重複して含まれない。
- `command/exec` のレスポンスは、プロセスが終了するまで遅延され、その接続に対するそのプロセスの `command/exec/outputDelta` 通知がすべて発行された後にのみ送信される。
- `command/exec/outputDelta` 通知は接続スコープである。発信元の接続が閉じられると、サーバーはプロセスを終了させる。

PTY セッションが任意のバイトを運べるよう、stdin/stdout のストリーミングには base64 が使われる:

```json
{ "method": "command/exec", "id": 33, "params": {
    "command": ["bash", "-i"],
    "processId": "bash-1",
    "tty": true,
    "outputBytesCap": 32768
} }
{ "method": "command/exec/outputDelta", "params": {
    "processId": "bash-1",
    "stream": "stdout",
    "deltaBase64": "YmFzaC00LjQkIA==",
    "capReached": false
} }
{ "method": "command/exec/write", "id": 34, "params": {
    "processId": "bash-1",
    "deltaBase64": "cHdkCg=="
} }
{ "id": 34, "result": {} }
{ "method": "command/exec/write", "id": 35, "params": {
    "processId": "bash-1",
    "closeStdin": true
} }
{ "id": 35, "result": {} }
{ "method": "command/exec/resize", "id": 36, "params": {
    "processId": "bash-1",
    "size": { "rows": 48, "cols": 160 }
} }
{ "id": 36, "result": {} }
{ "method": "command/exec/terminate", "id": 37, "params": {
    "processId": "bash-1"
} }
{ "id": 37, "result": {} }
{ "id": 33, "result": {
    "exitCode": 137,
    "stdout": "",
    "stderr": ""
} }
```

- `command/exec/write` は `deltaBase64`、`closeStdin`、またはその両方を受け付ける。
- クライアントは `command/exec` に接続スコープの文字列 `processId` を指定できる。`command/exec/write`、`command/exec/resize`、`command/exec/terminate` は、そのクライアント指定の文字列 id のみを受け付ける。
- `command/exec/outputDelta.processId` は常に、元の `command/exec` リクエストからのクライアント指定の文字列 id である。
- `command/exec/outputDelta.stream` は `stdout` または `stderr` である。PTY モードはターミナル出力を `stdout` に多重化する。
- `command/exec/outputDelta.capReached` は、`outputBytesCap` がそのストリームを切り詰めた場合、ストリームの最終ストリーミングチャンクで `true` になる。そのストリームの以降の出力は破棄される。
- `command/exec.params.env` は、サーバーが計算した環境をキーごとに上書きする。継承された変数を解除するには、キーを `null` に設定する。
- `command/exec/resize` は PTY バックの `command/exec` セッションに対してのみサポートされる。

### Example: Process lifecycle execution

app server が動いているホスト上で、Codex サンドボックスなしにスタンドアロンの argv ベースのプロセスを起動するには `process/spawn` を使う。`process/*` API は実験的であり、`initialize.params.capabilities.experimentalApi: true` が必要である。spawn のレスポンスは、プロセスが起動し `processHandle` が登録されたことを意味する。完了は後で `process/exited` によって報告される。

```json
{ "method": "process/spawn", "id": 40, "params": {
    "command": ["cargo", "check"],
    "processHandle": "cargo-check-1",
    "cwd": "/Users/me/project",                    // required absolute path
    "env": { "RUST_LOG": null },                    // optional; override or unset app-server env vars
    "outputBytesCap": 1048576,                     // optional; omit for default, null disables
    "timeoutMs": 10000                             // optional; omit for default, null disables
} }
{ "id": 40, "result": {} }
{ "method": "process/exited", "params": {
    "processHandle": "cargo-check-1",
    "exitCode": 0,
    "stdout": "...",
    "stdoutCapReached": false,
    "stderr": "",
    "stderrCapReached": false
} }
```

対話的またはストリーミングのプロセスについては、`tty: true` または `streamStdoutStderr: true` を設定し、`processHandle` によって出力通知をルーティングする:

```json
{ "method": "process/spawn", "id": 41, "params": {
    "command": ["bash", "-i"],
    "processHandle": "bash-1",
    "cwd": "/Users/me/project",
    "tty": true,
    "size": { "rows": 40, "cols": 120 },
    "outputBytesCap": null,
    "timeoutMs": null
} }
{ "id": 41, "result": {} }
{ "method": "process/outputDelta", "params": {
    "processHandle": "bash-1",
    "stream": "stdout",
    "deltaBase64": "YmFzaC00LjQkIA==",
    "capReached": false
} }
{ "method": "process/writeStdin", "id": 42, "params": {
    "processHandle": "bash-1",
    "deltaBase64": "cHdkCg=="
} }
{ "id": 42, "result": {} }
{ "method": "process/resizePty", "id": 43, "params": {
    "processHandle": "bash-1",
    "size": { "rows": 48, "cols": 160 }
} }
{ "id": 43, "result": {} }
{ "method": "process/kill", "id": 44, "params": {
    "processHandle": "bash-1"
} }
{ "id": 44, "result": {} }
{ "method": "process/exited", "params": {
    "processHandle": "bash-1",
    "exitCode": 137,
    "stdout": "",
    "stdoutCapReached": false,
    "stderr": "",
    "stderrCapReached": false
} }
```

- 空の `command` 配列と空の `processHandle` 文字列は拒否される。
- `cwd` は必須であり、絶対パスでなければならない。
- `process/spawn` は意図的にサンドボックスなしであり、`sandboxPolicy` や `permissionProfile` のようなサンドボックス選択フィールドを定義しない。
- 重複するアクティブな `processHandle` の値は同じ接続上で拒否される。同じハンドルは、以前のプロセスが終了した後に再利用できる。
- `tty: true` は PTY モードに加えて `streamStdin: true` と `streamStdoutStderr: true` を暗示する。
- `process/writeStdin` は `deltaBase64`、`closeStdin`、またはその両方を受け付ける。
- 省略された場合、`timeoutMs` と `outputBytesCap` はサーバーのデフォルトにフォールバックする。ターミナル形式のセッションでその制限を無効化するには、いずれかのフィールドを `null` に設定する。
- `outputBytesCap` は `stdout` と `stderr` に独立して適用される。`process/exited.stdoutCapReached` と `stderrCapReached` は、各ストリームがキャップに達したかどうかを報告する。ストリーミングされたバイトは `process/exited` に重複して含まれない。
- `process/outputDelta` と `process/exited` の通知は接続スコープである。発信元の接続が閉じられると、サーバーはプロセスを終了させる。

### Example: Filesystem utilities

これらのメソッドは、ホストファイルシステム上の絶対パスに対して動作し、読み取り、書き込み、ディレクトリ走査、コピー、削除、変更通知をカバーする。

このセクションのすべてのファイルシステムパスは絶対パスでなければならない。

```json
{ "method": "fs/createDirectory", "id": 40, "params": {
    "path": "/tmp/example/nested",
    "recursive": true
} }
{ "id": 40, "result": {} }
{ "method": "fs/writeFile", "id": 41, "params": {
    "path": "/tmp/example/nested/note.txt",
    "dataBase64": "aGVsbG8="
} }
{ "id": 41, "result": {} }
{ "method": "fs/getMetadata", "id": 42, "params": {
    "path": "/tmp/example/nested/note.txt"
} }
{ "id": 42, "result": {
    "isDirectory": false,
    "isFile": true,
    "isSymlink": false,
    "createdAtMs": 1730910000000,
    "modifiedAtMs": 1730910000000
} }
{ "method": "fs/readFile", "id": 43, "params": {
    "path": "/tmp/example/nested/note.txt"
} }
{ "id": 43, "result": {
    "dataBase64": "aGVsbG8="
} }
```

- `fs/getMetadata` は、パスがディレクトリまたは通常のファイルのどちらに解決されるか、そのパス自体がシンボリックリンクかどうか、そして Unix ミリ秒での `createdAtMs` と `modifiedAtMs` を返す。現在のプラットフォームでタイムスタンプが利用不可の場合、そのフィールドは `0` になる。
- `fs/createDirectory` は、省略された場合 `recursive` をデフォルトで `true` にする。
- `fs/remove` は、省略された場合 `recursive` と `force` の両方をデフォルトで `true` にする。
- `fs/readFile` は常に `dataBase64` 経由で base64 バイトを返し、`fs/writeFile` は常に `dataBase64` に base64 バイトを期待する。
- `fs/copy` は、ファイルコピーとディレクトリツリーコピーの両方を扱う。`sourcePath` がディレクトリの場合は `recursive: true` を必要とする。再帰的なコピーは、通常のファイル、ディレクトリ、シンボリックリンクを走査する。他のエントリタイプはスキップされる。

### Example: Filesystem watch

`fs/watch` は絶対ファイルまたはディレクトリパスを受け付ける。ファイルを監視すると、置換またはリネーム操作経由で配信される更新を含め、そのファイルパスについて `fs/changed` が発行される。

```json
{ "method": "fs/watch", "id": 44, "params": {
    "watchId": "0195ec6b-1d6f-7c2e-8c7a-56f2c4a8b9d1",
    "path": "/Users/me/project/.git/HEAD"
} }
{ "id": 44, "result": {
    "path": "/Users/me/project/.git/HEAD"
} }
{ "method": "fs/changed", "params": {
    "watchId": "0195ec6b-1d6f-7c2e-8c7a-56f2c4a8b9d1",
    "changedPaths": ["/Users/me/project/.git/HEAD"]
} }
{ "method": "fs/unwatch", "id": 45, "params": {
    "watchId": "0195ec6b-1d6f-7c2e-8c7a-56f2c4a8b9d1"
} }
{ "id": 45, "result": {} }
```

## Events

イベント通知は、スレッドのライフサイクル、ターンのライフサイクル、およびそれらの中のアイテムに対する、サーバー起点のイベントストリームである。スレッドを開始または再開した後は、stdout を読み続けて `thread/started`、`thread/archived`、`thread/unarchived`、`thread/closed`、`turn/*`、`item/*` の通知を受け取る。

スレッドのリアルタイムは、別個のスレッドスコープの通知サーフェスを使う。`thread/realtime/*` 通知は一時的なトランスポートイベントであり、`ThreadItem` ではない。`thread/read`、`thread/resume`、`thread/fork` からは返されない。

復旧可能な設定・初期化の警告は、既存の `configWarning` 通知を使う: `{ summary, details?, path?, range? }`。app-server は、設定パースと関連するセットアップの診断について初期化中に、あるいはそのスレッドの exec-policy ルールのパースが失敗した際にリクエスト元の接続に、これを発行することがある。

汎用のランタイム警告は `warning` 通知を使う: `{ threadId?, message }`。app-server は、有効なスキルの一部がセッションのモデル可視スキルリストに含まれない場合を含め、コアのイベントストリームからの致命的でない警告についてこれを発行する。

### Notification opt-out

クライアントは、`initialize.params.capabilities.optOutNotificationMethods` に正確なメソッド名を送ることで、接続ごとに特定の通知を抑制できる。

- 完全一致のみ: `item/agentMessage/delta` はそのメソッドのみを抑制する。
- 未知のメソッド名は無視される。
- `thread/*`、`turn/*`、`item/*`、`rawResponseItem/*` のような、app-server の型付き通知に適用される。
- リクエスト/レスポンス/エラーには適用されない。

例:

- スレッドライフサイクル通知をオプトアウト: `thread/started`
- ストリーミングされるエージェントテキストのデルタをオプトアウト: `item/agentMessage/delta`

### Fuzzy file search events (experimental)

あいまいファイル検索セッション API は、クエリごとの通知を発行する:

- `fuzzyFileSearch/sessionUpdated` — アクティブなクエリに対する現在のマッチファイルを伴う `{ sessionId, query, files }`。
- `fuzzyFileSearch/sessionCompleted` — そのクエリのインデックス作成/マッチングが完了すると発行される `{ sessionId, query }`。

### Thread realtime events (experimental)

スレッドのリアルタイム API は、セッションのライフサイクルとストリーミングメディアについて、スレッドスコープの通知を発行する:

- `thread/realtime/started` — スレッドについてリアルタイムが開始すると一度発行される `{ threadId, realtimeSessionId }` (実験的)。`realtimeSessionId` は、Codex のセッション/スレッドグループ id ではなく、上流の Realtime API セッション識別子である。
- `thread/realtime/itemAdded` — 専用の型付き app-server 通知を持たない生の非音声リアルタイムアイテム (`handoff_request` を含む) 向けの `{ threadId, item }` (実験的)。`item` は、上流の websocket アイテムスキーマがまだ不安定である間、生の JSON として転送される。
- `thread/realtime/transcript/delta` — ライブのリアルタイム文字起こしのデルタ向けの `{ threadId, role, delta }` (実験的)。
- `thread/realtime/transcript/done` — リアルタイムが文字起こしパートの最終的な完全テキストを発行すると届く `{ threadId, role, text }` (実験的)。
- `thread/realtime/outputAudio/delta` — ストリーミングされる出力音声チャンク向けの `{ threadId, audio }` (実験的)。`audio` は camelCase フィールド (`data`、`sampleRate`、`numChannels`、`samplesPerChannel`) を使う。
- `thread/realtime/error` — リアルタイムがトランスポートまたはバックエンドのエラーに遭遇すると届く `{ threadId, message }` (実験的)。
- `thread/realtime/closed` — リアルタイムトランスポートが閉じられると届く `{ threadId, reason }` (実験的)。

音声は意図的に `ThreadItem` とは分離されているため、クライアントは `optOutNotificationMethods` で `thread/realtime/outputAudio/delta` だけを独立してオプトアウトできる。

### Windows sandbox setup events

- `windowsSandbox/setupCompleted` — `windowsSandbox/setupStart` リクエストが完了した後の `{ mode, success, error }`。

### MCP server startup events

- `mcpServer/startupStatus/updated` — app-server が MCP サーバー起動の遷移を観測すると届く `{ threadId, name, status, error, failureReason }`。`threadId` は、起動がスレッドスコープの場合は所有スレッドを識別し、起動がアプリスコープの場合は `null` になる。`status` は `starting`、`ready`、`failed`、`cancelled` のいずれかである。`error` と `failureReason` は `failed` 以外では `null` になる。保存されている OAuth 資格情報が期限切れでリフレッシュできない場合、`failureReason` は `reauthenticationRequired` になり、クライアントはユーザーに名前付きサーバーへの再接続を促せる。

### Turn events

app-server は、ターンが実行されている間、JSON-RPC 通知をストリーミングする。各ターンは、実行が始まると `turn/started` を発行し、`turn/completed` (最終的な `turn` ステータス) で終わる。トークン使用量イベントは `thread/tokenUsage/updated` 経由で別途ストリーミングされる。クライアントは関心のあるイベントを購読し、更新が届くたびに各アイテムをインクリメンタルに描画する。アイテムごとのライフサイクルは常に次の通りである: `item/started` → ゼロ個以上のアイテム固有のデルタ → `item/completed`。

- `turn/started` — ターン id、空の `items`、`status: "inProgress"` を伴う `{ turn }`。
- `turn/completed` — `turn.status` が `completed`、`interrupted`、`failed` のいずれかである `{ turn }`。成功したターンは利用可能な場合最終的なエージェントメッセージを含み、失敗は `{ error: { message, codexErrorInfo?, additionalDetails? } }` を伴う。
- `turn/diff/updated` — ターンレベルの統一 diff の最新スナップショットを表す `{ threadId, turnId, diff }`。すべての FileChange アイテムの後に発行される。`diff` は、そのターン内のすべてのファイル変更にわたる最新の集約された統一 diff である。UI は、個々の `fileChange` アイテムを結合することなく、完全な「何が変わったか」ビューを表示するためにこれを描画できる。
- `turn/plan/updated` — エージェントがプランを共有または変更するたびに届く `{ turnId, explanation?, plan }`。各 `plan` エントリは `{ step, status }` で、`status` は `pending`、`inProgress`、`completed` のいずれかである。
- `rawResponse/completed` — 内部専用。`thread/start.experimentalRawEvents` が有効な場合、上流の Responses API の完了ごとに一度 `{ threadId, turnId, responseId, usage }` を発行する。`usage` は、app-server のトークン内訳の形にマッピングされた正確な上流の使用量ペイロードであり、上流の完了が使用量を省略した場合は `null` になる。`thread/tokenUsage/updated` と異なり、この通知は累積・推定・永続化・リプレイされない。
- `model/safetyBuffering/updated` — レスポンスが safety buffering に入ると届く `{ threadId, turnId, model, useCases, reasons, showBufferingUi, fasterModel }`。`fasterModel` は null 許容である。この通知は一時的であり、rollout 履歴には永続化されない。
- `model/rerouted` — バックエンドがリクエストを別のモデルに再ルーティングすると届く `{ threadId, turnId, fromModel, toModel, reason }` (例えば、高リスクなサイバーセーフティチェックによる)。
- `model/verification` — バックエンドが追加のアカウント検証 (`trustedAccessForCyber` など) をフラグすると届く `{ threadId, turnId, verifications }`。
- `turn/moderationMetadata` — 実験的。ファーストパーティのバックエンドがターンスコープのモデレーションメタデータをクライアント側の表示用に提供すると届く `{ threadId, turnId, metadata }`。

`turn/started` はアイテムを運ばない。`turn/completed` はフォールバックのサマリーとして最終的なエージェントメッセージのみを運ぶ。完全な正規のアイテムリストについては、引き続き `item/*` 通知を消費すること。

#### Items

`ThreadItem` は、ターンのレスポンスと `item/*` 通知の中で運ばれるタグ付きユニオンである。現在、以下のアイテムに対するイベントをサポートしている:

- `userMessage` — `clientId` が `turn/start` または `turn/steer` に指定されたオプションの `clientUserMessageId` であり、`content` がユーザー入力 (`text`、`image`、`localImage`、`audio`、`localAudio`) のリストである `{id, clientId, content}`。
- `agentMessage` — 蓄積されたエージェントの返信を含む `{id, text}`。
- `plan` — plan モードのターンに対して発行される `{id, text}`。plan テキストは `item/plan/delta` (実験的) でストリーミングされ得る。
- `reasoning` — `summary` がストリーミングされる推論サマリーを保持し (ほとんどの OpenAI モデルに該当)、`content` が生の推論ブロックを保持する (例えばオープンソースモデルに該当) `{id, summary, content}`。
- `commandExecution` — サンドボックス化されたコマンド向けの `{id, pluginId?, scriptPath?, command, cwd, status, commandActions, aggregatedOutput?, exitCode?, durationMs?}`。`pluginId` は信頼済みのファーストパーティプラグインに帰属するコマンドについてのみ存在する。新しく帰属されたアイテムは、信頼済みプラグインルートからの相対的な安全な `/` 区切りパスとして `scriptPath` も含み、古い履歴は `scriptPath` を省略することがある。`status` は `inProgress`、`completed`、`failed`、`declined` のいずれかである。
  `cwd` と読み取り用の `commandActions[].path` は、app-server が異なる OS で動いている場合でも、実行者のネイティブパス規約を使う。例えば、Linux 上で動いている app-server が、Windows のエグゼキューターについて `C:\repo\src\main.rs` を返すことがある。クライアントはそのパスを app-server にとってのローカルパスと解釈してはならない。
- `fileChange` — `{id, changes, status}` は提案された編集を記述する。`changes` は `{path, kind, diff}` を一覧し、`status` は `inProgress`、`completed`、`failed`、`declined` のいずれかである。
- `mcpToolCall` — MCP 呼び出しを記述する `{id, server, tool, status, arguments, appContext, mcpAppResourceUri?, pluginId, readOnlyHint, result?, error?}`。`appContext` は、信頼済みの MCP アプリ経由の呼び出しについては `{connectorId, linkId, resourceUri, appName, actionName}` であり、`connectorId` はそのツールを所有するコネクタを識別し、`linkId` はアプリリンクを識別し、`resourceUri` はウィジェットテンプレートを指し、`appName` はコネクタの表示名、`actionName` は安定したコネクタの `Action.name` である。`readOnlyHint` は、読み取り専用のツールについては `true`、書き込み可能なツールについては `false`、古い rollout エントリを含みそのアノテーションが利用不可な場合は `null` である。このヒントはツールの能力を記述するものであり、呼び出しが成功したか書き込みを実行したかを示すものではない。実行結果を判定するには `status`、`result`、`error` を使う。`appName` と `actionName` は古い rollout エントリでは null になり得る。トップレベルの `mcpAppResourceUri` は非推奨であり、クライアント移行のため一時的に重複している。`tool` は生の MCP ツールを識別する。`status` は `inProgress`、`completed`、`failed` のいずれかである。
- `collabToolCall` — collab ツール呼び出し (`spawn_agent`、`send_input`、`resume_agent`、`wait`、`close_agent`) を記述する `{id, tool, status, senderThreadId, receiverThreadId?, newThreadId?, prompt?, agentStatus?}`。`status` は `inProgress`、`completed`、`failed` のいずれかである。
- `webSearch` — エージェントが発行した web 検索リクエスト向けの `{id, query, action?, results?}`。`action` は Responses API の web_search アクションペイロード (`search`、`open_page`、`find_in_page`) を反映し、完了まで省略され得る。スタンドアロンの web 検索については、`results` は `/v1/alpha/search` が返す帯域外の構造化された結果 DTO を含む。クライアントは理解できない結果タイプやフィールドを無視すべきである。
- `imageView` — エージェントが画像ビューアーツールを呼び出すと発行される `{id, path}`。
- `sleep` — エージェントが一定時間または新しい入力を待つ間に発行される `{id, durationMs}`。
- `enteredReviewMode` — レビューアーが開始すると送られる `{id, review}`。`review` は `"current changes"` のような短いユーザー向けラベルか、要求されたターゲットの説明である。
- `exitedReviewMode` — レビューアーが終了すると発行される `{id, review}`。`review` は (通常、全体の所感と箇条書きの所見を伴う) レビュー全文である。
- `contextCompaction` — codex が会話履歴を圧縮すると発行される `{id}`。これは自動的に起こりうる。
- `compacted` - codex が会話履歴を圧縮すると届く `{threadId, turnId}`。これは自動的に起こりうる。**非推奨:** 代わりに `contextCompaction` を使うこと。

すべてのアイテムは共通のライフサイクルイベントを発行する:

- `item/started` — UI が即座に描画できるよう、新しい作業単位が始まると完全な `item` を発行する。このペイロードの `item.id` は、デルタで使われる `itemId` と一致する。
- `item/completed` — その作業自体が終わると (例えば、ツール呼び出しやメッセージが完了した後) 最終的な `item` を送る。これを権威ある実行/結果状態として扱う。
- `item/autoApprovalReview/started` — [不安定] パーミッション自動レビューが始まると `{threadId, turnId, targetItemId, review, action}` を運ぶ一時的な自動レビュー通知。この形はまもなく変わると予想される。
- `item/autoApprovalReview/completed` — [不安定] パーミッション自動レビューが解決すると `{threadId, turnId, targetItemId, review, action}` を運ぶ一時的な自動レビュー通知。この形はまもなく変わると予想される。

`review` は [不安定] であり、現在 `{status, riskLevel?, userAuthorization?, rationale?}` を持つ。`status` は `inProgress`、`approved`、`denied`、`aborted` のいずれかである。`riskLevel` は存在する場合 `"low"`、`"medium"`、`"high"`、`"critical"` のいずれかである。`userAuthorization` は存在する場合 `"unknown"`、`"low"`、`"medium"`、`"high"` のいずれかである。`action` は `type: "command" | "execve" | "applyPatch" | "networkAccess" | "mcpToolCall"` を伴うタグ付きユニオンである。コマンド系のアクションは `source` 判別子 (`"shell"` または `"unifiedExec"`) を含む。これらの通知は、対象アイテム自身の `item/completed` ライフサイクルとは別であり、自動レビューアプリのプロトコルがまだ設計中である間、意図的に一時的なものである。

さらに、アイテム固有のイベントがある:

#### agentMessage

- `item/agentMessage/delta` — エージェントメッセージのストリーミングされるテキストを追記する。同じ `itemId` について `delta` の値を順番に連結すると、完全な返信を再構築できる。

#### plan

- `item/plan/delta` — plan アイテムの提案されたプラン内容をストリーミングする (実験的)。同じ plan `itemId` について `delta` の値を連結する。これらのデルタは `<proposed_plan>` ブロックに対応する。

#### reasoning

- `item/reasoning/summaryTextDelta` — 読みやすい推論サマリーをストリーミングする。新しいサマリーセクションが開くと `summaryIndex` が増分する。
- `item/reasoning/summaryPartAdded` — `itemId` について推論サマリーセクション間の境界をマークする。以降の `summaryTextDelta` エントリは同じ `summaryIndex` を共有する。
- `item/reasoning/textDelta` — 生の推論テキストをストリーミングする (例えばオープンソースモデルにのみ該当)。UI に表示する前に一緒に属するデルタをグループ化するには `contentIndex` を使う。

#### commandExecution

- `item/commandExecution/outputDelta` — コマンドの stdout/stderr をストリーミングする。最終アイテムの `aggregatedOutput` と並んでライブ出力を描画するには、デルタを順番に追記する。
  最終的な `commandExecution` アイテムは、パース済みの `commandActions`、`status`、`exitCode`、`durationMs` を含むため、UI は何が実行され成功したかどうかを要約できる。

#### fileChange

- `item/fileChange/patchUpdated` - `features.apply_patch_streaming_events` が有効な場合、モデルが生成したパッチが実行される前に、そこからパースされた構造化されたファイル変更のスナップショットをストリーミングする。
- `item/fileChange/outputDelta` - `apply_patch` のテキスト出力用の非推奨のレガシープロトコルエントリ。互換性のため保持されているが、サーバーはもはや発行しない。

### Errors

`error` イベントは、サーバーがターンの途中でエラーに遭遇するたびに発行される (例えば上流のモデルエラーやクォータ制限)。`turn.status: "failed"` と同じ `{ error: { message, codexErrorInfo?, additionalDetails? } }` ペイロードを運び、その終端通知に先行することがある。

`codexErrorInfo` は `CodexErrorInfo` enum にマッピングされる。一般的な値:

- `ContextWindowExceeded`
- `SessionBudgetExceeded`
- `UsageLimitExceeded`
- `HttpConnectionFailed { httpStatusCode? }`: 4xx/5xx を含む上流の HTTP 失敗
- `ResponseStreamConnectionFailed { httpStatusCode? }`: レスポンス SSE ストリームへの接続失敗
- `ResponseStreamDisconnected { httpStatusCode? }`: ターンの完了前、途中でのレスポンス SSE ストリームの切断
- `ResponseTooManyFailedAttempts { httpStatusCode? }`
- `ActiveTurnNotSteerable { turnKind }`: 現在アクティブなターンがステア不可能な間 (例えば `/review` や手動 `/compact`) に `turn/start` や `turn/steer` が送信された
- `BadRequest`
- `Unauthorized`
- `SandboxError`
- `InternalServerError`
- `Other`: 未分類のすべてのエラー

上流の HTTP ステータスが利用可能な場合 (例えば Responses API やプロバイダーから)、それは該当する `codexErrorInfo` バリアントの `httpStatusCode` として転送される。

## Approvals

特定のアクション (シェルコマンドやファイルの変更) は、ユーザーの設定によっては明示的なユーザー承認を必要とすることがある。`turn/start` が使われる場合、app-server はサーバー起点の JSON-RPC リクエストをクライアントに送ることで承認フローを進める。クライアントは、Codex に進めるべきかどうかを伝えるために応答しなければならない。UI は、ユーザーが選択する前に提案されたコマンドや diff をレビューできるよう、これらのリクエストをアクティブなターンとインラインで表示すべきである。

- リクエストは `threadId` と `turnId` を含む。これらを使って UI 状態をアクティブな会話にスコープする。
- 単一の `{ "decision": ... }` ペイロードで応答する。コマンド承認は `accept`、`acceptForSession`、`acceptWithExecpolicyAmendment`、`applyNetworkPolicyAmendment`、`decline`、`cancel` をサポートする。サーバーは作業を再開または拒否し、`item/completed` でアイテムを終了する。

### Command execution approvals

メッセージの順序:

1. `item/started` — `command`、`cwd`、その他のフィールドを伴う保留中の `commandExecution` アイテムを示し、提案されたアクションを描画できる。
2. `item/commandExecution/requestApproval` (request) — 同じ `itemId`、`threadId`、`turnId`、コマンドが実行される null 許容の `environmentId`、オプションの (サブコマンドのコールバック用の) `approvalId`、`reason` を運ぶ。新しいシェルと unified-exec の承認は `environmentId` を設定する。それを提供しない古いイベントは `null` として公開される。通常のコマンド承認については、リクエストはフレンドリーな表示のための `command`、`cwd`、`commandActions` も含む。`initialize.params.capabilities.experimentalApi = true` の場合、要求されたコマンドごとのサンドボックスアクセスを記述する実験的な `additionalPermissions` も含めることがある。そのペイロード内の任意のファイルシステムパスはワイヤー上で絶対パスであり、ネットワークアクセスは `additionalPermissions.network.enabled` として表現される。ネットワークのみの承認については、それらのコマンドフィールドは省略され、代わりに `networkApprovalContext` が提供されることがある。オプションの永続化ヒントも `proposedExecpolicyAmendment` と `proposedNetworkPolicyAmendments` 経由で含めることがある。クライアントは、サーバーが公開したい正確な選択肢の集合を描画するために、存在する場合 `availableDecisions` を優先しつつ、それが省略された場合は引き続き古いヒューリスティックにフォールバックできる。
3. クライアントの応答 — 例えば `{ "decision": "accept" }`、`{ "decision": "acceptForSession" }`、`{ "decision": { "acceptWithExecpolicyAmendment": { "execpolicy_amendment": [...] } } }`、`{ "decision": { "applyNetworkPolicyAmendment": { "network_policy_amendment": { "host": "example.com", "action": "allow" } } } }`、`{ "decision": "decline" }`、`{ "decision": "cancel" }`。
4. `serverRequest/resolved` — `{ threadId, requestId }` は、保留中のリクエストが解決またはクリアされたことを確認する (ターンの開始/完了/中断時のライフサイクルクリーンアップを含む)。
5. `item/completed` — `status: "completed" | "failed" | "declined"` と実行出力を伴う最終的な `commandExecution` アイテム。これを権威ある結果として描画する。

### File change approvals

メッセージの順序:

1. `item/started` — `changes` (diff チャンクのサマリー) と `status: "inProgress"` を伴う `fileChange` アイテムを発行する。提案された編集とパスをユーザーに表示する。
2. `item/fileChange/requestApproval` (request) — `itemId`、`threadId`、`turnId`、オプションの `reason` を含み、エージェントが特定のルート下でセッションスコープの書き込みアクセスを要求している場合は不安定な `grantRoot` を含むことがある。
3. クライアントの応答 — `{ "decision": "accept" }`、`{ "decision": "acceptForSession" }`、`{ "decision": "decline" }`、`{ "decision": "cancel" }`。
4. `serverRequest/resolved` — `{ threadId, requestId }` は、保留中のリクエストが解決またはクリアされたことを確認する (ターンの開始/完了/中断時のライフサイクルクリーンアップを含む)。
5. `item/completed` — パッチの試行後、`status` が `completed`、`failed`、`declined` に更新された同じ `fileChange` アイテムを返す。これに頼って成功/失敗を表示し、UI 内の diff 状態を確定させる。

IDE 向けの UI ガイダンス: リクエストが届いたらすぐに承認ダイアログを表示する。サーバーが承認リクエストへの応答を受け取った後、ターンは進行する。適切なステータスを伴う終端の `item/completed` 通知が送られる。

### request_user_input

クライアントが `item/tool/requestUserInput` に応答すると、サーバーは `{ threadId, requestId }` を伴う `serverRequest/resolved` を発行する。クライアントが応答する前に、ターンの開始・完了・中断によって保留中のリクエストがクリアされた場合、サーバーはそのクリーンアップについて同じ通知を発行する。

### Attestation generation

上流の attestation を提供するデスクトップホストは、`initialize` 中に `capabilities.requestAttestation` を設定し、サーバー起点の `attestation/generate` リクエストを処理すべきである。app-server は、ChatGPT Codex が `x-oai-attestation` を転送するリクエストの直前にこれを発行する。クライアントは `{ "token": "v1.<opaque>" }` で応答する。`token` は opaque なクライアント所有の値である。app-server はクライアントの応答を受け取ると、`t` に変更なしのクライアントトークンを含む `{ "v": 1, "s": 0, "t": "v1.<opaque>" }` のような一貫した外側のエンベロープを転送する。app-server が attestation を試みたが、自身の境界内で失敗した場合、app-server のステータスコードを伴い `t` を含まない同じエンベロープの形を送る (`1 = timeout`、`2 = request failed`、`3 = request canceled`、`4 = malformed response`)。attestation にオプトインした初期化済みクライアントがない場合、app-server はその上流リクエストについて `x-oai-attestation` を省略する。

### Current time

`clock_source = "external"` を伴う `[features.current_time_reminder]` が有効な場合、時刻リマインダーが期限を迎えると、app-server はスレッドを購読しているクライアントに、`{ "threadId": "thr_123" }` を伴う実験的な `currentTime/read` リクエストを送信する。クライアントは `{ "currentTimeAt": 1781717655 }` で応答する。`currentTimeAt` は Unix タイムスタンプ (秒) の整数である。失敗、キャンセル、タイムアウト、または不正な形式の応答は、モデルリクエストが送られる前にターンを停止する。

### MCP server elicitations

MCP サーバーはターンを中断し、`mcpServer/elicitation/request` 経由でクライアントに構造化された入力を求めることができる。

メッセージの順序:

1. `mcpServer/elicitation/request` (request) — `threadId`、null 許容の `turnId`、`serverName`、そして以下のいずれかを含む:
   - フォームリクエスト: `{ "mode": "form", "message": "...", "requestedSchema": { ... } }`
   - OpenAI 拡張フォームリクエスト: `{ "mode": "openai/form", "message": "...", "requestedSchema": { ... } }`
   - URL リクエスト: `{ "mode": "url", "message": "...", "url": "...", "elicitationId": "..." }`
2. クライアントの応答 — `{ "action": "accept", "content": ... }`、`{ "action": "decline", "content": null }`、`{ "action": "cancel", "content": null }`。
3. `serverRequest/resolved` — `{ threadId, requestId }` は、保留中のリクエストが解決またはクリアされたことを確認する (ターンの開始/完了/中断時のライフサイクルクリーンアップを含む)。

`turnId` はベストエフォートである。elicitation がアクティブなターンと相関している場合、リクエストはそのターン id を含む。そうでない場合は `null` になる。

`openai/form` については、app-server は `requestedSchema` を opaque な JSON として転送する。クライアントは、サポートされているフィールドタイプの検証と描画を所有し、フォームを描画できない場合は有効な `decline` または `cancel` の応答を返さなければならない。

MCP ツール承認の elicitation については、フォームリクエストの `meta` は `codex_approval_kind: "mcp_tool_call"` を含み、クライアントがセッションスコープおよび/または永続的な承認の選択肢を提供できることを通知するために `persist: "session"`、`persist: "always"`、または `persist: ["session", "always"]` を含めることがある。

### Permission requests

組み込みの `request_permissions` ツールは、要求されたパーミッションプロファイルを伴う `item/permissions/requestApproval` JSON-RPC リクエストをクライアントに送る。この v2 ペイロードは、コマンド実行の `additionalPermissions` の形を反映する。ネットワークアクセスと追加のファイルシステムアクセスを要求できる。`environmentId` と `cwd` フィールドは、プロジェクトルートのパーミッションと相対的な deny グロブを解決するために使われる環境とディレクトリを識別する。

```json
{
  "method": "item/permissions/requestApproval",
  "id": 61,
  "params": {
    "threadId": "thr_123",
    "turnId": "turn_123",
    "itemId": "call_123",
    "environmentId": "local",
    "cwd": "/Users/me/project",
    "reason": "Select a workspace root",
    "permissions": {
      "fileSystem": {
        "write": ["/Users/me/project", "/Users/me/shared"]
      }
    }
  }
}
```

クライアントは `result.permissions` で応答する。これは、要求されたパーミッションプロファイルのうち許可されたサブセットであるべきである。同じセッション内の後のターンに対して許可を永続化するために `result.scope` を `"session"` に設定することもできる。省略または `"turn"` は既存のターンスコープの挙動を保持する:

```json
{
  "id": 61,
  "result": {
    "scope": "session",
    "permissions": {
      "fileSystem": {
        "write": ["/Users/me/project"]
      }
    }
  }
}
```

ワイヤー上で重要なのは許可されたサブセットのみである。`result.permissions` から省略されたパーミッションは、拒否されたものとして扱われる。元のリクエストに存在しないパーミッションはサーバーによって無視される。

同じターン内では、許可されたパーミッションは sticky (固定) である: 後続のシェル系ツール呼び出しは、別のパーミッションリクエストを再発行することなく、自動的に許可されたサブセットを再利用できる。

セッションの承認ポリシーが `request_permissions: false` を伴う `Granular` を使う場合、スタンドアロンの `request_permissions` ツール呼び出しは自動拒否され、`item/permissions/requestApproval` のプロンプトは送られない。インラインの `with_additional_permissions` コマンドリクエストは引き続き `sandbox_approval` によって制御され、以前に許可されたパーミッションは同じターン内の後のシェル系呼び出しに対して引き続き sticky である。

### Dynamic tool calls (experimental)

`thread/start` の `dynamicTools` と、対応する `item/tool/call` リクエスト/レスポンスフローは実験的な API である。これらを有効にするには、`initialize.params.capabilities.experimentalApi = true` を設定する。

`dynamicTools` の各エントリは、トップレベルの関数か、関数ツールを含む名前空間のいずれかである。動的ツールの識別子は、Responses ツールと同じ制約に従う:

- `name` は `^[a-zA-Z0-9_-]+$` に一致し、1〜128 文字でなければならない。
- 名前空間の名前は `^[a-zA-Z0-9_-]+$` に一致し、1〜64 文字でなければならない。
- 名前空間の説明は最大 1,024 文字でなければならない。
- 名前空間の名前は、`functions`、`multi_tool_use`、`file_search`、`web`、`browser`、`image_gen`、`computer`、`container`、`terminal`、`python`、`python_user_visible`、`api_tool`、`tool_search`、`submodel_delegator` のような予約済みの Responses ランタイム名前空間と衝突してはならない。

各関数は `deferLoading` を設定できる。省略された場合、デフォルトは `false` である。遅延される関数は名前空間に属さなければならない。関数を登録済みのまま `code_mode` のようなランタイム機能から呼び出し可能にしつつ、通常のターンでモデル向けのツールリストからは除外するには、これを `true` に設定する。`tool_search` が利用可能な場合、遅延された動的ツールは検索可能であり、マッチする検索結果によって公開され得る。

ターン中に動的ツールが呼び出されると、サーバーは `item/tool/call` JSON-RPC リクエストをクライアントに送る:

```json
{
  "method": "item/tool/call",
  "id": 60,
  "params": {
    "threadId": "thr_123",
    "turnId": "turn_123",
    "callId": "call_123",
    "namespace": "tickets",
    "tool": "lookup_ticket",
    "arguments": { "id": "ABC-123" }
  }
}
```

サーバーはまた、リクエストの前後にアイテムのライフサイクル通知を発行する:

1. `item.type = "dynamicToolCall"`、`status = "inProgress"`、`tool` と `arguments` を伴う `item/started`。
2. `item/tool/call` リクエスト。
3. クライアントの応答。
4. `item.type = "dynamicToolCall"`、最終的な `status`、返された `contentItems`/`success` を伴う `item/completed`。

クライアントはコンテンツアイテムで応答しなければならない。テキストには `inputText`、インライン画像 data URL には `inputImage`、インライン音声 data URL には `inputAudio` を使う。音声の data URL は wav、mp3、m4a、webm、ogg のメディアタイプを受け付ける。リモートの HTTP(S) 画像 URL と data 形式でない音声 URL は、動的ツールの応答を無効にする。

```json
{
  "id": 60,
  "result": {
    "contentItems": [
      { "type": "inputText", "text": "Ticket ABC-123 is open." },
      { "type": "inputImage", "imageUrl": "data:image/png;base64,AAA" },
      { "type": "inputAudio", "audioUrl": "data:audio/wav;base64,AAA" }
    ],
    "success": true
  }
}
```

## Skills

テキスト入力に `$<skill-name>` を含めることで、スキルを呼び出す。バックエンドが完全なスキル命令を注入するようにするため (モデルが名前を解決することに頼るのではなく)、`skill` 入力アイテムを追加する (推奨)。

```json
{
  "method": "turn/start",
  "id": 101,
  "params": {
    "threadId": "thread-1",
    "input": [
      {
        "type": "text",
        "text": "$skill-creator Add a new skill for triaging flaky CI."
      },
      {
        "type": "skill",
        "name": "skill-creator",
        "path": "/Users/me/.codex/skills/skill-creator/SKILL.md"
      }
    ]
  }
}
```

`skill` アイテムを省略すると、モデルは引き続き `$<skill-name>` マーカーをパースしてスキルを探そうとするが、これはレイテンシを追加することがある。

例:

```
$skill-creator Add a new skill for triaging flaky CI and include step-by-step usage.
```

利用可能なスキルを取得するには `skills/list` を使う (オプションで `cwds` によりスコープし、`forceReload` を指定できる)。
`skills/list` は `cwd` ごとにキャッシュされた結果を再利用することがある。`forceReload` を `true` に設定するとディスクから結果をリフレッシュする。
サーバーはまた、監視されているローカルのスキルファイルが変化すると `skills/changed` 通知を発行する。これを無効化シグナルとして扱い、必要に応じて現在のパラメータで `skills/list` を再実行すること。
現在の app-server プロセスの追加スタンドアロンスキルルートを置き換えるには `skills/extraRoots/set` を使う。これらのルートは、他のスタンドアロンスキルルートと同じレイアウトを使う: 各ルートはスキルディレクトリを含み、各スキルディレクトリは `SKILL.md` を含む。存在しないルートは受け入れられ、それが存在するようになるまでスキルをロードしない。この設定は app-server が終了すると失われる。

```json
{ "method": "skills/list", "id": 25, "params": {
    "cwds": ["/Users/me/project", "/Users/me/other-project"],
    "forceReload": true
} }
{ "id": 25, "result": {
    "data": [{
        "cwd": "/Users/me/project",
        "skills": [
            {
              "name": "skill-creator",
              "description": "Create or update a Codex skill",
              "enabled": true,
              "interface": {
                "displayName": "Skill Creator",
                "shortDescription": "Create or update a Codex skill",
                "iconSmall": "icon.svg",
                "iconLarge": "icon-large.svg",
                "brandColor": "#111111",
                "defaultPrompt": "Add a new skill for triaging flaky CI."
              }
            }
        ],
        "errors": []
    }]
} }
```

```json
{
  "method": "skills/changed",
  "params": {}
}
```

```json
{
  "method": "skills/extraRoots/set",
  "id": 26,
  "params": {
    "extraRoots": ["/Users/me/generated-skills"]
  }
}
{ "id": 26, "result": {} }
```

絶対パスによってスキルを有効化・無効化するには:

```json
{
  "method": "skills/config/write",
  "id": 27,
  "params": {
    "path": "/Users/alice/.codex/skills/skill-creator/SKILL.md",
    "name": null,
    "enabled": false
  }
}
```

名前によってスキルを有効化・無効化するには:

```json
{
  "method": "skills/config/write",
  "id": 28,
  "params": {
    "path": null,
    "name": "github:yeet",
    "enabled": false
  }
}
```

1 つ以上の `cwds` について検出されたフックを取得するには `hooks/list` を使う。各結果は、その `cwd` の有効な設定で評価されるため、機能ゲートと検出された設定レイヤーは単一のレスポンス内でも異なることがある。

リンクされた Git worktree については、プロジェクトのフック宣言は、リンクされた worktree にのみ保存されている乖離したフック宣言ではなく、ルートのチェックアウト内の一致する `.codex/` フォルダから来る。これにより、各リポジトリは 1 つの権威あるプロジェクトフック定義と 1 つの信頼状態に保たれる。

フックは、クライアントが描画して再有効化できるよう、無効化されている場合でも返される。ユーザーが制御する状態は `hooks.state` にある。管理対象のフックは設定不可であり、管理対象フックのキーに対するユーザーエントリはロード時に無視される。

管理対象でないフックについては、`currentHash` と `trustStatus` が、現在の定義が初見か、承認済みか、承認後に変更されたかを記述する。信頼済みの管理対象でないフックのみが実行可能になる。フックキーはソースの identity と、現在は位置的な末尾のイベント/グループ/ハンドラーセレクターを組み合わせる。

```json
{
  "method": "hooks/list",
  "id": 28,
  "params": {
    "cwds": ["/Users/me/project"]
  }
}
```

```json
{
  "id": 28,
  "result": {
    "data": [{
      "cwd": "/Users/me/project",
      "hooks": [{
        "key": "/Users/me/.codex/config.toml:pre_tool_use:0:0",
        "eventName": "pre_tool_use",
        "handlerType": "command",
        "isManaged": false,
        "matcher": "Bash",
        "command": "python3 /Users/me/hook.py",
        "timeoutSec": 5,
        "statusMessage": "running hook",
        "additionalContextLimit": null,
        "sourcePath": "/Users/me/.codex/config.toml",
        "source": "user",
        "pluginId": null,
        "displayOrder": 0,
        "enabled": true,
        "currentHash": "sha256:...",
        "trustStatus": "untrusted"
      }],
      "warnings": [],
      "errors": []
    }]
  }
}
```

管理対象でないフックを無効化するには、`config/batchWrite` で `hooks.state` に状態エントリを upsert する:

```json
{
  "method": "config/batchWrite",
  "id": 29,
  "params": {
    "edits": [{
      "keyPath": "hooks.state",
      "value": {
        "/Users/me/.codex/config.toml:pre_tool_use:0:0": {
          "enabled": false
        }
      },
      "mergeStrategy": "upsert"
    }],
    "reloadUserConfig": true
  }
}
```

再度有効化するには、同じフックキーを `"enabled": true` で upsert する。
## Apps

インストール済みのアプリと、各アプリが現在有効かつ呼び出し可能かどうかを読み取るには `app/installed` を使う。

```json
{ "method": "app/installed", "id": 49, "params": {
    "threadId": "thr_123",
    "forceRefresh": false
} }
{ "id": 49, "result": {
    "apps": [
        {
            "id": "demo-app",
            "runtimeName": "Demo App",
            "enabled": true,
            "callable": true
        }
    ]
} }
```

`id` はアプリのコネクタ ID であり、`runtimeName` はランタイムが報告する null 許容の名前である。`enabled` は有効なアプリ設定とワークスペースポリシーを反映する。`callable` は、アプリが有効であり、アプリおよびツールポリシーによって許可されたモデル可視のツールを少なくとも 1 つ持つ場合に true になる。

`threadId` が指定された場合、レスポンスはそのスレッドの有効な設定を使う。そうでない場合は現在のグローバル設定を使う。`forceRefresh` はデフォルトで `false` である。レスポンスを読む前にホストされたコネクタランタイムツールのスナップショットをリフレッシュするには `true` に設定する。アプリがグローバルまたはワークスペースポリシーによって無効化されている場合、以前に観測されたアプリは `enabled` と `callable` が `false` に設定されたまま返されることがある。

利用可能なアプリ (コネクタ) を取得するには `app/list` を使う。各エントリは、アプリの `id`、表示 `name`、`installUrl`、レガシーなロゴ URL、構造化されたライト/ダークのアイコンアセット、`branding`、`appMetadata`、`labels`、現在アクセス可能かどうか、設定で有効かどうかのようなメタデータを含む。

```json
{ "method": "app/list", "id": 50, "params": {
    "cursor": null,
    "limit": 50,
    "threadId": "thr_123",
    "forceRefetch": false
} }
{ "id": 50, "result": {
    "data": [
        {
            "id": "demo-app",
            "name": "Demo App",
            "description": "Example connector for documentation.",
            "logoUrl": "https://example.com/demo-app.png",
            "logoUrlDark": null,
            "iconAssets": {
                "256_square": "https://example.com/demo-app-square.png"
            },
            "iconDarkAssets": null,
            "distributionChannel": null,
            "branding": null,
            "appMetadata": null,
            "labels": null,
            "installUrl": "https://chatgpt.com/apps/demo-app/demo-app",
            "isAccessible": true,
            "isEnabled": true
        }
    ],
    "nextCursor": null
} }
```

`threadId` が指定された場合、アプリの機能ゲート (`Feature::Apps`) はそのスレッドの設定スナップショットを使って評価される。省略された場合は最新のグローバル設定が使われる。

`app/list` は、アクセス可能なアプリとディレクトリアプリの両方がロードされた後に返る。キャッシュをバイパスして新しいデータをソースから取得するには `forceRefetch: true` を設定する。キャッシュエントリは、それらの再取得が成功した場合のみ置き換えられる。

サーバーはまた、新しくロードされたアクセス可能な、またはディレクトリのアプリがマージされたアプリリストを変更する場合、`app/list/updated` 通知を発行する。各通知は最新のマージ済みアプリリストを含む。初期のキャッシュされた `app/list` でも、他の初期化済みクライアントがアプリリストをリフレッシュできるよう、最終通知を 1 回発行する。一方、変更のないキャッシュされた続きのページを読む場合は重複した通知を発行しない。`forceRefetch: true` は、新しいデータがロードされる間、既存の段階的な通知を保持する。

```json
{
  "method": "app/list/updated",
  "params": {
    "data": [
      {
        "id": "demo-app",
        "name": "Demo App",
        "description": "Example connector for documentation.",
        "logoUrl": "https://example.com/demo-app.png",
        "logoUrlDark": null,
        "iconAssets": {
          "256_square": "https://example.com/demo-app-square.png"
        },
        "iconDarkAssets": null,
        "distributionChannel": null,
        "branding": null,
        "appMetadata": null,
        "labels": null,
        "installUrl": "https://chatgpt.com/apps/demo-app/demo-app",
        "isAccessible": true,
        "isEnabled": true
      }
    ]
  }
}
```

クライアントが既にアプリ id を持ち、メタデータのみが必要な場合は `app/read` を使う。リクエストは最大 100 個の `appIds` を受け付ける。重複する id は、最初のリクエスト順を保持したまま重複排除される。`apps` と `missingAppIds` の両方はその順序に従う。未知または未認可の id は、リクエスト全体を失敗させるのではなく、部分的な欠落として返される。

```json
{ "method": "app/read", "id": 51, "params": {
    "appIds": ["demo-app", "missing-app"],
    "includeTools": true
} }
{ "id": 51, "result": {
    "apps": [
        {
            "id": "demo-app",
            "name": "Demo App",
            "description": "Example app for documentation.",
            "iconUrl": "https://files.openai.com/content?id=demo-app",
            "toolSummaries": [
                {
                    "name": "search",
                    "title": "Search",
                    "description": "Search the app.",
                    "isEnabled": true,
                    "disabledReason": null,
                    "isReadOnly": true
                }
            ]
        }
    ],
    "missingAppIds": ["missing-app"]
} }
```

`app/read` は、バックエンド URL と ChatGPT アカウント/ワークスペースの identity によって分割されたキャッシュから新しいメタデータレコードを読み取り、次に不足または期限切れの id について最大 1 回の `POST /ps/apps/batch` を行う。`includeTools` はデフォルトで false であり、`include_tools` として転送される。ツールサマリーが要求されると、新しいメタデータのみのキャッシュエントリが再取得される。バックエンドまたはトランスポートの失敗は、既存のキャッシュレコードを置き換えずに RPC エラーを返す。そのメタデータの形は、有効/読み取り専用状態を伴う表示専用の公開ツールサマリーを含むことができ、意図的にランタイム状態、MCP ツール状態、完全なアクション、モデルの説明を除外する。

接続されたアプリは、`config.toml` でスレッドの承認レビューアーを上書きできる。すべてのアプリのレビューアーを設定するには `apps._default.approvals_reviewer` を使い、そのデフォルトを上書きするにはアプリごとの値を使う。両方が省略された場合、アプリはトップレベルの `approvals_reviewer` の値を継承する:

```toml
approvals_reviewer = "auto_review"

[apps._default]
approvals_reviewer = "user"
default_tools_approval_mode = "prompt"

[apps.demo-app]
approvals_reviewer = "auto_review"
default_tools_approval_mode = "approve"
```

アプリの値を `"user"` に設定すると、その承認プロンプトは Guardian ではなくユーザーにルーティングされる。`"auto_review"` に設定すると、設定要件によって許可されている場合、そのアプリを Guardian レビューにオプトインさせる。

アプリごとまたはツールごとの上書きがないツールの承認モードを設定するには `apps._default.default_tools_approval_mode` を使う。サポートされる値は `"auto"`、`"prompt"`、`"writes"`、`"approve"` である。`"writes"` モードは、`readOnlyHint = true` を公開しないツールについてプロンプトを出し、宣言された読み取り専用ツールはスキップする。ツールレベルの `approval_mode` はアプリごとの `default_tools_approval_mode` より優先され、それは `apps._default` の値より優先される。管理対象のツール要件は、これらすべての設定より優先される。何も設定されていない場合、モードはデフォルトで `"auto"` になる。

テキスト入力に `$<app-slug>` を挿入することで、アプリを呼び出す。slug はアプリ名から派生し、小文字化され、英数字以外の文字は `-` に置き換えられる (例えば、"Demo App" は `$demo-app` になる)。サーバーが名前による推測ではなく正確な `app://<connector-id>` パスを使うよう、`mention` 入力アイテムを追加する (推奨)。プラグインも同じ `mention` アイテムの形を使うが、`plugin/installed` または `plugin/list` からの `plugin://<plugin-name>@<marketplace-name>` パスを使う。

例:

```
$demo-app Pull the latest updates from the team.
```

```json
{
  "method": "turn/start",
  "id": 51,
  "params": {
    "threadId": "thread-1",
    "input": [
      {
        "type": "text",
        "text": "$demo-app Pull the latest updates from the team."
      },
      { "type": "mention", "name": "Demo App", "path": "app://demo-app" }
    ]
  }
}
```

## Auth endpoints

JSON-RPC の auth/account サーフェスは、リクエスト/レスポンスのメソッドと、サーバー起点の通知 (id なし) を公開する。これらを使って認証状態を判定し、ログインの開始やキャンセル、ログアウトを行い、ChatGPT のレート制限を調べる。

### Authentication modes

Codex はこれらの認証モードをサポートする。現在のモードは `account/updated` (`authMode`) で公開され、利用可能な場合は現在の ChatGPT `planType` も含まれ、`account/read` から推測できる。セルフサーブの Business ProLite アカウントは `self_serve_business_prolite` プランタイプを使い、Enterprise 自動化アカウントは `enterprise_cbp_automation` を使う。

- **API key (`apiKey`)**: 呼び出し元は `type: "apiKey"` を伴う `account/login/start` 経由で OpenAI API キーを提供する。API キーは保存され、API リクエストに使われる。
- **ChatGPT managed (`chatgpt`)** (推奨): Codex が ChatGPT の OAuth フローとリフレッシュトークンを所有する。ブラウザフローには `type: "chatgpt"` を、デバイスコードには `type: "chatgptDeviceCode"` を伴う `account/login/start` から開始する。Codex はトークンをディスクに永続化し、自動的にリフレッシュする。
- **Codex managed Amazon Bedrock auth (`amazonBedrock`, experimental)**: 呼び出し元は `type: "amazonBedrock"` を伴う `account/login/start` 経由で Amazon Bedrock の API キーとリージョンを提供する。クライアントは、Codex が管理する Amazon Bedrock ログインには `experimentalApi` 初期化能力を有効にしなければならない。Codex は、現在のプライマリな Codex 認証を Bedrock の資格情報に置き換え、`model_provider = "amazon-bedrock"` をユーザー設定に書き込む。
- **Personal access token (`personalAccessToken`)**: Codex は、`codex login --with-access-token` や `CODEX_ACCESS_TOKEN` のように app-server のログイン RPC の外でロードされた、ChatGPT バックの personal access token を使う。

### API Overview

- `account/read` — 現在のアカウント情報を取得する。オプションでトークンをリフレッシュする。
- `account/login/start` — ログインを開始する (`apiKey`、`chatgpt`、`chatgptDeviceCode`、`amazonBedrock`)。
- `account/login/completed` (通知) — ログイン試行が終わる (成功またはエラー) と発行される。
- `account/login/cancel` — `loginId` によって、保留中の管理対象 ChatGPT ログインをキャンセルする。
- `account/logout` — サインアウトする。成功すると `account/updated` をトリガーする。
- `account/updated` (通知) — 認証モードが変化するたびに発行される (`authMode`: `apikey`、`bedrockApiKey`、`chatgpt`、`personalAccessToken`、`null` のいずれか)。利用可能な場合は現在の ChatGPT `planType` も含む。
- `account/rateLimits/read` — ChatGPT のレート制限、オプションの有効な月間クレジット制限、支出制限に達したかどうか、バックエンドが提供する場合は有効期限の詳細を含め現在利用可能な獲得済みレート制限リセットを取得する。レート制限の更新は `account/rateLimits/updated` (通知) 経由で届く。リセットクレジットのデータはスナップショットのみである。
- `account/rateLimitResetCredit/consume` — 呼び出し元が指定した冪等性キーを使って 1 つの獲得済みリセットを消費する。オプションで `account/rateLimits/read` が返すリセットクレジット ID を選択できる。
- `account/usage/read` — ChatGPT アカウントのトークンアクティビティのサマリーと日次バケットを取得する。
- `account/workspaceMessages/read` — 利用可能な場合、ワークスペースの通知見出しを含む、アクティブなワークスペースメッセージを取得する。
- `account/rateLimits/updated` (通知) — ユーザーの ChatGPT レート制限が変化するたびに発行される。これはスパースなローリング更新である。利用可能な値を最新の `account/rateLimits/read` レスポンスにマージするか、そのスナップショットを再取得すること。
  `spendControlReached` は、バックエンドが支出制限の状態を報告する場合 `true` または `false` になる。`null` は利用不可を意味し、スパースな更新において以前に観測された値をクリアしてはならない。
- `account/sendAddCreditsNudgeEmail` — 枯渇したクレジットや到達した使用量制限について、ワークスペースオーナーにメールで通知するよう ChatGPT に依頼する。
- `mcpServer/oauthLogin/completed` (通知) — サーバーに対する `mcpServer/oauth/login` フローが終わると発行される。ペイロードは `{ name, threadId, success, error? }` を含む。
- `mcpServer/startupStatus/updated` (通知) — 設定済みの MCP サーバーの起動ステータスが変化すると発行される。ペイロードは `{ threadId, name, status, error, failureReason }` を含む。`threadId` は起動がスレッドスコープの場合は所有スレッド、アプリスコープの場合は `null` である。`status` は `starting`、`ready`、`failed`、`cancelled` のいずれかである。保存されている OAuth 資格情報が期限切れでリフレッシュできない場合、`failureReason` は `reauthenticationRequired` になり、クライアントはユーザーに名前付きサーバーへの再接続を促せる。

### 1) Check auth state

リクエスト:

```json
{ "method": "account/read", "id": 1, "params": { "refreshToken": false } }
```

レスポンス例:

```json
{ "id": 1, "result": { "account": { "type": "chatgpt", "email": "user@example.com", "planType": "pro" }, "requiresOpenaiAuth": true } }
{ "id": 1, "result": { "account": { "type": "amazonBedrock", "usesCodexManagedCredentials": false }, "requiresOpenaiAuth": false } }
```

フィールドの補足:

- `refreshToken` (bool): トークンリフレッシュを強制するには `true` に設定する。
- `email` は、ChatGPT アカウントがメールアドレスを持たない場合 `null` になる。
- `requiresOpenaiAuth` はアクティブなプロバイダーを反映する。`false` の場合、Codex は OpenAI の資格情報なしで動作できる。
- Amazon Bedrock は、Codex が管理する Bedrock の API キーを使う場合 `usesCodexManagedCredentials: true` を報告する。AWS の資格情報チェーンや設定済みのコマンド認証を含む外部の資格情報経路については `false` を報告する。これは、Codex が管理する資格情報が選択されているかどうかを識別するものであり、その資格情報ソースが資格情報を解決できることを検証するものではない。

### 2) Log in with an API key

1. 送信する:
   ```json
   {
     "method": "account/login/start",
     "id": 2,
     "params": { "type": "apiKey", "apiKey": "sk-…" }
   }
   ```
2. 期待する:
   ```json
   { "id": 2, "result": { "type": "apiKey" } }
   ```
3. 通知:
   ```json
   { "method": "account/login/completed", "params": { "loginId": null, "success": true, "error": null } }
   { "method": "account/updated", "params": { "authMode": "apikey", "planType": null } }
   ```

### 3) Log in with ChatGPT (browser flow)

1. 開始する:
   ```json
   { "method": "account/login/start", "id": 3, "params": { "type": "chatgpt" } }
   { "id": 3, "result": { "type": "chatgpt", "loginId": "<uuid>", "authUrl": "https://chatgpt.com/…&redirect_uri=http%3A%2F%2Flocalhost%3A<port>%2Fauth%2Fcallback" } }
   ```
2. `authUrl` をブラウザで開く。app-server がローカルのコールバックをホストする。
   デフォルトでは、成功したコールバックはローカルの成功ページにリダイレクトする。クライアントは
   `useHostedLoginSuccessPage: true` を設定して、組織セットアップを必要としない成功したコールバックを
   代わりにホストされた Codex の成功ページにリダイレクトできる。ホストされたログイン成功が
   有効な場合、クライアントは `appBrand` を `"codex"` または `"chatgpt"` に設定して一致する
   ホストされたページのアートワークを選べる。省略または `null` の値はデフォルトで `"codex"` になる。
3. 通知を待つ:
   ```json
   { "method": "account/login/completed", "params": { "loginId": "<uuid>", "success": true, "error": null } }
   { "method": "account/updated", "params": { "authMode": "chatgpt", "planType": "plus" } }
   ```

### 3) Log in with an Amazon Bedrock API key

この実験的なフローは、クライアントが `experimentalApi: true` で初期化することを必要とする。

1. 送信する:
   ```json
   {
     "method": "account/login/start",
     "id": 3,
     "params": { "type": "amazonBedrock", "apiKey": "…", "region": "us-west-2" }
   }
   ```
2. 期待する:
   ```json
   { "id": 3, "result": { "type": "amazonBedrock" } }
   ```
3. 通知:
   ```json
   { "method": "account/login/completed", "params": { "loginId": null, "success": true, "error": null } }
   { "method": "account/updated", "params": { "authMode": "bedrockApiKey", "planType": null } }
   ```

Codex はキーとリージョンをプライマリな Codex 認証として保存し、以前に保存されていたログインを置き換え、アクティブなユーザー設定に `model_provider = "amazon-bedrock"` を書き込む。既存のロード済みセッションは現在のプロバイダー選択を保持するため、クライアントはより多くのモデルリクエストを送る前に app-server を再起動すべきである。この制限は今後対応される予定である。

### 4) Log in with ChatGPT (device code flow)

1. 開始する:
   ```json
   { "method": "account/login/start", "id": 4, "params": { "type": "chatgptDeviceCode" } }
   { "id": 4, "result": { "type": "chatgptDeviceCode", "loginId": "<uuid>", "verificationUrl": "https://auth.openai.com/codex/device", "userCode": "ABCD-1234" } }
   ```
2. `verificationUrl` と `userCode` をユーザーに表示する。UX はフロントエンドが所有する。
3. 通知を待つ:
   ```json
   { "method": "account/login/completed", "params": { "loginId": "<uuid>", "success": true, "error": null } }
   { "method": "account/updated", "params": { "authMode": "chatgpt", "planType": "plus" } }
   ```

### 5) Cancel a ChatGPT login

```json
{ "method": "account/login/cancel", "id": 5, "params": { "loginId": "<uuid>" } }
{ "method": "account/login/completed", "params": { "loginId": "<uuid>", "success": false, "error": "…" } }
```

### 6) Logout

```json
{ "method": "account/logout", "id": 6 }
{ "id": 6, "result": {} }
{ "method": "account/updated", "params": { "authMode": null, "planType": null } }
```

Codex が管理する Bedrock キーを使っている場合、ログアウトはそのキーを削除し、`model_provider` がまだ `"amazon-bedrock"` に設定されている場合はそれをクリアする。AWS が管理する資格情報を使っている場合は、ログアウトする前に AWS 経由でそれらを管理するか、プロバイダーを切り替えること。

### 7) Rate limits (ChatGPT)

```json
{ "method": "account/rateLimits/read", "id": 7 }
{
  "id": 7,
  "result": {
    "rateLimits": {
      "primary": { "usedPercent": 25, "windowDurationMins": 15, "resetsAt": 1730947200 },
      "secondary": null,
      "rateLimitReachedType": null
    },
    "rateLimitResetCredits": {
      "availableCount": 2,
      "credits": [
        {
          "id": "RateLimitResetCredit_1",
          "resetType": "codexRateLimits",
          "status": "available",
          "grantedAt": 1781654400,
          "expiresAt": 1784246400,
          "title": "Full reset (Weekly + 5 hr)",
          "description": "Ready to redeem"
        }
      ]
    }
  }
}
{ "method": "account/rateLimits/updated", "params": { "rateLimits": { … } } }
```

フィールドの補足:

- `usedPercent` は OpenAI クォータウィンドウ内の現在の使用量である。
- `windowDurationMins` はクォータウィンドウの長さである。
- `resetsAt` は次のリセットの Unix タイムスタンプ (秒) である。
- `rateLimitReachedType` は、制限に達した場合、バックエンドが分類した制限状態を識別する。
- `individualLimit` は、利用可能な場合、有効な月間クレジット制限を記述する。`account/rateLimits/read` のレスポンスでは、`null` は月間制限が利用不可であることを意味する。スパースな `account/rateLimits/updated` 通知では、null 許容のアカウントメタデータが利用不可のことがあり、それは以前に観測された値をクリアしない。
- `rateLimitResetCredits` は、バックエンドが提供する場合、利用可能な獲得済みリセットの数を含む。そうでなければ `null` である。
- `rateLimitResetCredits.credits` は、カウントのみが利用可能な場合 `null` になる。空配列は、詳細が取得され、利用可能なクレジットが返されなかったことを意味する。
- バックエンドは `rateLimitResetCredits.credits` をキャップすることがあるため、`availableCount` が権威ある合計であり、詳細行の数より多いことがある。
- リセットを消費した後は `account/rateLimits/read` を再取得すること。

### 8) Earned rate-limit resets (ChatGPT)

```json
{ "method": "account/rateLimitResetCredit/consume", "id": 8, "params": { "idempotencyKey": "8ae96ff3-3425-4f4c-8772-b6fd61502868", "creditId": "RateLimitResetCredit_1" } }
{ "id": 8, "result": { "outcome": "reset" } }
```

フィールドの補足:

- `idempotencyKey` は空であってはならない。論理的な各リデンプション試行には UUID が推奨される。その試行をリトライする際は同じ値を再利用すること。
- `creditId` はオプションである。指定する場合、`account/rateLimits/read` が返す空でない opaque な ID でなければならない。省略された場合、バックエンドは次に利用可能なクレジットを選択する。
- `reset` はクレジットが消費されたことを意味する。
- `alreadyRedeemed` は同じリデンプションが以前に完了したことを意味する。冪等な成功として扱い、アカウントの制限をリフレッシュすること。
- `nothingToReset` は、リセットに適格なレート制限ウィンドウがないことを意味する。
- `noCredit` は、アカウントに利用可能な獲得済みリセットクレジットがないことを意味する。
- このレスポンスから更新後の状態を推測するのではなく、リセットを消費した後は `account/rateLimits/read` を再取得すること。

### 9) Workspace messages (ChatGPT)

```json
{ "method": "account/workspaceMessages/read", "id": 9 }
{ "id": 9, "result": { "featureEnabled": true, "messages": [
    { "messageId": "msg_123", "messageType": "headline", "messageBody": "Workspace maintenance starts at 5pm.", "createdAt": 1781395200, "archivedAt": null }
] } }
```

上流のワークスペースメッセージ機能が無効な場合、`featureEnabled` は `false` になり、`messages` は空になる。

### 10) Notify a workspace owner about a limit

```json
{ "method": "account/sendAddCreditsNudgeEmail", "id": 9, "params": { "creditType": "credits" } }
{ "id": 9, "result": { "status": "sent" } }
```

ワークスペースのクレジットが枯渇している場合は `creditType: "credits"` を、ワークスペースの使用量制限に達した場合は `creditType: "usage_limit"` を使う。オーナーが最近既に通知されている場合、レスポンスの status は `cooldown_active` になる。

## Experimental API Opt-in

一部の app-server メソッドとフィールドは、後方互換性の保証なしに、意図的に実験的な能力の背後にゲートされている。これにより、クライアントは以下を選べる:

- 安定サーフェスのみ (デフォルト): オプトインなし、実験的なメソッド/フィールドの公開なし。
- 実験的サーフェス: `initialize` 中にオプトインする。

### Generating stable vs experimental client schemas

`codex app-server` のスキーマ生成は、デフォルトで安定した API サーフェス (実験的なフィールドとメソッドはフィルタで除外) を使う。生成される TypeScript や JSON Schema に実験的なメソッド/フィールドを含めるには `--experimental` を渡す:

```bash
# Stable-only output (default)
codex app-server generate-ts --out DIR
codex app-server generate-json-schema --out DIR

# Include experimental API surface
codex app-server generate-ts --out DIR --experimental
codex app-server generate-json-schema --out DIR --experimental
```

### How clients opt in at runtime

単一の `initialize` リクエストで `capabilities.experimentalApi` を `true` に設定する:

```json
{
  "method": "initialize",
  "id": 1,
  "params": {
    "clientInfo": {
      "name": "my_client",
      "title": "My Client",
      "version": "0.1.0"
    },
    "capabilities": {
      "experimentalApi": true
    }
  }
}
```

その後、標準の `initialized` 通知を送り、通常通り進める。

補足:

- `capabilities` が省略された場合、`experimentalApi` は `false` として扱われる。
- この設定は、初期化時に一度、プロセスの生存期間について交渉される (再初期化は `"Already initialized"` で拒否される)。

### What happens without opt-in

リクエストが、オプトインせずに実験的なメソッドを使うか実験的なフィールドを設定した場合、app-server はそれを JSON-RPC エラーで拒否する。メッセージは次の通りである:

`<descriptor> requires experimentalApi capability`

descriptor 文字列の例:

- `mock/experimentalMethod` (メソッドレベルのゲート)
- `thread/start.mockExperimentalField` (フィールドレベルのゲート)
- `askForApproval.granular` (enum バリアントのゲート。`approvalPolicy: { "granular": ... }` 向け)

### For maintainers: Adding experimental fields and methods

クライアントが実験的な API にオプトインした場合にのみ利用可能にすべきフィールド/メソッドを導入する際は、このチェックリストを使う。

ランタイムでは、クライアントは実験的なメソッドやフィールドを使うために `capabilities.experimentalApi = true` を伴う `initialize` を送らなければならない。

1. プロトコル型 (通常 `app-server-protocol/src/protocol/v2.rs`) 内のフィールドに次のようにアノテーションする:
   ```rust
   #[experimental("thread/start.myField")]
   pub my_field: Option<String>,
   ```
2. params 型が `ExperimentalApi` を derive し、フィールドレベルのゲーティングがランタイムで検出できるようにする。

3. `app-server-protocol/src/protocol/common.rs` では、(`thread/start` のように) 一部のフィールドのみが実験的な場合はメソッドを安定のままにし `inspect_params: true` を使う。メソッド全体が実験的な場合は、メソッドバリアントに `#[experimental("method/name")]` をアノテーションする。

Enum バリアントもゲートできる:

```rust
#[derive(ExperimentalApi)]
enum AskForApproval {
    #[experimental("askForApproval.granular")]
    Granular { /* ... */ },
}
```

安定したフィールドがそれ自体実験的かもしれないネストされた型を含む場合、`ExperimentalApi` がそのネストされた理由を含む型全体に波及するよう、そのフィールドに `#[experimental(nested)]` をマークする:

```rust
#[derive(ExperimentalApi)]
struct Config {
    #[experimental(nested)]
    approval_policy: Option<AskForApproval>,
}
```

サーバー起点のリクエストペイロードについては、スキーマ生成がそれを実験的として扱うよう同じ方法でフィールドにアノテーションし、クライアントが `experimentalApi` にオプトインしなかった場合に app-server がそのフィールドを省略することを確認する。

4. プロトコルのフィクスチャを再生成する:

   ```bash
   just write-app-server-schema
   # Refresh the embedded exports that include experimental API fields/methods.
   just write-app-server-schema --experimental
   ```

5. プロトコルクレートを検証する:

   ```bash
   just test -p codex-app-server-protocol
   ```
