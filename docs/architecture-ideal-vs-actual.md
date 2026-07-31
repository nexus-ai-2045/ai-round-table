# 理想図 vs 現実図 — ai-round-table はどこまで作れるか

- 日付: 2026-07-27
- 前提: DESIGN v5 + 先行調査 2 本 (`multi-ai-realtime-linking` / `multi-ai-roundtable-prior-art`)
- 結論を先に: **理想図の約 8 割は現行技術で作成可能**。残り 2 割は外部依存
  (Gemini の公式口が存在しない / アプリ画面への即時反映が未検証) で、こちらの実装力では埋まらない。

## 1. 理想図 (CEO が欲しい最終形)

```
CEO はどのアプリで喋ってもいい。判断だけする。コピペゼロ。

   Codex アプリ ──┐
   Claude       ──┤  全席が公式 API で自動 relay (Tier1)
   Grok         ──┼─→ 議事録 minutes.md (自動更新・監査可能)
   Gemini       ──┘        │
                            ▼
        席のチャット画面にも履歴がリアルタイムに見える
        CEO はいつでもアプリから割り込める
        壁打ち・三者会談が「1 回の指名」で 1 ラウンド自動で回る
```

理想の性質:
- I1: 人間の操作 = 議題・指名・裁定のみ (コピー機仕事ゼロ)
- I2: 全席 Tier1 (公式 API 自動 relay)
- I3: 席の履歴がアプリ画面にリアルタイム表示
- I4: 議事録が常に正・改ざん不能
- I5: どの AI をホストにしても同じ体験 (host-agnostic)

## 2. 現実図 (今の技術で確実に組める形 = v0.1〜v0.2)

```
CEO（判断 + Gemini の分だけ貼り付けが残る）
 │
 ├─ Claude (CC) ── ホスト。操作不要 (このチャットが司会席)
 ├─ Codex ──────── Tier1 候補: 自前 spawn の app-server + stdio JSON-RPC
 │                  (公式プロトコル・stable。セッションストアはアプリと共有。
 │                   ただし「開いてるアプリ画面に即時反映」は未検証)
 ├─ Grok ────────── Tier1 候補: grok agent serve (WebSocket) / leader socket
 │                  (公式の口はある。会話の席として使えるかは spike 待ち)
 └─ Gemini ──────── Tier3 固定: 人間 relay (公式の口が存在しない)
        │
        ▼
   dispatcher (packet / watcher / journal / merge)  ← ここは理想と同一
   minutes.md = 改ざん「検知」(防止は不能。アプリ agent は FS 権限を持つ)
```

## 3. ギャップ表 (理想 → 現実の差分と、埋まるか)

| # | 理想 | 現実 | 埋まるか |
|---|---|---|---|
| I1 コピペゼロ | Gemini だけ貼り付けが残る | **外部依存**。agy/Antigravity に serve 系がない (実測)。UI 自動化 (Tier2) なら埋まるが CEO 承認制。将来 Google が口を開ければ解消 |
| I2 全席 Tier1 | Codex/Grok は候補あり、CC はホストなので不要、Gemini ✗ | **3/4 まで可**。Codex は app-server (stable プロトコル・先行クライアント多数)。Windows で daemon 常駐だけ不可 → 自前 spawn + stdio で代替 (2026-07-27 実測) |
| I3 画面リアルタイム | セッションストア共有は確認済み。**開いている画面への即時反映は未検証** | **不明 (spike 1 で判定)**。最悪でも「チャット一覧に席が出る・開けば読める」は成立見込み。DESIGN §10-6 で要件を 2 分割済み |
| I4 改ざん不能 | 防止は不能 (アプリ agent の FS 権限を制御できない) → hash + git で**検知** | **設計変更なしでこれが上限**。検知 + fail-closed で監査は成立 |
| I5 host-agnostic | PROTOCOL.md + scripts で設計上は達成。実利用は CC ホストから | **可**。他ホストは薄いアダプタ追加のみ |
| 1 指名 1 ラウンド自動 | dispatcher のバッチ指名で可 (Tier3 席が混ざるとその分だけ人手) | **可** (混成 tier で自然に動く設計) |

## 4. 判定

- **作成できる**: 理想図は「Gemini 自動化」と「画面即時反映」を除いて全部こちらで実装可能。
  この 2 つは相手側 (Google / OpenAI のアプリ実装) の問題で、待つか Tier2 (UI 自動化・承認制) で
  埋めるかの選択。
- 逆に言うと: **v0.1 (Tier3 のみ) → v0.2 (Codex/Grok を Tier1 化) と進めば、
  CEO の手作業は「Gemini への貼り付け」だけになる**。三者会談 (Codex×CC×Grok) なら
  理想図どおりコピペゼロが達成可能。

## 5. 参考資料の扱い (SSOT 方針)

外部資料は `references/` に日本語で固定する。方式は 3 段階 (詳細: references/README.md):

- **A: 浅 clone** — 深く読む小規模 repo (codex-client, llm-council 等) は
  `Documents/.repos/external/` に shallow clone し、commit hash を台帳記録
- **B: ファイル snapshot** — 巨大 monorepo (openai/codex) は必要ファイルだけ
  references/ に取得日 + commit + URL 付きで保存
- **C: 日本語要約のみ** — 記事・ブログは URL + 要約 (原文は保存しない)
