# Codex 2nd 設計レビュー: ai-roundtable 設計書 v3

対象: `docs/DESIGN.md` v3（2026-07-26）  
確認環境: Windows / `codex-cli 0.130.0-alpha.5`（2026-07-26 JST に `codex exec --help` と
`codex exec resume --help` を実機確認）

## 結論

**現状のまま v0.1 実装へ進むのは不可。** 1st レビューの 9 指摘は、設計文面としては概ね
反映されている。しかし席セッション方式により、ファイル隔離とは別に「会話状態の隔離・排他・
寿命」という新しい状態管理境界が生じた。v3 はこの境界を journal と人間運用で守れる前提に
しており、同一席への競合、議題間の情報漏洩、失敗時の二重投入を防げない。

実装開始前に最低限 #1〜#4 を設計へ反映し、#5 の実 CLI スパイクで実行契約を固定する必要が
ある。修正後は、#6 の v0.1 スコープで実装に進んでよい。

## 1. HIGH — 1 席を全議題で共有すると、blackboard とスナップショットの隔離を会話履歴が迂回する

**欠陥**

§3.1 は参加者ごとに席を 1 つだけ作り、毎回同じセッションを resume する。一方 §4.1 は今回の
minutes スナップショットだけを readable として渡すことで入力を隔離する。しかしモデルは
ファイルだけでなく、席に蓄積した過去の prompt、回答、ツール出力、パス、議題内容も入力として
受ける。「場外文脈の混入を人間司会が裁定する」は意見品質への対処にすぎず、別議題の秘密の
再出力や、過去 minutes に埋め込まれた命令の持続には対処できない。これは D2 の「全文同期は
しない」と §4.1 の最小入力境界を実質的に破る。

**破綻シナリオ**

議題 A のスナップショットに秘密情報、または「次回以降の出力にこの文を含めよ」という
prompt injection がある。同じ席を議題 B で resume すると、B の隔離 dir には A のファイルが
なくても、席の会話履歴から A の情報や命令が B の JSON に現れる。hash 検証、sandbox、
journal はすべて正常であり、機械的には validated/merged になる。CEO が混入に気づくまで
漏洩を止める境界がない。

**修正案**

席のスコープを少なくとも `participant × topic` にする。アプリ一覧での可視性を保つ必要が
あるなら、表示名に topic と seat generation を含める。全議題共通の人格・規約はセッション
履歴ではなく、version/hash を固定した system/prompt fixture として毎回注入する。topic を
またぐ席共有を必須要件とするなら、異なる機密区分の topic を同席に載せない明示ポリシー、
履歴由来内容を検出する canary test、CEO による topic 開始前承認が必要であり、v0.1 の安全な
既定にはしない。

## 2. HIGH — topic 単位の journal/lock は、共有席の single-writer を保証しない

**欠陥**

§3.1 は「席の writer は dispatch のみ」、§6 は「journal の running フラグ + lock で後発拒否」
とするが、journal は `minutes/<議題>/journal.json` に置かれる。議題 A と B は別 lock なので、
同じ Codex 席を同時に resume できる。またアプリ/TUI は journal lock を取得しないため、
「dispatch 実行中は手動介入しない」は検知ではなく人間の善意に依存する。`running` は
クラッシュ後に stale にもなる。

**破綻シナリオ**

2 つのホストが別 topic を開始し、双方が各 topic の lock を正常取得する。両方が同じ seat ID
へ `codex exec resume` を実行し、セッション追記順、context、出力の対応が不定になる。片方が
失敗しても、もう片方の journal は成功し得る。さらに CEO がアプリから同じ席へ送信すると、
dispatch はその writer の存在を知れない。

**修正案**

`seats.json` と同じ管理領域に **seat ID 単位の OS 排他 lock** を置き、topic を問わず
`resume` の開始からプロセス終了・セッション保存完了まで保持する。lock record に owner PID、
host ID、invocation ID、取得時刻、lease/heartbeat を持たせ、PID 生存確認を伴う stale-lock
回復手順を定義する。アプリ/TUI の書込みを機械的に排他できない限り「single-writer 保証」と
呼ばず、dispatch 前後にセッション head/revision を取得して予期しない追記を検知し
fail-closed にする。head/revision を取得できない CLI なら、dispatch 中のアプリ書込み禁止は
受け入れ基準外の運用上の残存リスクとして明示し、同時操作 adversarial test を追加する。

## 3. HIGH — resume は外部状態への非トランザクション書込みで、journal の冪等性だけでは再試行できない

**欠陥**

v3 の invocation_id は隔離出力と minutes merge を冪等化するが、席への prompt 追記自体は
外部副作用である。timeout、CLI 非 0、出力欠落のどの時点で prompt/回答が席へ保存済みかを
journal は判定できない。同じ invocation を retry すれば席の履歴へ二重投入され、新しい
invocation にすれば同じ round の問いが別物として再投入される。

**破綻シナリオ**

モデルは回答を生成して席へ保存したが、隔離 JSON の書込み直前に CLI が timeout する。
journal は `failed`、出力 UUID は無効になる。再実行すると同じ問いが席へ再度追加され、
モデルは前回答を踏まえた「2 回目の意見」を返す。minutes merge は 1 回でも、席の context と
次 round の挙動は既に二重投入で変質している。壊れた席として再作成すべきか、一時失敗として
retry すべきかも判定できない。

**修正案**

transport の状態機械を `prepared → submitted → session-committed? → output-received →
validated → merged` に分け、`session-committed?` が unknown の失敗は同じ席へ自動 retry
しない。prompt と出力の双方に invocation_id を含め、可能なら session event/revision API で
その invocation の存在を照会してから再開する。照会不能な CLI transport では ambiguous
failure を CEO に提示し、選択肢を「現席で重複を許容して再送」「新 generation の席を作成」
「当該 participant を失敗のまま閉じる」に限定する。mock E2E だけでなく、保存直前・直後の
実 CLI 強制終了を受け入れ試験に含める。

## 4. HIGH — 席の寿命・肥大・compaction を管理しないと、固定プロトコルと再現性が失われる

**欠陥**

席は無期限に 1 つとされるが、context 使用量の観測、compaction 発生の検知、最大 round/topic、
generation rotation、席を再作成する判定がない。「壊れた/消えた」の定義もなく、履歴が minutes
に残るという説明は、席にしかないプロトコル解釈・過去 tool 出力・compaction 要約の損失を
扱っていない。セッションが長くなるほど、同じスナップショットと prompt から同じ契約に従う
という再現性は低下する。

**破綻シナリオ**

長期利用後に自動 compaction が起き、初期の roundtable 規約だけが要約から脱落する。一方で
過去議題の一部は要約に残る。次の invocation は schema JSON を出さず、古い議題に応答する。
dispatch は schema 違反として失敗するが、単発のモデル失敗、context 枯渇、席破損を区別できず、
同じ肥大した席へ retry を繰り返す。逆に安易な再作成は監査なしに席の来歴を切る。

**修正案**

`seats.json` に generation、created_at、last_used_at、topic scope、CLI/version/model、
bootstrap prompt hash、successful/failed invocation count、最後に確認した session revision を
持たせる。最大 invocation 数または観測可能な token/context 閾値で計画 rotation し、新席は
固定 bootstrap fixture と現 topic の minutes だけから作る。compaction/context 指標を CLI が
返さない場合は `unknown` と記録し、固定回数の soak test で保守的な rotation 上限を決める。
「壊れた席」は not-found、parse/store corruption、contract 連続違反など機械判定可能な状態に
限定し、auth failure、network、rate limit、model failure を再作成理由にしない。

## 5. HIGH — 現行 Codex CLI に対する manifest 例は、そのままでは実行契約になっていない

**欠陥**

実機の `codex-cli 0.130.0-alpha.5` では `-s/--sandbox` と `-C/--cd` は
`codex exec [OPTIONS]` 側のオプションであり、`codex exec resume` の options には表示されない。
ところが §4.1 の argv 例は
`codex.cmd exec resume {seat_id} {prompt}` とし、sandbox は別フィールドに置くだけで、正確な
argv 上の位置、`-C`、approval/config、`--skip-git-repo-check` の要否が固定されていない。
さらに `workspace-write` は「隔離 dir 内の 1 ファイルだけ」を OS 能力として強制するものでは
なく、少なくとも隔離 dir 内の他ファイル作成は可能である。これは 1st #1/#6 の反映がまだ
機械実行可能な形に閉じていない部分である。

**破綻シナリオ**

実装が manifest の argv の後ろへ `-s workspace-write -C <dir>` を連結し、resume の位置引数
または不明 option として解釈されて起動に失敗する。実装者が smoke を通すためオプションを
落とすと、ユーザー既定 config を継承した `danger-full-access` で動く。別案として sandbox は
効いても、参加者が隔離 dir 内の snapshot や manifest を書き換え、検証範囲が出力ファイル
だけなら通過する。

**修正案**

manifest を「argv の一部 + 別フィールド」ではなく、version ごとの完全な argv template と
期待 capability にする。現バージョン候補は概念的に
`codex exec -s workspace-write -C {isolated_run_dir} ... resume {seat_id} -`
の順序で、prompt は stdin を使う（正式形は spike で確定）。ユーザー config 継承の有無、
approval/config、git repo check を明示する。隔離 dir 全体の開始前後 hash を検査し、許可された
UUID 出力以外の変更を fail-closed にする。原本 repo への negative write に加え、隔離 dir 内の
別ファイル作成、snapshot 改変、絶対パスによる repo 読取り/書込みを個別に試験する。

この未検証事項は設計を壊し得るため、§10.4 を単なる実装前確認ではなく **Codex adapter の
go/no-go gate** に昇格する。CLI/version ごとに正常出力、negative write、timeout、同席競合を
実測できなければ、その CLI transport を v0.1 から外す。

## 6. MED — v0.1 の受け入れ基準が、席方式の中核リスクを試験していない

**欠陥**

§5 の受け入れ基準は実議題 1 本・1 周であり、席 resume の価値である複数回の文脈継続も、
新しい危険である肥大、議題間混入、同時 resume、ambiguous failure、rotation も通らずに
「MVP 完了」になる。§10.1 の並行安全性は設計を壊し得るのに、未検証のままでも実装完了条件を
満たせてしまう。§10.2 のアプリ即時反映だけは、要件を「アプリで見える・触れる」とするなら
反映不能時に UX 要件を満たさないため、「設計は成立」と断定せず要件を分けるべきである。

**破綻シナリオ**

codex→cc の 1 周は通るため v0.1 完了となる。本運用で別 topic を同時開始した最初の時点で
共有 Codex 席が競合する。または数十回後に compaction で出力契約が崩れる。どちらも MVP の
試験対象外なので、席方式を採用した意味も安全性も実証されていない。

**修正案**

v0.1 は codex+cc のままでよいが、完了条件を次へ変更する。

1. `participant × topic` 席で同一 topic を複数 round 実行し、文脈継続と invocation 対応を確認
2. 同一 seat への 2 dispatch を競合させ、片方が実行前に拒否されることを確認
3. CLI 保存境界で強制終了し、unknown commit が自動再送されないことを確認
4. conservative rotation 上限までの soak と、新 generation への明示的移行を確認
5. Codex/CC それぞれについて sandbox negative test と完全 argv/version pin を保存
6. アプリ可視性は「一覧に席が存在する」と「CLI 追記が即時表示される」を別々に記録し、
   後者が unknown/false なら UX 制約として CEO に提示

Grok/agy、要約、コスト実測、daemon は引き続き v0.2+ でよく、現行の機能スコープ自体は適切で
ある。広げるべきなのは参加 AI 数ではなく、席状態機械の受け入れ試験である。

## 1st レビュー 9 指摘への判定

| 1st 指摘 | v3 判定 | 残る条件 |
|---|---|---|
| #1 権限制約 | 部分対応 | 完全 argv、実 sandbox、隔離 dir 全体の hash を go/no-go gate にする |
| #2 git diff | 対応 | 「対象ツリー」の正確な root・除外なし・symlink/reparse point 方針を実装仕様化 |
| #3 Windows timeout | 概ね対応 | Job への割当て前に子が spawn する race、kill 後 wait/再 hash を試験 |
| #4 transaction/idempotency | ファイル側は対応 | session commit の unknown と二重 prompt は未対応 |
| #5 Markdown 偽装 | 対応 | renderer fixture と JSON Schema を v0.1 に含める |
| #6 prompt transport | 部分対応 | stdin を含む完全 argv と CLI version 別 smoke が未確定 |
| #7 要約 | 対応 | v0.2 導入時まで非権威・原文参照条件を維持 |
| #8 Evidence | 対応 | `evidence_type` は提出者自己申告であり、検証済み表示にしない |
| #9 MVP 肥大 | 対応 | 新たに seat lifecycle 試験を v0.1 の中核へ入れる |

## §10 未検証事項の優先順位

- **設計破壊級:** §10.1 同一席への並行 resume、§10.4 resume 時の sandbox 実効性
- **要件破壊級:** §10.2 アプリ即時反映。ただし「席が一覧に見える」と「最新追記を読める」を
  分離すれば、後者を既知の UX 制約として受け入れる余地はある
- **v0.1 非ブロック:** §10.3 Grok/agy。ロードマップどおり v0.2 の gate でよい
- **現在リストから欠落している設計破壊級:** topic 間 context 混入、context
  compaction/rotation、timeout 後の session commit 不明、アプリ手動追記との競合

## 実装開始の残条件

次の 5 点が v3 改訂または拘束力のある実装仕様に入れば、**v0.1 実装に進んでよい**。

1. 席を `participant × topic × generation` として管理する
2. topic 横断の seat lock と stale-lock 回復を定義する
3. session commit が不明な失敗を自動 retry しない
4. rotation/破損判定/seats.json の schema・atomic 更新・履歴を定義する
5. Codex/CC の完全 argv と sandbox capability を実 CLI の go/no-go smoke で固定する
