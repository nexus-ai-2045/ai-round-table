import hashlib

import pytest

from roundtable import cli, ledger
from roundtable.journal import Journal
from roundtable.paths import ensure_topic
from roundtable.relay import DeliveryUnknownError, save_seats
from roundtable.relay_codex import CodexAppServerRelay, RelayError

THREAD = "d5f923c8-984f-44ce-b168-ed5a37762474"
OTHER_THREAD = "e0c606e4-749d-4a2e-b9ee-7248f3ef8bda"


@pytest.fixture
def prepared(tmp_path):
    common = ["design", "--root", str(tmp_path)]
    assert cli.main(["new-topic", *common, "--topic", "Fixture", "--participants", "codex"]) == 0
    assert cli.main(["dispatch", *common, "--participant", "codex", "--no-clipboard", "--async"]) == 0
    tp = ensure_topic(tmp_path, "design")
    inv = next(iter(Journal.load(tp).data["invocations"]))
    save_seats(tp, {"rt/design/codex": {"participant": "codex", "thread_ref": THREAD}})
    request = tp.root / "requests" / f"{inv}.md"
    snapshot = tp.root / "requests" / f"{inv}.snapshot.md"
    argv = [
        "codex-send", *common, "--invocation", inv, "--thread-id", THREAD,
        "--packet-sha256", hashlib.sha256(request.read_bytes()).hexdigest(),
        "--snapshot-sha256", hashlib.sha256(snapshot.read_bytes()).hexdigest(), "--timeout", "0",
    ]
    return tp, inv, argv, request, snapshot


def test_frozen_send_binds_preview_and_persists_receipt(prepared, monkeypatch):
    tp, inv, argv, request, snapshot = prepared
    stale = Journal.load(tp)
    calls = []

    def send(_self, seat, text):
        calls.append(text)
        assert seat["thread_ref"] == THREAD
        assert "議事録の固定版" in text
        assert str(snapshot) not in text
        stale.record_human_action("test", "stale reader")
        assert Journal.load(tp).data["invocations"][inv]["codex_send_claim"]["thread_id"] == THREAD
        seat["submission_receipt"] = {
            "provider": "codex", "thread_id": THREAD, "turn_id": "fixture-turn", "status": "accepted",
        }

    monkeypatch.setattr("roundtable.relay_codex.resolve_codex_binary", lambda _: "unused")
    monkeypatch.setattr(CodexAppServerRelay, "send_existing", send)
    assert cli.main(argv) == 1  # acceptance alone has no collected output
    record = Journal.load(tp).data["invocations"][inv]
    assert record["submission_receipt"]["turn_id"] == "fixture-turn"
    assert record["state"] == "waiting"
    assert cli.main(argv) == 2
    assert len(calls) == 1


@pytest.mark.parametrize("reason", ["thread", "snapshot", "request"])
def test_preview_mismatch_cannot_start_transport(prepared, monkeypatch, reason):
    tp, inv, argv, request, snapshot = prepared
    if reason == "thread":
        argv[argv.index(THREAD)] = OTHER_THREAD
    else:
        ledger.write_state(request if reason == "request" else snapshot, "changed", "test")
    monkeypatch.setattr(CodexAppServerRelay, "__init__", lambda *_a, **_kw: pytest.fail("started"))
    assert cli.main(argv) == 2
    assert "codex_send_claim" not in Journal.load(tp).data["invocations"][inv]


def test_unknown_delivery_remains_collectable_and_cannot_send_again(prepared, monkeypatch):
    tp, inv, argv, *_ = prepared
    calls = []

    def send(*_args):
        calls.append(True)
        raise DeliveryUnknownError("fixture timeout")

    monkeypatch.setattr("roundtable.relay_codex.resolve_codex_binary", lambda _: "unused")
    monkeypatch.setattr(CodexAppServerRelay, "send_existing", send)
    assert cli.main(argv) == 1
    assert Journal.load(tp).data["invocations"][inv]["state"] == "delivery-unknown"
    assert cli.main(argv) == 2
    assert calls == [True]


def test_crash_after_claim_cannot_send_again(prepared, monkeypatch):
    tp, inv, argv, *_ = prepared

    def send(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr("roundtable.relay_codex.resolve_codex_binary", lambda _: "unused")
    monkeypatch.setattr(CodexAppServerRelay, "send_existing", send)
    with pytest.raises(KeyboardInterrupt):
        cli.main(argv)
    assert Journal.load(tp).data["invocations"][inv]["state"] == "prepared"
    assert cli.main(argv) == 2


def test_exact_relay_never_starts_a_new_thread(monkeypatch):
    monkeypatch.setattr("roundtable.relay_codex.resolve_codex_binary", lambda _: "unused")
    relay = CodexAppServerRelay()
    monkeypatch.setattr(relay, "_ensure", lambda: None)
    calls = []

    def rpc(method, params, **_):
        calls.append((method, params))
        return {"thread": {"id": OTHER_THREAD}}

    monkeypatch.setattr(relay, "_rpc", rpc)
    with pytest.raises(RelayError, match="different or missing thread"):
        relay.send_existing({"thread_ref": THREAD}, "fixture")
    assert [name for name, _ in calls] == ["thread/resume"]


def test_existing_relay_ack_and_watcher_collect_with_same_invocation(prepared, monkeypatch):
    import json

    tp, inv, argv, request, snapshot = prepared
    calls = []

    def rpc(_self, method, params, **_):
        calls.append(method)
        assert params["threadId"] == THREAD
        if method == "thread/resume":
            snapshot.write_text("DIFFERENT AFTER APPROVAL", encoding="utf-8")
            return {"thread": {"id": THREAD}}
        assert method == "turn/start"
        text = params["input"][0]["text"]
        assert "DIFFERENT AFTER APPROVAL" not in text
        assert "議事録の固定版" in text
        assert str(snapshot) not in text
        (tp.scratch / f"{inv}.json").write_text(json.dumps({
            "invocation_id": inv, "participant": "codex", "opinion": "Fixture opinion",
            "claims": [{"claim": "A", "evidence_type": "argument", "evidence": "B"}],
        }), encoding="utf-8")
        return {"turn": {"id": "fixture-turn"}}

    monkeypatch.setattr("roundtable.relay_codex.resolve_codex_binary", lambda _: "unused")
    monkeypatch.setattr(CodexAppServerRelay, "_ensure", lambda _: None)
    monkeypatch.setattr(CodexAppServerRelay, "_rpc", rpc)
    assert cli.main(argv) == 0
    assert calls == ["thread/resume", "turn/start"]
    record = Journal.load(tp).data["invocations"][inv]
    assert record["state"] == "merged"
    assert record["submission_receipt"]["turn_id"] == "fixture-turn"
    assert "Fixture opinion" in tp.minutes.read_text(encoding="utf-8")
