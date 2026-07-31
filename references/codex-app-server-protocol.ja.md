# Codex app-server プロトコル (日本語 SSOT)

- 取得日: 2026-07-27 / 種別: C (実装前に B: 原典 README snapshot へ昇格予定)
- 原典: https://github.com/openai/codex/tree/main/codex-rs/app-server (README 含む) /
  解説: codex.danielvaughan.com 2026-03-28, 03-31, 04-15 / gist (oneryalcin) /
  Node クライアント: https://github.com/raylin01/codex-client
- 実測環境: codex-cli 0.144.6 (npm) / Windows 11 / 2026-07-26〜27

## これは何か

Codex CLI の TUI・VS Code 拡張・デスクトップアプリ・JetBrains・Xcode が**全部共通で使っている
JSON-RPC 2.0 サーバ**。`codex app-server` で起動する長寿命プロセスで、Codex agent の
thread (会話) と turn (1 往復) をホストする。第三者クライアントが乗ることが公式に想定されている。

## トランスポート

- **stdio (既定)**: 改行区切り JSON (JSONL)。**stable 扱い** — 実装の第一候補
- WebSocket: リモート・ヘッドレス用 (experimental 寄り)
- 接続開始は必ず `initialize` → `initialized` の handshake

## 主要メソッド (schema 生成で実在確認済み 2026-07-26)

| メソッド | 用途 |
|---|---|
| thread/start | 会話 (席) の新規作成 |
| thread/resume | 既存 thread の再開 |
| thread/list / thread/read | 一覧・読み取り (席 revision 照合に使える) |
| thread/name / thread/metadata / thread/goal | 表示名等の設定 (席の命名 `rt-<議題>-codex` に使う) |
| thread/archive / delete / compact / fork / rollback | 管理系 |
| turn/start | **メッセージ送信** (1 turn 開始) |
| turn/steer / turn/interrupt | 実行中 turn への介入・中断 |
| approval 系 (ApplyPatchApproval 等) | ツール実行の承認フロー |

概念: **Turn = ユーザー入力 + それに対する agent の全作業**。中断・rollback の単位。

## Windows 制約 (実測)

- `codex app-server daemon start` → **"daemon lifecycle is only supported on Unix platforms"**
  (2026-07-27 実測)。常駐 daemon 管理・`proxy` 前提の運用は Windows では使えない
- 回避: **自前で `codex app-server` を spawn し stdio で直接会話する** (これが本流の使い方。
  VS Code 拡張も同方式)。roundtable では dispatcher が接続を管理する
- セッションストア `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` は CLI/アプリ共有 (実測 2026-07-26)

## 未確認事項 (spike 1 で判定)

1. 自前 spawn した app-server で作った thread が、**開いている Codex アプリの画面に
   いつ現れるか** (即時 or 再起動後)
2. turn 単位での sandbox / 権限指定のパラメタ
3. thread/read の revision 意味論 (席の予期しない追記の検知に使えるか)

## 実装メモ

- 参照実装: codex-client (Node) が「生 transport + thread/turn ラッパ」の 2 層構成 —
  relay_codex.py も同じ 2 層で書くのが素直
- DESIGN v5 の Tier1 relay は本プロトコルの thread/resume + turn/start に 1:1 対応する
