"""Explicit, receipt-backed Mac handoff; submission is not AI acceptance.

Uses the collection invocation lock. Unknown delivery is deliberately never retried.
No background process or GUI API is installed or started by this module.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path
import subprocess
import sys
import uuid

from . import ledger, minutes, packet
from .journal import Journal
from .paths import TopicPaths
from .watcher import _invocation_lock

_PROCESSES = {name: name for name in ("codex", "claude", "gemini", "grok")}
_PROCESSES.update({"cc": "claude", "claude-code": "claude"})


def _paths(tp, inv):
    return (tp.root / "requests" / f"{inv}.md",
            tp.root / "requests" / f"{inv}.snapshot.md",
            tp.root / "deliveries" / f"{inv}.json")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path):
    if not path.parent.exists():
        return None
    raw = ledger.read_state(path)
    return json.loads(raw) if raw is not None else None


def _save(path, record):
    ledger.write_state(path, json.dumps(record, ensure_ascii=False, indent=2),
                       f"handoff: {record['invocation']} {record['state']}")


def _result(record, **extra):
    return {"ok": record["state"] not in {"sending", "delivery-unknown"},
            **record, **extra}


def _blocked(tp, inv, rec, *, capture=False):
    if rec is None:
        return {"ok": False, "reason": "unknown-invocation"}
    if rec.get("delivery_route") == "relay" and not capture:
        return {"ok": False, "reason": "relay-route", "next_action": "collect-or-inspect"}
    if rec["state"] != "prepared" and not (
        rec["state"] == "waiting" and rec.get("delivery_route") == "stdout-only"
    ):
        return {"ok": False, "reason": rec["state"], "next_action": "collect-or-inspect"}
    if not capture and any((tp.scratch / f"{inv}.json{suffix}").exists() for suffix in ("", ".tmp")):
        return {"ok": False, "reason": "response-present", "next_action": "collect"}
    return None


def _verify(record, request, snapshot):
    verified = {}
    for path, key in ((request, "packet_sha256"), (snapshot, "snapshot_sha256")):
        raw = ledger.read_state(path)
        if raw is None or _hash(raw) != record[key]:
            raise ValueError(f"handoff artifact hash mismatch: {key}")
        verified[key] = raw
    return verified["packet_sha256"]


def _script(value):
    path = Path(value) if value else Path()
    if not path.is_absolute() or path.name != "cmux_file_signal.py" or not path.is_file():
        raise ValueError("script must be an existing absolute cmux_file_signal.py path")
    resolved = path.resolve()
    if resolved.name != "cmux_file_signal.py":
        raise ValueError("resolved script must be named cmux_file_signal.py")
    return str(resolved)


def _capture_request(tp, inv_id, text):
    request, snapshot, _ = _paths(tp, inv_id)
    request.parent.mkdir(parents=True, exist_ok=True)
    existing = ledger.read_state(request)
    if existing is not None:
        raise ValueError("request already captured")
    if snapshot.exists():
        raise ValueError("incomplete request capture; inspect ledger before retry")
    with minutes._lock(tp):
        raw = minutes._read_verified(tp).encode("utf-8")
    participant = Journal.load(tp).data["invocations"][inv_id]["participant"]
    # 自由記述のrole_hintを探索せず、生成テンプレートの固定末尾だけを差し替える。
    body = packet.build(tp, participant, inv_id).split("\n", 2)[2]
    frozen_body = packet.build(tp, participant, inv_id, snapshot_path=snapshot).split("\n", 2)[2]
    if not text.endswith(body):
        raise ValueError("packet instruction block does not match generated template")
    text = text[:-len(body)] + frozen_body
    ledger.write_state(snapshot, raw.decode("utf-8"), f"handoff: {inv_id} snapshot")
    ledger.write_state(request, text, f"handoff: {inv_id} request")
    return text


def capture_request(tp: TopicPaths, inv_id: str, text: str) -> str:
    """Freeze context before dispatch prints or delivers a newly issued packet."""
    with _invocation_lock(tp, inv_id):
        rec = Journal.load(tp).data["invocations"].get(inv_id)
        blocked = _blocked(tp, inv_id, rec, capture=True)
        if blocked:
            raise ValueError(f"cannot capture request: {blocked['reason']}")
        return _capture_request(tp, inv_id, text)


def prepare(tp: TopicPaths, inv_id: str, transport="cmux", workspace=None,
            surface=None, script=None) -> dict:
    """Persist an immutable packet/snapshot and bind an explicit destination."""
    with _invocation_lock(tp, inv_id):
        rec = Journal.load(tp).data["invocations"].get(inv_id)
        if rec is None:
            return {"ok": False, "reason": "unknown-invocation"}
        if transport == "cmux":
            if not workspace or not surface:
                raise ValueError("explicit workspace and surface UUIDs required")
            workspace, surface = str(uuid.UUID(str(workspace))), str(uuid.UUID(str(surface)))
            script = _script(script)
            process = _PROCESSES.get(rec["participant"])
            if process is None:
                raise ValueError("participant has no supported cmux process mapping")
        elif transport == "claude-desktop":
            if workspace is not None or surface is not None or script is not None:
                raise ValueError("claude-desktop does not accept cmux destination options")
            if _PROCESSES.get(rec["participant"]) != "claude":
                raise ValueError("claude-desktop requires participant claude")
            process = None
        else:
            raise ValueError("unsupported handoff transport")
        config = dict(invocation=inv_id, participant=rec["participant"], transport=transport,
                      workspace=workspace, surface=surface, script=script, require_process=process)
        request, snapshot, receipt = _paths(tp, inv_id)
        for directory in (request.parent, receipt.parent):
            directory.mkdir(parents=True, exist_ok=True)
        old = _read(receipt)
        if old is not None:
            if any(old.get(key) != value for key, value in config.items()):
                raise ValueError("handoff already prepared with different destination")
            _verify(old, request, snapshot)
            return _result(old)
        blocked = _blocked(tp, inv_id, rec)
        if blocked:
            return blocked
        root = ledger.require_root(tp.root)
        ledger.require_clean(root, [request, snapshot, receipt])
        text_raw = ledger.read_state(request)
        source = "dispatch-captured"
        if text_raw is None:
            source = "legacy-current-minutes"
            text = packet.build(tp, rec["participant"], inv_id, role_hint=rec.get("role_hint", ""))
            _capture_request(tp, inv_id, text)
            text_raw = ledger.read_state(request)
        raw = ledger.read_state(snapshot)
        if raw is None:
            raise ValueError("request snapshot missing")
        record = dict(config, state="prepared", packet=str(request.resolve()),
                      snapshot=str(snapshot.resolve()), packet_sha256=_hash(text_raw),
                      snapshot_sha256=_hash(raw), source=source)
        _save(receipt, record)
        return _result(record)


def status(tp: TopicPaths, inv_id: str) -> dict:
    """Read receipts; an interrupted sending record means unknown, never safe retry."""
    with _invocation_lock(tp, inv_id):
        request, snapshot, receipt = _paths(tp, inv_id)
        record = _read(receipt)
        if record is None:
            return {"ok": False, "reason": "handoff-not-prepared"}
        _verify(record, request, snapshot)
        record["collection_state"] = Journal.load(tp).data["invocations"].get(
            inv_id, {}).get("state", "unknown-invocation")
        if record["state"] == "sending":
            return _result(record, ok=False, state="delivery-unknown", reason="interrupted-send")
        return _result(record)


def _cmux_preflight(script: str, timeout_s: float) -> dict | None:
    """送信しない起動検査。正式wrapperのargparse --helpはmain処理前に終了する。"""
    if shutil.which("cmux") is None:
        return {"reason": "cmux-preflight-failed", "preflight_error": "cmux-not-on-path"}
    try:
        result = subprocess.run([sys.executable, script, "--help"], shell=False,
                                capture_output=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", None) or b""
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8")
        return {"reason": "cmux-preflight-failed", "preflight_error": type(exc).__name__,
                "stderr_sha256": _hash(stderr)}
    if result.returncode != 0:
        return {"reason": "cmux-preflight-failed", "preflight_error": "wrapper-startup-failed",
                "returncode": result.returncode, "stderr_sha256": _hash(result.stderr or b"")}
    return None


def deliver(tp: TopicPaths, inv_id: str, timeout_s=30) -> dict:
    """Explicit one-shot delivery. Every uncertain outcome permanently blocks resend."""
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout must be positive and finite")
    with _invocation_lock(tp, inv_id):
        request, snapshot, receipt = _paths(tp, inv_id)
        record = _read(receipt)
        if record is None:
            return {"ok": False, "reason": "handoff-not-prepared"}
        verified_packet = _verify(record, request, snapshot)
        if record["state"] != "prepared":
            return _result(record, ok=False, reason="resend-forbidden",
                           state="delivery-unknown" if record["state"] == "sending" else record["state"])
        blocked = _blocked(tp, inv_id, Journal.load(tp).data["invocations"].get(inv_id))
        if blocked:
            return blocked
        if record["transport"] == "cmux":
            script = _script(record["script"])
            preflight = _cmux_preflight(script, timeout_s)
            if preflight is not None:
                return _result(record, ok=False, **preflight)
            args = [sys.executable, script, "--message-file", str(request.resolve()),
                    "--workspace", record["workspace"], "--surface", record["surface"],
                    "--path-only", "--no-control-block", "--mode", "pointer", "--transport", "file",
                    "--require-process", record["require_process"], "--verify-submit", "--verify-submit-json"]
        else:
            try:
                command, _, _ = packet.clipboard_command()
            except NotImplementedError as exc:
                return _result(record, ok=False, reason="clipboard-preflight-failed",
                               preflight_error=type(exc).__name__)
            if shutil.which(command) is None:
                return _result(record, ok=False, reason="clipboard-preflight-failed",
                               preflight_error="clipboard-command-not-on-path")
        record["state"] = "sending"
        _save(receipt, record)  # Durable before any possible clipboard/send side effect.
        try:
            if record["transport"] == "claude-desktop":
                packet.to_clipboard(verified_packet.decode("utf-8"), timeout_s=timeout_s)
                record.update(state="clipboard-ready", next_action="human-paste-required")
            else:
                result = subprocess.run(args, shell=False, capture_output=True,
                                        timeout=timeout_s)
                record.update(state="submitted" if result.returncode == 0 else "delivery-unknown",
                              returncode=result.returncode,
                              # Output can contain private screen text. Keep only a digest.
                              transport_output_sha256=_hash(result.stdout or b""))
        except (OSError, subprocess.SubprocessError, NotImplementedError) as exc:
            record.update(state="delivery-unknown", reason=type(exc).__name__)
        _save(receipt, record)
        return _result(record)
