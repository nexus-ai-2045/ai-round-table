---
name: verify-roundtable
description: AI Round Tableの読取りexportと共通画面を、分離した議題で検証する。
---

まずリポジトリのルートで `git rev-parse HEAD` と `git status --short` を記録する。`python -m roundtable.cli export --help` と `python -m roundtable.cli gui --help` で対象版の入口を確認する。通常の `doctor` は送信試験になり得るため、このスキルの事前確認には使わない。

CLIの受入は `uv run --offline --with pytest python -m pytest -q --basetemp=.pytest-tmp-roundtable-verify tests/test_read_app.py tests/test_read_model.py`。このテストが生成するのは人工の議題とSafe exportだけ。JSONの観測状態、両席の宛先、議事録、ファイル不変を確認する。終了コードと結果を呼出元へ返す。

GUIの受入は許可された端末で、専用fixtureのパスとslugを指定して `python -m roundtable.cli gui selected --root <fixture-root> --safe-export <fixture-json>` を起動する。Tkの利用可否と対象プロセスを照合し、画面で両席と議事録を確認する。タブ切替と再読込みを行い、送達不明と再開観測の区別を読む。ウィンドウを通常終了して所有プロセスがなくなったことを確かめる。機能別の合格条件は [マップ](features/README.md) に従う。

証拠は専用fixtureの外に置く。版、実行コマンド、OS、終了コード、fixtureの前後ハッシュを保存する。画面証拠は対象ウィンドウに限定する。終了後は自分のfixtureとプロセスだけを片付け、証拠を再読する。CLI合格、GUI合格、実席合格、配布版合格は別々に記録する。
