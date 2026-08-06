"""relay 層の 1 契約 (DESIGN v6 §4「ACP 収束戦略」)。

席に text を届ける、という 1 契約だけを持つ。ACP が 4 席に揃った時点で
adapter を 1 本に置換できるよう、意図的にこれ以上広げない:

    class Relay(Protocol):
        def send(self, seat: dict, text: str) -> None
        def close(self) -> None

出力の回収はこの層の責務ではない。回収は watcher.collect() の scratch
ファイル polling が行う (2026-08-05 spike 実測: 成果物は turn/completed の
80s 前に書かれた = 完了通知を待つ設計は不要)。

seat は seats.json の 1 エントリ (DESIGN v6 §7):
    {"participant": "codex", "topic": "<slug>", "surface": "codex-app",
     "tier": 1, "thread_ref": "(Tier1 のみ)", "created_at": ..., "note": ...}
"""
import subprocess
from typing import Protocol, runtime_checkable

from . import packet


class RelayError(RuntimeError):
    """relay 層の失敗。呼び出し側はこれを Tier3 への縮退判断に使う。

    Tier1 が落ちても勝手に Tier2 (UI 自動化) へ昇格してはならない (DESIGN v6 §4)。
    """


@runtime_checkable
class Relay(Protocol):
    """席への配達契約。実装は tier ごとに 1 クラス。"""

    def send(self, seat: dict, text: str) -> None:
        """seat に text を届ける。届いたことまでを保証し、応答は待たない。"""
        ...

    def close(self) -> None:
        """確保した資源 (プロセス・接続) を撤収する。冪等であること。"""
        ...


class ClipboardRelay:
    """Tier3 (人間 relay)。クリップボードに載せるだけで、貼り付けは CEO が行う。

    packet.to_clipboard をラップするだけの薄い adapter。搬出成功 = 席への
    着信ではない (DESIGN v6 §6: Tier3 の delivered は「搬出済み」の意味)。
    """

    tier = 3

    def send(self, seat: dict, text: str) -> None:
        """seat は使わない (クリップボードは席を区別できない) が契約上受け取る。"""
        try:
            packet.to_clipboard(text)
        except (OSError, subprocess.SubprocessError) as e:
            raise RelayError(f"クリップボード搬出に失敗: {e!r}") from e

    def close(self) -> None:
        """撤収する資源がない (no-op)。"""


def get_relay(tier: int, participant: str, **options) -> Relay:
    """tier と席に対応する Relay を返す。

    Tier1 は 2026-08-05 spike で実測できた codex 席のみ。未実測の席を
    推測で Tier1 扱いすると「届いたつもり」で議題が止まるため、
    NotImplementedError で明示的に落として Tier3 起動を促す。

    options は Tier1 adapter のコンストラクタへそのまま渡す (cwd 等)。
    Tier3 は席も作業ディレクトリも区別しないため無視する。
    """
    if tier == 3:
        return ClipboardRelay()
    if tier == 2:
        raise NotImplementedError(
            "Tier2 (UI 自動化) は席単位の CEO 明示承認を seats.json に記録してから"
            " (DESIGN v6 §4/§12)。v0.2 では実装しない"
        )
    if tier == 1:
        if participant == "codex":
            # 循環 import 回避のため遅延 import。relay_codex 側は relay を import する。
            from .relay_codex import CodexRelay

            return CodexRelay(**options)
        raise NotImplementedError(
            f"Tier1 の実測済み経路は codex 席のみ (2026-08-05 spike)。"
            f"{participant!r} は未 spike のため Tier3 で起動すること"
        )
    raise ValueError(f"unknown tier: {tier!r} (1/2/3 のみ)")
