# 読取り専用の共通画面

## Sub-features

指定議題のCodexとClaude Codeの席、受付と回収、議事録、Safe Resumeの観測。

## How to get to it (user POV)

`export` で指定議題のJSONを取得する。`gui` の「会話」「円卓」「議事録」「再開」を開く。

## Driving it with CLI and native UI

`tests/test_read_model.py` と `tests/test_read_app.py` の人工データで、元の会話ID、別々の受付と回収、議事録、壊れた入力の不明を検証する。許可されたGUI試験では両席の表示、タブ切替、再読込み、通常終了を操作する。fixtureのファイルハッシュと一覧を前後で比較する。

## Gotchas

TkのないPythonではCLIだけが利用できる。Safe exportの観測時刻を現在の接続と混同しない。選択済み議題の記録だけを使用する。Windowsのネイティブ読取り境界とGUIは実機別の受入を要する。
