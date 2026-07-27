# ai-roundtable 設計書 v4

- 日付: 2026-07-26 (v3 同日改訂)
- 状態: CEO 最終レビュー待ち (Codex 2nd レビューの実装開始 5 条件を反映済み)
- v3→v4: (a) CEO 決定「CLI は基本使わない」= transport を常駐サーバ + socket に統一
  (b) Codex 2nd レビュー (scratch/codex-design-review2-reply.md) の 5 条件反映
- レビュー履歴: CC 敵対 8 / Web 裏取り 14 / Codex 1st 9 / Codex 2nd 6 (+実装開始 5 条件)

## 1. 目的

Codex / CC / Grok / Gemini を Windows 上でリンクし、**人間 (CEO) が司会**する壁打ち・多者会談基盤。
CEO の動線はアプリのみ。**単発 CLI プロセス起動は配管にも使わない (CEO 決定)**。
新 UI なし。cmux 非依存。UI 自動化は設計上の最終 fallback として記載のみ。

## 2. 決定事項

| # | 決定 | 根拠 |
|---|---|---|
| D1 | 司会 = 人間固定 | 網目 17.2x / 中央 4.4x (arXiv 2512.08296) |
| D2 | 共有点 = 議事録 (blackboard)。全文同期しない | arXiv 2510.01285 |
| D3 | 書記 = 決定的スクリプト (v0.1 に LLM 判断なし)。要約層は v0.2 | CC#1, Codex1st#7/#9 |
| D4 | host-agnostic。アダプタは薄い参照のみ | — |
| D5 | **transport = 常駐サーバ + socket** (単発 CLI 起動禁止) | CEO 決定 2026-07-26 |
| D6 | **席 = `participant × topic × generation`** | Codex2nd#1 (全議題共有席は履歴経由の漏洩・injection 持続を防げない) |
| D7 | 席の排他 = **seat 単位 OS lock** (lease/heartbeat + stale 回復) | Codex2nd#2 |
| D8 | 収束判定・打ち切り・裁定・メンバー変更 = CEO 宣言のみ | CC#1 |
| D9 | 参加者は minutes に直接書かない。隔離出力 + hash 検証 + 書記 merge | CC#3 + Codex1st#1/#2 |
| D10 | **session-committed 不明の失敗は自動 retry 禁止** | Codex2nd#3 |

## 3. アーキテクチャ

```
CEO（アプリのみ触る。司会・裁定・打ち切り）
  ⇄ ホストチャット（任意の AI。CEO 対話と scripts 起動のみ）
        │
        ▼
   書記 = scripts/dispatch.py（決定的）
   ├─ transport 層: 常駐サーバへの socket 接続 (§3.2)
   ├─ seat 管理: seats.json + seat lock (§3.1, §3.3)
   ├─ 回収・検証: 隔離 dir hash / schema JSON (§4)
   └─ journal: round / invocation 状態機械 (§4.4)
        ▼
  minutes/<議題>/ … minutes.md + journal.json + scratch/ + snapshot/
```

### 3.1 席 (seat) — `participant × topic × generation`

- 議題ごとに各参加者の席を新設。命名: `rt/<topic-slug>/<participant>#g<N>`
  (アプリの一覧で判別できる表示名。thread/name で設定)
- 全議題共通の人格・規約は席の履歴に頼らず、**version/hash 固定の bootstrap fixture** を
  席作成時に注入 (compaction で消えても再注入で復元可能)
- 「議題をまたいで文脈を育てたい」場合は CEO の明示指示 + 機密区分が同じ議題のみ
  (v0.1 の既定にはしない)
- 席の writer は dispatch のみ。CEO のアプリからの手動送信は「dispatch 実行中以外」が原則。
  機械排他はできないため、**dispatch 前後に thread/read で席 revision を照合し、
  予期しない追記を検知したら fail-closed** (検知不能な transport は残存リスクとして明示)

### 3.2 transport (D5: 常駐サーバ + socket のみ)

| 席 | サーバ | プロトコル | 主要操作 | 状態 |
|---|---|---|---|---|
| Codex | `codex app-server daemon` (制御 socket) | JSON-RPC (schema 生成済み・実機確認) | `thread/start` `thread/resume` `turn/start` `thread/read` `thread/list` `turn/interrupt` | v0.1 |
| CC | `claude -p --input-format stream-json --output-format stream-json` の**長寿命プロセス 1 本** | stream-json (stdin/stdout 常時接続) | 起動時に席開始、以後 turn を stream | v0.1 |
| Grok | `grok agent serve` (WebSocket 127.0.0.1:2419 + secret) / leader socket | WebSocket | spike で確定 | v0.2 |
| Gemini (agy) | サーバモード未確認 | — | 調査 | v0.2 |

- daemon / serve の起動は環境セットアップ (1 回) として scripts/setup_daemons.py に分離。
  dispatch は接続のみ行い、プロセスを起動しない
- **go/no-go gate (Codex2nd#5 の昇格)**: 各 transport は (a) 正常往復 (b) sandbox/権限の
  実効性 negative test (c) timeout 挙動 (d) 同席競合拒否、の 4 点を実測して版数 pin
  できなければ **その transport を v0.1 から外す**
- 未確認事項: GUI アプリが同じ daemon に接続するか / daemon 経由 thread がアプリ一覧に
  出るタイミング (§10)

### 3.3 seats.json (Codex2nd#4)

atomic 更新 (tmp→rename) + 変更履歴保持。schema:

```json
{
  "rt/<topic>/codex#g1": {
    "thread_id": "...", "participant": "codex", "topic": "...", "generation": 1,
    "created_at": "...", "last_used_at": "...",
    "transport": {"kind": "codex-app-server", "version": "..."},
    "bootstrap_hash": "...", "invocations": {"ok": 0, "failed": 0},
    "last_seen_revision": "...", "state": "active | rotating | broken | archived"
  }
}
```

- **rotation**: 最大 invocation 数 or 観測可能な context 指標の閾値で新 generation へ計画移行。
  新席は bootstrap fixture + 現議題 minutes のみから作る。指標が取れない transport は
  `unknown` と記録し soak test で保守的上限を決める
- **broken 判定は機械判定可能な状態のみ** (not-found / store corruption / 契約連続違反)。
  auth / network / rate limit / 単発のモデル失敗は再作成理由にしない

### 3.4 seat lock (Codex2nd#2)

- `seats/locks/<seat-id>.lock` に owner PID / host / invocation_id / 取得時刻 / lease
- topic をまたいで seat 単位で排他。取得〜session 保存完了まで保持
- stale 回復: lease 失効 + PID 生存確認で解放。回復操作は journal に記録

## 4. 実行契約

### 4.1 隔離実行 (Codex1st#1/#6 + 2nd#5)

- 参加者への入力: minutes の**読み取り専用スナップショットのコピー** (原本パス非開示) +
  出力契約 + 隔離出力パス (invocation UUID 入り)
- transport manifest は「完全な操作 template + 期待 capability」を版数ごとに固定
  (JSON-RPC のメソッド/パラメタ、または stream-json の turn 形式)
- 検証は隔離 dir **全体** の前後 hash (出力 UUID ファイル以外の作成・変更・削除も fail)。
  snapshot 改変・絶対パスでの repo 読み書きを negative test に含める

### 4.2 整合性 (Codex1st#2)

- 開始/終了時にツリー hash 採取、fail-closed。対象 root・除外なし・symlink/reparse point の
  扱いを実装仕様で固定。minutes 更新は snapshot hash 一致時のみ `os.replace`。
  git diff は人間向け補助

### 4.3 プロセス/接続管理 (Codex1st#3)

- v0.1 の常駐プロセス (daemon / CC stream) は Job Object 管理 + 監視。dispatch 自体は
  socket 通信のみで子プロセスを作らない (遅延書き込み問題は turn 単位の UUID 無効化で対処)
- timeout: turn 単位 600s。`turn/interrupt` が使える transport は interrupt → 状態照会

### 4.4 journal + transport 状態機械 (Codex1st#4 + 2nd#3)

```
prepared → submitted → session-committed? → output-received → validated → merged
                          │ unknown
                          ▼
      自動 retry 禁止。thread/read 等で照会 → 不能なら CEO に 3 択提示:
      「現席で重複許容して再送」「新 generation 作成」「failed のまま閉じる」
```

- merge は invocation_id で冪等。復旧時は journal × minutes 埋込 ID × 席 revision を照合
- round は「指名バッチ全 merge 完了」で +1。participants 変更は CEO 明示指示のみ

### 4.5 参加者出力契約 (Codex1st#5/#8)

- schema 検証 JSON (invocation_id / opinion / claims[]、evidence_type =
  observed|log|diff|source|argument|none)。evidence_type は自己申告であり
  「検証済み」表示にしない
- minutes 描画は escape + 予約見出し防御。裁定は機械管理フィールドのみから描画。
  悪意 fixture (偽装見出し / frontmatter / HTML) をテストに含める

## 5. v0.1 受け入れ基準 (Codex2nd#6 で全面改訂)

codex + cc の 2 席で:

1. `participant × topic` 席で同一議題を**複数 round** 実行し、文脈継続と invocation 対応を確認
2. 同一 seat への 2 dispatch を競合させ、後発が実行前に拒否される
3. 保存境界での強制終了 → unknown commit が**自動再送されない**ことを確認
4. 保守的 rotation 上限までの soak + 新 generation への明示移行を確認
5. 両 transport の go/no-go gate 4 点 (正常 / negative / timeout / 競合) を実測し版数 pin を保存
6. アプリ可視性を「席が一覧に存在する」「追記が即時表示される」に分けて記録。
   後者が unknown/false なら UX 制約として CEO に提示

## 6. エラー処理

- 失敗分類: 空出力 / schema 違反 / hash 違反 / timeout / 接続断 / session-commit 不明 /
  サーバ死亡。すべて journal + minutes に記録。close 前に失敗一覧を CEO に必ず提示
- daemon / serve 死亡: dispatch は fail し、setup_daemons.py の再起動を CEO に提案
  (dispatch が勝手にプロセスを起こさない = D5 維持)

## 7. テスト

- unit: seats schema / seat lock (stale 回復含む) / hash / schema 検証 / journal 冪等
- E2E (mock transport): 1 議題複数 round + 各境界強制終了 → 復旧
- adversarial: 偽装見出し / snapshot 改変 / 同席競合 / 遅延 turn / 別議題への履歴漏洩 canary
- 実 transport smoke (手動): §5-5 の go/no-go gate

## 8. ロードマップ

| 版 | 内容 | 完了条件 |
|---|---|---|
| v0.1 | setup_daemons + dispatch (codex app-server, cc stream-json) + seat/journal/出力契約 + CC アダプタ | §5 の 6 項目 |
| v0.2 | grok serve / agy 調査・追加。要約層 (non-authoritative + 原文参照)。コスト記録 | 三者会談 1 本 |
| v0.3 | latency 改善 (turn/steer 等)。議題横断席 (機密区分ポリシー付き) の検討 | 実測 |

## 9. 境界

- repo private。push / 公開は gate + CEO 承認
- 裁定は常に CEO。書記に判断なし (v0.1)
- 単発 CLI 起動は配管にも使わない (D5)。`--dangerously-skip-permissions` 不使用
- 既存の作業チャット・ネイティブ履歴に触れない。席は専用新設 (D6)

## 10. 未検証事項 (spike = go/no-go gate)

1. codex app-server daemon: GUI アプリと同一 daemon か / thread のアプリ一覧反映 /
   turn 単位の sandbox・権限指定の可否 / thread/read の revision 意味論
2. CC stream-json 長寿命プロセス: session 維持・turn 境界・timeout 挙動
3. 同一席並行 dispatch の実挙動 (lock 突破時に何が起きるか)
4. grok serve / agy (v0.2 gate)

## 11. レビュー反映ログ

v1→v2: CC 敵対 8 + Web 裏取り 14 / v2→v3: Codex 1st 9 + 席セッション化 (詳細は git 履歴)
v3→v4:
| 変更 | 由来 |
|---|---|
| transport を常駐サーバ + socket に統一。単発 CLI 起動を配管からも排除 | CEO 決定 (B) |
| 席を participant × topic × generation に分割。bootstrap fixture | Codex2nd#1 |
| seat 単位 OS lock + lease + stale 回復 | Codex2nd#2 |
| session-committed 不明 → 自動 retry 禁止 + CEO 3 択 | Codex2nd#3 |
| seats.json schema / rotation / broken 判定の機械化 | Codex2nd#4 |
| transport go/no-go gate (通らない transport は v0.1 から外す) | Codex2nd#5 |
| 受け入れ基準を席状態機械の 6 試験に全面改訂 | Codex2nd#6 |
