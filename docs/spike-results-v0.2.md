# v0.2 Phase 0 spike 結果 — Codex app-server Tier1

- 日付: 2026-08-06 / 追記 2026-08-07
- 環境: Windows / `codex` 0.130.0-alpha.5 (`C:\Users\yas\AppData\Local\OpenAI\Codex\bin\codex.exe`)
- 目的: DESIGN v6 §8 spike 1 / v0.2 plan Phase 0 go/no-go / **実席経路の切り分け**

## 実測

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

## go / no-go 判定 (FDE 圧縮後)

| 軸 | 判定 | 制御 |
|---|---|---|
| 会話ストア**読取** | go | `thread/list` で既存席の存在確認はできる |
| 会話ストア**書込** (自前 spawn) | **no-go (現状)** | start/resume/turn が無応答。これ以上パラメータ総当りは次元の呪い |
| 実席運用 | **Tier3 が本線** | クリップボード → CEO 貼付 → scratch JSON → collect |
| Tier1 コード | **残す (縮退前提)** | 将来 desktop 接続やプロトコル差分が解けたらそのまま使える。失敗しても path を壊さない |

**やらないこと (MPC)**: `thread/start` の全パラメータ探索 / 別プロトコルの再発明 / UI 自動化 (Tier2)。
次の 1 手は「自前 spawn ではなく **デスクトップ側 app-server に接続**できるか」だけを v0.3 で観測する。

## 実席の定義 (用語)

- **実席** = Codex / Claude など**アプリの実チャット**に packet が入り、席が scratch に契約 JSON を書くこと
- **実績** ≠ 実席。CLI の mock smoke 成功は実績だが実席ではない
- 2026-08-07 時点の機械保証: mock + Tier3 縮退 + (任意) 人間貼付

## 再計画メモ

| 予測 | 実際 (2026-08-07) | 学び |
|---|---|---|
| 手数で死ぬ | Tier3 貼付が残る | KPI は core 3 + paste +1 |
| thread/start パラメータ不足 | list は成功、start/resume/turn 全滅 | パラメータより **接続先 (spawn vs desktop)** が支配項 |
| fallback が支配的 | 実測どおり常時 fallback | v0.2 運用は Tier3 前提でよい |

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

## 運用コマンド (実席 = Tier3)

```powershell
# 1) 議題
python -m roundtable.cli new-topic <slug> --topic "..." --participants codex --background "..." --root <root>

# 2) 搬出 (Tier1 試行 → 失敗時クリップボード)
python -m roundtable.cli dispatch <slug> --participant codex --tier 1 --async --root <root>

# 3) CEO: Codex アプリの席チャットに貼付。席は scratch/<inv>.json を書く

# 4) 回収
python -m roundtable.cli collect <slug> --invocation <inv> --root <root>

# 5) 裁定
python -m roundtable.cli close <slug> --verdict "..." --root <root>
```

再現スクリプト: `scripts/ops-real-seat-tier3.ps1` (貼付待ちまで自動化、席 JSON は人間/席が書く)。
