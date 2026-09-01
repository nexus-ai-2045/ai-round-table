# space-civilization-choice 年次シミュレーター設計レビュー packet

## このファイルの役割

この文書は、`nexus-ai-2045/space-civilization-choice` の年次化設計を、
`nexus-ai-2045/ai-round-table` の独立AI席へ共有するための正本packetです。
実装指示やmerge承認ではありません。レビュー回答は別ファイルとして回収します。

roundtableでdispatchする前に、議題背景へこのrepository-relative pathを明記し、
その後にsnapshotを生成してください。

`minutes/space-civilization-annual-design-20260830/DESIGN_REVIEW_PACKET.md`

参加席は`minutes.snapshot.md`内の上記pathを確認し、このファイルを全文読んでから回答します。

## 現在成立しているMVP

現行MVPは、ローカルで実行可能な決定論的マルチエージェント・シミュレーターです。

- 20個の入力パラメータ
- 5主体
- 2026 / 2030 / 2035 / 2040 の4ラウンド
- 各主体の提案、資源制約による調停、状態更新、外部ショック
- 6つの観測軸と因果trace
- 同一seedによる再現
- `meta-security-run-bundle/v1`
- run request、event stream、replay、evidenceを同じ`run_id`へ束縛

したがってMVP自体は成立しています。ただし現在のWebは結果を一括描画するため、
利用者からは進行しているように見えにくい状態です。

## ユーザーが今回求める変更

2026年から2040年までを1年刻みの15ラウンドにする。
各年にエージェント同士の相互作用を増やし、計算と画面の両方で進行を知覚できるようにする。

## 変更してはいけない正本

- 固定三分岐runnerの4時点契約
- `meta-security-run-bundle/v1`
- 既存runtime/domain logicを状態遷移の単一writerとする境界
- 決定論的seed、event順序、trace、replay
- `space-civilization-choice`を実装正本とする配置
- 既存テスト、ライセンス、アクセシビリティ

`simulation.ROUNDS`は固定三分岐でも使うため、直接15年へ変更してはいけません。
適応型Web専用の`ADAPTIVE_YEARS = tuple(range(2026, 2041))`が必要です。

## 設計候補

### 年次ラウンド

各年で次を実行します。

1. 5主体が現在状態を観測
2. 初期提案
3. 他主体の提案へ支持・反対・修正要求
4. 反応を踏まえた再提案
5. 予算・人材・時間制約で調停
6. 決定論コアだけが状態更新
7. 外部ショックと6軸評価
8. 翌年へ進む

### interaction data contract

既存の`proposals`は後方互換のため最終再提案として残します。

- `initial_proposals`
- `responses`
- `reproposals`
- `interaction_audit`

response候補:

```json
{
  "responder_agent_id": "...",
  "target_agent_id": "...",
  "stance": "support | oppose | amend",
  "priority_delta": 0,
  "rationale": "..."
}
```

interaction recordは状態遷移用`execution_records`へ混ぜず、別契約としてtraceへ含めます。

### 年次効果の正規化

既存の行動効果を15回そのまま適用すると、各軸が0または100へ早期飽和します。
単純なfull-effect × 15は禁止です。

候補はcarry付きfixed-point annualizationです。

```text
numerator = action_delta * 4 + carry
annual_delta = trunc_toward_zero(numerator / 15)
next_carry = numerator - annual_delta * 15
```

同じ行動が15年間続いた場合、累積効果を従来4回分と同程度に保ちます。
carryのbefore/afterも記録し、replay可能にします。外部ショックも同じ考え方で年次配分します。

代替案は、毎年協議するが状態commitは2026/2030/2035/2040だけにする方式です。
これは低リスクですが、「1年ごとに状態が動く」という要求には弱いため第二候補です。

## UI・進行通知候補

- 2026～2040の15年timeline
- 初期提案中 → 相互反応中 → 再提案中 → 調停中 → 状態更新中 → 完了
- 各年の支持・反対・修正を表示
- サーバーはSSEまたはNDJSONで`year_started`、`interaction_completed`、
  `year_completed`、`simulation_completed`を逐次送る
- UIは受信済みeventだけを描画し、実計算中の年と完了済みの年を区別する
- batch APIをfallbackとして残す場合は、計算完了後の表示を「結果replay」と明記し、
  live計算のように見せない
- `prefers-reduced-motion`ではアニメーションを抑止し、状態文字列は維持
- 15年ボタンは横scrollまたはcompact year ticks

## レビューしてほしい論点

1. fixed-point annualizationは既存スケールを守りつつ、意味のある年次状態遷移になるか。
2. carryが説明可能性を下げる場合、より単純で妥当な代替案は何か。
3. 支持・反対・修正・再提案は、見かけだけでない主体間相互作用になっているか。
4. 最小MVPで残すinteractionと、提出後へ延期するinteractionは何か。
5. 0/100への飽和、同じ行動の反復、偽の創発を検知するテストは何か。
6. 15年化しても決定論・trace・replay・単一writerを壊さないか。
7. Cloud Runへ載せる際もローカルと同一結果を保証できるか。

## 回答形式

roundtableの収集契約に従い、回答全体は次のJSON envelopeに入れてください。
以下の日本語Markdown templateは`opinion`文字列の中身です。

```json
{
  "invocation_id": "dispatch packetに記載された値",
  "participant": "grok",
  "opinion": "下記templateを埋めた日本語Markdown",
  "claims": [
    {
      "claim": "最重要の設計判断または反証",
      "evidence_type": "argument",
      "evidence": "packet内の契約に対する具体的根拠"
    }
  ]
}
```

`claims`は1件以上必須です。`opinion`には次のtemplateを入れてください。

```text
結論: GO / REVISE / NO-GO
重大リスク:
- ...
採用する設計:
- ...
却下する設計:
- ...
必須テスト:
- ...
MVPに残す範囲:
- ...
実装前の人間判断:
- ...
```

予測や一般論だけでなく、このpacket内の契約に対する具体的な反証を優先してください。
