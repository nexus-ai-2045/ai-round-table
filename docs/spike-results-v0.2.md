# v0.2 Phase 0 spike 結果 — Codex app-server Tier1

- 日付: 2026-08-06 / 追記 2026-08-07 / **撤回と再測 2026-08-08**
- 目的: DESIGN v6 §8 spike 1 / v0.2 plan Phase 0 go/no-go / **実席経路の切り分け**

> ## ⚠️ この文書の当初結論 (自前 spawn 書込 no-go / Tier3 が本線) は撤回済み
>
> **Tier1 は成立する。実席運用の本線は Tier1、人間の貼付は不要。**
>
> 当初の測定は全て `codex` **0.130.0-alpha.5** で行われた。**この版の app-server は
> `thread/start` に永久に応答しない** (`initialize` には 1.3s で返すため、一見
> 疎通しているように見える)。`shutil.which("codex")` が PATH 先頭のこの版を掴んでおり、
> 「無応答」は環境の性質ではなく**掴んだバイナリの性質**だった。
>
> 0.144.6 を明示して同一環境で測り直すと `thread/start` は **ok**、
> 実往復も成立する (`minutes/tier1-final/`: `tier=1` / `merged` /
> `human_actions: ['new-topic','dispatch']` = **貼付 0 回**)。
>
> 撤回の全文・前後比較・真因 3 層は [review-backlog.md](review-backlog.md)
> 「PR #6 の no-go 判定を撤回」を参照。**運用手順は本文書末尾の Tier1 版を使うこと。**
>
> なお **Desktop control socket が Windows で作られない**という観測は撤回しない
> (下記「Desktop 接続 spike」は現在も有効)。Tier1 は desktop attach ではなく
> **自前 spawn** で成立している。

## 実測 (2026-08-06/07 / 旧バイナリ 0.130.0-alpha.5)

以下は**応答しない版で測った記録**として残す。「無応答」行は全て版に起因する。
環境そのものの性質ではないので、そのまま引用しないこと。

| 手順 | 結果 | 証拠 |
|---|---|---|
| `codex app-server` spawn (stdio) | **OK** | プロセス生存、stdout JSONL |
| `initialize` + `clientInfo` | **OK** | `result.userAgent` / `codexHome` / `platformOs=windows` 返却 |
| `initialized` 通知 | **OK** | エラーなし |
| `thread/list` | **OK (2026-08-07)** | 既存 thread 25 件を返却。`status: notLoaded` 含む |
| `thread/start` (empty / cwd+sandbox / ephemeral) | **no response** | id 付き応答が timeout 内に来ない |
| `thread/resume` (list で得た id) | **no response (2026-08-07)** | 同上 |
| `turn/start` (resume 後) | **no response (2026-08-07)** | 同上 |
| デスクトップアプリで thread 可視 | **未確認** (CEO 目視) | list は会話ストアを読めるが、自前 spawn サーバからの書込は未成立 |
| 席が scratch に JSON 書込 | **Tier3 経路のみ運用可** | Tier1 turn が届かないため |
| Tier1 障害時の縮退 | **実装済み** | `FallbackRelay` → Tier3。Tier2 へは昇格しない |

## go / no-go 判定 (2026-08-08 更新)

| 軸 | 当初判定 (旧版で誤測) | **現判定 (0.144.6 実測)** | 根拠 |
|---|---|---|---|
| 会話ストア**読取** | go | **go** | `thread/list` が既存席を返す |
| 会話ストア**書込** (自前 spawn) | ~~no-go~~ | **go** | `thread/start` ok → `turn/start` → 席が scratch JSON を書く |
| 実席運用 | ~~Tier3 が本線~~ | **Tier1 が本線** | `human_actions: ['new-topic','dispatch']` = 貼付 0 |
| Tier3 | 本線 | **縮退先として維持** | Tier1 失敗時のみ `FallbackRelay` が使う |
| Desktop attach (control socket) | no-go | **no-go (据え置き)** | Windows で socket が作られない。Tier1 は attach を必要としない |

**成立条件 (退行防止)**: `codex` **0.144 以上**であること。下回る版では
`thread/start` が返らない。`resolve_codex_binary()` が PATH 候補を `--version`
検査して選び、**候補が全部下回る場合は即エラー**にする (待たずに Tier3 へ縮退)。

**やらないこと (MPC)**: `thread/start` の全パラメータ探索 / 別プロトコルの再発明 / UI 自動化 (Tier2)。

## 実席の定義 (用語)

- **実席** = Codex / Claude など**アプリの実チャット**に packet が入り、席が scratch に契約 JSON を書くこと
- **実績** ≠ 実席。CLI の mock smoke 成功は実績だが実席ではない
- **2026-08-08 時点の機械保証: Tier1 実往復** (`minutes/tier1-final/` = `tier=1` / `merged` / 貼付 0 回)。
  席は `thread/list` に `rt-tier1-final-codex` として出る (CEO 目視で UI 表示も確認済み)

## 再計画メモ

| 予測 | 2026-08-07 の解釈 | **2026-08-08 の確定** |
|---|---|---|
| 手数で死ぬ | Tier3 貼付が残る → KPI は core 3 + paste +1 | **貼付が消え、実測 2 手**。KPI ≤ 3 を満たす |
| thread/start パラメータ不足 | 支配項は **接続先 (spawn vs desktop)** | どちらも外れ。支配項は **バイナリの版** |
| fallback が支配的 | 常時 fallback。Tier3 前提でよい | fallback は例外経路。**Tier1 が既定** |

**学び**: 「無応答」は環境の性質に見えたが、実体は**掴んだ実行ファイルの性質**だった。
症状が同じで原因が複数層 (timeout 値 / バイナリ版 / 並走負荷) にあると、1 層直しても
症状が変わらず「直した判断」自体を疑ってしまう。probe と実装経路を**両方**測って
差分を取るのが正しい切り分けだった (詳細は review-backlog.md「真因は 3 層あり」)。

## Desktop 接続 spike (2026-08-07 追記 2)

| 手順 | 結果 | 証拠 |
|---|---|---|
| `$CODEX_HOME/app-server-control/*.sock` | **不在** | ディレクトリ自体が無い |
| `codex app-server proxy` (default) | **fail** | `failed to connect to socket` / WinError 10050 |
| 自前 `--listen unix://` + proxy | sock ファイルは作れるが **INIT 不可** | proxy は websocket フレーム前提 |
| 自前 `--listen ws://127.0.0.1:PORT` | **initialize OK** | healthz あり |
| 同上 `thread/start` | **timeout (60s でも無応答)** | skill YAML エラーは stderr に出るが応答なし |
| 結論 | Desktop 接続 **no-go (現状)** | 修復ではなく `roundtable doctor` で検知する |

**やらないこと**: thread/start の全パラメータ総当り / 壊れた skill YAML の一括修正 / Tier2 UI 自動化。
次の観測点は「Desktop が control socket を作り始めたか」だけ (`doctor` の `desktop_socket_exists`)。

## 運用コマンド (実席 = Tier1 / 現行手順)

**前提**: `codex --version` が **0.144 以上**。下回ると `thread/start` が返らない。
`python -m roundtable.cli doctor` が `recommended_tier: 1` を返すことで確認できる。

```powershell
# 0) 事前確認 (recommended_tier: 1 / thread_start: ok を見る)
python -m roundtable.cli doctor

# 1) 議題  ← 人間の操作 1
python -m roundtable.cli new-topic <slug> --topic "..." --participants codex --background "..." --root <root>

# 2) 搬出  ← 人間の操作 2。席チャットに直接届く (貼付なし)
python -m roundtable.cli dispatch <slug> --participant codex --tier 1 --async --root <root>

# 3) 回収 (席が書いた scratch/<inv>.json を議事録へ merge)
python -m roundtable.cli collect <slug> --invocation <inv> --root <root>

# 4) 裁定  ← 人間の操作 3 (裁定は人間の専権)
python -m roundtable.cli close <slug> --verdict "..." --root <root>
```

席は Codex アプリのチャット一覧に `rt-<slug>-<participant>` の名前で出る。
CEO はそのチャットを直接開いて読める (要件どおり、履歴はアプリ側に残る)。

### 縮退時 (Tier3 = 人間貼付)

Tier1 が失敗すると `FallbackRelay` が自動で Tier3 に落ちる (Tier2 = UI 自動化へは
昇格しない)。このとき手順 2 の後に**貼付が 1 手増える**:

```powershell
# 2') クリップボードに packet が入る。CEO が席チャットに貼付 → 席が scratch/<inv>.json を書く
```

縮退したかは `seats.json` の `tier` と `fallback_reason` で機械的に分かる。
再現スクリプト: `scripts/ops-real-seat-tier3.ps1` (縮退経路の検証用)。
