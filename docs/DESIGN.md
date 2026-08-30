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
| D7 | 議事録保護は「防止」でなく「検知」(fail-closed)。**検知手段は v0.3 で hash 照合 → git に一本化 (D12)** | アプリ agent の FS 権限は制御外。Codex1st#2 の hash 方式は D12 で superseded |
| D8 | Evidence 型付け (observed/log/diff/source/argument/none)。自己申告であり検証済み表示にしない | Codex1st#8 |
| D9 | round 上限 3。収束の自動判定はしない | CC#1 + ai-council-framework の独立採用例 |
| D10 | v0.1 に要約層・コスト警告を入れない (raw 提示)。v0.2 で non-authoritative + 原文参照付きで導入 | Codex1st#7/#9 |
| D11 | **CC (ホストランタイム) は席にしない**。参加者は異ベンダーの AI で埋める | 独立性 > 頭数 (下記 D11 節) |
| D12 | **改ざん証跡は git に一本化**。`.integrity/` witness 層は廃止。lock は排他専用として維持 | witness は grok 席から届く (実測) = 検知不成立。git は既にあり、より強い (下記 D12 節) |
| D13 | **議事録 root は必ず git 管理下**。dispatcher が検査し、なければ init する。実運用の正本 root は本 repo の `minutes/` | CEO 要件「議事録は Git 管理」をツールが強制する。引数任せにしない |
| D14 | **独立reviewとproduction実装を別workflowにする**。review成果物をstart gateとし、採否・root cause・ownership・receipt・fan-inはproduction integration ownerが持つ | ホストの非評価契約を壊さず、実装責任と単一PR closeoutを機械検査する (`docs/adr/0002-*.md`) |

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
| 1 | **公式 API 直結** — AI を起動せず、アプリと同じ会話ストアの席にメッセージを届ける | 下の「席別 Tier1 経路」参照 | ゼロ | **spike で go/no-go** (§8)。4 席共通の残検証点 = 「プログラムから投げた会話がアプリ画面に出るか」。**Codex は 2026-08-07 クローズ** (`thread/list` に通常チャットと同列で表示・CEO 目視確認済み。詳細: `docs/review-backlog.md` 「未確認事項 #1 クローズ」節)。Grok / Gemini は v0.2 対象で未検証のまま |
| 2 | UI 自動化 (Windows-MCP 等) | 公式 API のないアプリ | CEO の明示承認後のみ有効化 | 承認は席 (アプリ) 単位で記録 |
| 3 | 人間 relay (クリップボード → 貼り付け) | Tier1/2 不成立の席 | 貼り付け | 常に利用可能な縮退運転。v0.1 の受け入れはこの経路でも成立すること |

### 席別 Tier1 経路 (実測・調査済み 2026-07-26〜28)

| 席 | 経路 | 状態 | 出典 |
|---|---|---|---|
| Codex | **自前 spawn の `codex app-server` + stdio JSONL** (JSON-RPC 2.0)。`thread/start·resume·read·list·name` / `turn/start·steer·interrupt`。VS Code 拡張・デスクトップアプリと同一プロトコル (stable 扱い)。**daemon 常駐管理は Unix 専用 (実測) のため使わない** | v0.1 spike 対象 | references/codex-app-server-README.ja.md (全文訳) |
| CC | **ホスト専任。席にしない** (D11)。relay 不要 | 確定 (2026-08-07 CEO 判断) | 下記 D11 |
| Grok | `grok agent serve` (WebSocket :2419 + secret) / `grok leader` (`~/.grok/leader.sock`, 複数 client で 1 backend 共有) / **ACP** | v0.2 spike | references/grok-build-integration.ja.md |
| Gemini | **Antigravity 経由 3 経路**: 公式 Python SDK (`google.antigravity`) / コミュニティ ACP ラッパ (antigravity-acp) / agy への ACP native 実装 (公式 feature request 中)。agy 素体に serve 系なし (実測) | v0.2 spike (v5 の「✗」から昇格) | docs/architecture-ideal-vs-actual.md |

### D11: CC を席にしない理由 (2026-08-07 確定)

ホストは CEO との会話を全部見ている。議題の立て方も、CEO が何を気にしているかも、
その設計を誰が書いたかも知っている。その状態で「独立した参加者の意見」を出すのは、
**司会が自分の望む結論を参加者の口から言わせる**のと同じで、D3 (ホストは意見を言わない)
の趣旨に正面から反する。しかも実装当事者は自分の実装を擁護する方向に偏る。

技術的に可能な選択肢は 3 つあったが、いずれも席としては採らない:

| 案 | 手数 | 独立性 | 席として読めるか | 判定 |
|---|---|---|---|---|
| ホストが CC 席も兼ねる | 0 回 | **最悪** (場外文脈を全部持つ) | ここが席 | **却下** |
| 別 CC セッションを席にする | 貼付 or `send_message` | 良 | 可 | **保留** — `send_message` はツール説明で「background work のオーケストレーションに使うな」と明示されており、毎ラウンドの自動 relay は意図から外れる |
| 使い捨て subagent に議事録だけ渡す | 0 回 | 良 (履歴なし) | **不可** | 「席」ではない。必要な時だけ **明示ラベル付きの第三者意見**として議事録に入れる運用に留める |

**AI の頭数より独立性の方が価値が高い。** 先行 OSS (agent-review-panel) の既知の弱点は
「全レビュアーが同一モデル = 共有バイアス」であり、本設計は異ベンダー構成でこれを構造的に
避けている。そこを崩してまで CC を席にする利得がない。

### D12/D13: 証跡は git に一本化 (2026-08-10 確定)

CEO の要件定義: **人間判断がメイン / 共有点は議事録だけ / 議事録は git 管理 /
各 AI は自分のチャットで議事録を読めれば足りる**。この要件に照らすと、
`.integrity/` の witness 層 (状態ファイルの sha256 控えを議題ディレクトリの外に置き、
dispatcher が merge 前に照合する) は二重に間違っていた:

1. **効かない**。「席は自分の作業部屋の外に書けない」前提だったが、grok 席は
   Windows で PowerShell を任意実行でき、`.integrity/` に実測で届く
   (spike `raw-grok-p3-packet.jsonl`)。対象を書き換えられる相手は控えも書き換えられる。
2. **既にある仕組みと重複する**。議事録が git 管理なら、改ざんの記録は git そのもの。
   `git diff` を人間が読む — 本システムの「人間が判断する」原則そのままの形で検知できる。

| 案 | 検知が成立する条件 | 判定 |
|---|---|---|
| witness 層 (`.integrity/`) | 席が sandbox に閉じていること — **grok で実測破綻、codex は未検証** | **廃止** |
| ローカル git | 席が git 履歴を書き換えないこと (working tree の改変は次の操作で dirty として出る) | **採用 (一次検知)** |
| origin へ push した履歴 | 席がリモートに届かないこと — 席に credential を渡さない限り成立 | **採用 (最終証跡)** |

- **Principle**: 改ざんは防がず検知する (D7)。検知者は機械でなく最終的に人間 (D1)。
- **Invariant**: dispatcher は書込操作の前に対象 topic path の working tree が clean で
  あることを要求し、書込後に auto-commit する。dirty = dispatcher 以外の書込。
- **Detector**: 操作前 clean 検査 (fail-closed で停止し diff を提示) + push 済み履歴との照合。
- **Repair Path**: CEO が `git diff` を読んで裁定。正当なら commit、不当なら checkout で復元。
- **Evidence**: grok 席の sandbox 破れ (2026-08-07 実測) / journal 消失事故 (並行 dispatch、
  2026-08-07) — 後者の教訓である **lock (排他) は証跡と別問題として維持**する。

D13 はこの前提を支える: 検知が git 依存になるため、**議事録が git 外に置かれた瞬間に
検知が消える**。だから root の git 管理は運用注意ではなくツールの検査事項にする
(new-topic 時に git work tree でなければ init。実運用の正本 root は本 repo の `minutes/`。
Tier1 実証 `minutes/tier1-final/` も ignore を外して履歴に残す)。

副作用として、witness 前提で保留していた判断が消える: **grok 席 A/B/C 判断は不要**
(sandbox が破れていても git で検知できる)、**codex sandbox 実効範囲 spike も不要**
(検知が sandbox に依存しなくなった)。CLI の grok 警告も削除する。

### ACP 収束戦略 (v6 追加)

gemini-cli / claude は `--acp` 実装済み、Grok Build は ACP 対応、agy は公式 request 中 —
**ACP (Agent Client Protocol) が 4 者共通の統一接続口に収束する可能性が高い**。
方針: v0.1 の relay adapter は席別実装で作るが、**interface を「席に text を届け、
出力を回収する」1 契約に絞っておき、ACP が揃った時点で adapter を 1 本に置換できる形**にする。

- Tier1 の位置づけ: CEO 禁止事項は「CLI で AI を**実行**する」こと。Tier1 はプロセスを
  起こさず、既存の席 (アプリで可視) へ turn を送るだけであり、履歴もアプリに残る
- relay 層は adapter として分離し、席ごとに `tier` を seats.json に記録。
  Tier1 の未送信が確定した障害は自動で Tier3 に縮退し、その旨を CEO に表示する
  (勝手に Tier2 へ昇格しない)。`turn/start` timeout のように席が受理した可能性が
  残る場合は、二重送信を避けるため縮退・再送せず `delivery-unknown` として CEO 判断へ戻す
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
          └→ delivery-unknown → output-received / failed
```

- `delivery-unknown` は再送禁止だが終端ではない。後から成果物が現れた場合は同じ
  invocation を検証・回収し、二重送信せずに状態を前進させる。
- Tier3 の delivered は「クリップボード搬出済み」であり席着信は未知 — 未着のまま
  timeout したら「未貼り付け?」として CEO に確認 (自動再送しない)
- merge は invocation_id で冪等。round は指名バッチ全 merge で +1 (機械更新)
- 失敗分類: 空出力 / schema 違反 / id 不一致 / hash 違反 / timeout / parse 失敗 /
  relay / delivery-unknown。
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
