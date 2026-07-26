# ai-roundtable 設計書

- 日付: 2026-07-26
- 状態: CEO レビュー待ち
- 前身 context: `~/Projects/Documents/inbox/2026-07-26-ai-roundtable-design-context-for-all-ai.md`
- 調査根拠: `~/Projects/Documents/nexus_ai/research/multi-ai-realtime-linking/report.md`

## 1. 目的

Codex / CC (Claude Code) / Grok / Gemini (agy) を Windows 上でリンクし、
**人間 (CEO) が司会**する壁打ち・多者会談を回す基盤。

- 新 UI は作らない。ホストは既存チャット (どの AI でもよい = host-agnostic)
- チャット履歴は各 AI のネイティブ管理のまま。共有するのは議事録だけ
- cmux (Mac 専用) に依存しない

## 2. 決定事項と根拠

| # | 決定 | 根拠 |
|---|---|---|
| D1 | 司会 = 人間固定 | 分散網目型は誤り 17.2 倍 / 中央 orchestrator 型 4.4 倍。人間司会なら echo chamber を指名で防げる |
| D2 | 共有点 = 議事録 md 1 枚 (blackboard) | agent は直接会話せず共有ワークスペースへ追記して間接協調 (arXiv 2510.01285 ほか)。全文同期は conformity bias を作るのでしない |
| D3 | 書記 = ホストから分離 | ホスト AI の「書記と参加者の兼任バイアス」を構造排除。書記は判断しない機械作業 |
| D4 | 書記の実体 = 決定的スクリプト (`dispatch.py`) | ホスト間の品質差を最小化。エージェントはスクリプトを叩くだけ |
| D5 | host-agnostic | プロトコルは `PROTOCOL.md` に置き、各 AI は薄いアダプタで入る。特定 AI の skill に埋めない |
| D6 | 参加者は完全対称 | ホスト AI 自身の意見も `claude -p` 等で他と同列に呼ぶ |
| D7 | MCP 相互接続は v0.3 まで見送り | `--dangerously-skip-permissions` 前提レシピは停止線と衝突。MVP には過剰 |

## 3. アーキテクチャ

```
CEO（司会・裁定）⇄ ホストチャット（任意の AI: 会話のみ、議事録に触らない）
                      │ PROTOCOL.md に従い書記を起動
                      ▼
                書記（scribe）
                ├─ CC ホスト時: subagent (安いモデル) が scripts/ を実行
                ├─ 他ホスト時: 同スクリプトを直接実行
                ├─ minutes/<議題>.md の追記・整形・ラウンド管理
                ├─ dispatch.py で参加者を呼び出し
                └─ 各発言の要点をホストへ返す
                      ▼
              minutes/<議題>.md（blackboard = 唯一の共有点）
   ▲              ▲              ▲              ▲
 codex exec    claude -p     grok agent      agy -p
 (Codex)       (CC)          (Grok)          (Gemini)
```

## 4. コンポーネント

### 4.1 PROTOCOL.md（本体・AI 非依存の手順書）

ホストが読む司会補佐マニュアル。内容:

1. 議題開始: `minutes/YYYY-MM-DD-<slug>.md` を template から生成
2. 発言依頼: CEO が指名した参加者に dispatch (パスだけ渡す)
3. 発言回収: 参加者は議事録の自セクションに追記。要点をホストがチャットに貼る
4. ラウンド管理: 上限 3 ラウンド。収束 (同結論帯) または反復検知で早期打ち切りを CEO に進言
5. 裁定: CEO の裁定を書記が記録し `status: closed` に

### 4.2 議事録フォーマット（minutes/_template.md）

```markdown
---
topic: <議題>
status: open | closed
round: 1
participants: [codex, cc, grok, agy]
created: YYYY-MM-DD
verdict: (裁定。closed 時に必須)
---

# <議題>

## 背景 (CEO 記入 or 書記が代筆)

## Round 1
### codex (役割: 実装)     ← 発言 + evidence (URL / 実行ログ / 差分)
### cc (役割: 設計・反証)
...

## 裁定 (CEO)
```

規約: 発言には evidence 欄必須 (「同意します」だけは無効)。他者セクションの編集禁止 (追記のみ)。

### 4.3 scripts/dispatch.py（書記の実体）

```
python scripts/dispatch.py <participant> <minutes-path> [--role <役割>] [--round N]
```

- participant → コマンドのマッピング (実機確認済み):
  - `codex`: `codex exec` / 継続 `codex resume`
  - `cc`: `claude -p` / 継続 `--continue`
  - `grok`: `grok agent stdio` / 継続 `grok sessions restore`
  - `agy`: `agy -p` / 継続 `agy --conversation <ID>`
- prompt は file-backed (一時ファイルに組み立て、パス + 短文 pointer を渡す)
- timeout 既定 300s。失敗は議事録に `(dispatch failed: <理由>)` を記録して続行 (fail-soft)
- 会話 ID / session ID を `minutes/.sessions.json` に保存し、同一議題の継続に使う

### 4.4 adapters/（各 AI の薄い入口）

| AI | アダプタ | 中身 |
|---|---|---|
| CC | `adapters/cc-skill/` (/roundtable) | PROTOCOL.md を読み、書記 subagent (sonnet, run_in_background) に scripts 実行を委譲 |
| Codex | `AGENTS.md` | ホスト時の手順 = PROTOCOL.md 参照、の 1 節 |
| Grok / agy | `adapters/<ai>.md` | 同型の参照ノート |

アダプタは「PROTOCOL.md を読め + scripts を使え」以上のことを書かない (drift 防止、SSOT は PROTOCOL.md)。

## 5. データフロー（1 議題 1 周 = MVP の受け入れ基準）

1. CEO: 「議題: X。まず codex の意見」
2. ホスト → 書記: 議事録生成 + `dispatch.py codex minutes/....md --role 実装`
3. Codex: 議事録を読み、自セクションに意見 + evidence を追記
4. 書記: 要点 3 行をホストに返す → ホストがチャット表示
5. CEO: 「cc は反証して」→ 2-4 を繰り返し
6. CEO: 「裁定: Y で行く」→ 書記が verdict 記入、status: closed

## 6. エラー処理

- dispatch 失敗 (CLI 不在 / timeout / 非 0 終了): 議事録に記録して続行。会談を止めない
- 議事録の同時書き込み: 逐次 dispatch のみ (並列発言させない) で構造的に回避
- 書記の暴走防止: 書記は minutes/ 配下と一時ファイル以外に書かない (アダプタで明記)

## 7. テスト

- `dispatch.py`: participant マッピング・prompt 組み立て・fail-soft を pytest (CLI は mock)
- E2E smoke: mock participant (echo スクリプト) で 1 周が回り、議事録が規約どおりになること
- 実 CLI smoke は手動 1 回 (課金があるため CI に入れない)

## 8. ロードマップ

| 版 | 内容 | 完了条件 |
|---|---|---|
| v0.1 (MVP) | template + PROTOCOL.md + dispatch.py (codex, cc) + CC アダプタ | 実議題 1 本が 1 周し議事録が残る |
| v0.2 | grok / agy 追加。得意分野プリセット (Codex=実装 / Grok=逆張り / Gemini=長文読解) | 三者以上の会談 1 本 |
| v0.3 | Codex を MCP 接続へ昇格 (conversation_id 往復)。収束判定の型化 | 往復レイテンシ改善を実測 |

## 9. 境界

- repo は private 起点。GitHub push / 公開は gate 通過 + CEO 明示承認後のみ
- 書記は判断しない。裁定は常に CEO
- `--dangerously-skip-permissions` は使わない
- 各 AI のネイティブ履歴には触らない (読み込みは export 経由のみ)

## 10. 既存資産との関係

- `shared/scripts/multi_ai/`: 「1 プロンプト N 体撒き」型。本 repo は多ラウンド壁打ちで補完関係。
  provider 呼び出しの知見 (timeout / CLI 検出) は dispatch.py 実装時に参照する
- inbox context pack 規約: 本 repo の minutes フォーマットは inbox md と同じ frontmatter 文化に揃える
