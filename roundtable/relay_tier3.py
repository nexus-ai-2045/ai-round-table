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
