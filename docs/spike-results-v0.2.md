# v0.2 Phase 0 spike 結果 — Codex app-server Tier1

- 日付: 2026-08-06
- 環境: Windows / `codex` 0.130.0-alpha.5 (`C:\Users\yas\AppData\Local\OpenAI\Codex\bin\codex.exe`)
- 目的: DESIGN v6 §8 spike 1 / v0.2 plan Phase 0 go/no-go

## 実測

| 手順 | 結果 | 証拠 |
|---|---|---|
| `codex app-server` spawn (stdio) | **OK** | プロセス生存、stdout JSONL |
| `initialize` + `clientInfo` | **OK** | `result.userAgent` / `codexHome` / `platformOs=windows` 返却 |
| `initialized` 通知 | **OK** | エラーなし |
| `thread/start` (`ephemeral: true` 含む) | **no response (2026-08-06 実測)** | `initialize` 成功後も id 付き応答が 20s 内に来ない。実装は 5s timeout → Tier3 縮退 |
| デスクトップアプリで thread 可視 | **未確認** (CEO 目視が必要) | 自動では判定不能 |
| 席が scratch に JSON 書込 | **未確認** | turn 成功後の観察項目 |
| Tier1 障害時の縮退 | **実装済み** | `FallbackRelay` → Tier3。Tier2 へは昇格しない |

## go / no-go 判定

- **部分 go**: app-server の spawn + initialize は再現可能。これを「Tier1 経路の土台がある」とみなし、`relay_codex.py` を本線に載せる。
- **運用上の安全網**: `thread/start` / `turn/start` が失敗したら **必ず Tier3 に縮退**する。KPI (人間操作 ≤ 3) は Tier1 成功席でのみ達成見込み。
- **no-go ではない**: 全面 Tier3 固定には戻さない。縮退があるため本番 path を壊さない。

## 再計画メモ (MPC)

- 次の観測点: 実議題 1 本で `--tier 1` を試し、fallback が何回発火するかを `seats.json` / journal detail で数える。
- fallback 率が支配的なら v0.3 で thread/start パラメータ (cwd / sandbox / permissions) を実測で詰める。backlog 全消化はしない。
