# ai-roundtable 設計書 v3

- 日付: 2026-07-26 (v2 同日改訂)
- 状態: Codex 2nd レビュー待ち → CEO レビュー
- v2→v3 の変更: (a) Codex 1st レビュー 9 指摘の反映 (b) **席セッション方式** への転換 (CEO 指示:
  参加者は使い捨てプロセスではなく、アプリからも見える常駐の「席」に投げる)
- 前身 context: `~/Projects/Documents/inbox/2026-07-26-ai-roundtable-design-context-for-all-ai.md`
- レビュー履歴: CC 敵対 8 件 (v2 反映) / Web 裏取り 14 件 (v2 反映) / Codex 1st 9 件 (v3 反映, scratch/codex-design-review-reply.md)

## 1. 目的

Codex / CC / Grok / Gemini (agy) を Windows 上でリンクし、**人間 (CEO) が司会**する壁打ち・多者会談を回す基盤。
新 UI は作らない。cmux (Mac 専用) に依存しない。UI 自動化は最終 fallback のみ。

## 2. 決定事項と根拠

| # | 決定 | 根拠 |
|---|---|---|
| D1 | 司会 = 人間固定 | 網目型は誤り 17.2 倍 / 中央型 4.4 倍 (arXiv 2512.08296)。echo chamber は指名で防ぐ |
| D2 | 共有点 = 議事録 (blackboard) | arXiv 2510.01285。全文同期はしない |
| D3 | 書記 = ホストから分離した別プロセス。判断は持たない (v3: 要約層は v0.2 送りで v0.1 の書記は完全に決定的) | 兼任バイアス排除 + Codex#7/#9 |
| D4 | 書記の機械部分 = 決定的スクリプト | ホスト間品質差の最小化。orchestrator ループがコスト 69% という先行実測 |
| D5 | host-agnostic | PROTOCOL.md + scripts が本体。アダプタは薄い参照のみ |
| D6 | **参加者 = 席セッション (persistent seat)** | v2 の stateless 対称を撤回 (CEO 指示)。各 AI に roundtable 専用セッションを 1 つ作り、毎回 resume で投げる。既存の作業チャットには触れない。場外文脈の混入は「人間司会が裁定する」前提で許容するトレードオフと明記 |
| D7 | MCP 相互接続・常駐サーバは v0.2+ | 席 resume で MVP は成立。daemon/serve は latency 改善フェーズで |
| D8 | 収束判定は自動化しない。打ち切り・裁定・メンバー変更は CEO 宣言のみ | CC#1 |
| D9 | 参加者は minutes に直接書かない。**隔離出力 + hash 検証 + 書記 merge** | CC#3 + Codex#1/#2 で強化 |

## 3. アーキテクチャ

```
CEO（司会・裁定・打ち切り宣言）
  ⇄ ホストチャット（任意の AI: CEO 対話と scripts 起動のみ。意見を言わない・要約しない）
        │
        ▼
   書記 = scripts/dispatch.py（決定的。v0.1 に LLM 判断なし）
   ├─ 席セッションへの投げ込み (transport 層)
   ├─ 隔離出力の回収・検証 (hash / schema)・minutes への merge
   └─ journal による round / invocation 管理
        ▼
  minutes/<議題>/minutes.md（blackboard）+ journal.json + scratch/
   ▲              ▲              ▲              ▲
 Codex の席     CC の席        Grok の席      agy の席
 (専用 session   (専用 session   (専用 session   (専用 session
  アプリにも表示)  --continue)     restore)        --conversation)
```

### 3.1 席セッション (seat)

- 各参加者に **roundtable 専用のセッションを 1 つ**作成し、`seats.json` に ID を登録
- dispatch は毎回その席を resume して短文 pointer を投げる:
  「minutes/<議題>/minutes.md を読み、出力契約に従い <隔離出力パス> に JSON で意見を書け」
- **席の writer は dispatch のみ** (single-writer)。CEO がアプリ/TUI で席を開いて読む・
  手動介入するのは自由だが、dispatch 実行中はしない (journal の実行中フラグで検知)
- Codex はセッションストアが CLI/アプリ共有 (実機確認済み) のため、席はアプリのチャット一覧にも
  現れる = 「アプリで見える・触れる」を UI 自動化なしで満たす
- 席が壊れた/消えた場合: 新席を作り seats.json を更新 (履歴は minutes に残っているので損失は文脈のみ)

### 3.2 transport 優先順位

| 優先 | 方式 | 状態 |
|---|---|---|
| 1 | 席 resume: `codex exec resume <id>` / `claude -p --continue 相当` / `grok sessions restore` / `agy --conversation <id>` | v0.1。各コマンドの正確な形は adapter manifest で固定 (Codex#6) |
| 2 | 常駐サーバ: `codex app-server daemon` (制御 socket + JSON-RPC) / `grok agent serve` (WebSocket :2419) / `grok leader` | v0.2+ (latency 改善) |
| 3 | UI 自動化 (Windows-MCP) | 最終 fallback。設計上は存在のみ記す |

## 4. コンポーネント

### 4.1 adapter manifest（Codex#1/#6 対応・機械可読）

`adapters/manifest.json` に参加者ごとの実行契約を固定:

```json
{
  "codex": {
    "argv": ["<フルパス>/codex.cmd", "exec", "resume", "{seat_id}", "{prompt}"],
    "cli_path_note": "同名 CLI が複数存在 (実測)。フルパス必須 + version pin",
    "prompt_transport": "argv",
    "sandbox": ["-s", "workspace-write などバージョンで smoke 確定"],
    "cwd": "{isolated_run_dir}",
    "writable": ["{isolated_run_dir}"],
    "readable": ["{minutes_snapshot}"]
  }
}
```

- `shell=False` の argv 配列のみ。`encoding="utf-8"` 明示。`CREATE_NEW_PROCESS_GROUP` 不使用
- 参加者の cwd は**隔離実行ディレクトリ**。minutes は読み取り用スナップショットのコピーを渡す
  (原本パスを教えない)。出力は隔離 dir 内の 1 ファイルのみ
- negative test 必須: 参加者に「minutes 原本と repo 内の別ファイルを変更しろ」と指示して
  **両方拒否される**ことを smoke で確認 (先行 OSS で read-only 強制の機能不全が実際に発生)

### 4.2 整合性検証（Codex#2 対応: git diff → hash に置換）

- dispatch 開始時: 対象ツリーのパス一覧 + 内容 hash を採取
- 参加者終了直後: 再採取。**許可された隔離出力以外の作成・削除・変更があれば fail-closed**
- minutes 更新: 開始時スナップショット hash と一致する場合のみ、書記が構造化出力から
  新ファイルを生成して `os.replace` (atomic)。git diff は人間向け補助に格下げ

### 4.3 プロセス管理（Codex#3 対応）

- 各 dispatch を **Windows Job Object** に割り当て `KILL_ON_JOB_CLOSE` でツリー kill 保証
- 出力パスは invocation UUID 入り。timeout 後はその UUID を無効化 (遅延書き込みの混入防止)
- timeout 既定 600s。exit code / 無出力サイレント死 (DLL 欠如 exit -1073741515 実例) を分類記録

### 4.4 journal（Codex#4 対応）

`minutes/<議題>/journal.json` (atomic 更新):

- round_id / invocation_id / order / participant ごとの state
  (pending → running → validated → merged / failed) / input hash / output hash
- merge は invocation_id キーで冪等。復旧時は journal と minutes の埋込 ID を照合、
  曖昧なら自動続行せず CEO に提示
- round は「指名バッチ一巡の全 merge 完了」で書記が +1。participants 変更は CEO 明示指示のみ

### 4.5 参加者出力契約（Codex#5/#8 対応）

出力は Markdown 直書きではなく **schema 検証 JSON**:

```json
{
  "invocation_id": "...",
  "opinion": "本文 (markdown 可)",
  "claims": [
    {"claim": "...", "evidence_type": "observed|log|diff|source|argument|none", "evidence": "..."}
  ]
}
```

- minutes 描画時に本文を fenced block / escape で埋め込み、`## 裁定` 等の予約見出しを
  参加者が生成できないようにする。裁定は機械管理フィールドからのみ描画
- evidence は「型が明示されている」ことだけを機械検証。真偽・十分性は未検証と表示
  (非空チェックの形骸化対策)。悪意ある見出し / frontmatter / HTML を fixture にしたテストを用意

### 4.6 要約層 → v0.2 送り（Codex#7/#9 対応）

- v0.1 は `--raw` 相当が既定: ホストは merge 済み minutes の該当セクションをそのまま提示
- v0.2 で要約導入時は: 各行に participant / invocation_id / 原文段落 ID を付け、
  `non-authoritative` 明示、裁定前に原文確認を要求する形で入れる
- コスト記録も v0.1 では `estimate/observed/unknown` の別レコード。unknown でも受け入れ可

### 4.7 adapters/（薄い入口）

各 AI 向け「PROTOCOL.md を読め + scripts を使え」のみ。CC は skill、Codex は AGENTS.md 1 節、
Grok / agy は参照ノート。**要約・merge・判断をホストにさせない**。

## 5. データフロー（1 議題 1 周 = MVP 受け入れ基準）

1. CEO: 「議題: X。この周は codex→cc で」
2. dispatch.py: minutes 生成 → codex の席へ pointer 投げ (隔離 dir + スナップショット準備)
3. Codex (席の文脈で): minutes スナップショットを読み、隔離 dir に JSON 出力
4. dispatch.py: hash 検証 → schema 検証 → minutes へ atomic merge → journal 更新 → cc も同様
5. ホスト: merge 済みセクションを提示 → CEO が次ラウンド or 裁定宣言
6. 裁定 → verdict 記入 (機械フィールド)、失敗一覧提示 → status: closed

受け入れ基準: 実議題 1 本が 1 周し、(a) negative test 通過済みの隔離で (b) journal が
全 invocation を追跡し (c) 議事録が規約どおりであること。

## 6. エラー処理

- dispatch 失敗分類: 空出力 / schema 違反 / hash 違反 / timeout / 非 0 / 無出力サイレント死。
  すべて journal + minutes に記録し fail-soft 続行、close 前に一覧提示
- 席セッション消失: 新席作成 + seats.json 更新
- 複数ホスト同時起動: journal の running フラグ + lock で後発拒否
- 書記の書き込み範囲: `minutes/<議題>/` と隔離 run dir のみ

## 7. テスト

- unit: manifest 解決 / hash 検証 / schema 検証 / journal 冪等 merge / atomic write / lock
- E2E (mock): echo participant で 1 周 + **各境界での強制終了 → 再実行** (journal 復旧)
- adversarial fixture: 予約見出し偽装 / minutes 直接改変 / 遅延書き込み / 未追跡ファイル作成
- 実 CLI smoke (手動): 席 resume の文脈継続確認 / sandbox 実効性 (negative test) /
  アプリ画面への反映有無の確認 (未検証事項)

## 8. ロードマップ

| 版 | 内容 | 完了条件 |
|---|---|---|
| v0.1 | manifest + dispatch.py (席 resume, codex+cc) + journal + 出力契約 + CC アダプタ | §5 受け入れ基準 |
| v0.2 | grok / agy 席追加。要約層 (参照付き)。コスト実測。常駐サーバ transport 検証 | 三者会談 1 本 + コスト記録 |
| v0.3 | app-server daemon / serve への transport 昇格。並行性の再評価 | latency 改善を実測 |

## 9. 境界

- repo は private。GitHub push / 公開は gate + CEO 承認後のみ
- 裁定・打ち切り・メンバー変更は常に CEO。書記に判断なし (v0.1)
- `--dangerously-skip-permissions` 不使用。参加者 sandbox は manifest で明示 (Codex 既定
  `danger-full-access` を実測済みのため、既定に依存しない)
- 既存の作業チャット・ネイティブ履歴には触れない (席は専用新設)

## 10. 未検証事項 (実装前スパイクで確認)

1. `codex exec resume <id>` が別プロセス並行時にセッションを破損しないか (席は single-writer 運用だが要確認)
2. CLI から席に追記した内容がアプリ画面に即時反映されるか (反映されなくても設計は成立)
3. `grok sessions restore` / `agy --conversation` の非対話モードでの挙動
4. codex exec resume 時の sandbox フラグの効き方 (negative test で確認)

## 11. レビュー反映ログ

v1→v2: CC 敵対 8 件 + Web 裏取り 14 件 (詳細は git 履歴の v2 §11)
v2→v3:
| 変更 | 由来 |
|---|---|
| adapter manifest + 隔離実行 dir + negative test | Codex#1, #6 |
| git diff → hash 検証 (fail-closed) | Codex#2 |
| Job Object ツリー kill + invocation UUID | Codex#3 |
| journal + 冪等 merge + クラッシュ復旧 | Codex#4 |
| 出力を schema 検証 JSON に / 予約見出し防御 | Codex#5 |
| evidence 型付け | Codex#8 |
| 要約層・コスト警告を v0.2 送り / v0.1 は raw 既定 | Codex#7, #9 |
| 席セッション方式 (stateless 撤回) / transport 優先順位 | CEO 指示 2026-07-26 |
