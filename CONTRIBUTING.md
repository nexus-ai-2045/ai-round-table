# 開発の進め方

日本語で書く。Issue / PR / commit メッセージも日本語。

## 環境

- Python 3.13 以上
- git（改ざん検知に使うため必須）
- Windows で開発・実測しています。POSIX 経路は実装がありますが未実測です

```bash
python -m pytest -q          # 全テスト
python -m pytest -q -rx      # 未検証の前提（xfail）も一覧表示
```

テストは `.pytest-tmp/` を basetemp に使います。議事録の保存 1 回につき git commit が
1 回走るため、全体で 10 分以上かかります。

## この repo での約束

### 1. 「検知」を作る前に脅威モデルを書く

「検知」「改ざん防止」「保護」「検証」を目的とする仕組みを追加するときは、
[SECURITY.md](SECURITY.md) の脅威モデルに照らして**必要かどうかを先に判断**してください。

- 誰から / 何を / どうなると困る / **守らないもの**
- 書けないなら、その仕組みは作らない

この repo は同じ型の設計ミスを 3 回繰り返しています（snapshot hash → witness 証跡 → git）。
1 回目は並行運用を考えておらず、正常な動作が警報を出しました。2 回目は「席は sandbox から
出られない」という**未検証の前提**の上に建て、実測で破綻しました。3 回目でようやく
「既にある仕組み（git）で足りる」と分かりました。経緯は
[docs/review-backlog.md](docs/review-backlog.md) にあります。

### 2. 未検証の前提はテストに置く

「未検証」「未確認」「仮定」と文章に書いたら、同じ commit で**反証テストを `xfail` として
置いてください**。文章だけの前提を残さないためです。

```python
@pytest.mark.xfail(reason="未検証: 席が <対象> に書けないこと")
def test_seat_cannot_reach_it():
    ...
```

前提が検証されれば xpass として pytest が報告します。2 回目の設計ミスは、docstring に
「未検証」と明記した**上で**その前提に構造を建てたことが原因でした。
**記録することが検証の代わりになっていた**わけです。

### 3. 実測を根拠にする

タイムアウト値・バージョン下限などの定数には、実測値をコメントで残してください。
この repo の Tier1 が長期間「必ず縮退する」状態だったのは、`thread/start` の timeout が
実測 20.9 秒に対して既定 5 秒だったためです。fake stdio のユニットテストは即答するので、
この穴を検出できませんでした。

### 4. 席を増やすとき

relay の契約は 3 つだけです。

```python
send(seat, text) -> str      # 席へ届ける
poll(seat) -> str | None     # 生出力の回収（未使用なら None）
close() -> None              # プロセス木の回収
```

`roundtable/relay_codex.py`（JSON-RPC）と `roundtable/relay_grok.py`（ACP）が実装例です。
ACP が各社に揃えば adapter を 1 本に寄せる方針です。

## PR

- `main` へ直接 push しません。ブランチを切って PR にしてください
- commit メッセージは `<type>: <説明>`（feat / fix / docs / refactor / test / chore）
- 変更が「なぜ必要か」を PR 本文に書いてください。「何をしたか」は diff で読めます

## やらないこと

- Tier2（UI 自動化）の有効化を既定にしない
- 席に認証情報を渡さない
- `--dangerously-skip-permissions` を使わない
