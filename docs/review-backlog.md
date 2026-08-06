# レビュー残課題台帳 (v0.1 時点)

敵対レビュー (2026-07-28, workflow run wf_fb78fdfa-246) の所見のうち、
H1 (TOCTOU) と M1-M4 は修正済み。以下 LOW は v0.1 では記録のみ (レビュー結論どおり)。

| # | 内容 | 対応予定 |
|---|---|---|
| L1 | set_state が detail を上書きし delivered の "tier3" 記録が消える (監査ログは最新のみ) | v0.2 (state 履歴を配列化) |
| L2 | 「空出力」が parse に合流 / OSError も parse と誤分類 | v0.2 (分類細分化) |
| L3 | 状態機械の遷移検証なし (failed→merged の逆行が可能) | **解消 (v0.2)**: `journal.py` に `TRANSITIONS` 表 + `InvalidTransitionError` (`set_state` が非許可遷移を拒否) |
| L4 | close 二重実行で verdict 行が結合破損 / status: open 置換の topic 誤爆 | v0.2 (closed 済み検知) |
| L5 | slug 未サニタイズ (`..\..` で root 外に書ける。CEO 入力のみだが機械担保なし) | **解消 (v0.2)**: `paths.py` に `SLUG_RE` (`^[a-z0-9][a-z0-9-]*$`) + `validate_slug`。`topic_dir` の唯一の path 合成点で強制 |
| L6 | 席出力の UTF-8 BOM / UTF-16 を parse 失敗にする (Windows 席の現実的な躓き) | v0.2 (utf-8-sig 許容を検討) |
| L7 | round 進行が「指名バッチ」でなく frontmatter 全参加者基準 (計画と設計の不整合) | v0.2 で設計に合わせる (部分指名 round の扱いを CEO と決める) |
| L8 | watcher 経由の schema 失敗経路にテストなし | v0.2 (結線テスト追加) |
| L9 | merge_opinion 単体は participant/invocation_id を無 escape (watcher が唯一の防壁) | v0.2 (defense-in-depth) |

## 実席 smoke で判明した v0.1 の穴 (2026-08-05)

| # | 内容 | 対応予定 |
|---|---|---|
| S1 | 議題の「背景」を書く CLI コマンドがない (今回は python から直接 atomic_write した) | v0.2 (`new-topic --background` or `set-background`) |
| S2 | dispatch を `| tail -N` に通すと exit code が tail のものになり、timeout 失敗が成功に見える | **解消 (v0.2)**: PROTOCOL.md §1 禁止事項 + §2.2a に「dispatch はパイプに通さない」を明記 (本改訂、2026-08-06) |
| S3 | Tier3 で「貼り付けたか」を機械的に知る手段がない。delivered のまま timeout しても未貼付/席無応答の区別がつかない | **配線済み・運用未検証 (v0.2)**: `cli._deliver` が `seats.json` の tier を見て `get_relay` を呼び、Tier1 失敗時は Tier3 へ縮退する。ただし**既定 tier は 3** で、Tier1 の発動には `dispatch --tier 1` の明示操作が要る (fail-safe な既定)。実席での往復はまだ 0 回 — **実運用で効いていることは未確認** |

原本: workflow 出力 (scratch には保存せず、本台帳を正とする)

## v0.2 進捗メモ (2026-08-06)

上記のうち **解消は L3 / L5 / S2 の 3 件のみ**。L1 / L2 / L4 / L6 / L7 / L8 / L9 / S1 は
`roundtable/journal.py` / `watcher.py` / `minutes.py` / `cli.py` を実装 Read して確認した限り
未着手のまま (対応予定どおり v0.2 内で残っている)。

S3 の記述は 2026-08-06 に訂正した: 当初「cli 未配線」と書いたが、実測すると
`cli._deliver` → `get_relay` の配線は入っている (`grep -n "get_relay" roundtable/cli.py`)。
ただし既定 tier は 3 のままで、Tier1 は `dispatch --tier 1` の明示操作でのみ発動する。
**実装物があることと運用で効いていることは別**という原則は維持し、実席での往復 0 回のうちは
「運用未検証」と書く。
