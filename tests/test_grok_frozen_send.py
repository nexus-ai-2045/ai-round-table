from hashlib import sha256
import json

from roundtable import cli
from roundtable.journal import Journal
from roundtable.paths import ensure_topic
from roundtable.relay import RelayError, save_seats
from roundtable.relay_grok import GrokAcpRelay


def test_grok_sends_frozen_review_and_collects_once(tmp_path, monkeypatch):
    common = ["design", "--root", str(tmp_path)]
    assert cli.main(["new-topic", *common, "--topic", "Fixture", "--participants", "grok"]) == 0
    assert cli.main(["dispatch", *common, "--participant", "grok", "--no-clipboard", "--async"]) == 0
    tp = ensure_topic(tmp_path, "design")
    inv = next(iter(Journal.load(tp).data["invocations"]))
    target = "grok-fixture-session"
    save_seats(tp, {"rt/design/grok": {"participant": "grok", "thread_ref": target}})
    request = tp.root / "requests" / f"{inv}.md"
    snapshot = tp.root / "requests" / f"{inv}.snapshot.md"
    argv = ["grok-send", *common, "--invocation", inv, "--thread-id", target,
            "--packet-sha256", sha256(request.read_bytes()).hexdigest(),
            "--snapshot-sha256", sha256(snapshot.read_bytes()).hexdigest(), "--timeout", "0"]
    calls = []
    monkeypatch.setattr("roundtable.relay_grok.resolve_grok_binary", lambda _: "unused")

    def send(self, seat, text):
        calls.append(seat["thread_ref"])
        assert "Fixture" in text and str(snapshot) not in text
        assert self.auto_allow_kinds == ()
        assert Journal.load(tp).data["invocations"][inv]["grok_send_claim"]
        (tp.scratch / f"{inv}.json").write_text(json.dumps({
            "invocation_id": inv, "participant": "grok", "opinion": "Fixture opinion",
            "claims": [{"claim": "A", "evidence_type": "argument", "evidence": "B"}],
        }), encoding="utf-8")
        raise RelayError("reply lost after submission")

    monkeypatch.setattr(GrokAcpRelay, "send", send)
    assert cli.main([*argv[:7], "different-session", *argv[8:]]) == 2
    assert calls == []
    assert cli.main(argv) == 0
    assert Journal.load(tp).data["invocations"][inv]["state"] == "merged"
    assert cli.main(argv) == 2
    assert calls == [target]


def _frozen_send_fixture(tmp_path, monkeypatch):
    common = ["design", "--root", str(tmp_path)]
    assert cli.main(["new-topic", *common, "--topic", "Fixture", "--participants", "grok"]) == 0
    assert cli.main(["dispatch", *common, "--participant", "grok", "--no-clipboard", "--async"]) == 0
    tp = ensure_topic(tmp_path, "design")
    inv = next(iter(Journal.load(tp).data["invocations"]))
    target = "grok-fixture-session"
    save_seats(tp, {"rt/design/grok": {"participant": "grok", "thread_ref": target}})
    request = tp.root / "requests" / f"{inv}.md"
    snapshot = tp.root / "requests" / f"{inv}.snapshot.md"
    argv = ["grok-send", *common, "--invocation", inv, "--thread-id", target,
            "--packet-sha256", sha256(request.read_bytes()).hexdigest(),
            "--snapshot-sha256", sha256(snapshot.read_bytes()).hexdigest(), "--timeout", "0"]
    monkeypatch.setattr("roundtable.relay_grok.resolve_grok_binary", lambda _: "unused")
    return tp, inv, target, argv


def _chunk(session_id, text, kind="agent_message_chunk"):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": session_id,
        "update": {"sessionUpdate": kind, "content": {"type": "text", "text": text}}}}


def test_grok_answer_in_conversation_is_collected_when_scratch_write_was_denied(tmp_path, monkeypatch):
    tp, inv, target, argv = _frozen_send_fixture(tmp_path, monkeypatch)
    answer = json.dumps({
        "invocation_id": inv, "participant": "grok", "opinion": "Answered in chat",
        "claims": [{"claim": "A", "evidence_type": "argument", "evidence": "B"}],
    })

    def send(self, seat, text):
        self._begin_answer_capture(seat["thread_ref"])
        self._handle_notification(_chunk(target, "書込みが拒否されたので返答で渡します。\n```json\n"))
        self._handle_notification(_chunk(target, answer + "\n```\n"))
        return "tier1-prompt:end_turn"

    monkeypatch.setattr(GrokAcpRelay, "send", send)
    assert cli.main(argv) == 0
    data = Journal.load(tp).data["invocations"][inv]
    assert data["state"] == "merged"
    assert data["answer_source"] == "conversation"


def test_grok_conversation_without_answer_json_is_not_collected(tmp_path, monkeypatch):
    tp, inv, target, argv = _frozen_send_fixture(tmp_path, monkeypatch)

    def send(self, seat, text):
        self._begin_answer_capture(seat["thread_ref"])
        self._handle_notification(_chunk(target, "ファイルを書けませんでした。"))
        return "tier1-prompt:end_turn"

    monkeypatch.setattr(GrokAcpRelay, "send", send)
    assert cli.main(argv) != 0
    data = Journal.load(tp).data["invocations"][inv]
    assert data["state"] != "merged"
    assert "answer_source" not in data
    assert not (tp.scratch / f"{inv}.json").exists()


def test_grok_answer_capture_ignores_replayed_and_foreign_chunks(monkeypatch):
    monkeypatch.setattr("roundtable.relay_grok.resolve_grok_binary", lambda _: "unused")
    relay = GrokAcpRelay(auto_allow_kinds=())
    relay._handle_notification(_chunk("s1", '{"replayed": true}'))
    relay._begin_answer_capture("s1")
    relay._handle_notification(_chunk("s1", '{"user": true}', kind="user_message_chunk"))
    relay._handle_notification(_chunk("other", '{"foreign": true}'))
    relay._handle_notification(_chunk("s1", 'prefix {"current": '))
    relay._handle_notification(_chunk("s1", 'true} suffix'))
    assert json.loads(relay.captured_answer_json()) == {"current": True}


def test_grok_answer_capture_rejects_non_object_or_ambiguous_text(monkeypatch):
    monkeypatch.setattr("roundtable.relay_grok.resolve_grok_binary", lambda _: "unused")
    relay = GrokAcpRelay(auto_allow_kinds=())
    for text in ("no json here", "[1, 2]", '{"a": 1} and {"b": 2}', '{"broken": '):
        relay._begin_answer_capture("s1")
        relay._handle_notification(_chunk("s1", text))
        assert relay.captured_answer_json() is None, text
