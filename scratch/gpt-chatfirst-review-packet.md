# レビュー依頼: ai-roundtable「chat-first」再設計 (3rd round)

あなたはこの設計の 3 人目のレビュアーです。過去 2 回のレビュー (別セッションの Codex CLI) で
出た指摘は把握不要 — 以下の設計だけを敵対的に評価してください。

## 背景

人間 (CEO) が司会するマルチ AI 壁打ち基盤。過去案は「CLI で AI を呼ぶ」「常駐サーバ + socket」
と進化したが、CEO が要件を訂正した:

- CLI 経由の AI 実行は全面禁止 (codex exec / claude -p / subprocess 呼び出し)
- 席 = アプリ上の専用チャット。CEO が直接読める・会話履歴がアプリに残ることが必須
- dispatcher は AI を実行しない (議題 packet 生成・議事録・状態管理のみ)
- 公式チャット連携 API がなければ v0.1 は人間 relay
- UI 自動化は明示承認なしに使わない

## chat-first 設計 (レビュー対象)

```
CEO（アプリのチャットを直接読む・書く・裁く）
 ├─ Codex アプリ「rt-<議題>-codex」チャット（CEO が作る専用チャット = 席）
 ├─ Claude「rt-<議題>-cc」チャット（同上）
 ├─ (v0.2+) Grok / Antigravity
 │
 │  relay: CEO が packet を貼る（人間 relay。API 確認できた席だけ後日自動化）
 │
 └─ dispatcher（AI を実行しない純ローカルツール / Python）
     ├─ packet 生成:「議事録 minutes.md を読み、契約 JSON を scratch/<uuid>.json に
     │   書け」という短文をクリップボードへ。CEO は席チャットに貼るだけ
     ├─ watcher: scratch にファイルが現れたら schema + hash 検証 → minutes へ
     │   atomic merge → journal 更新 → 次の参加者の packet を用意
     └─ minutes（blackboard）/ journal / round 状態管理
```

- 出力契約: schema 検証 JSON (invocation_id / opinion / claims[] with evidence_type)。
  minutes 描画時に escape、予約見出し（裁定など）は機械管理フィールドのみから描画
- 議事録保護は「防止」でなく「検知」: アプリ agent は FS 権限を持つため minutes 直接
  改変を防げない。merge 前 hash 照合 + git 履歴で検知し fail-closed
- round 上限 3、打ち切り・裁定は CEO 宣言のみ。1 ラウンドの CEO 操作 = 席数ぶんの貼り付け
- v0.1 = Codex + Claude の 2 席、1 議題複数 round が回ること

## 特に攻めてほしい点

1. **人間 relay の運用現実性**: 貼り付け N 回/round は続くのか。packet と回収通知の UX 設計で
   緩和できる範囲はどこまでか
2. **アプリチャット側の前提**: Codex アプリのチャット/タスクが「ローカルの指定パスに JSON を
   書く」を安定して実行できるか。workspace スコープ・権限・失敗モード (書かない/別の場所に
   書く/フォーマット無視) をどう扱うか
3. **watcher 設計の穴**: ファイル出現待ちの timeout、部分書き込み検知、間違った席からの
   応答の識別 (invocation_id を知らない第三者チャットが偶然書くケースは考えなくてよいが、
   packet の貼り間違いは考える)
4. **検知型の議事録保護で十分か**: 防止できない前提で、監査として成立する条件
5. **v0.1 スコープの過不足**: 人間 relay 前提で削れるもの・逆に足りないもの
6. **この設計はそもそも「壁打ちが楽になる」か**: 素朴に 2 つのアプリに自分で聞くのと比べた
   付加価値がどこにあるか、正直に

## 出力形式

- 指摘は「欠陥 / 破綻シナリオ / 修正案」の 3 点セット + severity (HIGH/MED/LOW)
- 「同意します」だけは無効。本質的な問題がなければ「実装に進んでよい」と明言し、残る条件を列挙
- 日本語で
