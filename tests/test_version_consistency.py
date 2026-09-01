"""版番号の単一ソース検査。

version は「宣言 (pyproject.toml)」と「実行時に席へ名乗る値 (roundtable.__version__)」の
2 面を持つ。0.2.0 時代は relay_codex / relay_grok の clientInfo に数字が直書きされており、
pyproject だけ上げると**席には古い版を名乗り続ける**状態になっていた (実際 v0.3 の Phase 1-3
着地後も 3 箇所とも 0.2.0 のままだった)。

数字を書く場所を __init__ の 1 箇所に寄せたうえで、宣言との一致をここで機械検査する。
CONTRIBUTING の約束「文章で守るのではなくテストで守る」に対応する detector。
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from roundtable import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent

# `"version": "0.2.0"` 形式の直書き。値が数字で始まるものだけを見る
# (`"version": __version__` や変数参照は拾わない)。
HARDCODED_VERSION = re.compile(r'"version"\s*:\s*"[0-9]')


def _declared_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_runtime_version_matches_pyproject():
    """pyproject の宣言と実行時定数がずれていない。"""
    assert __version__ == _declared_version()


@pytest.mark.parametrize("module_name", ["relay_codex", "relay_grok"])
def test_relay_does_not_hardcode_version(module_name):
    """席へ名乗る版番号を直書きしていない (= __init__ の定数を参照している)。

    handshake を実走させるには席のプロセスが要るのでソースを読む。
    直書きが再び入ったらここで落ちる。
    """
    source = (REPO_ROOT / "roundtable" / f"{module_name}.py").read_text(encoding="utf-8")
    hits = [line.strip() for line in source.splitlines() if HARDCODED_VERSION.search(line)]
    assert not hits, f"{module_name}: 版番号の直書き -> {hits}"
    assert "__version__" in source, f"{module_name}: __version__ を参照していない"


def test_detector_catches_a_reintroduced_hardcode():
    """detector 自身が効くことを確かめる (空振りする正規表現を置かないため)。"""
    assert HARDCODED_VERSION.search('"version": "0.2.0",')
    assert not HARDCODED_VERSION.search('"version": __version__,')
