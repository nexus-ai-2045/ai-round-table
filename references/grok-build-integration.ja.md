# Grok Build 連携ポイント (日本語 SSOT)

- 取得日: 2026-07-27 / 種別: C
- 原典: https://github.com/xai-org/grok-build (公式 OSS, Rust) / https://docs.x.ai/build/overview /
  実測: `grok --help` / `grok agent --help` / `grok leader --help` (2026-07-25, grok.exe ローカル)

## 席として使える口 (実測ベース)

| 口 | 内容 | 状態 |
|---|---|---|
| `grok agent serve` | **WebSocket サーバ** (既定 127.0.0.1:2419、secret 認証。`GROK_AGENT_SECRET`) | 実在 (--help 確認)。protocol 詳細は未調査 |
| `grok agent leader` / `grok --leader` | **共有 leader プロセス** (`~/.grok/leader.sock`)。複数クライアントが 1 backend を共有 | 実在。TUI と機械クライアントの同居 = 「常駐ターミナルに投げる」の公式形 |
| `grok agent stdio` | stdio agent (非対話) | CLI 実行に該当するため roundtable の席には使わない (CEO 方針) |
| **ACP (Agent Client Protocol)** | エディタ埋め込み用の標準プロトコル。Grok Build は公式対応 | **重要**: ACP が席の統一接続口になる可能性 (v0.2 spike 候補) |
| `grok export` / `grok sessions list|search|restore` | セッションの Markdown 書き出し・管理 | 過去チャットの持ち込みに使える |

## 未確認事項

1. serve / leader 経由の会話が **TUI/アプリ画面のどこに見えるか** (Codex の spike 1 と同型の問い)
2. serve の message protocol (スキーマ未取得)
3. ACP で接続した場合の会話の永続化・表示

## roundtable での位置づけ

- v0.1: 参加しない (Codex + CC で開始)
- v0.2: serve or leader の spike → go なら Tier1、no-go なら Tier3
- 役割プリセット案: 逆張り・トレンド視点 (X 文脈)
