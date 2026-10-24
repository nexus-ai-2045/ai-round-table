# Codex Tier1 配送安全性 — 運用保証記録

## 対象

- ブランチ: `codex/tier1-delivery-safety`
- 議題: `minutes/delivery-safety-smoke-20260819`
- 実測日: 2026-08-19
- 環境: Windows / Codex app-server

## 実装と性質テストで守る性質

1. stderr が大量に出ても PIPE 詰まりで app-server を停止させない。
2. `turn/start` timeout は送達成否不明として止め、Tier3 へ二重送信しない。
3. stale `thread_ref` は `thread not found` が確定した場合だけ1回作り直す。
4. JSON-RPCとsend全体を直列化し、二重spawnや単一応答queueでの応答競合を防ぐ。
5. Windows Job Object が成立しない場合も、親終了前にプロセス木回収を試みる。

## 実席スモーク

Codex Tier1へ2ラウンド連続で送信した。

- Round 1 invocation: `fbd7970ceec6` — `tier1-sent`、merged
- Round 2 invocation: `239d4fe6f985` — 同じ `thread_ref` をresume、merged
- thread_ref: `01a0176c-31f7-7952-b701-6733b9a5d72e`
- `journal.json`: round 3、failure 0、human_actions 3
- Tier3貼り付け: 0

この実測が保証するのは app-server 上の永続threadへの送達、成果物回収、同一thread継続である。
Codex Desktop UIに当該threadが表示されることの画面read-backは、この実測には含めない。

## 縮退規則

- spawn失敗、initialize失敗、明示的なJSON-RPC errorなど未送信が確定する失敗:
  Tier3へ縮退できる。
- `turn/start` timeout: 受理済みの可能性があるため `delivery-unknown`。自動縮退・再送禁止。
- `thread/resume` の明示的な `thread not found`: stale席として新規threadを1回だけ作る。
- その他のresume失敗・timeout: 席を分裂させず停止する。

## 公式SDK移行

公式 `openai-codex` 0.147.0 は transport の重複を減らせる。ただし既定 approval handler、
Windowsプロセス木、timeout後の送達不明処理が既存契約と一致しないため、
`docs/adr/0001-official-codex-sdk-adoption.md` の採用ゲートを満たす独立差分で移行する。
