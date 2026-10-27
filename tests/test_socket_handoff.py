import json

import pytest

from roundtable import handoff, minutes, packet
from roundtable.journal import Journal
from roundtable.paths import ensure_topic


def setup_topic(tmp_path, monkeypatch):
    from roundtable import claude_socket
    tp = ensure_topic(tmp_path, "local-seat")
    minutes.create(tp, "既存チャットへの送受信", ["claude"])
    minutes.make_snapshot(tp)
    inv = Journal.load(tp).new_invocation("claude", 3)
    handoff.capture_request(tp, inv, packet.build(tp, "claude", inv))
    session = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setattr(claude_socket, "resolve_target", lambda sid: {"session_id": sid})
    handoff.prepare(tp, inv, transport="claude-socket", claude_session=session)
    return tp, inv, claude_socket


def answer(inv):
    return {"invocation_id": inv, "participant": "claude", "opinion": "画面操作を省ける",
            "claims": [{"claim": "送信と回収を分ける", "evidence_type": "argument", "evidence": "未回収を成功にしない"}]}


def test_prepare_does_not_send_and_binds_session(tmp_path, monkeypatch):
    tp, inv, socket = setup_topic(tmp_path, monkeypatch)
    monkeypatch.setattr(socket, "send", lambda *a: pytest.fail("prepare must not send"))
    status = handoff.status(tp, inv)
    assert status["state"] == "prepared"
    with pytest.raises(ValueError, match="different destination"):
        handoff.prepare(tp, inv, transport="claude-socket", claude_session="00000000-0000-4000-8000-000000000002")


def test_socket_send_once_and_collect_exactly_once(tmp_path, monkeypatch):
    tp, inv, socket = setup_topic(tmp_path, monkeypatch)
    calls = []
    def send(target, text, msg_id):
        calls.append(msg_id)
        assert target["request_marker"] == msg_id
        assert inv in text and msg_id in text
        return {"state": "frame-written"}
    monkeypatch.setattr(socket, "send", send)
    delivered = handoff.deliver(tp, inv)
    assert delivered["state"] == "submitted"
    assert delivered["ok"] is False
    assert handoff.deliver(tp, inv)["reason"] == "resend-forbidden"
    assert len(calls) == 1
    monkeypatch.setattr(socket, "reply", lambda *a: None)
    assert handoff.collect_socket(tp, inv)["state"] == "waiting"
    monkeypatch.setattr(socket, "reply", lambda *a: {"text": json.dumps(answer(inv)), "response_uuid": "r1"})
    assert handoff.collect_socket(tp, inv)["state"] == "answer-observed"
    assert handoff.collect_socket(tp, inv)["ok"]
    assert Journal.load(tp).is_merged(inv)
    assert handoff.collect_socket(tp, inv)["already_collected"]


def test_uncertain_send_can_collect_but_never_resend(tmp_path, monkeypatch):
    tp, inv, socket = setup_topic(tmp_path, monkeypatch)
    def lost(*a):
        raise OSError("connection lost")
    monkeypatch.setattr(socket, "send", lost)
    assert handoff.deliver(tp, inv)["state"] == "delivery-unknown"
    assert handoff.deliver(tp, inv)["reason"] == "resend-forbidden"
    monkeypatch.setattr(socket, "reply", lambda *a: {"text": json.dumps(answer(inv)), "response_uuid": "r1"})
    assert handoff.collect_socket(tp, inv)["state"] == "answer-observed"
    assert handoff.collect_socket(tp, inv)["ok"]


def test_changed_target_fails_before_sending(tmp_path, monkeypatch):
    tp, inv, socket = setup_topic(tmp_path, monkeypatch)
    def changed(*a):
        raise ValueError("session replaced")
    monkeypatch.setattr(socket, "resolve_target", changed)
    monkeypatch.setattr(socket, "send", lambda *a: pytest.fail("must not send"))
    with pytest.raises(ValueError):
        handoff.deliver(tp, inv)
    assert handoff.status(tp, inv)["state"] == "prepared"


def test_transport_rejection_is_unknown_and_not_retryable(tmp_path, monkeypatch):
    tp, inv, socket = setup_topic(tmp_path, monkeypatch)
    monkeypatch.setattr(socket, "send", lambda *a: {"state": "transport-rejected"})
    result = handoff.deliver(tp, inv)
    assert not result["ok"] and result["state"] == "delivery-unknown"
    assert handoff.deliver(tp, inv)["reason"] == "resend-forbidden"


def test_wrong_response_is_not_saved(tmp_path, monkeypatch):
    tp, inv, socket = setup_topic(tmp_path, monkeypatch)
    monkeypatch.setattr(socket, "send", lambda *a: {})
    handoff.deliver(tp, inv)
    monkeypatch.setattr(socket, "reply", lambda *a: {"text": json.dumps(answer("wrong")), "response_uuid": "r1"})
    with pytest.raises(ValueError, match="contract"):
        handoff.collect_socket(tp, inv)
    assert not (tp.scratch / f"{inv}.json").exists()


def test_cancelled_invocation_not_collected(tmp_path, monkeypatch):
    tp, inv, socket = setup_topic(tmp_path, monkeypatch)
    monkeypatch.setattr(socket, "send", lambda *a: {})
    handoff.deliver(tp, inv)
    Journal.load(tp).set_state(inv, "cancelled", "operator")
    monkeypatch.setattr(socket, "reply", lambda *a: pytest.fail("cancelled"))
    assert handoff.collect_socket(tp, inv)["reason"] == "socket-invocation-not-collectable"


def test_response_uuid_change_needs_new_stability_observation(tmp_path, monkeypatch):
    tp, inv, socket = setup_topic(tmp_path, monkeypatch)
    monkeypatch.setattr(socket, "send", lambda *a: {})
    handoff.deliver(tp, inv)
    for response in ("first", "replacement"):
        monkeypatch.setattr(socket, "reply", lambda *a: {"text": json.dumps(answer(inv)), "response_uuid": response})
        assert handoff.collect_socket(tp, inv)["state"] == "answer-observed"
    assert not (tp.scratch / f"{inv}.json").exists()
