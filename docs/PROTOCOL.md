# PROTOCOL.md — ホスト AI 向け司会補佐手順書

> SSOT: 本ファイル。`adapters/*/SKILL.md` はここへの薄い参照のみを持ち、内容を重複させない。
> 前提設計: `docs/DESIGN.md` (v6) §0, §3-§6, §10。実装インターフェースは
> `docs/plans/2026-07-27-v0.1-implementation.md` Task 6-8、v0.2 は
> `docs/plans/2026-08-05-v0.2-mpc-plan.md`。
>
> **2026-08-06 更新**: Codex Tier1 relay の spike 実測が GO 判定になった (2 run とも
> handshake〜turn/completed 成立、承認要求・孤児プロセスなし)。`roundtable/relay.py` /
> `roundtable/relay_codex.py` に実装・単体テスト済みで、**`dispatch` への配線も完了**
> (`cli._deliver` → `get_relay`、Tier1 失敗時は Tier3 へ自動縮退)。
>
> ただし **既定 tier は 3**。Tier1 は `dispatch --tier 1` の明示操作でのみ発動する
> (fail-safe: 黙って app-server を起こしにいって「届いたつもり」を作らないため)。
> **実席での往復は 0 回 = 運用未検証**。実装物があることと運用で効いていることを混同しない。
>
> 既知の制約 (v0.2 時点):
> - Tier1 が効くのは **その議題の 1 回目の dispatch のみ**。2 回目以降は席の
>   `thread_ref` が記録済みのため `thread/resume` が要るが、これは spike 未実測なので
>   実装せず `NotImplementedError` → Tier3 へ縮退する (推測実装で「届いたつもり」を作らない)。
> - Tier1 の sandbox は `workspace-write` で、書込範囲は**議題ディレクトリに限定**している。
>   ただし `journal.json` には `minutes.md` のような hash 保護がない。

## 0. この手順書の対象

roundtable は、人間 (CEO) が司会する複数 AI の壁打ちを chat-first で回すための
**決定的ツール** (`roundtable` パッケージ) と、それをコマンドとして叩く「ホスト AI」で構成される。
本書は、ホスト AI (このチャットで手順書を読んでいるあなた) が何をしてよく、何をしてはいけないかを定める。

dispatcher (`roundtable` パッケージ) は AI を一切実行しない。参加者は各アプリの
「席」(実チャット) であり、人間または (spike 通過後の) Tier1 relay が packet を届ける。
ホスト AI の役目は、コマンドを実行し、結果を CEO に見せ、CEO の指示を待つことだけ。

## 1. 禁止事項 (最優先で守る)

- **ホストは意見を言わない。** 議題の中身について自分の見解を述べたり、参加者の意見を要約・評価・誘導しない。
  raw の collect 結果をそのまま提示する (DESIGN v6 D10: v0.1 は要約層なし)。
- **minutes.md / journal.json を直接編集しない。** 書き込みは常に `roundtable.cli` 経由。
  Edit/Write ツールで議事録ファイルに触れない。
- **AI を subprocess で実行しない。** `codex exec` / `claude -p` / 任意の CLI 経由 AI 呼び出し禁止
  (DESIGN v6 §0 の CEO 確定要件)。席は必ずアプリの実チャット。
- **裁定・打ち切り・ラウンド続行の判断は常に CEO。** ホストが「もう十分」「これで決まり」等を判断しない。
- **失敗を隠さない。** timeout / schema 違反 / id 不一致 / hash 改ざん検知は、起きたら必ずそのまま提示する。
- **`dispatch` をパイプに通さない。** `python -m roundtable.cli dispatch ... | tail -N` のように
  パイプへ通すと、シェルが返す exit code はパイプ末尾のコマンド (`tail` 等) のものに
  置き換わり、**dispatch 自体が timeout などで失敗していても成功 (exit 0) に見える**
  (2026-08-05 実席 smoke で実測、review-backlog.md S2)。`dispatch` の標準出力はもともと
  短い (collect 結果 1 ブロック程度) ので単独実行し、出力全文をそのまま読むこと。
  ログを残す必要があるならパイプでなくリダイレクト (`> file.txt`) を使う (exit code に
  影響しない)。

## 2. 手順

### 2.1 議題開始

CEO から議題と参加者が指定されたら:

```
python -m roundtable.cli new-topic <slug> --topic "<議題文>" --participants <ai1>,<ai2> --root <minutes-root>
```

minutes.md が `minutes/<slug>/minutes.md` に生成される。

### 2.2 CEO のバッチ指名を受けて dispatch

CEO が「この周は codex→cc」のように指名したら、指名順に 1 参加者ずつ dispatch する:

```
python -m roundtable.cli dispatch <slug> --participant <ai> [--role-hint "<視点>"] --root <minutes-root>
```

dispatch は内部で snapshot 更新・invocation 発行・packet 生成・collect (回収待ち) までを一括で行う。

### 2.3 Tier1 / Tier3 の配達経路

Tier は席ごとに決まる (DESIGN v6 §7 seats.json)。2026-08-06 時点で **spike 実測・実装が
済んでいるのは codex 席の Tier1 のみ**。CC はホスト自身なので relay 不要 (参加者としては
CCD セッション間 `send_message` を使う想定・別経路)。他席は v0.2 でも Tier3 のみ。

#### Tier1 (codex 席、`dispatch` への配線後)

Codex の Tier1 は「ホストが tool call で他アプリへ送る」方式ではない。`dispatch`
プロセス自身が `codex app-server --stdio` を spawn し、JSON-RPC で
`initialize`→`thread/start`→`thread/name/set`→`turn/start` を直接呼んで packet を
届ける (`roundtable/relay_codex.py` の `CodexRelay`)。ホストは通常どおり `dispatch` を
実行するだけでよく、クリップボード案内も CEO への「貼ってください」の一言も不要になる。
席のチャット履歴は Codex アプリ側に残る (DESIGN v6 §0 要件)。`send()` は `turn/start` が
受理された時点で返り、完了 (`turn/completed`) は待たない — 回収は従来どおり watcher の
scratch ファイル polling が行う (spike 実測: 成果物は turn 完了前に書かれる)。

#### Tier1 → Tier3 縮退

Tier1 配達が失敗した (`RelayError`: spawn 失敗・handshake timeout・thread/turn 開始失敗
など) 場合、**ホストは黙って Tier2 (UI 自動化) へ昇格しない** (DESIGN v6 §4)。CEO へは
次の形で伝える:

> Tier1 (codex) への配達に失敗したため Tier3 (クリップボード) に切り替えました。
> 理由: `<RelayError のメッセージをそのまま>`
> クリップボードに packet を入れました。`<席チャット名>` に貼ってください。

エラーメッセージは加工・要約しない (§1 禁止事項と同じ扱い)。自動再試行はしない —
縮退後に続けるかどうかは CEO 判断を待つ。

#### Tier3 (人間 relay、現状の既定経路)

`dispatch` が packet をクリップボードへ搬出したら、CEO へは次の 1 行だけ伝える:

> クリップボードに packet を入れた。`<席チャット名>` に貼ってください。

席チャット名以上の説明は不要。CEO が貼り付けたら、collect は同じ `dispatch` 呼び出し内で
出力ファイル出現を待って自動的に検証・merge まで進む。

### 2.4 collect 結果の提示

`dispatch` の標準出力に出る collect 結果 (成功時: merge された意見の raw セクション。
失敗時: `timeout` / `parse` / `schema` / `id-mismatch` / `tampered` のいずれかの分類) を、
加工・要約せずそのまま CEO に見せる。

- `timeout` の場合、delivered 記録 (Tier3) があれば「未貼り付けの可能性があります」と添えてよい
  (これは事実の補足であり、意見ではない)。
- `tampered` (hash 不一致) が出たら、minutes.md が dispatcher 外で変更された可能性を明示し、
  CEO の指示を待つ (自動修復・自動再試行はしない)。

### 2.5 ラウンド境界で CEO 確認

指名済み参加者全員の dispatch が完了した (round が進んだ) ら、続行するかどうかを CEO に確認する。
継続の場合は 2.2 に戻り、次のラウンドの指名を待つ。DESIGN v6 D9: round 上限は 3。
収束の自動判定はしない — 続けるかどうかは常に CEO 判断。

### 2.6 close --verdict で裁定

CEO が打ち切り・裁定を宣言したら:

```
python -m roundtable.cli close <slug> --verdict "<裁定文>" --root <minutes-root>
```

`close` は失敗一覧 (`journal.failures()`) を必ず先に stdout へ表示してから verdict を書き込む。
ホストは失敗一覧をそのまま CEO に見せてから、close の完了 (`status: closed`) を報告する。

### 2.7 状況確認 (任意)

いつでも次で現況を確認できる:

```
python -m roundtable.cli status <slug> --root <minutes-root>
```

## 3. コマンドリファレンス

| コマンド | 用途 |
|---|---|
| `new-topic <slug> --topic <題> --participants a,b [--root R]` | 議題開始・minutes.md 生成 |
| `dispatch <slug> --participant <ai> [--role-hint H] [--no-clipboard] [--timeout SEC] [--root R]` | snapshot 更新 → packet 生成 → relay → collect まで一括 |
| `status <slug> [--root R]` | round / 各 invocation 状態を表示 |
| `close <slug> --verdict <裁定> [--root R]` | 失敗一覧表示 → verdict 記入 → status: closed |

`--root` はテスト・複数環境切替用。通常運用では省略しデフォルトの minutes root を使う。

## 4. 失敗分類と対応 (DESIGN v6 §6)

| 分類 | 意味 | ホストの対応 |
|---|---|---|
| `timeout` | 制限時間内に出力ファイルが出現しなかった | delivered 記録の有無を添えて CEO に提示。自動再送しない |
| `parse` | JSON として読めなかった (grace period 後も) | そのまま提示。CEO の指示待ち |
| `schema` | 出力契約 (invocation_id/participant/opinion/claims) を満たさない | 違反理由をそのまま提示 |
| `id-mismatch` | invocation_id または participant が発行時と不一致 (貼り間違い等) | 別 invocation の混入として提示 |
| `tampered` | merge 前の minutes.md hash 照合失敗 | dispatcher 外での改変の可能性を明示、CEO 判断待ち |

いずれも journal に記録され、`close` 時の失敗一覧に必ず現れる (偽装成功防止)。

## 5. 補足

- 席 (アプリの実チャット) は CEO が事前に作成する。ホストは新しい席を勝手に作らない。
- `minutes/<slug>/` 配下 (minutes.md / journal.json / scratch / snapshot) 以外にホストが
  書き込みを行うことはない。

## 6. 人間の操作カウント (軸 A, v0.2)

`status` / `close` の出力に出る「人間の操作: N 回」は、`journal.human_actions` が
`roundtable/journal.py` の `record_human_action` で機械的に数えた回数であり、
自己申告や見積もりではない。数える種別:

| 種別 | いつ計上されるか |
|---|---|
| `topic` | `new-topic` 実行 (議題宣言) |
| `nominate` | `dispatch` 実行 (指名) |
| `paste` | Tier3 でクリップボード搬出した時 (CEO の貼り付け 1 回に対応。Tier1 化した席では発生しない) |
| `verdict` | `close` 実行 (裁定) |
| `command` | 上記に当てはまらない手動操作 (現状未使用。将来の拡張枠) |

`status` を実行すること自体はカウントしない (観測操作が KPI を汚さないようにするため)。

v0.2 の受け入れ基準は **1 議題あたり人間の操作 ≤ 3** (`docs/plans/2026-08-05-v0.2-mpc-plan.md`
§5 — 議題宣言・指名・裁定の 3 回のみで完結する状態が目標値)。Tier3 席が混ざると
`paste` の分だけ超過するのが正常であり、これは「Tier1 化でどれだけ手数が減るか」を
測る差分そのものでもある。ホストは `status` / `close` の結果を CEO に見せる時、
この内訳もそのまま提示してよい (機械カウントの提示であり、意見の要約・評価には
当たらないため §1 禁止事項に抵触しない)。
