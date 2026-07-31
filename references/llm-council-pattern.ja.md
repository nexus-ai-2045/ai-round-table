# LLM Council パターン (Karpathy) — 日本語 SSOT と ai-round-table との差分

- 取得日: 2026-07-27 / 種別: C
- 原典: https://github.com/karpathy/llm-council (2025-11 公開, ~23.3k★ 2026-07 時点) /
  派生 index: https://github.com/danielrosehill/Awesome-LLM-Council-Projects /
  hosted 版: https://council-ai.app

## パターン本体 (3 段階)

1. **First opinions**: 質問を全 LLM に個別に投げ、回答をタブ表示で並べる
2. **Review**: 各 LLM に**他の LLM の回答を匿名化して**渡し、相互レビュー・ランク付けさせる
   (匿名化 = モデルが「どのモデルの回答か」で贔屓しないため)
3. **Final**: Chairman 役の LLM が全回答 + レビューを統合して最終回答を出す

実装: ローカル web アプリ + OpenRouter (API キー)。週末ハック品質と作者自身が明言。

## ai-round-table との差分 (設計上の対立点)

| 軸 | llm-council | ai-round-table |
|---|---|---|
| 司会/裁定 | Chairman **LLM** | **人間** (CEO) |
| ラウンド | 1 パス固定 (質問→回答→レビュー→統合) | 多ラウンド (上限 3)、CEO が指名・打ち切り |
| 接続 | API キー (OpenRouter) | **アプリ課金のまま** (席 = アプリのチャット) |
| 履歴 | 使い捨て (アプリ内) | 席のチャットに残る + **議事録が監査成果物** |
| 根拠 | なし (回答のみ) | evidence 型付き claims が必須 |
| 失敗 | 表面化しない | journal に分類記録・close 前に必ず提示 |

## 盗む点

- **匿名相互レビュー段階**: 同調バイアス (echo chamber) を減らす実証済みの仕掛け。
  ai-round-table では v0.2 のラウンド種別「blind-review round」として輸入候補
  (司会が「この周は匿名レビュー」と宣言 → dispatcher が発言者名を伏せた snapshot を配る)
- タブ並列表示の UX (将来 status 表示の参考)

## 注意 (ポジショニング)

- 「council」の語は Karpathy パターン (自動 1 パス合議) として既に定着している。
  public 化時の README は **roundtable = 人間が座長** の差を冒頭 3 行で明示し、
  council を自称しない (誤解による期待ズレ防止)
