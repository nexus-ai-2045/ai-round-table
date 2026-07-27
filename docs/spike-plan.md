# Spike 提案 — Tier1 relay の go/no-go 判定 (v0.1 実装前)

- 日付: 2026-07-27 / 対象: DESIGN v5 §8
- 目的: 「relay 自動化 (Tier1) が使える席」を実測で確定する。**spike が全滅しても
  v0.1 は Tier3 (人間 relay) で成立する**ため、実装をブロックしない。結果は tier 設定に
  反映するだけ。

## Spike 1: Codex app-server daemon (30 分)

| 手順 | 内容 | 判定 |
|---|---|---|
| 1 | `codex app-server daemon start` (npm 版 0.144.6 フルパス) | 起動成功 |
| 2 | 制御 socket に JSON-RPC `initialize` → `thread/start` (表示名 `rt-spike-codex`) | thread_id 取得 |
| 3 | `turn/start` で「`<path>/scratch/spike.json` に {"ok":true} を書け」を送信 | 応答受信 |
| 4 | **CEO が Codex アプリを開き**、`rt-spike-codex` が一覧に見えるか・turn が読めるか確認 | 可視性 |
| 5 | scratch/spike.json が書かれたか確認 | FS 実行力 |
| 6 | `codex app-server daemon stop` で撤収 | 掃除 |

- go 条件: 2,3,5 成功 + 4 で「一覧に見える」(即時反映は不問、DESIGN v5 §10-6 の扱い)
- no-go 時: seats.json で codex を tier 3 に設定。v0.2 で再評価

## Spike 2: CCD send_message (15 分)

| 手順 | 内容 | 判定 |
|---|---|---|
| 1 | Claude Code Desktop で席用セッションを 1 個作る (CEO 操作、名前 `rt-spike-cc`) | — |
| 2 | ホスト (このセッション) から `mcp__ccd_session_mgmt__send_message` で packet 文を送る | 送信成功 |
| 3 | 席セッションが packet に従い scratch へ JSON を書くか確認 | FS 実行力 |
| 4 | CEO が席セッションの画面で履歴を確認 | 可視性 |

- go 条件: 2,3,4 すべて成功
- no-go 時: cc を tier 3 に設定

## 実施タイミング

実装計画 Phase 1 (Tier3 で動く核) の完了後・Phase 2 の前に実施。
結果は `docs/spike-results.md` に記録し、`seats.json` の tier に反映する。

## 撤収・安全

- daemon は spike 終了時に必ず stop。常用開始は v0.1 受け入れ後
- spike で作った席チャットは CEO が任意に削除可 (記録は spike-results.md に残す)
- UI 自動化はこの spike に含まれない (Tier2 は別途 CEO 承認後)
