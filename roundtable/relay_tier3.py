"""Tier3: クリップボード搬出。人間が席チャットへ貼り付ける (DESIGN v6 §4)。"""
from . import packet


class Tier3Relay:
    tier = 3

    def send(self, seat: dict, text: str) -> str:
        packet.to_clipboard(text)
        return "delivered"

    def poll(self, seat: dict) -> str | None:
        # 出力は scratch 監視 (watcher) が担当。relay は搬出のみ。
        return None

    def close(self) -> None:
        """Tier3 はプロセスを持たないので no-op。

        契約 (`relay.Relay`) を満たすためだけに置く。呼び出し側が
        `hasattr` で分岐しなくて済むようにする。
        """
        return None
