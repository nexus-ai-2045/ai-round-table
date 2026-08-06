# ai-roundtable 設計書 v6 — chat-first architecture

- 日付: 2026-07-28 (v5: 2026-07-27 CEO GO)
- 状態: 実装計画あり (docs/plans/) → Task 1 開始待ち
- v5→v6: 追加調査・実測の統合 (機能変更なし):
  Windows での app-server 実測 / ACP 収束戦略 / Gemini の Tier1 候補昇格 /
  fs API による検証補助 / 先行事例ポジショニング。詳細根拠は references/ と
  `~/Projects/Documents/nexus_ai/research/multi-ai-roundtable-prior-art/report.md`
- レビュー履歴: CC 敵対 8 / Web 裏取り 14 / Codex 1st 9 / Codex 2nd 6 /
  実装計画への内部コードレビュー 13 (計画に反映済み)。
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

| Tier | 方式 | 対象 (2026-07-28 実測反映) | 人間の関与 | 前提 |
|---|---|---|---|---|
| 1 | **公式 API 直結** — AI を起動せず、アプリと同じ会話ストアの席にメッセージを届ける | 下の「席別 Tier1 経路」参照 | ゼロ | **spike で go/no-go** (§8)。4 席共通の残検証点 = 「プログラムから投げた会話がアプリ画面に出るか」 |
| 2 | UI 自動化 (Windows-MCP 等) | 公式 API のないアプリ | CEO の明示承認後のみ有効化 | 承認は席 (アプリ) 単位で記録 |
| 3 | 人間 relay (クリップボード → 貼り付け) | Tier1/2 不成立の席 | 貼り付け | 常に利用可能な縮退運転。v0.1 の受け入れはこの経路でも成立すること |

### 席別 Tier1 経路 (実測・調査済み 2026-07-26〜28)

| 席 | 経路 | 状態 | 出典 |
|---|---|---|---|
| Codex | **自前 spawn の `codex app-server` + stdio JSONL** (JSON-RPC 2.0)。**spike で実測できたのは** `initialize`→`initialized`(params なしの裸通知)→`thread/start`→`thread/name/set`→`turn/start`→`turn/completed` の 6 メソッドのみ。`thread/resume·read·list` / `turn/steer·interrupt` は README 記載だが未実測のため実装しない (`relay_codex.py` は呼ばれたら `NotImplementedError` で明示的に落とす)。VS Code 拡張・デスクトップアプリと同一プロトコル (stable 扱い)。**daemon 常駐管理は Unix 専用 (実測) のため使わない**。sandbox はリクエスト側 hyphen 表記 (`"workspace-write"`) / レスポンス側 camelCase。`thread/name/updated` の params は `threadName` (README の `name` ではない) | **spike 実測 GO (2026-08-05〜06, 2 run)**: handshake〜`turn/completed` が両 run とも成立、サーバー発リクエスト (承認要求) 0 件、terminate 後の孤児プロセスなし。実装 (`roundtable/relay.py` / `roundtable/relay_codex.py`) 済み・単体テスト済み。`dispatch` への配線済み (`cli._deliver` → `get_relay`、Tier1 失敗時は Tier3 へ縮退)。**既定 tier は 3** で、Tier1 は `dispatch --tier 1` の明示操作でのみ発動する (fail-safe)。実席での往復は 0 回 = 運用未検証 | `roundtable/relay_codex.py` (spike 実測事実をモジュール docstring に転記済み) / references/codex-app-server-README.ja.md (全文訳) |
| CC | ホストなので relay 不要。参加者としては CCD セッション間 send_message | v0.1 spike 対象 | ハーネス公式機能 |
| Grok | `grok agent serve` (WebSocket :2419 + secret) / `grok leader` (`~/.grok/leader.sock`, 複数 client で 1 backend 共有) / **ACP** | v0.2 spike | references/grok-build-integration.ja.md |
| Gemini | **Antigravity 経由 3 経路**: 公式 Python SDK (`google.antigravity`) / コミュニティ ACP ラッパ (antigravity-acp) / agy への ACP native 実装 (公式 feature request 中)。agy 素体に serve 系なし (実測) | v0.2 spike (v5 の「✗」から昇格) | docs/architecture-ideal-vs-actual.md |

### ACP 収束戦略 (v6 追加)

gemini-cli / claude は `--acp` 実装済み、Grok Build は ACP 対応、agy は公式 request 中 —
**ACP (Agent Client Protocol) が 4 者共通の統一接続口に収束する可能性が高い**。
方針: v0.1 の relay adapter は席別実装で作るが、**interface を「席に text を届け、
出力を回収する」1 契約に絞っておき、ACP が揃った時点で adapter を 1 本に置換できる形**にする。

- Tier1 の位置づけ: CEO 禁止事項は「CLI で AI を**実行**する」こと。Tier1 はプロセスを
  起こさず、既存の席 (アプリで可視) へ turn を送るだけであり、履歴もアプリに残る
- relay 層は adapter として分離し、席ごとに `tier` を seats.json に記録。
  Tier1 障害時は自動で Tier3 に縮退し、その旨を CEO に表示 (勝手に Tier2 へ昇格しない)
- 補助発見: Codex app-server は `fs/readFile` / `fs/watch` 等の FS API も持つ —
  scratch 出力の検証・監視を app-server 側からも行える可能性 (spike 1 の観察項目)

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

1. **Codex Tier1** (v6 更新: daemon でなく**自前 spawn + stdio**): `codex app-server` を
   spawn → `initialize`/`initialized` handshake → `thread/start` (+ `thread/name` で
   `rt-spike-codex` 命名) → `turn/start` → (a) アプリ一覧に席が見えるか (b) turn が
   アプリで読めるか (c) 席の agent が scratch へファイルを書けるか
   (d) `fs/readFile`/`fs/watch` で scratch 検証を補助できるか。× なら Codex は Tier3 で v0.1 開始
2. **CC Tier1**: CCD send_message で別セッション (席) に packet を届けられるか。
   × なら Tier3
3. spike の結果 (可否・制約) は docs/spike-results.md に記録し、seats.json の tier に反映
4. (v0.2) **ACP 統一 spike**: Grok Build の ACP 接続で席が成立するか。成立すれば
   relay adapter の 1 本化 (§4 ACP 収束戦略) を前倒し

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
| v0.2 | Grok (serve/leader/ACP) / Gemini (Antigravity SDK or ACP) 席追加。要約層 (non-authoritative)。コスト記録。Tier2 の承認フロー。**blind-review round** (llm-council の匿名相互レビュー段階の輸入 — 司会が宣言する round 種別として) |
| v0.3 | ACP 統一 adapter への置換 (§4)、議題テンプレ・得意分野プリセット |

### 命名・ポジショニング (v6 追加, public 化時)

- 「council」を自称しない (Karpathy llm-council = 自動 1 パス合議として定着済み)。
  README 冒頭 3 行で差分を明示: **人間が座長 / アプリ課金のまま動く (API キー不要) /
  議事録が監査可能な成果物**。詳細: references/llm-council-pattern.ja.md

## 12. 境界

- repo private。push / 公開は gate + CEO 承認
- dispatcher は `minutes/<議題>/` と一時領域以外に書かない
- UI 自動化 (Tier2) は席単位の CEO 明示承認を seats.json に記録してから
- 既存の作業チャットに触れない。席は専用新設
- `--dangerously-skip-permissions` 不使用

## 13. レビュー反映ログ

v5→v6 (2026-07-28, 機能変更なしの知見統合):
| 変更 | 由来 |
|---|---|
| Codex Tier1 を「自前 spawn app-server + stdio」に確定 (daemon は Unix 専用と実測) | 実測 2026-07-27 + README 全文訳 |
| 席別 Tier1 経路表 + Gemini を Tier1 候補に昇格 (Antigravity 3 経路) | 追加調査 2026-07-27 |
| ACP 収束戦略 (adapter interface を 1 契約に絞る) | gemini-cli/claude --acp 実装済み + Grok ACP 対応 + agy request 中 |
| fs API による scratch 検証補助を spike 観察項目に追加 | app-server README |
| blind-review round を v0.2 に / council を自称しない | 先行事例調査 (llm-council ~23.3k★) |

v5 分:

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
