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
