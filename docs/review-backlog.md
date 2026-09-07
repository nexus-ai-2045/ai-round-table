# レビュー残課題台帳

敵対レビュー (2026-07-28, workflow run wf_fb78fdfa-246) の所見のうち、
H1 (TOCTOU) と M1-M4 は修正済み。

v0.2 は MPC + FDE 次元圧縮により **主軸 A (手数) + detector 3 個** に畳んだ。
観測されていない穴は消化しない (次元の呪い回避)。詳細: `docs/plans/2026-08-05-v0.2-mpc-plan.md`。

| # | 内容 | 状態 |
|---|---|---|
| L1 | set_state が detail を上書きし delivered の "tier3" 記録が消える | **hold** (v0.3 で履歴配列化。現状は最新 detail のみ) |
| L2 | 「空出力」が parse に合流 / OSError も parse と誤分類 | **hold** (軸 B detector = failure_stats のみ) |
| L3 | 状態機械の遷移検証なし (failed→merged の逆行が可能) | **done v0.2** (前進のみ / 終端逆行 ValueError) |
| L4 | close 二重実行で verdict 行が結合破損 / status: open 置換の topic 誤爆 | **hold** (未観測) |
| L5 | slug 未サニタイズ | **done v0.2** (`[a-z0-9]+(?:-[a-z0-9]+)*`) |
| L6 | 席出力の UTF-8 BOM | **done partial v0.2** (`utf-8-sig` 受理) |
| L7 | round 進行が指名バッチ vs frontmatter 全参加者 | **hold** (設計どおり frontmatter 参加者基準を維持) |
| L8 | watcher 経由 schema 失敗経路のテスト | **hold** (既存 e2e で一部カバー、専用は v0.3) |
| L9 | merge_opinion 単体の escape | **hold** (defense-in-depth は v0.3) |
| S1 | 背景を書く CLI が無い | **done v0.2** (`--background` / `set-background`) |
| S2 | パイプで exit code が消える | **done v0.2** (`last-result.json` + PROTOCOL 明記) |
| S3 | Tier3 貼付可否が不明 | **done partial v0.2** (縮退 + `tier3_paste_required`)。実席本線 = **Tier1** (Codex, 2026-08-06 `tier1-final` で実往復成立)。旧記述「実席本線=Tier3。`thread/list` のみ go / start·resume·turn no-go」(2026-08-07 追記) は**誤判定であり撤回**: 判定に使った doctor が (a) PATH 先頭の旧 codex 0.130.0-alpha.5 を掴み `thread/start` に無応答、(b) `start_timeout` 既定 5s (実測は 20.9-58.4s) の 2 バグを持ち、環境が正常でも必ず「Tier1 不可」と出る構造だった。`thread/list` が go という観測自体は正しく、`start`/`resume`/`turn/start` も稼働している (誤って no-go と混同されていた)。詳細: 下記「PR #6 の no-go 判定を撤回」節。Tier3 は縮退経路として引き続き健在 |

## v0.2 で置いた detector (修正ではなく検知)

| 軸 | Detector | 置き場所 |
|---|---|---|
| A 手数 | `human_actions` カウンタ | journal / `status` |
| B 回収頑健性 | `failure_stats` | journal / `status` |
| C 状態正しさ | 遷移検証 | `Journal.set_state` |
| D 境界 | slug 検証 | `paths.ensure_topic` |

原本: workflow 出力 (scratch には保存せず、本台帳を正とする)

## v0.2 Tier1 ハードニング (2026-08-06)

PR #3 (別セッション実装) が main に入った後、そちらの Tier1 経路を敵対レビュー観点で
検査して見つかった穴。**設計フォーク (PR #4) は superseded として閉じ、main を土台に
安全性だけ移植した**。

| # | severity | 内容 | 対応 |
|---|---|---|---|
| T1 | HIGH | `subprocess.Popen(text=True)` に encoding 指定がなく、Windows では cp932。**日本語 packet が壊れる** | `encoding="utf-8"` / `errors="replace"` を明示 |
| T2 | HIGH | `thread/start` に sandbox / approvalPolicy が無く**サーバ既定依存**。実測で Codex の既定は `danger-full-access` になりうる | `sandbox="workspace-write"` + `approvalPolicy="never"` を明示 |
| T3 | HIGH | `cwd` が未配線で app-server が CLI の実行ディレクトリを継承。書込範囲が不定 | `cli` → `get_relay(cwd=str(tp.root))` → spawn/thread の両方へ。**議題ディレクトリに限定** |
| T4 | HIGH | `close()` が `proc.kill()` のみ。孫プロセスが孤児化 | stdin close → terminate → wait → `taskkill /T /F` |
| T5 | MED | `ephemeral=True` にしていた | 削除。一時席にすると会話がアプリに残らず「席を CEO が読める」要件 (DESIGN v6 §0) を壊す |

検証: `tests/test_v02_tier1_hardening.py` (6 本)。実 CLI を起動せず、
危険な既定に戻ったら落ちる形にしてある。全 suite 49 passed。

~~**未対応で残す穴 (v0.3)**: `journal.json` / `seats.json` に `minutes.md` 相当の hash 保護がない。
Tier1 の書込範囲を議題配下に絞ったので露出は減ったが、同一議題内では席が journal.json を書ける。~~
→ **done (2026-08-07)**: `roundtable/integrity.py`。証跡は議題ディレクトリの外
(`<root>/.integrity/<slug>/`) に置く。下記「並行 dispatch で journal の記録が消える」節を参照。

## PR #4 のテスト資産を main へ移植 (2026-08-06)

設計フォークで PR #4 を閉じた際、そこにあった**守備範囲まで一緒に失われていた**ことに
気づいたため、`tests/test_v02_invariants.py` として main の API に合わせて持ち込んだ。
実装の内部構造ではなく**守るべき性質**でまとめてある (API が変わっても生き残る形)。

移植前後の守備範囲:

| 対象 | main (移植前) | 移植後 |
|---|---|---|
| 状態遷移の逆行・終端 | 部分的 | 6 本 (merged/failed 終端・逆行拒否・非永続化・失敗分岐) |
| slug パストラバーサル | 1 本 | 24 本 (`..` `/abs` `C:/abs` `\server\share` `nul\x00` 等) |
| KPI 計測の性質 | なし | 2 本 (再ロード永続・status が観測専用) |
| 失敗集計 | 部分的 | 2 本 (schema 畳み込み・空 detail を落とさない) |
| 議事録防御 | 11 本 | +4 本 (改ざん fail-closed・見出し注入全経路・Round 抑止攻撃) |

**この移植で main の実バグを 1 件検出した**: `topic_dir()` が slug を検証しておらず、
`ensure_topic()` 経由でしか軸 D が効いていなかった。`topic_dir` を直接使う経路
(将来の CLI・ツール) は root 外を指せる状態だった → 合成と検証を同じ場所に置くよう修正。

全 suite 49 → **113 passed**。

## Codex bot レビュー (PR #5, 2026-08-06) — 対応

| # | 判定 | 内容 | 対応 |
|---|---|---|---|
| P1-a | **却下 (指摘が誤り)** | 「sandbox は camelCase `workspaceWrite` を送れ」 | スキーマで確定: `thread/start.sandbox` の型は **`SandboxMode` = `["read-only", "workspace-write", "danger-full-access"]`** で **hyphen が正**。camelCase は `turn/start.sandboxPolicy` (型 `SandboxPolicy` のオブジェクト `{"type": "workspaceWrite"}`) の話で、bot が README の turn 例を thread と混同している。変更しない |
| P1-b | 採用 | 非 Windows で子孫が回収されない | `_ProcessTree` を新設。POSIX は `start_new_session=True` + `killpg` |
| P1-c | 採用 | Windows で terminate 成功時に木の掃除を飛ばす | **Job Object + `KILL_ON_JOB_CLOSE`** に変更。ハンドルを閉じた時点で木ごと終わるので、terminate が成功した経路でも取りこぼさない |
| P2 | 採用 | 相対 `--root` だと cwd が二重解決される | `str(tp.root.resolve())` を渡す |

検証: `tests/test_v02_tier1_hardening.py` に 3 本追加 (POSIX グループ分離 / terminate 成功時の木回収 / 相対 root の絶対化)。全 suite **115 passed**。

一次情報: `codex app-server generate-json-schema` の `ClientRequest.json` →
`definitions.SandboxMode` / `definitions.SandboxPolicy` / `definitions.AskForApproval`。

## 実往復スモークで判明した致命的ブロッカー (2026-08-06)

**Tier1 は実環境で 100% 失敗していた。** 実議題 `2026-08-06-tier1-live` を `--tier 1` で
dispatch したところ `thread/start: timeout after 5s` で Tier3 に縮退した。

素の app-server を計測した結果:

| 段階 | 実測 | 修正前の既定 | 判定 |
|---|---|---|---|
| `initialize` | **6.66s** | 8s | ギリギリ (環境次第で落ちる) |
| `thread/start` | **20.9s** | **5s** | **必ず失敗** |

→ `TIMEOUT_INITIALIZE=60` / `TIMEOUT_THREAD_START=120` / `TIMEOUT_TURN_START=120` に修正し、
実測値を根拠としてコードに残した。回帰防止テスト
`test_timeouts_have_margin_over_measured_latency` で数値を不変条件として固定。

**学び**: fake stdio のユニットテストは即答するので、この種の穴を構造的に検出できない。
「実装済み・単体テスト green」と「運用で効いている」は別物であることの実例。
実往復スモークを受け入れ基準に入れていた判断が正しかった。

**副産物**: 実サーバが `sandbox: "workspace-write"` (hyphen) を受理した。Codex bot の
camelCase 指摘 (PR #5 P1-a) が誤りであることが、schema に加えて**実測でも裏付けられた**。

## Tier1 実往復 成功 (2026-08-06) — 3 段構えの真因

`tier1-final` 議題で **Tier1 が初めて実環境で成立**した。

```
seats.json  tier: 1 / thread_ref: 019fd900-... (fallback_reason なし)
journal     merged / human_actions: ['new-topic', 'dispatch']  ← paste 0 回
minutes     Round 1 に codex の意見 + claims 表 (evidence_type=observed) が merge 済み
```

**KPI 実測: 人間の操作 2 回** (受け入れ基準 ≤ 3 を満たす)。

### 真因は 3 層あり、1 つ直しても症状が変わらなかった

| # | 症状 | 真因 | 修正 |
|---|---|---|---|
| 1 | `thread/start: timeout after 5s` | 実測 20.9s に対し既定 5s | 300s 枠 (実測レンジ 20.9-58.4s + 負荷時 120s 超) |
| 2 | 120s でも無応答 | **PATH 先頭の古い codex 0.130.0-alpha.5 を spawn**。この版の app-server は `thread/start` に永久に応答しない (initialize には 1.3s で返す) | `resolve_codex_binary()` — PATH 候補を全部 `--version` 検査し 0.144 以上を選ぶ |
| 3 | 修正後も timeout | pytest 等の**並走負荷**。app-server の応答時間は idle 58.4s / 負荷時 120s 超と極端にばらつく | timeout 枠を実測レンジ基準に |

**学び**: 症状 (timeout) が同じで原因が複数層あると、片方を直しても症状が変わらず「直した判断」自体を疑ってしまう。probe (素の実装) と実装経路を**両方**測って差分を取るのが正しい切り分けだった。実際 2 回誤診した (「並行負荷が主因」「バイナリ修正で解決」)。

### 未確認のまま残る 1 点 (要件の核心)

**thread がアプリのチャット一覧に出るか**は未確認。共有セッションストア
(`~/.codex/sessions/2026/08/07/rollout-*.jsonl`, `originator: ai-round-table`) には
**実在を確認済み**だが、アプリ UI が `originator` で絞る等の可能性は排除できていない。
DESIGN v6 §10 未検証事項 #1 として最初から開いており、CEO の目視でのみ閉じられる。
ここが × なら「送れるが席にならない」= 要件未達で、方式の見直しが要る。

## PR #6 の no-go 判定を撤回 (2026-08-07)

PR #6 (別セッション, merge 済み) は「Desktop attach no-go / `thread/start` timeout /
実席は Tier3 貼付を推奨」と結論した。**これは doctor 自身が持つ 2 つのバグによる誤判定**で、
同一環境で条件を直すと判定が反転する。

### doctor が内蔵していた 2 バグ

| 箇所 | 問題 | 修正 |
|---|---|---|
| `_which_codex` | `shutil.which("codex")` は PATH 先頭を返す。そこに **0.130.0-alpha.5** があり、この版の app-server は `thread/start` に**永久に応答しない** (`initialize` には 1.3s で返す) | `resolve_codex_binary()` に委譲 (PATH 候補を `--version` 検査し 0.144 以上を選ぶ) |
| `start_timeout` | 既定 **5.0s**。実測は idle 20.9-58.4s / 負荷時 120s 超 | **180.0s** |

環境が正常でも必ず「Tier1 不可」と答える doctor の出力を根拠に no-go 判定が出ており、循環していた。

### 同一環境での前後比較 (機械の出力)

| 項目 | 修正前 (PR #6) | 修正後 |
|---|---|---|
| `codex_binary` | (PATH 先頭 = 0.130) | `npm-global\codex.cmd` (0.144.6) |
| `thread_start` | **timeout** | **ok** |
| `recommended_tier` | **3** | **1** |
| `real_seat_path` | tier3-clipboard-paste | **tier1-app-server** |

### 実往復の裏付け

`minutes/tier1-final/`: `tier=1` / `merged` / `human_actions: ['new-topic','dispatch']` (paste 0)。
議事録 Round 1 に codex の意見 + claims 表 (evidence_type=observed) が merge 済み。

**なお Desktop control socket が無いこと自体は PR #6 の観測どおり (Windows で未作成)。**

### 撤回の波及先 (2026-08-08 / PR #7 レビュー P2-b で発覚)

撤回を review-backlog に書いただけでは足りなかった。**手順書が古い結論のまま残っていた**:
[`spike-results-v0.2.md`](spike-results-v0.2.md) は「自前 spawn 書込 no-go /
Tier3 が本線 / CEO が packet を貼付」と読者に指示し続けていた。
この文書は spike 記録**兼**運用手順書なので、放置すると次の担当者は撤回済みの
手順で運用する。CLAUDE.md の「同じ事実を 2 箇所に置かない」が効かない構造
(記録と手順が同居している) が原因。

対応: 先頭に撤回バナー / 旧測定は「旧バイナリでの記録」と明示して保存 /
go-no-go 表を前後比較に書き換え / 運用コマンドを Tier1 版に差し替え、Tier3 は
縮退時の手順として分離。

**残る負債**: 同文書が記録と手順を兼ねている点は未解消。手順を別ファイルに
切り出すのが筋だが、v0.3 で運用手順が固まってからにする (今分けると両方動く)。
no-go だったのは「Desktop への attach」であって、**spawn した app-server 経由の Tier1 は成立する**。
この 2 つが混同されていた。

## `.tmp` 取り残しによる偽陰性 (バグ2) — 修正 (2026-08-07)

`minutes/tier1-final` の実測で、**席が正しく応答したのに失敗と記録される**経路を発見した。
DESIGN の「失敗を隠さない」の裏返しで、**成功を失敗と誤記録する**方向の穴。

### 観測 (実測タイムライン, invocation `aca57e43a4f8`)

| 時刻 (JST) | 根拠 | 出来事 |
|---|---|---|
| 12:04:06 | `seats.json` mtime | Tier1 送信成功 (fallback_reason なし) |
| 12:05:26 | `scratch/aca57e43a4f8.json.tmp` mtime | **席が .tmp を書き終えた** (623B / 契約を完全に満たす) |
| 12:19:07 | `journal.json` mtime | 900 秒経過で `failed: timeout` |

.tmp 完成から断念まで **13 分 41 秒**。プロセスは生きて polling していたので「rename 直前に
殺された」ではない。席が `.tmp` → `.json` の rename を実行せずに止まっている。

### 真因は 2 つ重なっている

| # | 箇所 | 内容 |
|---|---|---|
| 1 | `packet.py:14` | rename 契約が**自然言語の指示だけ**。機械的な強制も検証も回収も無い。席→dispatcher の通信路が「rename が起きること」に全依存 |
| 2 | `watcher.py:38,41` | 待機条件が `out.exists()` のみで `.tmp` を一度も stat しない。timeout 分岐も scratch を走査せず `detail="timeout"` を確定する |

結果、別の失敗モード (席は応答済み / 確定操作のみ欠落) が `timeout` に紛れ込み、
軸 B detector (`failure_stats`) でも計量できなかった。CLI も `未貼り付け?` と**事実と逆の**案内を出していた。

### 対応 (この修正の範囲)

| 対応 | 内容 |
|---|---|
| 失敗分類の分離 | timeout 到達時に `<inv>.json.tmp` を stat。在れば `stalled-tmp` として別分類 (`failure_stats` に新しい列が立つ) |
| 救済 | `.tmp` が **grace 中に内容不変** かつ parse + id 照合 + schema を全部通る時だけ merge する。判定は `.json` 経路と同じ厳しさ |
| 来歴 | 救済時は merged の `detail` に `recovered-from-tmp: <file>` を必ず残し、CLI と `last-result.json` にも出す。**成功も来歴を隠さない** |
| 物証 | 採用した `.tmp` は削除しない (何を merge したかを事後に検証できる) |
| 誤誘導の除去 | timeout でも scratch に `*.tmp` が残っていれば必ず提示し、`(未貼り付け?)` を出さない |
| 競合回避 | grace 中に席が rename を完了したら救済せず通常経路に戻す (二重処理しない) |

**あえてやらなかったこと**: `.tmp` を dispatcher が `.json` に rename すること。契約成果物を
dispatcher が捏造する形になり、「rename 完了 = 完成」という席側の契約を勝手に緩めるため。

検証: `tests/test_tmp_recovery.py` (13 本)。救済 / 書きかけ非採用 (不完全 JSON・grace 中の
書き足し・schema 違反・id 不一致) / 来歴の永続 / 通常経路に来歴が混ざらないこと / CEO 出力の
文言まで固定。全 suite **123 → 136 passed**。

### 残る穴 (v0.3 / 未対応)

- **席が黙って止まっても 900 秒待つ**構造は残る。`relay_codex.py:266-299` の `_read_loop` /
  `_rpc` が id 不一致の通知 (`turn/completed` / `turn/failed` / `warning`) を**黙って捨てて**おり、
  `poll()` は無条件 `None` (`relay_codex.py:385-387`)。turn 終了を待機条件に入れれば
  「turn は終わったのに出力が無い」を即 fail にできる。
- **`--async` + Tier1 は Windows で構造的に .tmp を作る**: Job Object が `KILL_ON_JOB_CLOSE`
  (`relay_codex.py:166`) なので、dispatch プロセスが return した時点で席ごと即死する。
  `--async` を Tier1 で禁止するか Job を切り離す判断が別途要る。
- 席が rename しなかった**席側の理由** (turn 打ち切り / sandbox 判定 / 手順飛ばし) は未特定。
  dispatcher が turn 通知を捨てている以上、現状のログからは原理的に切り分け不能。

## 並行 dispatch で journal の記録が消える (2026-08-07 / バグ1)

`minutes/tier1-final` の実測: minutes.md には `### codex (invocation: fd4e3c84d152)` が
merge 済みなのに、journal.json の invocations から **fd4e3c84d152 が消滅**していた。
journal round=1 と minutes round=2 の食い違いが上書きの指紋。

### 真因

`Journal.save()` が「load 時のスナップショットを全文書き戻す」last-writer-wins だった。
dispatch は「起動時に load → 最大 900 秒後に save」なので、read-modify-write の窓が
**プロセスの全寿命**に等しい。P1 が 06:27 の像を 06:44 に書き戻し、その間に P2 が書いた
invocation・human_actions・round がまとめて消えた。`seats.json` も同じ形
(`load_seats` → `save_seats` の窓 = relay.send 全体) で、片方の tier=3 + fallback_reason が消えていた。

### 採った手 (診断 fix_options の A)

| 案 | 判定 |
|---|---|
| A 重ね合わせ (read-modify-write) + hash 照合 | **採用**。minutes.md で既に使っている「hash 照合 + fail-closed」をそのまま持ち込む形で、schema も既存 API も壊さない |
| B append-only event log (journal.jsonl) | 根治だが schema / status / close / 全テストに波及。観測 1 事象に対して大きすぎる → **v0.3 の設計課題として保留** |
| C 排他ロック単独 | 900 秒握れないので古いスナップショット問題が残る。**単独では解にならない** → A の critical section 用に縮小して併用 |

実装:

| 対象 | 変更 |
|---|---|
| `journal.save()` | ロック内で journal.json を読み直し、自分の記録を**重ねて**書く。invocation は key 単位で和、round は max、human_actions は id で和 |
| `human_actions` | 発行時に `id` を付与。同時刻・同内容でも取り違えない (旧記録は内容の多重度 max で畳む = 過去は区別できないという限界を明記) |
| 解決不能な衝突 | `conflicts` 配列に kept / dropped を積む (append-only)。`status` が件数と両状態を出す。捨てた記録を黙って消さない |
| `save_seats()` | 席 (key) 単位の重ね合わせ。`thread_ref` だけはディスク側を落とさない (席の同一性を失うと CEO が見ていない別チャットへ分裂する) |
| `roundtable/filelock.py` | O_CREAT\|O_EXCL + mtime による stale 回収。保持は数ミリ秒のみ。msvcrt/fcntl を使わないのは、回収条件を OS 実装依存にせず自分のコードに書くため |
| `roundtable/integrity.py` | journal.json / seats.json の hash 証跡。不一致・消去は `StateTamperedError` (ValueError 継承) で fail-closed、CLI は exit 3 で CEO に提示 |

証跡の置き場は **`<root>/.integrity/<slug>/`** (議題ディレクトリの外)。Tier1 の席は
sandbox=workspace-write / cwd=議題ディレクトリなので、証跡を同じ場所に置くと対象を
書ける相手が証跡も書けて照合が成立しない。

本文と証跡は別ファイルなので、その間で落ちると必ず不一致になる。それを改ざん扱いに
すると事故のたびに議題が開けなくなるため、証跡は「書込後 hash」と「書込前 hash
(`pending_from`)」の 2 段記録にし、実物が後者なら *書き込みが届かなかった* と判定する。
残る穴は「攻撃者が直前の内容へ戻す」ケースのみ (中断と区別できない)。

### 検証

- `tests/test_concurrency_and_integrity.py` (22 本): 交互書き込み / 8 スレッド × 10
  invocation の総当り / round 巻き戻し / 改ざん・消去の fail-closed / 証跡の位置 /
  中断書込の非誤検知 / stale ロック回収 / seats の席単位重ね合わせ。
- 旧 `save()` を復元して同じ手順を踏むと **merge 済み invocation が消え、human_actions 0 件、
  round=1 のまま**になることを実測 (このテストが本当にバグを捕まえる証拠)。
- 実 2 プロセス並行 dispatch (`--no-clipboard` / codex は起動しない): invocation 2 本・
  human_actions 3 件がすべて残存、conflicts 0。
- 全 suite **158 passed** (exit 0)。

### 残る穴 (v0.3)

- **B (append-only event log) は未実施**。A は CAS 相当なので、read と write の隙間は
  原理的に残る (ロックで守っているのは save 内部だけ)。事故確率を大きく下げるが根絶ではない。
- ~~**`minutes.md` の他経路が無防備**~~ → **done (2026-08-07)**。下記 H3 参照。
- ~~**`snapshot/minutes.snapshot.md` が議題に 1 本しかない**~~ → 改ざん検知の baseline
  ではなくなったので **偽陽性/偽陰性は解消** (下記 H3)。snapshot が dispatch ごとに
  上書きされること自体は残るが、席が読む版が新しくなるだけで判定には効かない。
- **`last-result.json` が議題あたり 1 スロット**。並行時は最後に終わったものだけが残るので、
  S2 (パイプで exit code が消える対策) が並行下では信用できない。

## 敵対レビュー (2026-08-07 / 158 tests green の状態に対して) — HIGH 対応

「テストが通る経路の外側」を突いたレビュー。HIGH 4 件のうち **3 件を修正、1 件は
指摘の一部を採用し提案された修正案は却下**。MED/LOW は下表に記録のみ。

### H1 + H2 — ロック外の reader が偽の改ざん検知と WinError 5 を作る (修正)

`integrity.verify_and_read` は純粋な読み取りではない (`pending_from` 一致時の巻き戻しと、
証跡不在時の baseline 採用で **書く**)。にもかかわらず `Journal.load` / `refresh` /
`load_seats` はロックを取らずに呼んでいた。結果:

| # | 破綻 | 実測 |
|---|---|---|
| H1 | reader が writer の「予告 → 本文 → 確定」の隙間に割り込んで証跡を巻き戻す。その書き込みが writer の確定より後に着地すると **証跡=旧 / 本文=新** で固定され、誰も改ざんしていないのに `StateTamperedError` が永続する (fail-closed なので議題が二度と開けない) | `repro.py R1` / `repro2.py r1_threads` |
| H2 | reader が journal.json / 証跡を開いている間、writer の `os.replace` が Windows で **WinError 5**。`atomic_write` の retry (5 回 / 累計 1.0 秒) を使い切り、`journal.save()` 内の未捕捉例外で **dispatch が traceback で死ぬ** = その invocation の記録がどこにも残らない (バグ1 と同じ「記録が消える」結果) | `repro3.py` (writer 4 / reader 4 で 1 発) |

**修正**: 読み書きを同じ `FileLock` で直列化する (`journal._read_verified` /
`relay.load_seats` / `minutes._lock`)。ロック配下に寄せると pending 窓は reader から
観測不能になり、巻き戻し経路そのものが競合しなくなる。対象ファイルを同時に開く経路も
消えるので H2 も同じ 1 手で閉じる。保持は読み取りの数ミリ秒だけで、dispatch の
900 秒は握らない。併せて `atomic_write` の retry を 5 回/1.0 秒 → 8 回/指数 backoff
(最大 ~2.5 秒) に広げ、外部プロセス (席・AV・エディタ) の読みにも耐えるようにした。

呼び出し規約 (「`verify_and_read` / `write_verified` はロック保持下で呼ぶ」) を
`integrity.py` の module docstring に明記した。ロックの外で呼べば同じ穴が再発する。

### H3 — 並行 dispatch の 2 席目が必ず `failed: tampered` になる (修正)

`merge_opinion` の照合基準が **dispatch 開始時 snapshot の hash** だったため、
先に merge した席が minutes.md を伸ばした時点で、後続席の base_hash は**正常運用でも**
必ず古くなる。実測 (`repro.py R2`) では 2 席目が毎回 `failed: tampered`:

```
A: {'ok': True}
B: {'ok': False, 'reason': 'tampered'}
journal: {'...': 'merged', '...': 'failed'}
```

二重に悪い: (1) 並行 dispatch = この PR が想定する運用そのもので、正しく書かれた意見が
捨てられる。(2) 分類が `tampered` = セキュリティ警報なので、正常な並行追記のたびに
警報が出て **本物の改ざんを CEO が無視するよう訓練される**。fail-closed の意味が消える。

**修正**: minutes.md を journal.json / seats.json と同じ機構に揃えた。

| 変更 | 内容 |
|---|---|
| 証跡 | `<root>/.integrity/<slug>/minutes.md.sha256` を追加 |
| 書き込み経路 | `create` / `set_background` / `merge_opinion` / `write_verdict` / `sync_round` を **すべて** `integrity.write_verified` 経由に統一 (1 経路でも素の atomic_write が残ると、その直後から証跡が古くなり次の merge が偽陽性 tamper になる = L3 も同時に解消) |
| 排他 | 読み書きを `FileLock(.integrity/<slug>/minutes.md.lock)` 配下に置き、その場で読み直してから追記する |
| API | `merge_opinion(tp, opinion, round_no)` — `base_hash` 引数を **削除**。watcher も snapshot hash を計算しない |
| 循環回避 | `atomic_write` を `roundtable/atomicio.py` へ切り出し (minutes → integrity の依存を作るため)。`minutes.atomic_write` は re-export で維持 |

「その場で読み直した hash で照合したら常に一致して無意味では」(内部レビュー #2) は、
照合先が *自分が今読んだ値* だった旧設計への指摘。ここでの照合先は **別ファイルに
記録された、dispatcher の書き込みでしか更新されない証跡**なので、席が minutes.md を
書き換えれば時点によらず検知できる。むしろ「snapshot 採取から merge までの窓」に
限定されていた旧方式より **検知範囲は広い**。

### H4 — 「証跡は席の書込範囲の外」は未検証の前提 (一部採用 / 修正案は却下)

> **更新 (2026-08-07 / 下記 H3 対応)**: この節の結論は **codex 席についてのみ有効**。
> **grok 席では前提が実測で破れている** — 席は `run_terminal_command` で PowerShell を
> 任意実行でき (Windows で grok の sandbox は一次 docs の Platform Support 表に不在)、
> spike 生ログ `raw-grok-p3-packet.jsonl` には絶対パス指定の
> `[System.IO.File]::WriteAllText` が残っている。cwd は境界として機能していない。
> したがって **grok 席が立った議題では `.integrity` の改ざん検知は成立しない**。
> 下記「grok 席では改ざん検知が成立しない」節を参照。

**採用した部分**: repo 内にも `references/` にも `workspace-write` の実効書込範囲を
示す一次情報が無く、`paths.py` / `integrity.py` の docstring は未検証の命題を断定で
書いていた。既存テスト `test_witness_lives_outside_the_seat_writable_area` も、
確かめているのは **path の入れ子関係だけ**で sandbox の実効範囲は一度も観測していない。
→ docstring を「未検証の前提」と明記する形に書き換え、テスト名から
`seat_writable_area` を外した (`test_witness_is_placed_outside_the_topic_directory`)。
検証していない範囲を名前で主張しないため。

**却下した部分**: 提案された「`turn/start` に
`sandboxPolicy: {"type":"workspaceWrite","writableRoots":[<議題ディレクトリ>]}` を
明示して構造で閉じる」は、**この穴を閉じない**。

| 根拠 | 一次情報 |
|---|---|
| `writableRoots` は **追加リスト**であって上限指定ではない。既定は `[]` で、`/tmp` と `$TMPDIR` は別枠の `excludeSlashTmp` / `excludeTmpdirEnvVar` で除外する形。「これらだけを書込可にする」と言えるフィールドが存在しない | `codex app-server generate-json-schema` → `ClientRequest.json` `definitions.SandboxPolicy` の `WorkspaceWriteSandboxPolicy` |
| したがって `writableRoots: [<議題ディレクトリ>]` は既に cwd として書込可の場所を足すだけ = no-op。サーバが暗黙に与える範囲は 1 バイトも削れない | 同上 |
| `thread/start` には `sandboxPolicy` 自体が無い (`sandbox: SandboxMode` のみ)。ポリシーオブジェクトは turn 単位でしか送れない | `definitions.ThreadStartParams` / `definitions.TurnStartParams` |

検証不能な RPC パラメータを足して「対応済み」にするのは、この repo が既に学んだ
「単体テスト green と運用で効いているは別物」(Tier1 timeout の節) を逆向きに踏む。
**閉じるには実測しかない**:

1. Tier1 で席に `<議題ディレクトリ>/../../.integrity/<slug>/probe` への書き込みを 1 回試させる
2. 拒否されることを spike 記録に残す (成功したら証跡機構は無効 = 置き場を repo 外へ動かす設計判断が要る)

このスモークは実 CLI (codex app-server) の起動を伴うため、本修正では未実施。
**それまで `.integrity` による検知は「席が届かない」前提の上に立っている**。

### 検証

- `tests/test_concurrency_high_findings.py` (8 本) を追加。reader を生 read ではなく
  `Journal.load` に通す並行テスト (レビュー T1) / 2 席並行 collect (レビュー T2) /
  minutes 全書き込み経路の証跡同期 / 改ざんは従来どおり fail-closed。
- **修正前の挙動を復元して落ちることを実測** (テストが本当にバグを捕まえる証拠):
  `_read_verified` をロック無しに戻すと reader/writer 並走で `StateTamperedError` 4 件、
  merge を snapshot hash 照合に戻すと 2 席目が `failed: tampered`。
- 元の再現スクリプトの前後: `repro3.py` 例外 1 件 → **0 件**、`repro.py R2`
  `B: {'ok': False, 'reason': 'tampered'}` → **`B: {'ok': True}` / 両方 merged**。
- 全 suite **158 → 166 passed** (exit 0)。

## 同レビューの MED / LOW (記録のみ / 未対応)

| # | severity | 内容 | 状態 |
|---|---|---|---|
| M1 | MED | `LockTimeout` が CLI 境界で未捕捉 → CEO に traceback、`last-result.json` も書かれない | **done (2026-08-07)**。H1/H2 修正で読み取りもロックを取るようになり露出が増えたため同時に対応。`cli.main` が `reason: "lock"` を書いて **exit 4**。PROTOCOL に exit code 表を追加 |
| M2 | MED | stale 回収された後の `release()` が所有権を検証せず他プロセスのロックを削除する。ロックファイルに `pid` を書いているのに読み返していない | **hold**。取得時に uuid token を書き、`release()` は自分の token の時だけ unlink する案。30 秒超のストールが前提なので発生頻度は低いが、起きると重ね合わせが不可分でなくなる |
| M3 | MED | `failed` / `merged` 済み invocation の再 collect が未捕捉 `ValueError: invalid transition`。`collect` 冒頭の冪等ガードが `refresh()` しない | **hold**。`stalled-tmp` が「席を直して再試行」を促す分類なのに、その再試行が traceback で落ちるので優先度は高い。`collect` 冒頭の `refresh()` + `failed` からの再開を明示コマンド化する案 |
| M4 | MED | 証跡ディレクトリを消すと `adopted: True` で無言に再 baseline される。`adopted` は `status` にも `last-result.json` にも出ない。しかも tamper メッセージ自身が「証跡を削除して再実行」と案内している | **hold**。H1 の偽陽性が消えたので削除手順の常用リスクは下がったが、`adopted` を CEO 出力に必ず出す対応は未実施 |
| M5 | MED | merge 先の Round 見出しに `journal.round_no` (グローバル) を使っており、invocation 自身の `rec["round"]` ではない。round 1 の意見が Round 2 節に入りうる | **hold**。`journal.data["invocations"][inv]["round"]` を渡すだけ。議事録は CEO が読む正本なので帰属の取り違えは静かに議論順序を壊す |
| L1 | LOW | `_merge_data` が既存 top-level key のローカル更新を黙って捨てる (`k not in merged`)。今は個別処理済みの 4 key しかないので実害なし | **hold** |
| L2 | LOW | `.gitignore` に `.integrity/` が無い。本文 (untracked) と証跡 (tracked) の片方だけが checkout/stash/clean で巻き戻ると恒久的な偽 tamper になる | **hold**。ignore するか、本文と原子的に commit する運用を明記するかの判断が要る |
| L3 | LOW | minutes.md の保護レベルが journal/seats に追いついていない (`sync_round` / `write_verdict` / `set_background` が無検証) | **done (2026-08-07)**。H3 の修正で全経路が証跡経由になった |
| L4 | LOW | 席由来の文字列 (`.tmp` ファイル名 / `detail`) を CLI 出力へ無検証で print。minutes 側は `_escape_body` / `_escape_cell` で潰しているのに出力面で規律が切れている | **hold**。Windows のファイル名は制御文字を弾くので実害は小さい |
| T3 | LOW | `LockTimeout` が CLI/journal 経路をどう伝わるかのテストが無い | **partial**。M1 対応の実測はしたが、回帰テストは未追加 |
| T4 | LOW | `test_stale_lock_is_reclaimed` は「回収された側が後から release する」系列を見ていない | **hold** (M2 とセット) |
| T5 | LOW | 「並行」テストの大半が単一スレッドの逐次呼び出し (= `_merge_data` の代数的性質のテスト) | **partial**。H1/H2/H3 用に実スレッドのテストを追加したが、既存分の性格は変えていない |
| T6 | LOW | `.tmp` 系 13 本はすべて擬似時計の単一スレッド。`.json` と `.tmp` の同時存在 / 救済後の再 collect / 別 invocation の `.tmp` 同居が未検査 | **hold** |

## 未確認事項 #1 クローズ (2026-08-07) — 席はアプリに表示される

DESIGN v6 §10 の「thread が Codex アプリのチャット一覧に出るか」を確認した。**出る。**

- 機械的裏取り: `thread/list` の 25 件に、アプリの通常チャット (音声セッションの
  引き継ぎ等) と**同列で** roundtable の席が入っている。
  `019fd900-... | rt-tier1-final-codex` / `rt-spike-codex` ×2。
  `thread/name/set` も効いており、preview には packet 本文が入っている。
- UI 表示: CEO が実機で確認済み (2026-08-07)。

これで chat-first の中核要件「席 = アプリ上の専用チャット / CEO が直接読める /
会話履歴がアプリに残る」(DESIGN v6 §0) が**実測で全部満たされた**。

~~残る Tier1 の穴は「CC 席がまだ Tier3」だけ (Codex のみ Tier1)。2 席の議題では
CC の分だけ貼り付けが残るため、KPI ≤ 3 は 1 席議題でのみ達成できている。~~
→ 前提が 2 つ変わった: (a) **CC は席にしない** (D11 / 2026-08-07 CEO 判断) ので
「CC 席が Tier3」という穴は消滅した。(b) **Grok 席の Tier1 が入った**
(`roundtable/relay_grok.py`)。2 席議題の Tier1 全席化については下記「2 席 Tier1 の
統合検証」節を参照。

## 2 席 Tier1 の統合検証 (2026-08-07 / 模擬席)

Grok Tier1 (`relay_grok.py`) 追加後、**codex 相当 + grok 相当の 2 席で 1 議題 2 round**
を通し、軸 A KPI (人間の手数) が席数・round 数で増えないことを機械的に確認した。

検証: `tests/test_v02_two_seat_tier1_mock.py` (8 本)。**実 CLI は起動していない** —
codex app-server / grok ACP の口を fake stdio に差し替え、`cli.main` は本物のまま
4 回 dispatch する。模擬席は packet の指示どおり `<inv>.json.tmp` → rename で確定させる
(契約をテストの都合で緩めない)。

| 観点 | 結果 |
|---|---|
| 軸 A KPI (Tier1) | `human_actions` = `new-topic` ×1 + `dispatch` ×4 = **5 件**。`tier3_paste_required` **0 件** |
| 対照実験 (同手順 Tier3) | **9 件** (= 5 + 貼り付け 4)。差分 4 件がそのまま「自動化で消えた手数」 |
| 席の同一性 | 2 round 目は codex が `thread/resume` (同じ `threadId`)、grok が `session/load` (同じ `sessionId`)。新規作成に落ちない = 席が分裂しない |
| 議事録 / round | Round 1・2 に 2 席分が merge、`round` は機械的に 3 へ。`conflicts` 0 |
| 縮退の粒度 | grok 席だけ落とすと **その席だけ** tier=3 + `fallback_reason`。codex は tier=1 のまま、貼り付けも 1 件だけ増える |
| 書込境界 | 2 席とも `cwd` = 議題ディレクトリ (`thread/start` / `session/new` の params で固定) |

**テストが本当にバグを捕まえる証拠** (実装を壊して落ちることを確認):

| 変異 | 落ちたテスト |
|---|---|
| `relay_codex.send` が `thread_ref` を無視して毎回新しい席を作る | `test_second_round_reuses_the_same_seat_on_both_relays` |
| `cli` が Tier1 でも `tier3_paste_required` を記録する (`if relay.tier == 3` → `if True`) | `test_tier1_seats_do_not_add_human_actions` / `test_one_tier1_seat_failure_does_not_drag_the_other_down` |

全 suite **199 → 207 passed** (exit 0)。

### この検証で言えないこと

- **模擬席であって実席ではない**。「Tier1 席では人間の手数が増えない」を担保するのは
  dispatcher 側の配線 (packet 搬出 → 回収 → journal) までで、実 grok 席が実 packet で
  同じ経路を通るかは別問題。grok の実往復は spike (2026-08-07 / 5 turn) で確認済みだが、
  **roundtable の議題を 2 席同時に回す実往復スモークは未実施**。
- fake は即答するので **timeout 枠の妥当性は一切検証していない** (数値の不変条件は
  `test_timeouts_have_margin_over_measured_latency` 側で別途固定)。codex 版で
  「単体テスト green なのに Tier1 が 100% 失敗」を踏んだのと同じ構造の穴は残る。
- grok 席の `session/request_permission` は、この統合テストの模擬席では発生させていない
  (単体は `test_v02_relay_grok.py` が担当)。実席では許可要求が入るため、
  **実往復では手数が増えない保証はまだ無い** (`allow_once` 自動応答経路そのものが未実測)。

## 敵対レビュー (2026-08-07 / grok Tier1 追加後 207 tests green の状態に対して) — HIGH 対応

`roundtable/*.py` 15 本 + `tests/*.py` 19 本 + spike 生ログ (`.pytest-tmp-sp/`) を
突き合わせたレビュー。HIGH 3 件を修正、MED/LOW は下表に記録のみ。
**移設 (`_ProcessTree` → `relay_process.ProcessTree`) 自体は docstring 以外同一で
codex 側の退行はゼロ**という判定はこちらでも確認した (挙動を変えるコードが無い)。

### H1 — 失敗した round で `permission_log` が前 round のまま残る (修正)

`GrokAcpRelay.send` は `seat["permission_log"] = list(self.permission_log)` を
**`session/prompt` 成功後にしか書いていなかった**。コード自身が「空でも書く / 書かないと
前ラウンドの記録が残り続ける」と宣言しているのに、失敗パスでその不変条件が丸ごと崩れる。

再現 (fake stdio で「許可を 1 件出した直後に席が落ちる」):

```
RelayError: session/prompt: grok agent が終了した rc=1
relay.permission_log  : [{"tool": "execute", "decision": "allow-once", ...}]  ← 実際に出した許可
seat["permission_log"]: [{"tool": "write",   "decision": "allow-once", ...}]  ← 前 round の記録
```

破綻は 2 方向ある。(a) round2 で `execute` を許可した直後に落ちて Tier3 縮退すると、
`save_seats` が round1 の記録を書き戻し、CEO は「tier=3 なのに write を許可した記録がある」
という現実と無関係な状態を読む。(b) **より重いのは逆向き** — この round で実際に出した許可
(= 席が PowerShell を走らせた事実) が記録から消える。`_decide_permission` の docstring
どおり書込境界は OS 強制ではなく記録が唯一の証跡なので、失敗時に消えると監査の意味を失う。

**修正**: `send()` を `try/finally` にし、成否によらず `seat["permission_log"]` を書く。
turn 未完で抜けた場合は `seat["permission_log_partial"] = True` を立てて
「全部の許可要求を見たとは言えない」ことを区別する (成功時は pop で消す)。

### H2 — `close()` に本番の呼び出し元が無く、POSIX の孤児回収が到達不能だった (修正)

relay の `close()` を呼ぶ本番コードは **ゼロ**だった (呼んでいたのはテストだけ)。
Windows は Job Object の `KILL_ON_JOB_CLOSE` により Python 終了時にハンドルが閉じて
木ごと死ぬので偶然助かっていたが、POSIX は `relay_process.kill_tree` の `killpg` が
一度も走らず、CLI 終了後に席のプロセス木 (実測 **1 席 10 プロセス**) が孤児として残る。
`relay_process.py` の docstring が両 OS 等価であるかのように書いていた点も含めて穴だった。

**修正**:

| 変更 | 内容 |
|---|---|
| `relay.Relay` | 契約に `close()` を追加 (module docstring に経緯) |
| `relay_tier3.Tier3Relay` | no-op の `close()` (呼び出し側が `hasattr` で分岐しなくて済む) |
| `relay.FallbackRelay` | preferred / fallback の **両方**に委譲。`_active` だけでは足りない (プロセスを起こしたのは失敗した preferred の方)。`close` 非装備の相手は素通り |
| `cli._cmd_dispatch` | `try/finally` で `_close_relay(relay)`。**collect の後**に閉じる (codex の `turn/start` は投げっぱなしなので、前に閉じると実行中の席を殺す) |
| `cli._close_relay` | 回収の失敗で dispatch の exit code を変えず、stderr には出す (黙って捨てない) |

`--async` は「搬出だけして席を動かしたまま返る」契約なので **閉じない**。
なお「`--async` + Tier1 は Windows では dispatch プロセス終了時に Job が閉じて席が死ぬ」
という既知の穴 (上記「残る穴 (v0.3)」) は本修正で変わっていない。relay 側に
「send が返った時点で turn が完了しているか」を持たせて分岐する案 (レビュー提案) は
契約変更を伴うので v0.3 の設計課題として保留する。

### H3 — grok 席では改ざん検知が成立しないのに SSOT も runtime も黙っていた (修正)

`.integrity` の検知は「席が証跡に届かない」前提の上に立っている (H4 節)。ところが
`relay_grok._decide_permission` の docstring が同じ前提を **実測で否定**していた:
Windows で grok の sandbox は一次 docs の Platform Support 表に載っておらず、実測でも席は
`run_terminal_command` で PowerShell を任意実行できた。spike 生ログでも裏が取れる
(`.pytest-tmp-sp/raw-grok-p3-packet.jsonl`: `toolCall.kind: "execute"` で
`[System.IO.File]::WriteAllText($tmp, ...)` を**絶対パス指定**で実行。cwd は境界として
機能していない)。にもかかわらず H4 節は「未検証」のままで、CEO が画面から知る手段も無かった。

**修正**:

| 対応 | 内容 |
|---|---|
| SSOT | H4 節の冒頭に「codex: 未検証 / **grok: 実測で破れている**」を明記。`paths.TopicPaths.integrity` / `integrity.py` の docstring も席ごとに書き分け |
| runtime | `cli._warn_if_integrity_detection_does_not_hold` — `tier==1 and participant=="grok"` の dispatch と `status` で必ず印字する。**検知できないなら、検知できないことを検知可能にする** |
| status | 席ごとの `tier` / `fallback_reason` / 許可件数 (partial マーク付き) を表示するようにした (従来 tier はどこにも出ていなかった) |

**未対応 (設計判断 / CEO 確認事項)**: 許可の記録を `seats.json` (= 監査対象が書き換えられる
場所) から repo 外の append-only ログへ逃がす案は H4-2 として保留。監査記録を監査対象の
書込範囲に置いている構図自体は残っている。

### 検証

- 追加テスト 7 本 (`test_v02_relay_grok.py` 3 / `test_v02_two_seat_tier1_mock.py` 4)。
- **テストが本当にバグを捕まえる証拠** (実装を壊して落ちることを実測):

  | 変異 | 落ちたテスト |
  |---|---|
  | `send()` の `finally` を消す (失敗時に `permission_log` を書かない) | `test_permission_log_is_written_even_when_the_turn_fails` |
  | `_cmd_dispatch` が `_close_relay` を呼ばない | `test_dispatch_closes_the_seat_process_after_collect` |
  | grok 席の警告を出さない | `test_grok_tier1_dispatch_says_integrity_detection_does_not_hold` / `test_status_surfaces_the_seat_tier_and_the_grok_warning` |

- 全 suite **207 → 214 passed** (exit 0 / 46.6s)。

## 同レビューの MED / LOW (記録のみ / 未対応)

| # | severity | 内容 | 状態 |
|---|---|---|---|
| M1 | MED | packet の tmp→rename 契約が、席を唯一の境界破り手段である `execute` に追い込んでいる。spike 対照実験: 素の書き込みのみ (p1) は許可要求 **0 件** / 実 packet (p3・run1) は **1-3 件すべて `execute` の PowerShell**。rename ツールが無いのでシェルに逃げており、H3 の境界破りと軸 A の手数増の唯一の発生源になっている | **hold**。案 (a) Tier1 席には `.json` 直書きを許し原子性を dispatcher 側 (`watcher._read_stable` + parse + id 照合 + schema) に寄せる / 案 (b) `clientCapabilities.fs` を実装して `fs/write_text_file` を client で受ける。どちらも要 spike。当面は「rename 要求が permission prompt を生む」を既知コストとして本行で記録する |
| M2 | MED | `AUTO_ALLOW_KINDS` の既定 `("allow_once",)` は実測 4 回すべてで一度も選ばれていない (spike client は `("allow_always","allow_once")` で走り、`permission_grants` は全部 `always-allow`)。「未実測の経路を既定にし、実測済みの経路を既定から外した」形。なお run1 は `always-allow` を選んでも **3 回聞かれている** = `allow_always` は session 内ですら永続していない (「承認が席を跨いで残る」懸念は spike の範囲では未観測) | **hold**。`allow_once` で 1 turn の実往復 spike を回して `docs/spike-results-v0.2.md` に追記する。それまで grok Tier1 は既定にせず `--tier 1 --participant grok` の明示操作に留める |
| M3 | MED | 共通化の切り出し面が違う。`ProcessTree` は codex/grok 完全同一だったので共有は正しいが、**分岐しているのは transport 層**で重複が残る: (a) codex は `stderr=PIPE` を誰も読まない (Win 64KB で埋まると app-server がブロック → `turn/start` が 120s 満了して毎回 Tier3 縮退) (b) codex の deadline が `time.time()` (壁時計 / NTP 補正で枠が伸縮) (c) codex は `queue.Empty` 時に `proc.poll()` を見ないのでプロセス死亡後も 300s 待ち切る | **hold**。少なくとも stderr drain は codex へ backport すべき。ACP 統一時に `_read_loop` / `_rpc` / `_send_raw` / `close` を `relay_acp.py` へ上げる案 |
| M4 | MED | `thread_ref` に修復経路がない。`session not found` で `_load_session` が落ちると恒久 Tier3 になり、`merge_seats` がディスク側から復元するため **relay 側から席 id を消せない**。grok は session ストアが cwd スコープで TUI からも掃除されうるので codex より踏みやすい | **hold**。`seat["thread_ref_retired"] = <old id>` のような明示的無効化を `merge_seats` に通す案。retire は journal に human-readable な 1 行として残す |
| M5 | MED | 縮退の瞬間に席が分裂する。`_new_session` が `thread_ref` を書いた **後**に prompt が timeout すると、CEO は Tier3 で別チャットに貼り、`thread_ref` が指す ACP session はその round の packet を見ていない。次 round は空の session を `session/load` する | **hold**。`seat["thread_ref_desync_round"]` を立てて `status` で提示する案 + 既に `<inv>.json` / `.tmp` があるなら Tier3 貼り付けを促さない (二重送信抑制) |
| M6 | MED | `tests/test_v02_two_seat_tier1_mock.py` は「**実 CLI は起動しない**」と宣言しているのに、`test_relay_wiring_picks_one_transport_per_participant` だけ `bench` fixture を取らず本物の `CodexAppServerRelay` を構築する → `resolve_codex_binary()` が PATH 上の全 codex 候補に `--version` を exec (実測 2 候補 × 約 1s / `_probe_version` の timeout は 30s なので最悪 60s ブロック)。また `.github/` が無く **CI が存在しない**ので、この suite は CEO の Windows 機でしか観測されていない | **hold**。当該 test に `bench` を取らせるか `resolve_codex_binary` を monkeypatch し、加えて `conftest.py` の autouse fixture で `_probe_version` を封じて宣言をテストで強制する案 |
| L1 | LOW | `_ensure()` の再 spawn 時、古い `self._tree` を `close()` せず上書きする (codex/grok 同型)。Windows では leak したハンドルが `KILL_ON_JOB_CLOSE` を止める | **hold**。`old = self._tree; old.close()` の 1 行 |
| L2 | LOW | `permission_log` のレコード形が 2 種類 (`{tool, method, decision}` と `{tool, decision, kind}`)。seats.json に混在するので機械集計で躓く | **hold** |
| L3 | LOW | `_handle_request` が `params["sessionId"]` を照合しない。今は 1 プロセス 1 session なので実害なし。`sessionCapabilities.list` を使い始めた瞬間に別席の許可要求へ答える | **hold** |
| L4 | LOW | `stopReason` を分類しない判断自体は妥当だが、`refusal` / `cancelled` も `delivered` として journal に入り、collect の 900s 後に `failed: timeout` に化ける。`failure_stats` 上「無応答」と区別できない | **hold**。`stopReason != "end_turn"` のとき `_write_last_result` に生値を載せるだけで切り分けが効く |
| L5 | LOW | `cli` が全席に `seat["thread_name"]` を設定するが、grok は `rename_seat` が `NotImplementedError` なので黙って捨てられる。「設定したのに効かない席がある」ことがどこにも書かれていない | **hold** |

## arXiv 2512.08296 の引用を撤回 (2026-09-01)

README / DESIGN の D1 根拠として挙げていた「検証を挟まない誤りの伝播は増幅する —
独立並列で 17.2 倍 / 網目状で 7.8 倍 / 中央集権型で 4.4 倍」を**撤回する**。
数値と出典の帰属は正しいが、**指標の意味を読み違えていた**。

原典 (Towards a Science of Scaling Agent Systems) を読み直した結果:

| 論点 | 実際 |
|---|---|
| 17.2 の指標 | `A_e^trace`。**誤り率ではなく**「協調失敗から生じる余分な計算量」を実行トレースのトークン解析から推定した値 |
| 誤り率ベースの指標 | `A_e^task` = E_MAS / E_SAS で **1.1–1.3 倍**。17.2 とは桁が違う |
| 統計的有意性 | 誤り増幅の主効果は **非有意** (β=0.014, p=0.658)。tool 数との交互作用も非有意 (p=0.332) |
| 論文自身の結論 | アーキテクチャ間の性能差は誤り伝播ではなく「効率 (E_c) とオーバーヘッド (O%)」で説明される、としている |

つまり **論文が「有意でない」と結論した効果を、単位を読み替えて設計の権威付けに使っていた**。
公開 repo で原典を開いた読者には、そう読まれる。

### 決定自体は維持する

D1 (司会 = 人間) は撤回しない。根拠を**構造の話に置き換える**:
席から席へ意見が中継される経路が実装上存在しないので、伝言ゲームが起きる場所が無い。
これは実装を読めば確認でき、外部の研究に依存しない。

### なぜ見逃したか

2026-09-01 に一度この引用を「検証」している。その時は Table 5 の
Independent 17.2 / Decentralized 7.8 / Centralized 4.4 という**数値の一致と帰属**だけを確認し、
`A_e^trace` が何を測っているかを確認しなかった。数字が合っていたので通してしまった。
**「出典が実在し数値が一致する」ことと「その数値が主張を支える」ことは別**。
二次情報の一次検証は、数値照合ではなく定義の確認まで行って初めて成立する。

## PR #22の追加レビュー対応（2026-09-08）

| 指摘 | 根本修正と回帰確認 |
|---|---|
| P1: 送信中の回答をpending回収すると状態遷移が壊れる | 既存invocation排他で送信から状態確定までを保護。回収は解放後に実行。巡回はbusy席を待たず他の回答へ進み、次回に再確認する |
| P2: 同じ通知先UUIDの表記差を拒否する | 準備・記録引数・receiptを同じUUID正規化へ統一。不正値と別UUIDは引き続き拒否する |
| P2: 取消の不正invocationでtraceback | 構造化したinvalid-invocationの失敗結果を返す |
| P2: 採用commit後の停止でscratch差替えを失敗扱い | validatedの保存hashと議事録の採用証跡を先に照合。scratch欠落も巡回で発見し、採用済み回答を復旧する |

統合後の関連試験は68件成功。追加したロック待機引数の宣言漏れは独立レビューで検出し、修正後に再実行した。
配布依存とconsole入口もCIで検査するため、パッケージを導入しcheckout外でCLIを起動する工程を追加した。

試験のbasetemp誤指定で発生したローカルの議事録commitは保全branchへ分離し、提出branchは元のPR HEADから復旧した。
試験履歴はリモートへ送っていない。再発防止として、checkout内のgitignore対象外basetempをfixture生成前に拒否する。
製品branchのHEAD不変と拒否・許可条件の回帰を確認した。

配布版CIのWindows実行で日本語helpのUnicodeEncodeErrorを検出した。プロセスのCLI入口だけで
stdout/stderrをUTF-8へ設定し、console scriptと`python -m`の両入口を統一した。
ライブラリとして呼ぶ`main(argv)`は変更せず、cp1252環境を再現する2回帰と独立レビューが通った。
