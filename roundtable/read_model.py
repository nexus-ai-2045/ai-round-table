"""指定された議題の既存ファイルだけを読む表示用投影。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .journal import STATES
from .local_read import read_local_file
from .paths import topic_dir

_MAX_BYTES = 8 * 1024 * 1024
_RECEIPT_FIELDS = ("state", "transport", "delivery_route", "model_receipt_verified")


def _receipt(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in _RECEIPT_FIELDS if key in value}


def export_topic(root: Path, slug: str) -> dict:
    """ロック、Git、通信を使わない観測。整合性やモデル受付の証明ではない。"""
    result = {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "status": "unknown", "reason": None,
        "topic": {"slug": slug, "round": None},
        "seats": [], "invocations": [], "minutes": "",
    }
    try:
        directory = topic_dir(root, slug)
        paths = [directory / name for name in ("seats.json", "journal.json", "minutes.md")]
        raw = [read_local_file(path, _MAX_BYTES) if path.exists() or path.is_symlink() else None for path in paths]
        if any(content is None for content in raw[1:]):
            raise ValueError("missing-file")
        seats = json.loads(raw[0]) if raw[0] is not None else {}
        journal = json.loads(raw[1])
        minutes = raw[2].decode("utf-8")
        if not isinstance(seats, dict) or not isinstance(journal, dict):
            raise ValueError("invalid-schema")
        invocations = journal.get("invocations")
        round_no = journal.get("round")
        if not isinstance(invocations, dict) or type(round_no) is not int or round_no < 1:
            raise ValueError("invalid-schema")
        projected_seats = []
        for key, seat in sorted(seats.items()):
            if not isinstance(seat, dict):
                raise ValueError("invalid-schema")
            participant = seat.get("participant", "")
            provider = ("codex" if participant == "codex" else
                        "claude" if participant in ("claude", "cc", "claude-code") else "other")
            projected_seats.append({
                "id": key, "provider": provider,
                "conversation_id": seat.get("thread_ref"),
                "title": seat.get("title", seat.get("thread_name", key)),
                "state": "unknown", "receipt": _receipt(seat.get("submission_receipt")),
            })
        projected_invocations = []
        for key, record in sorted(invocations.items()):
            if not isinstance(record, dict) or record.get("state") not in STATES:
                raise ValueError("invalid-schema")
            receipt_path = directory / "deliveries" / f"{key}.json"
            if not key or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in key):
                raise ValueError("invalid-invocation-id")
            receipt = {}
            if receipt_path.exists() or receipt_path.is_symlink():
                receipt_data = json.loads(read_local_file(receipt_path, _MAX_BYTES))
                if not isinstance(receipt_data, dict):
                    raise ValueError("invalid-schema")
                receipt = _receipt(receipt_data)
                session_id = receipt_data.get("claude_session")
                if record.get("participant") in ("claude", "cc", "claude-code") and isinstance(session_id, str):
                    projected_seats.append({
                        "id": f"handoff/{key}", "provider": "claude",
                        "conversation_id": session_id, "title": record["participant"],
                        "state": "unknown", "receipt": receipt,
                    })
            projected_invocations.append({
                "id": key, "participant": record.get("participant"),
                "round": record.get("round"),
                "state": ("delivery-unknown" if record["state"] == "prepared"
                          and (record.get("codex_send_claim") or receipt.get("state") == "sending")
                          else record["state"]),
                "receipt": receipt,
                "submission_receipt": {
                    field: value for field, value in record.get("submission_receipt", {}).items()
                    if field in ("provider", "thread_id", "turn_id", "status")
                } if isinstance(record.get("submission_receipt"), dict) else {},
            })
        # 一つのファイルだけ更新された観測を正常なスナップショットと扱わない。
        if any((read_local_file(path, _MAX_BYTES) if path.exists() or path.is_symlink() else None) != content
               for path, content in zip(paths, raw)):
            raise ValueError("source-changed")
        result.update(status="available", topic={"slug": slug, "round": round_no},
                      seats=projected_seats, invocations=projected_invocations, minutes=minutes)
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        result["reason"] = str(exc) if type(exc) is ValueError else type(exc).__name__
    return result
