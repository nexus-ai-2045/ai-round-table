"""relay 層 — 1 契約のみ (DESIGN v6 §4 + v0.2 Phase 1)。

    send(seat, text) -> str   # 搬出結果ラベル (delivered / tier1-sent / ...)
    poll(seat) -> str | None  # 席からの生出力 (未使用時は None; watcher が scratch を見る)

Tier1 障害時は Tier3 に縮退する。Tier2 (UI 自動化) へは自動昇格しない。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .paths import TopicPaths


class RelayError(Exception):
    """relay 搬出に失敗した。呼び出し側は Tier3 縮退を検討する。"""


class Relay(Protocol):
    tier: int

    def send(self, seat: dict, text: str) -> str: ...

    def poll(self, seat: dict) -> str | None: ...


def seats_path(tp: TopicPaths) -> Path:
    return tp.seats


def load_seats(tp: TopicPaths) -> dict:
    if not tp.seats.exists():
        return {}
    return json.loads(tp.seats.read_text(encoding="utf-8"))


def save_seats(tp: TopicPaths, data: dict) -> None:
    from .minutes import atomic_write

    atomic_write(tp.seats, json.dumps(data, ensure_ascii=False, indent=1))


class FallbackRelay:
    """preferred が失敗したら fallback に縮退するラッパ。Tier2 へは行かない。"""

    def __init__(self, preferred: Relay, fallback: Relay):
        self._preferred = preferred
        self._fallback = fallback
        self._active: Relay = preferred
        self.tier = preferred.tier

    def send(self, seat: dict, text: str) -> str:
        try:
            label = self._preferred.send(seat, text)
            self._active = self._preferred
            self.tier = self._preferred.tier
            return label
        except RelayError as exc:
            label = self._fallback.send(seat, text)
            self._active = self._fallback
            self.tier = self._fallback.tier
            seat["tier"] = self.tier
            seat["fallback_reason"] = str(exc)
            return f"fallback-tier3:{exc}"

    def poll(self, seat: dict) -> str | None:
        return self._active.poll(seat)


def get_relay(participant: str, tier: int = 3, allow_fallback: bool = True) -> Relay:
    """席に応じた relay を返す。未知参加者・Tier1 未対応は Tier3。"""
    from .relay_tier3 import Tier3Relay

    if tier == 3:
        return Tier3Relay()
    if tier == 2:
        # DESIGN: Tier2 は CEO 明示承認まで実装しない。要求されても Tier3 に落とす。
        return Tier3Relay()
    if tier == 1 and participant == "codex":
        from .relay_codex import CodexAppServerRelay

        preferred = CodexAppServerRelay()
        if allow_fallback:
            return FallbackRelay(preferred, Tier3Relay())
        return preferred
    return Tier3Relay()
