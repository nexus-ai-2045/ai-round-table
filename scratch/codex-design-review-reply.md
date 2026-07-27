# Codex 設計レビュー: ai-roundtable 設計書 v2

対象: `docs/DESIGN.md` v2（2026-07-26）  
方針: CC の既存指摘の繰り返しではなく、v2 の修正後にも残る穴と、修正によって新たに生じた実装上の問題を優先した。

## 1. HIGH — Codex の権限制約が設計されておらず、D9 を実機で保証できない

**欠陥**

「参加者は `minutes.md` を読めるが、自分の scratch だけに書ける」という能力分離を要求している一方、`codex exec` の正確な起動ディレクトリ、sandbox mode、追加 writable root、承認モード、prompt の渡し方が定義されていない。単にリポジトリ root で通常の workspace-write を使えば、Codex は `minutes.md` にも `docs/` にも書ける。逆に read-only では scratch に書けない。

**破綻シナリオ**

`dispatch.py` が repo root を cwd にして `codex exec` を起動する。Codex はプロンプトを善意に解釈して scratch を書く通常経路では成功するため smoke も通る。しかし誤ったパス解決やプロンプトインジェクションを受けた回だけ `minutes.md` を直接編集でき、D9 の「機構で守る」が成立しない。CLI の既定値が将来変わった場合も同様である。

**修正案**

参加者ごとに「実コマンド配列、cwd、sandbox、承認設定、readable path、writable path」を adapter の機械可読 manifest として固定する。Codex は隔離した実行ディレクトリを writable root とし、入力 minutes は書込不能なスナップショットとして渡し、出力先はその隔離ディレクトリ内の単一ファイルにする。dispatch 後に書記が検証済み出力を本来の scratch へ移す。実 CLI smoke は「正常出力」だけでなく、参加者に `minutes.md` と repo 内の別ファイルを意図的に変更させ、両方が拒否される negative test にする。

## 2. HIGH — `git diff` assert は直接改ざん、未追跡ファイル、dirty baseline を完全には検出しない

**欠陥**

`git diff` は作業ツリー状態に依存し、既定では未追跡ファイルを表示しない。また、「merge 後に追記された 1 セクションのみ」を見るだけでは、そのセクションを participant が merge 前に直接書いたのか、書記が scratch から書いたのかを識別できない。事前に存在するユーザー変更との境界も未定義である。

**破綻シナリオ**

新規 topic の `minutes.md` が未追跡のままなら `git diff` が空になり、改ざん検出自体が働かない。追跡済みでも、participant が `minutes.md` に期待形のセクションを先に追加し、scratch に同じ本文を書くと、最終差分の形だけを検査する実装は来歴を証明できない。さらに participant が別の未追跡ファイルへ書いても検出されない。

**修正案**

Git を整合性境界に使わない。dispatch 開始時に許可対象を含む repo ツリーのパス一覧・種別・内容ハッシュを採取し、participant 終了直後かつ merge 前に再採取する。許可された隔離出力以外の作成・削除・変更があれば fail-closed にする。minutes は開始時スナップショットのハッシュと一致する場合だけ、書記が構造化出力から新ファイルを生成し、`os.replace` する。`git diff` は人間向け補助証拠に格下げする。

## 3. HIGH — Windows の timeout がプロセスツリーを止めず、「失敗後の遅延書込み」が起きる

**欠陥**

`CREATE_NEW_PROCESS_GROUP` を使わない方針だけがあり、timeout 時に CLI が生成した子・孫プロセスをどう終了・回収するかがない。Python の `Popen.kill()` / `terminate()` だけでは通常、Windows の子孫プロセス全体の終了保証にならない。

**破綻シナリオ**

CLI 親プロセスが 600 秒で kill され、dispatch は failure を記録して lock を解放する。しかし CLI が起動したランタイム子プロセスが残り、数秒後に scratch を書き終える。次 participant または retry が同じファイル名を使うと、検証後の内容が差し替わる。書記は「失敗」と記録した出力を後続 round で誤って merge し得る。

**修正案**

各 dispatch を Windows Job Object に割り当て、`KILL_ON_JOB_CLOSE` でプロセスツリー終了を保証する。利用ライブラリを増やさないなら、Windows 専用 runner を明示して同等のツリー kill と wait を実装する。出力は invocation UUID を含む一回限りのパスへ書かせ、timeout 後はその UUID を永久に無効化する。kill 後の完全回収、ファイルハンドル解放、grace period 後の再検査をテストする。

## 4. HIGH — ラウンド処理に transaction / idempotency がなく、クラッシュ復旧で重複・取り違えが起きる

**欠陥**

round 番号と `<participant>-r<N>.md` だけでは、同一 round の retry、部分完了、dispatch 中クラッシュを区別できない。「バッチ一巡完了で +1」とあるが、途中失敗を一巡と数えるか、再開時に誰から始めるか、merge 済み判定を何で行うかがない。

**破綻シナリオ**

codex の merge 後、cc 起動前に書記が落ちる。再実行は round 1 の codex scratch を上書きするか、codex セクションを二重追記する。重複を本文一致で除外すると、同じ意見を意図して再提出した場合とクラッシュ再送を識別できない。round を先に 2 へ進めれば cc の欠落を隠す。

**修正案**

topic 配下に機械管理の journal/manifest を置き、`round_id`、`invocation_id`、order、participant ごとの `pending/running/validated/merged/failed`、input hash、output hash、merge commit marker を atomic に記録する。merge は invocation ID をキーに冪等化する。復旧時は journal と minutes の埋込み ID を照合し、曖昧なら自動続行せず CEO に提示する。mock E2E に各境界での強制終了と再実行を加える。

## 5. MED — Markdown をそのまま merge すると、参加者が議事録の構造と裁定を偽装できる

**欠陥**

Evidence 欄の有無と「1 セクション追記」だけでは、本文中の見出し、frontmatter、HTML、リンク、`## 裁定 (CEO)` などを制限できない。Markdown の見た目上のセクション境界と、文字列ベースの追記境界は一致しない。

**破綻シナリオ**

participant の本文が `## 裁定 (CEO)`、`status: closed`、または折り畳み HTML を含む。機械的には codex セクション内の文字列でも、レンダリング上は後続を CEO 裁定に見せたり、正規の失敗表示を隠したりできる。人間司会が偽の裁定を正本と誤認する。

**修正案**

participant 出力を Markdown ファイルではなく、schema 検証する JSON（本文、evidence 配列、invocation ID）として受ける。minutes 生成時は本文を fenced block、引用、または安全な escaping 規則でデータとして埋め込み、予約見出しを生成できないようにする。CEO 裁定は別の機械管理フィールドからだけ描画する。悪意ある見出し、frontmatter、HTML を fixture にしたテストを加える。

## 6. MED — file-backed prompt の「パス渡し」は CLI 間で同じ意味にならない

**欠陥**

「一時ファイル + パス渡し」とだけあり、各 CLI がそのパスの中身を読むことを保証する引数契約がない。`subprocess` に shell を使うか、argv を使うか、一時ファイルの ACL・削除・文字コード・パス空白の扱いも未定義である。

**破綻シナリオ**

`codex exec C:\...\prompt.txt` を呼ぶと、ファイル内容ではなくパス文字列そのものが prompt として扱われ、Codex がファイルを読めない sandbox/cwd では契約を理解できない。実装者が回避のため `shell=True` と文字列連結を使うと、topic や path に含まれる shell metacharacter がコマンド解釈される。

**修正案**

adapter manifest に CLI ごとの prompt transport（stdin、argv の prompt 本文、正式な file option）を明記し、`shell=False` の argv 配列だけを許可する。Codex については利用する CLI バージョンで対応する正式な入力方式を smoke で固定する。一時ファイルを使う場合は同一ホスト他ユーザーから読めない ACL、UUID 名、close 後起動、finally での削除、失敗時のredaction方針を定義する。

## 7. MED — 要約が CEO の観測経路なのに、原文との対応・失敗時停止線がない

**欠陥**

要約を「唯一の LLM 判断」と認めたが、その判断の検証方法、各要約行から原文への参照、要約失敗・矛盾時の扱いがない。ホストがチャットに貼る 3 行だけが通常の CEO 観測になるため、裁定への影響は小さくない。

**破綻シナリオ**

要約モデルが重大な反対意見や Evidence の但し書きを落とす。dispatch 自体は成功扱いで、CEO は 3 行要約を基に裁定する。minutes に原文が残っていても、毎回そこを読む運用でなければ「共有点が正しい」だけでは意思決定の誤りを防げない。

**修正案**

要約各行に participant、invocation ID、原文段落 ID を付け、CEO がワンステップで原文を確認できる表示にする。要約は明確に `non-authoritative` とし、裁定前には raw 原文または「原文確認済み」の明示を要求する。summarize の非 0、空、参照不能、入力上限超過は dispatch failure と別分類し、黙って raw に切り替えたことも表示する。v0.1 は YAGNI の観点から `--raw` を既定にし、要約層を v0.2 に送る選択も妥当である。

## 8. MED — 「Evidence 欄が空でない」は証拠品質ではなく、容易に形骸化する

**欠陥**

`Evidence` の非空検査は、URL、実行ログ、差分、推測、`N/A` を区別しない。さらに CLI がアクセスできない外部情報を証拠として要求すると、見かけだけ埋める圧力が生じる。

**破綻シナリオ**

参加者が `Evidence: 設計書より` や存在しない URL を書けば validation を通る。議事録上は「Evidence 必須を満たした発言」に見え、人間が検証済みと誤読する。逆に純粋な設計上の反例は外部証拠がなくても有益だが、空欄のため fail になる。

**修正案**

claim と evidence を構造化し、evidence type を `observed/log/diff/source/argument/none` に分ける。非空ではなく「主張の種類と証拠の種類が明示されている」ことだけを機械検証し、真偽・十分性は未検証と表示する。URL 到達確認等を行わない限り、単なる提出を「検証済み」と呼ばない。

## 9. LOW — v0.1 に要約層と概算コスト判定を同時投入するのは受け入れ基準を曖昧にする

**欠陥**

MVP の本質は、権限分離された participant 出力を壊さず冪等に回収し、人間が裁定できることにある。一方、別モデルによる要約と「概算コスト上限・超過見込み警告」は、ベンダー別の価格・token 使用量取得・推定規則を要し、決定的機械層の検証範囲を広げる。

**破綻シナリオ**

会談データフローは正しいのに、CLI が usage を返さないため概算コストが記録できず受け入れ失敗になる。実装者が固定の粗い推定値で埋めると、現在値のように見えるが実測ではない。逆に要約 CLI の不調が roundtable 本体の E2E を不安定にする。

**修正案**

v0.1 は codex/cc の隔離、回収、journal、raw 表示、CEO 裁定までに限定する。コストは `estimate/observed/unknown`、価格表の取得時刻、対象 CLI、算定式を持つ別レコードにし、unknown でも会談自体は受け入れ可能にする。要約とコスト警告は core E2E が安定した後の v0.2 候補にする。

## 修正優先順位

実装開始前に最低限解消すべきなのは #1〜#4 である。ここが未定義のままでは、正常系の 1 周 smoke が通っても「参加者が minutes を直接変更できない」「失敗後に出力が混入しない」「クラッシュ後も二重 merge しない」という設計の中核主張を証明できない。

#5〜#8 は人間司会が誤った来歴・要約・証拠表示を信頼する経路を閉じるものとして、同じく v0.1 の protocol/schema に反映すべきである。#9 はそれらの安全性実装へ集中するためのスコープ縮小案である。
