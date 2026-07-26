# ai-roundtable 設計書 v2

- 日付: 2026-07-26 (v1 同日。敵対的レビュー + 裏取りリサーチを反映)
- 状態: CEO レビュー待ち
- 前身 context: `~/Projects/Documents/inbox/2026-07-26-ai-roundtable-design-context-for-all-ai.md`
- 調査根拠: `~/Projects/Documents/nexus_ai/research/multi-ai-realtime-linking/report.md`
- v1→v2 の変更根拠: 敵対的レビュー 8 指摘 + 裏取り 14 発見 (§11 参照)

## 1. 目的

Codex / CC (Claude Code) / Grok / Gemini (agy) を Windows 上でリンクし、
**人間 (CEO) が司会**する壁打ち・多者会談を回す基盤。

- 新 UI は作らない。ホストは既存チャット (どの AI でもよい = host-agnostic)
- チャット履歴は各 AI のネイティブ管理のまま。共有するのは議事録だけ
- cmux (Mac 専用) に依存しない

## 2. 決定事項と根拠

| # | 決定 | 根拠 |
|---|---|---|
| D1 | 司会 = 人間固定 | 分散網目型は誤り 17.2 倍 / 中央 orchestrator 型 4.4 倍 (arXiv 2512.08296, 一次ソース確認済み)。echo chamber は司会の指名で防ぐ |
| D2 | 共有点 = 議事録 md 1 枚 (blackboard) | blackboard 方式の有効性は arXiv 2510.01285。全文同期をしない理由は同調圧力対策 (MachineSoM, ACL 2024 系の知見) ※v2 で根拠を差し替え |
| D3 | 書記 = ホストから分離した**別プロセス** | 兼任バイアスの構造排除。v2: 「判断しない」を改め、書記の判断は**要約のみ**に限定と正直に定義 |
| D4 | 書記の機械部分 = 決定的スクリプト (`dispatch.py`) | ホスト間の品質差を最小化。LLM 判断は要約 1 点に閉じ込める (コスト構造上も妥当: 先行実測で orchestrator ループが総コスト 69%) |
| D5 | host-agnostic | プロトコルは `PROTOCOL.md` に置く。**どの AI がホストでも書記は同じ別プロセス起動** (CC の subagent 機能に依存しない) |
| D6 | 参加者は完全対称 + **stateless** | ホスト AI 自身の意見も CLI で他と同列に呼ぶ。v2: セッション継続は使わず毎回新規プロセス + 議事録パスのみ (場外文脈の混入 = 隠れ非対称の排除) |
| D7 | MCP 相互接続は v0.3 まで見送り | `--dangerously-skip-permissions` 前提レシピは停止線と衝突。MVP には過剰 |
| D8 | 収束判定は自動化しない | 「同結論帯」「反復」の判定は判断業務。**打ち切りは常に CEO の宣言** (v2 新設。v1 の「早期打ち切り進言」を削除) |
| D9 | 参加者は議事録に直接書かない | 規約でなく機構で守る: 参加者→スクラッチファイル→書記が機械 merge + diff 検証 (v2 新設) |

## 3. アーキテクチャ

```
CEO（司会・裁定・打ち切り宣言）
  ⇄ ホストチャット（任意の AI: CEO との会話と scripts/ 起動のみ。議事録に触らず、意見も言わない）
        │
        ▼
   書記 = 2 層 (どのホストでも同じ形で別プロセス起動)
   ├─ 機械層: scripts/dispatch.py（決定的。LLM なし）
   │    ├─ 議事録の生成・merge・round 管理・検証
   │    └─ 参加者 CLI の起動・回収
   └─ 要約層: scripts/summarize.py → 固定の安いモデル CLI を別プロセスで呼ぶ
        （書記唯一の LLM 判断。ホストには要約させない）
        ▼
  minutes/<議題>/minutes.md（blackboard = 唯一の共有点）
   ▲              ▲              ▲              ▲
 codex exec    claude -p     grok agent      agy -p
 (Codex)       (CC)          (Grok)          (Gemini)
   └── 各参加者は minutes/<議題>/scratch/<participant>-r<N>.md にのみ書く
```

## 4. コンポーネント

### 4.1 PROTOCOL.md（本体・AI 非依存の手順書）

ホストが読む司会補佐マニュアル。内容:

1. 議題開始: `minutes/YYYY-MM-DD-<slug>/minutes.md` を template から生成
2. **指名はラウンド単位のバッチが基本**: CEO「この周は codex→cc→grok の順で」→ 書記が順に dispatch し、
   ラウンド完了時のみ CEO に戻る (毎ターン指名も可能だが必須にしない)
3. 発言回収: dispatch.py が scratch → minutes.md へ機械 merge、summarize.py の要約をホストがチャットに貼る
4. ラウンド管理: 上限 3。**打ち切り・続行・裁定はすべて CEO の宣言** (自動進言なし)
5. 裁定: CEO の裁定を書記が記録し `status: closed` に。
   **close 前に dispatch 失敗・検証 fail の一覧を CEO に必ず提示** (偽装成功防止)

### 4.2 議事録フォーマット（minutes/_template.md）

```markdown
---
topic: <議題>
status: open | closed
round: 1            # 規則: 指名バッチが一巡完了するたびに書記が +1 (機械更新)
participants: [codex, cc]   # 変更は CEO の明示指示→書記が機械更新 (フリーテキスト解釈しない)
created: YYYY-MM-DD
budget_note: <概算コスト上限。超過見込み時は書記が dispatch 前に警告>
verdict: (裁定。closed 時に必須)
---

# <議題>

## 背景

## Round 1
### codex (役割: 実装)
(発言本文)
**Evidence**: URL / 実行ログ / 差分   ← 空なら dispatch.py が fail 扱い

## 裁定 (CEO)
```

規約 (機構で強制): 参加者が書けるのは自分の scratch ファイルのみ。minutes.md への書き込みは
dispatch.py の merge だけ。merge 後に `git diff` で「追記された 1 セクションのみ変更」を assert。

### 4.3 scripts/dispatch.py（書記・機械層）

```
python scripts/dispatch.py run <topic-dir> --order codex,cc [--round N] [--timeout 600]
```

- participant → コマンド (実機確認済み。ただし DLL 欠如等の無言死があるため smoke 必須):
  - `codex`: `codex exec` / `cc`: `claude -p` / `grok`: `grok agent stdio` / `agy`: `agy -p`
- **stateless**: 毎回新規プロセス。継続機構 (`resume` / `--continue` / `--conversation`) は v0.1 では使わない
- prompt は file-backed (一時ファイル + パス渡し)。**`subprocess` は `encoding="utf-8"` 明示、
  `CREATE_NEW_PROCESS_GROUP` 不使用** (Windows cp1252 / stdin 事故対策)
- timeout 既定 **600s** (先行 OSS the-council の実測 600-900s に合わせ、300s から引き上げ)
- 書き込みは **atomic write (tmp→rename)** + 軽量ロックファイル (`.lock`)。
  Windows は追記の atomic 保証がないため「逐次だから安全」に依存しない
- **出力バリデーション** (すべて fail 扱いで議事録に記録):
  空出力 / 直前ラウンドとの完全一致 / Evidence 欄空 / 非 0 終了 / 無出力サイレント死。
  記録形式: `(dispatch failed: <分類> exit=<code>)`
- fail は fail-soft で続行するが、**close 時に失敗一覧を必ず CEO に提示** (§4.1-5)

### 4.4 scripts/summarize.py（書記・要約層）

- 固定の安いモデルを別プロセス CLI で呼び、参加者発言を 3 行要約 → ホストがチャットに貼る
- どのホストでも同じコマンド = ホストの subagent 機能・ネイティブ性能に依存しない (D5)
- 要約なしモード (`--raw`) も用意 (CEO が生ログを読む運用も許す)

### 4.5 adapters/（各 AI の薄い入口）

| AI | アダプタ | 中身 |
|---|---|---|
| CC | `adapters/cc-skill/` (/roundtable) | PROTOCOL.md を読み scripts/ を実行するだけ |
| Codex | `AGENTS.md` | 同上を指す 1 節 |
| Grok / agy | `adapters/<ai>.md` | 同型の参照ノート |

アダプタは「PROTOCOL.md を読め + scripts を使え」以上を書かない (SSOT は PROTOCOL.md)。
ホストの責務は CEO 対話と scripts 起動のみ。**要約・merge・判断をホストにさせない**。

## 5. データフロー（1 議題 1 周 = MVP の受け入れ基準）

1. CEO: 「議題: X。この周は codex→cc で」
2. ホスト → dispatch.py: 議事録生成 + codex, cc を順に dispatch
3. 各参加者: minutes.md を読み、自分の scratch に意見 + Evidence を書く
4. dispatch.py: scratch → minutes.md へ merge + diff assert。summarize.py が要約
5. ホスト: 要約をチャット表示 → CEO が次ラウンド or 裁定を宣言
6. 裁定 → verdict 記入、失敗一覧提示 → status: closed

受け入れ基準: 実議題 1 本が 1 周し、議事録が規約どおり + **1 議題の概算コストが記録されている**こと。

## 6. エラー処理

- dispatch 失敗: §4.3 のバリデーション分類で記録し続行。close 時に一覧提示
- 同時書き込み: 逐次 dispatch + atomic write + lock の 3 重 (機構で保証、運用前提にしない)
- 複数ホストから同一議題を開いた場合: lock ファイルで検知し後発を拒否
- 書記の暴走防止: dispatch.py は `minutes/<topic>/` 配下と一時ファイル以外に書かない

## 7. テスト

- `dispatch.py`: マッピング / merge + diff assert / バリデーション / atomic write / lock を pytest (CLI は mock)
- E2E smoke: mock participant (echo スクリプト) で 1 周 → 議事録が規約どおり
- 実 CLI smoke (手動 1 回): 各 CLI の起動確認に加え、**無言死の検知** (exit code 記録) と
  **サンドボックス境界の実効性** (参加者が minutes.md を直接書けないこと) を確認
  (先行 OSS で read-only 強制の機能不全が実際に起きている)

## 8. ロードマップ

| 版 | 内容 | 完了条件 |
|---|---|---|
| v0.1 (MVP) | template + PROTOCOL.md + dispatch.py + summarize.py (codex, cc) + CC アダプタ | §5 受け入れ基準 |
| v0.2 | grok / agy 追加。得意分野プリセット。バッチ指名の運用磨き | 三者以上の会談 1 本 + コスト実測記録 |
| v0.3 | セッション継続 (topic ごと store + atomic write) / Codex MCP 昇格 / 並行性の再評価 | 往復レイテンシ改善を実測 |

## 9. 境界

- repo は private 起点。GitHub push / 公開は gate 通過 + CEO 明示承認後のみ
- 書記の判断は要約のみ。裁定・打ち切り・メンバー変更は常に CEO
- `--dangerously-skip-permissions` は使わない
- 各 AI のネイティブ履歴には触らない

## 10. 既存資産との関係 / 先行 OSS

- `shared/scripts/multi_ai/`: 「1 プロンプト N 体撒き」型。本 repo は多ラウンド壁打ちで補完。
  timeout / CLI 検出の知見は dispatch.py で参照
- 先行 OSS 調査 (2026-07-26): 最近傍は the-council (Codex+Gemini 並列 1 ラウンド + Claude 統合)。
  llm-council 系は「並列 1 パス + 自動合意」が主流で、**人間司会×逐次多ラウンドは少数派 = 差別化点**。
  ai-council-framework が「ラウンド上限 3 / evidence 必須 / 証拠なき同意は無効」を独立採用しており思想の裏付け
- 異ベンダー CLI 構成のため「同一モデルの共有バイアス」問題を構造的に回避 (agent-review-panel の既知制限との対比)

## 11. v1→v2 変更ログ (揉みの記録)

| 変更 | 由来 |
|---|---|
| 書記の「判断しない」→「判断は要約のみ」に正直化。収束判定の自動化を削除 (D8) | 敵対的レビュー #1 |
| 書記はどのホストでも別プロセス起動 (CC subagent 依存をやめる) | 同 #2 |
| 参加者 scratch → 機械 merge + diff assert (D9) | 同 #3 |
| v0.1 stateless 化、`.sessions.json` を v0.3 送り | 同 #4, #6 |
| 出力バリデーション + close 時失敗一覧 | 同 #5 |
| round 更新規則 / participants 明示更新 / コスト概算を受け入れ基準に | 同 #7 |
| ラウンド単位バッチ指名 | 同 #8 |
| atomic write + lock (Windows append 非保証) | 裏取り 発見 2, 3 |
| `encoding="utf-8"` / `CREATE_NEW_PROCESS_GROUP` 不使用 | 発見 8 |
| timeout 300s→600s / 無言死検知 / sandbox 実効性 smoke | 発見 4, 9 |
| D2 の conformity bias 根拠を差し替え (2510.01285 からは直接確認できず) | 発見 0 |
