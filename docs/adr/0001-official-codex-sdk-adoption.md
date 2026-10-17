# ADR 0001: 公式 Codex SDK への段階移行

- 状態: 採用候補・移行ゲート待ち
- 日付: 2026-08-19

## 文脈

現在の Codex Tier1 relay は `codex app-server` の JSON-RPC transport をリポジトリ内で
実装している。公式 PyPI パッケージ `openai-codex` 0.147.0 には、UTF-8 subprocess、
stderr drain、JSON-RPC router、通知、`thread/start` / `thread/resume` を含む typed client が
ある。今後も transport を自前で追随するのは車輪の再発明になる。

一方、公式 SDK の低水準 `CodexClient` は既定 approval handler がコマンド・ファイル変更要求を
承認する。既存実装が持つ Windows Job Object、議題ディレクトリへの scope 制限、
送達成否不明時の fail-closed、Tier3 縮退、journal 証跡をそのまま置換できるわけではない。

## 決定

今回の安全修正は現在の adapter に適用し、重大事故を先に止める。公式 SDK への移行は
次の採用ゲートを全て満たす独立差分で行う。

1. 低水準 `CodexClient` に deny-by-default の approval handler を明示する。
2. request timeout 後を `delivery-unknown` として扱い、自動再送しない。
3. Windows のプロセス木回収と議題単位 cwd/sandbox を既存 contract と同等以上にする。
4. 同一 thread の2ラウンド継続、stderr大量出力、逆順応答、stale thread をテストする。
5. 公式 SDK が使えない環境では、既存 adapter か Tier3 へ明示的に縮退できる。

## 帰結

- 今回のPRは配送安全性を回復し、SDK移行の前提を固定する。
- SDKは直接依存として即追加せず、上記ゲートを満たす migration PR で採否を確定する。
- Node製非公式 client は依存にせず、比較資料としてのみ扱う。
