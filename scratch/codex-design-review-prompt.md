# レビュー依頼: ai-roundtable 設計書 v2 (from CEO, 司会: CC)

docs/DESIGN.md を読んで、敵対的にレビューしてください。

背景: 人間 (CEO) が司会するマルチ AI 壁打ち基盤の設計。CC (Claude) による敵対的レビュー 8 指摘と
Web 裏取り 14 発見は既に v2 に反映済み (§11 変更ログ参照)。あなたは 2 人目のレビュアーで、
**CC が見落としたもの・CC の修正が生んだ新しい問題**を探すのが仕事です。

観点 (自由に追加可):
1. dispatch.py の 2 層書記 (機械層 + 要約層) 設計の穴。特に Windows 実装の現実性
2. scratch → merge + git diff assert 方式の抜け道
3. あなた (Codex) が参加者として `codex exec` で呼ばれる立場から見た実行時の問題
   (sandbox 既定、作業ディレクトリ、minutes を読んで scratch に書くという契約の守りやすさ)
4. v0.1 スコープの過不足 (YAGNI / 不足)
5. 「同意します」ではなく、必ず具体的な欠陥 + 破綻シナリオ + 修正案の 3 点セットで

出力: このファイルと同じディレクトリに `codex-design-review-reply.md` を新規作成して書いてください。
severity (HIGH/MED/LOW) 付き、日本語で。docs/DESIGN.md 自体は編集しないこと。
