# ai-round-table：複数AIの議論と回答回収

**人間が座長を務める、マルチ AI 円卓のディスパッチャ。**

- **人間が座長**。裁定・打ち切り・指名は人間だけが宣言する。自動で結論を出さない
- **API キー不要**。席は各 AI アプリの実チャットで、普段の課金のまま動く
- **議事録が成果物**。git 管理された 1 枚の Markdown が唯一の共有状態

## 目的と仕組み

```
座長（人間）── 判断だけをする: 議題 / 指名 / 裁定 / 打ち切り
 │
 ├─ Codex アプリの「rt-<議題>-codex」チャット   ← 席。履歴はアプリに残る
 ├─ Grok の「rt-<議題>-grok」セッション          ← 席
 │        ▲ relay（自動 / 縮退時は人間が貼付）
 │        │
 └─ dispatcher（AI を実行しない決定的なツール）
      ├─ packet 生成 :「議事録を読み、契約 JSON を書け」
      ├─ relay      : 席へ届ける
      ├─ watcher    : 出力を検証して議事録へ merge
      └─ 議事録 / journal / round の管理
           ▼
    minutes/<議題>/minutes.md   ← 唯一の共有状態（git 管理）
```

設計上の要点は 2 つ。

**AI 同士は直接つながらない。** 各席は議事録のスナップショットを読み、自分の意見を JSON で
書くだけ。**席から席へ意見が中継される経路が無い**ので、伝言ゲームが起きる場所がそもそも無い。
全体を読んで裁定するのは座長（人間）だけ。

（当初この判断の根拠に arXiv 2512.08296 の「誤り増幅 17.2 倍」を挙げていたが、原典を読み直して
**撤回した**。同論文のその値は誤り率ではなく協調失敗による計算量オーバーヘッドで、誤り率ベースの
指標は 1.1–1.3 倍。しかも論文自身がアーキテクチャ間の誤り増幅の差を統計的に非有意としている。
経緯は [docs/review-backlog.md](docs/review-backlog.md) に残した）

**dispatcher は AI を実行しない。** 議題 packet を配って議事録を束ねるだけの決定的なツール。

## 使い方（できること）

Macでは `uv tool install /absolute/path/to/ai-round-table` で
`ai-roundtable` コマンドを登録できます。作業ディレクトリへの参照を残さず導入するため、
導入後にソースを変更した場合は再インストールします。開発中に変更を即時反映したい場合だけ
`--editable` を追加します。登録後はリポジトリ外から
`ai-roundtable --help` を使えます。以下の `python -m roundtable.cli` と同じ入口です。
回収後に元担当へ結果を戻す手順は [担当への返却](docs/operations/coordinator-followup.md) を参照してください。

```bash
# 0. 環境診断（Tier1 が使えるか）
python -m roundtable.cli doctor

# 1. 議題を立てる                                   ← 人間の操作 1
python -m roundtable.cli new-topic <slug> \
  --topic "議論したいこと" --participants codex,grok --root <root>

# 2. 席へ配る（--tier 1 で席のチャットへ直接届く。   ← 人間の操作 2
#    省略時の既定は Tier3 = クリップボード経由で人間が貼付）
python -m roundtable.cli dispatch <slug> --participant codex --tier 1 --root <root>

# 3. 状況を見る（観測専用。手数に数えない）
python -m roundtable.cli status <slug> --root <root>

# 4. 裁定して閉じる                                  ← 人間の操作 3
python -m roundtable.cli close <slug> --verdict "結論" --root <root>
```

**KPI は「人間の操作 3 回以内」**で、自己申告ではなく journal に機械記録される。
`status` を何回叩いても増えない（観測が KPI を汚さないため）。

## 議事録の構造

```
minutes/<議題>/
├─ minutes.md        議事録（唯一の共有状態。git 管理）
├─ journal.json      invocation の状態機械（git 管理）
├─ seats.json        席メタ — tier / fallback_reason / thread 参照（git 管理）
├─ scratch/          席が書いた生の出力（git 管理外）
├─ snapshot/         席に渡す読み取り用スナップショット（git 管理外）
└─ last-result.json  直近 CLI 実行の結果（git 管理外）
```

各席の意見には**根拠の型**が付く。

| 型 | 意味 |
|---|---|
| `observed` | 実際に見た / 測った |
| `log` / `diff` / `source` | ログ / 差分 / 出典がある |
| `argument` | 論として述べている（実測ではない） |
| `none` | 根拠なし |

これは**席の自己申告**であり、検証済みという意味ではない。そう表示もしない。

## 席への届け方（3 段）

| Tier | 方式 | 人間の関与 |
|---|---|---|
| 1 | 公式 API 経由でアプリの席へ直接 | ゼロ |
| 2 | UI 自動化 | **明示承認がない限り使わない** |
| 3 | クリップボード → 人間が貼付 | 貼付 1 回 |

Tier1 が失敗したら**自動で Tier3 に落ちる**（勝手に Tier2 へ上がらない）。縮退したかは
`seats.json` の `tier` と `fallback_reason` で機械的に分かる。

## 改ざん検知は git

議事録を席が書き換えていないか。これは**防がずに検知する**。

- 書き込む前に、対象ファイルが git 管理下で clean かを検査
- 書き込んだら pathspec 限定で auto-commit（`roundtable-dispatcher` 名義）
- dirty = dispatcher 以外が書いた → **diff を出して停止**
- 裁くのは人間（`git diff` を読んで、正当なら commit / 不当なら checkout）

以前は sha256 の控えを自前で持っていたが、**席が任意コマンドを実行できる環境では控えごと
書き換えられる**ことが実測で分かり廃止した。git は最初からそこにあり、より強く、しかも
「人間が読んで判断する」という本システムの原則に合う。最終的な証跡は origin に push した
履歴（席に認証情報を渡さない限り届かない）。

詳細と守れない範囲は [SECURITY.md](SECURITY.md) の脅威モデルを参照。

## 状態

| 席 | 状態 |
|---|---|
| Codex | Tier1 実往復を実測。アプリのチャット一覧に席が出ることも確認済み |
| Grok | Tier1（ACP）で意見の書き込みまで実測 |
| Gemini | 未実装（Antigravity 経由の 3 経路を調査済み） |
| CMUX上のCodex | Macの正式wrapperと通常CLIで実席往復・重複しない回収・元担当への返却を確認 |
| Claude Desktop Code | Macの依頼固定・clipboard受渡し・回答回収を実装。指定実席の往復は接続障害により未確認 |

**司会を担う実行主体と、独立した意見を求める参加席は分けます。** これは製品名による除外ではなく役割の境界です。ホストは座長との会話を全部見ているので、
「独立した参加者の意見」を出すと、司会が自分の望む結論を参加者の口から言わせるのと同じに
なる。加えて実装当事者は自分の実装を擁護する方向に偏る。**頭数より独立性を優先**する。

### 制約

- Windowsの既存Tier1実測と、MacのCMUX受渡し実測は別の搬送経路です。
  Macの試験・導入・外部接続の残務は[検証記録](docs/operations/collection-recovery-verification.md)を参照してください。
- Python 3.13 以上
- 席の追加には各 AI 側の接続経路の調査が要る
- Tier2（UI 自動化）は設計のみで未実装

## ドキュメント

| ファイル | 内容 |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | 設計書 v6。決定事項 D1–D13 とその根拠 |
| [docs/prior-art.md](docs/prior-art.md) | 先行事例と立ち位置（既存で代替できないかの裏取り） |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | 席と dispatcher の間の契約 |
| [docs/adr/](docs/adr/) | アーキテクチャ決定記録（0001: 公式 Codex SDK への段階移行） |
| [docs/operations/](docs/operations/) | 運用記録 |
| [回答の発見・回収と担当再開](docs/operations/collection-recovery.md) | 有限待機、遅着回収、取消、再開の保証境界 |
| [Macの席への受渡し](docs/operations/mac-handoff.md) | handoff / handoff-status、送達不明時の再送防止 |
| [Mac優先のDesktop接続](docs/operations/mac-desktop-connection.md) | CMUX・Round Table・Claude Desktop Codeの接続と保証範囲 |
| [CMUXの4席案](docs/operations/cmux-four-ai-proposal.md) | 入口調査と採否後の最小スモーク案 |
| [docs/review-backlog.md](docs/review-backlog.md) | レビュー指摘と対応の記録（撤回した判断も含む） |
| [docs/plans/](docs/plans/) | 実装計画 |
| [SECURITY.md](SECURITY.md) | 脅威モデル |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 開発の進め方 |

## ライセンス

MIT License. [LICENSE](LICENSE) を参照。

`references/` には OpenAI Codex app-server の README 訳を参考資料として含む。
これらは原典の権利者に帰属する。
