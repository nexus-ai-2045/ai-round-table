from hashlib import sha256
import json
from pathlib import Path
import uuid

import pytest

from roundtable import app_actions, codex_queue
from roundtable.atomicio import atomic_write
from roundtable.journal import Journal
from roundtable.paths import ensure_topic
from roundtable.relay import save_seats
from roundtable.read_model import export_topic


@pytest.mark.parametrize("lost_reply", [False, True])
def test_queue_matches_arrival_and_collects_without_resending(tmp_path, lost_reply):
    root = tmp_path.resolve()
    slug = "queue-test"
    target = str(uuid.uuid4())
    assert app_actions.execute(root, slug, "new-topic", "--topic", "Queue", "--participants", "codex").returncode == 0
    inv = app_actions.prepared_invocation(app_actions.prepare_request(root, slug, "codex", "通常", question="確認"))
    tp = ensure_topic(root, slug)
    save_seats(tp, {f"rt/{slug}/codex": {"participant": "codex", "thread_ref": target, "transport": "codex-queue"}})
    frozen = app_actions.preview(root, slug, "codex", inv, target)
    calls = []
    sent = {}

    class Client:
        def __init__(self, **kwargs):
            pass

        def _ensure(self):
            pass

        def close(self):
            pass

        def _rpc(self, method, params):
            calls.append(method)
            assert method not in ("thread/resume", "turn/start")
            claim = Journal.load(tp).data["invocations"][inv]["codex_queue_claim"]
            if method == "thread/queue/add":
                assert params["clientUserMessageId"] == claim["client_id"]
                assert params["threadId"] == target
                sent.update(params)
                atomic_write(tp.scratch / f"{inv}.json", json.dumps({
                    "invocation_id": inv, "participant": "codex", "opinion": "回収結果",
                    "claims": [{"claim": "A", "evidence_type": "argument", "evidence": "B"}],
                }))
                if lost_reply:
                    raise EOFError("fixture connection loss after receiver accepted")
                return {"queuedSubmission": {"id": str(uuid.uuid4()), "threadId": target,
                                            "clientUserMessageId": claim["client_id"]}}
            if not params.get("includeTurns"):
                return {"thread": {"id": target}}
            return {"thread": {"id": target, "turns": [{
                "id": "matching-turn", "status": "completed", "items": [{
                    "type": "userMessage", "clientId": claim["client_id"], "content": sent["input"],
                }],
            }]}}

    codex_queue.send(frozen, root, slug, client_factory=Client)
    current = Journal.load(tp).data["invocations"][inv]
    assert current["state"] == "merged"
    assert current["codex_queue_arrival"]["turn_id"] == "matching-turn"
    assert ("codex_queue_acceptance" in current) is not lost_reply
    before = export_topic(root, slug)["minutes"]
    codex_queue.recover(root, slug, inv, timeout=0, client_factory=Client)
    assert export_topic(root, slug)["minutes"] == before
    with pytest.raises(ValueError):
        codex_queue.send(frozen, root, slug, client_factory=Client)
    assert calls.count("thread/queue/add") == 1


@pytest.mark.parametrize("wrong", ["client", "body", "thread"])
def test_arrival_needs_exact_thread_client_and_body(tmp_path, wrong):
    target, client_id = str(uuid.uuid4()), str(uuid.uuid4())
    tp = ensure_topic(tmp_path, "arrival")
    from roundtable import minutes
    minutes.create(tp, "Check", ["codex"])
    journal = Journal.load(tp)
    inv = journal.new_invocation("codex", 1)
    journal.data["invocations"][inv]["codex_queue_claim"] = {
        "thread_id": target, "client_id": client_id, "body_sha256": sha256(b"approved").hexdigest(),
    }
    journal.save()

    class Client:
        def _rpc(self, method, params):
            return {"thread": {"id": "wrong" if wrong == "thread" else target, "turns": [{
                "id": "turn", "status": "completed", "items": [{
                    "type": "userMessage", "clientId": "wrong" if wrong == "client" else client_id,
                    "content": [{"type": "text", "text": "changed" if wrong == "body" else "approved"}],
                }],
            }]}}

    if wrong == "thread":
        with pytest.raises(ValueError):
            codex_queue.observe(tp, inv, Client())
    else:
        assert codex_queue.observe(tp, inv, Client())["state"] == "unknown"


def test_stale_journal_preserves_queue_acceptance_and_completed_arrival():
    from roundtable.journal import _merge_invocations

    saved = {
        "state": "delivered", "codex_queue_claim": {"thread_id": "target"},
        "codex_queue_acceptance": {"queue_id": "queue", "client_id": "client"},
        "codex_queue_arrival": {"turn_id": "turn", "state": "completed"},
    }
    stale = {"state": "delivered"}
    merged = _merge_invocations({"inv": saved}, {"inv": stale}, [])
    for field in ("codex_queue_acceptance", "codex_queue_arrival"):
        assert merged["inv"][field] == saved[field]
    older = {**stale, "codex_queue_arrival": {"turn_id": "turn", "state": "arrived"}}
    assert _merge_invocations({"inv": saved}, {"inv": older}, [])["inv"]["codex_queue_arrival"] == saved["codex_queue_arrival"]
    conflicts = []
    wrong = {**stale, "codex_queue_arrival": {"turn_id": "other", "state": "completed"}}
    result = _merge_invocations({"inv": saved}, {"inv": wrong}, conflicts)
    assert result["inv"]["codex_queue_arrival"] == saved["codex_queue_arrival"]
    assert len(conflicts) == 1
