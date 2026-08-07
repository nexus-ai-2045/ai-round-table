"""relay 層 — 1 契約のみ (DESIGN v6 §4 + v0.2 Phase 1)。

    send(seat, text) -> str   # 搬出結果ラベル (delivered / tier1-sent / ...)
    poll(seat) -> str | None  # 席からの生出力 (未使用時は None; watcher が scratch を見る)

Tier1 障害時は Tier3 に縮退する。Tier2 (UI 自動化) へは自動昇格しない。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from . import integrity
from .filelock import FileLock
from .paths import TopicPaths


class RelayError(Exception):
    """relay 搬出に失敗した。呼び出し側は Tier3 縮退を検討する。"""


class Relay(Protocol):
    tier: int

    def send(self, seat: dict, text: str) -> str: ...

    def poll(self, seat: dict) -> str | None: ...


def seats_path(tp: TopicPaths) -> Path:
    return tp.seats


_SEATS_NAME = "seats.json"


def load_seats(tp: TopicPaths) -> dict:
    """席メタを読む。dispatcher 以外に書き換えられていたら fail-closed (ValueError)。

    読みもロック配下で行う (journal._read_verified と同じ理由 / レビュー H1・H2)。
    `verify_and_read` は証跡を書きうるので、ロック外で呼ぶと writer の pending 窓に
    割り込んで偽の改ざん検知を作り、Windows では writer の `os.replace` も壊す。
    """
    with FileLock(tp.lock(_SEATS_NAME)):
        raw = integrity.verify_and_read(tp.seats, tp.witness(_SEATS_NAME))
    if raw is None:
        return {}
    return json.loads(raw.decode("utf-8"))


def merge_seats(disk: dict, local: dict) -> dict:
    """席メタを **席 (key) 単位** で重ねた新しい dict を返す。

    同じ席が両方に在れば local を採る (自分が今送った結果が最新)。ただし
    `thread_ref` だけはディスク側を落とさない: 席の同一性を失うと次ラウンドが
    thread/resume でなく thread/start に落ち、CEO が見ていない別チャットへ席が
    分裂する (relay_codex._resume_thread)。
    `fallback_reason` は引き継がない。消えた失敗痕跡を復活させるのではなく、
    invocation 単位の失敗記録 (journal の delivered detail) を正とする。
    """
    out = dict(disk)
    for key, seat in local.items():
        cur = out.get(key)
        if isinstance(cur, dict) and isinstance(seat, dict):
            seat = dict(seat)
            if not seat.get("thread_ref") and cur.get("thread_ref"):
                seat["thread_ref"] = cur["thread_ref"]
        out[key] = seat
    return out


def save_seats(tp: TopicPaths, data: dict) -> None:
    """席メタを read-modify-write で書く (全文上書きしない)。

    旧実装は load 時のスナップショットを全文書き戻していた。relay.send の寿命
    (Tier1 は thread/start 300s + turn/start 120s) がそのまま競合窓になり、
    並行 dispatch では他席のエントリごと消える (2026-08-07 実測: 一方の
    tier=3 + fallback_reason が消滅)。journal.save と同じ方針に揃える。
    """
    witness = tp.witness(_SEATS_NAME)
    with FileLock(tp.lock(_SEATS_NAME)):
        raw = integrity.verify_and_read(tp.seats, witness)
        disk = json.loads(raw.decode("utf-8")) if raw is not None else {}
        merged = merge_seats(disk, data)
        integrity.write_verified(
            tp.seats, json.dumps(merged, ensure_ascii=False, indent=1), witness
        )


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


def get_relay(
    participant: str,
    tier: int = 3,
    allow_fallback: bool = True,
    cwd: str | None = None,
) -> Relay:
    """席に応じた relay を返す。未知参加者・Tier1 未対応は Tier3。

    cwd は Tier1 の sandbox 書込範囲になる。**議題ディレクトリを渡すこと** —
    root を渡すと他議題の journal.json / seats.json (hash 保護なし) まで
    席の書込範囲に入る。
    """
    from .relay_tier3 import Tier3Relay

    if tier == 3:
        return Tier3Relay()
    if tier == 2:
        # DESIGN: Tier2 は CEO 明示承認まで実装しない。要求されても Tier3 に落とす。
        return Tier3Relay()
    if tier == 1 and participant == "codex":
        from .relay_codex import CodexAppServerRelay

        preferred = CodexAppServerRelay(cwd=cwd)
        if allow_fallback:
            return FallbackRelay(preferred, Tier3Relay())
        return preferred
    return Tier3Relay()
