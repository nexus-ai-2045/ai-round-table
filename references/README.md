# references/ — 外部参考資料の SSOT (日本語)

外部の設計知識・プロトコル情報を**日本語で・情報を落とさず・出所付き**で固定する場所。
実装や設計 doc からはここを参照する (原典 URL に依存しない = リンク切れ・改版に耐える)。

## 方式 3 段階

| 段階 | 対象 | 置き方 | 台帳 |
|---|---|---|---|
| **A: 浅 clone** | 深く読む小規模 OSS (例: codex-client, llm-council) | `~/Projects/Documents/.repos/external/<name>` に `git clone --depth 1`。読み取り専用 (改変しない) | `nexus_ai/config/repository_path_migration.json` に repo_class: external_oss_reference で登録。pinned commit を記録 |
| **B: ファイル snapshot** | 巨大 repo の一部だけ要るもの (例: openai/codex の app-server README/protocol) | 本ディレクトリに `<slug>.snapshot.md` として保存。冒頭に取得日 / 原典 URL / commit を明記 | 本 README の一覧表 |
| **C: 日本語要約** | 記事・ブログ・調査で得た知見 | 本ディレクトリに `<slug>.ja.md`。原文コピーせず要約 + 出典 URL | 同上 |

規約:
- ファイル冒頭に必ず: `取得日 / 原典 / 版 (commit・日付) / 種別 (A|B|C) / 未確認事項`
- 訳す時に**情報を削らない** (省略した節がある場合は「省略: …」と明記)
- 原典と食い違う実測をした場合は「実測メモ」節を追記し、日付を付ける

## 一覧

| ファイル | 種別 | 原典 | 内容 |
|---|---|---|---|
| codex-app-server-protocol.ja.md | C (→B 昇格予定) | openai/codex codex-rs/app-server + 解説群 | thread/turn JSON-RPC の要点・Windows 制約の実測 |
| llm-council-pattern.ja.md | C | karpathy/llm-council | 3 段階合議パターンと ai-round-table との差分 |
| grok-build-integration.ja.md | C | xai-org/grok-build + docs.x.ai | serve/leader/ACP の要点と未確認事項 |

(調査レポート本体は `~/Projects/Documents/nexus_ai/research/` 配下。ここには設計が依存する
知識だけを昇格させる)
