# ai-roundtable 設計書 v5 — chat-first architecture

- 日付: 2026-07-27
- 状態: **CEO GO 済み (2026-07-27)** → 実装計画へ
- v4→v5: CEO 要件訂正 (2026-07-26) による全面転換。CLI での AI 実行を全面禁止し、
  席をアプリの実チャットに置く。relay は 3 段自動化、人間は判断のみ (CEO GO 2026-07-27)
- レビュー履歴: CC 敵対 8 / Web 裏取り 14 / Codex 1st 9 / Codex 2nd 6。
  v4 (socket/daemon 案) は採用前に superseded、git 履歴に保全

## 0. 要件 (CEO 確定)

- **禁止**: `codex exec` / `claude -p` / subprocess 経由の AI 呼び出し / CLI セッションを席に
  使うこと / CLI 性能前提の smoke・実装
- **必須**: 席 = アプリ上の専用チャット。CEO が直接読める。会話履歴がアプリに残る。
  dispatcher は AI を実行せず、議題 packet・議事録・状態管理のみ担当。
  公式チャット連携 API がなければ人間 relay。UI 自動化は明示承認なしに使わない
- **人間の役割は判断のみ**: 議題・指名・裁定・打ち切り・Tier2 承認・任意の割り込み。
  コピー機仕事 (貼り付け) は自動化で排除する

## 1. 目的

Codex / CC / Grok / Gemini を Windows 上でリンクし、人間 (CEO) が司会するマルチ AI
壁打ち・多者会談基盤。新 UI なし。cmux 非依存。

## 2. 決定事項

| # | 決定 | 根拠 |
|---|---|---|
| D1 | 司会 = 人間。裁定・打ち切り・指名・メンバー変更は CEO 宣言のみ | 網目 17.2x / 中央 4.4x (arXiv 2512.08296) + CC#1 |
| D2 | 共有点 = 議事録 (blackboard)。チャット全文同期はしない | arXiv 2510.01285 |
| D3 | **席 = アプリの実チャット** (議題ごと・参加者ごとに CEO が作成: `rt-<議題>-<ai>`) | CEO 要件。議題間の履歴混入 (Codex2nd#1) も議題別チャットで構造回避 |
| D4 | **dispatcher は AI を実行しない** — packet 生成・回収検証・議事録/journal 管理のみの決定的ツール | CEO 要件 |
| D5 | **relay 3 段自動化** (§4)。人間 relay は縮退運転 | CEO GO 2026-07-27 |
| D6 | 出力契約 = schema 検証 JSON + 予約見出し防御。裁定は機械管理フィールドのみから描画 | Codex1st#5 |
| D7 | 議事録保護は「防止」でなく「検知」(merge 前 hash 照合 + git、fail-closed) | アプリ agent の FS 権限は制御外。Codex1st#2 の hash 方式を継承 |
| D8 | Evidence 型付け (observed/log/diff/source/argument/none)。自己申告であり検証済み表示にしない | Codex1st#8 |
| D9 | round 上限 3。収束の自動判定はしない | CC#1 + ai-council-framework の独立採用例 |
| D10 | v0.1 に要約層・コスト警告を入れない (raw 提示)。v0.2 で non-authoritative + 原文参照付きで導入 | Codex1st#7/#9 |

## 3. アーキテクチャ

```
CEO（判断のみ: 議題・指名・裁定・打ち切り・割り込み）
 │
 ├─ Codex アプリ「rt-<議題>-codex」チャット（席。履歴はアプリに残る）
 ├─ Claude   「rt-<議題>-cc」セッション（席。同上）
 ├─ (v0.2+) Grok / Antigravity の席
 │      ▲ relay (§4: Tier1 自動 / Tier2 UI 自動化・要承認 / Tier3 人間)
 │      │
 └─ dispatcher（AI を実行しない決定的ツール / Python）
     ├─ packet 生成:「minutes を読み、契約 JSON を scratch/<uuid>.json に書け」
     ├─ relay 層: 席へ packet を届ける (Tier1/2) or クリップボード (Tier3)
     ├─ watcher: scratch 出現 → schema + hash 検証 → minutes へ atomic merge → journal
     └─ minutes / journal / round 状態管理
          ▼
   minutes/<議題>/ … minutes.md + journal.json + scratch/ + snapshot/
```

## 4. relay 層 (v5 の核心)

| Tier | 方式 | 対象 (v0.1 時点の候補) | 人間の関与 | 前提 |
|---|---|---|---|---|
| 1 | **公式 API 直結** — AI を起動せず、アプリと同じ会話ストアの席にメッセージを届ける | Codex: `app-server daemon` JSON-RPC (`thread/start·resume·read·list` / `turn/start·interrupt` — スキーマ実在確認済み) / CC: Claude Code Desktop のセッション間 send_message (公式ハーネス機能) | ゼロ | **spike で go/no-go** (§8)。特に「席がアプリ UI に見えるか」 |
| 2 | UI 自動化 (Windows-MCP 等) | 公式 API のないアプリ | CEO の明示承認後のみ有効化 | 承認は席 (アプリ) 単位で記録 |
| 3 | 人間 relay (クリップボード → 貼り付け) | Tier1/2 不成立の席 | 貼り付け | 常に利用可能な縮退運転。v0.1 の受け入れはこの経路でも成立すること |

- Tier1 の位置づけ: CEO 禁止事項は「CLI で AI を**実行**する」こと。Tier1 はプロセスを
  起こさず、既存の席 (アプリで可視) へ turn を送るだけであり、履歴もアプリに残る
- relay 層は adapter として分離し、席ごとに `tier` を seats.json に記録。
  Tier1 障害時は自動で Tier3 に縮退し、その旨を CEO に表示 (勝手に Tier2 へ昇格しない)

## 5. 実行フロー (1 議題)

1. CEO:「議題 X。この周は codex→cc」(以後のラウンドも同順なら再指名不要)
2. dispatcher: minutes 生成 → codex の席へ packet relay (Tier1 なら全自動)
3. 席の AI: minutes (読み取り用スナップショット) を読み、契約 JSON を scratch へ書く
4. watcher: 検証 (schema / invocation_id 一致 / minutes 原本 hash / JSON parse) →
   atomic merge → journal 更新 → 次の参加者へ relay
5. ラウンド完了 → CEO に要点着信 (v0.1 は raw セクション提示) → 続行 or 裁定を宣言
6. 裁定 → verdict 記録 (機械フィールド)、失敗一覧提示 → status: closed

- 貼り間違い対策 (Tier3): packet に invocation_id が入っており、watcher は id 不一致の
  出力を「別 invocation の混入」として隔離・報告する
- 部分書き込み対策: 出力契約に「tmp に書いてから rename」を含める + watcher 側で
  JSON parse 失敗は grace period 内リトライ後に fail

## 6. journal / 状態機械

```
prepared → delivered(tier 記録) → output-received → validated → merged / failed
```

- Tier3 の delivered は「クリップボード搬出済み」であり席着信は未知 — 未着のまま
  timeout したら「未貼り付け?」として CEO に確認 (自動再送しない)
- merge は invocation_id で冪等。round は指名バッチ全 merge で +1 (機械更新)
- 失敗分類: 空出力 / schema 違反 / id 不一致 / hash 違反 / timeout / parse 失敗。
  close 前に失敗一覧を必ず CEO に提示 (偽装成功防止)

## 7. seats.json (簡素化)

```json
{
  "rt/<topic>/codex": {
    "participant": "codex", "topic": "...",
    "surface": "codex-app", "tier": 1,
    "thread_ref": "(Tier1 のみ: thread id 等)",
    "created_at": "...", "note": "CEO が作成したチャット名"
  }
}
```

- v4 の generation / rotation / seat lock は**廃止** (議題別チャット + 人間司会が
  構造的に代替。肥大したら CEO が新チャットを作って seats.json を更新するだけ)

## 8. spike (実装前 go/no-go、各 30 分級)

1. **Codex Tier1**: `app-server daemon` 起動 → `thread/start` + `turn/start` →
   (a) アプリ一覧に席が見えるか (b) turn がアプリで読めるか (c) 席の agent が
   scratch へファイルを書けるか。× なら Codex は Tier3 で v0.1 開始
2. **CC Tier1**: CCD send_message で別セッション (席) に packet を届けられるか。
   × なら Tier3
3. spike の結果 (可否・制約) は docs/spike-results.md に記録し、seats.json の tier に反映

## 9. テスト

- unit: packet 生成 / watcher 検証 (schema・id・hash・parse) / journal 冪等 / atomic write
- E2E (mock): 手動でファイルを置く「模擬席」で 1 議題複数 round + 強制中断からの復旧
- adversarial fixture: 偽装見出し / minutes 直接改変 (検知確認) / id 不一致出力 / 部分書き込み
- 実席 smoke (手動 1 回): Tier3 経路で codex + cc の実アプリを使い 1 周

## 10. v0.1 受け入れ基準

codex + cc の 2 席 (tier は spike 結果に従う。**全席 Tier3 でも合格可**):

1. 実議題 1 本が複数 round 回り、議事録が契約どおり
2. 失敗 (未応答 timeout / 契約違反) が隠れず記録・提示される
3. minutes 直接改変を watcher が検知して fail-closed する (adversarial テスト)
4. CEO の操作が「議題・指名・(Tier3 なら貼り付け)・裁定」だけで完結する
5. 全チャット履歴がアプリ側に残っている

## 11. ロードマップ

| 版 | 内容 |
|---|---|
| v0.1 | dispatcher (packet/watcher/journal) + spike 反映済み relay + codex/cc 2 席 |
| v0.2 | Grok / Gemini 席追加。要約層 (non-authoritative)。コスト記録。Tier2 の承認フロー |
| v0.3 | relay の完全自動化拡大 (公式 API の拡充追随)、議題テンプレ・得意分野プリセット |

## 12. 境界

- repo private。push / 公開は gate + CEO 承認
- dispatcher は `minutes/<議題>/` と一時領域以外に書かない
- UI 自動化 (Tier2) は席単位の CEO 明示承認を seats.json に記録してから
- 既存の作業チャットに触れない。席は専用新設
- `--dangerously-skip-permissions` 不使用

## 13. レビュー反映ログ (v5)

| 継承 | 由来 |
|---|---|
| 出力 JSON 契約 / 予約見出し防御 / escape 描画 | Codex1st#5 |
| hash 検知 fail-closed (防止でなく検知に再定義) | Codex1st#2 + chat-first 制約 |
| journal 冪等 / 失敗一覧提示 | Codex1st#4, CC#5 |
| Evidence 型付け | Codex1st#8 |
| round 規則 / CEO 宣言のみ | CC#1/#7 |
| 議題別席 (混入の構造回避) | Codex2nd#1 の解を chat-first で実現 |
| **廃止**: seat lock / generation rotation / session 状態機械 / transport manifest / Job Object | v4 の CLI/socket 前提が消えたため |
| relay 3 段 + 人間は判断のみ | CEO 2026-07-27 |
