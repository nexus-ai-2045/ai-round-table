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
            assert not params.get("includeTurns")
            if method == "thread/read":
                return {"thread": {"id": target}}
            assert method == "thread/turns/list" and params["itemsView"] == "full"
            return {"data": [{
                "id": "matching-turn", "status": "completed", "itemsView": "full", "items": [{
                    "type": "userMessage", "clientId": claim["client_id"], "content": sent["input"],
                }],
            }], "nextCursor": None}

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
            if method == "thread/read":
                return {"thread": {"id": "wrong" if wrong == "thread" else target}}
            return {"data": [{
                "id": "turn", "status": "completed", "items": [{
                    "type": "userMessage", "clientId": "wrong" if wrong == "client" else client_id,
                    "content": [{"type": "text", "text": "changed" if wrong == "body" else "approved"}],
                }],
            }], "nextCursor": None}

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


def test_arrival_reads_paginated_thread_history_with_turns_list(tmp_path):
    target, client_id = str(uuid.uuid4()), str(uuid.uuid4())
    tp = ensure_topic(tmp_path, "paginated")
    from roundtable import minutes
    minutes.create(tp, "Check", ["codex"])
    journal = Journal.load(tp)
    inv = journal.new_invocation("codex", 1)
    journal.data["invocations"][inv]["codex_queue_claim"] = {
        "thread_id": target, "client_id": client_id, "body_sha256": sha256(b"approved").hexdigest(),
    }
    journal.save()
    pages = []

    class Client:
        def _rpc(self, method, params):
            if method == "thread/read":
                if params.get("includeTurns"):
                    raise RuntimeError("includeTurns is unsupported for paginated threads")
                return {"thread": {"id": target}}
            assert method == "thread/turns/list" and params["itemsView"] == "full"
            pages.append(params.get("cursor"))
            if params.get("cursor") is None:
                return {"data": [{"id": "newer", "status": "completed", "itemsView": "full", "items": [
                    {"type": "userMessage", "clientId": "other", "content": [{"type": "text", "text": "x"}]}]}],
                    "nextCursor": "older"}
            return {"data": [{"id": "turn", "status": "completed", "itemsView": "full", "items": [
                {"type": "userMessage", "clientId": client_id, "content": [{"type": "text", "text": "approved"}]}]}],
                "nextCursor": None}

    assert codex_queue.observe(tp, inv, Client()) == {"state": "completed", "turn_id": "turn"}
    assert pages == [None, "older"]


def test_queue_client_allows_turns_list(monkeypatch):
    from roundtable.relay_codex import CodexAppServerRelay

    monkeypatch.setattr(CodexAppServerRelay, "_rpc", lambda self, method, params, timeout=15: method)
    client = codex_queue.QueueClient.__new__(codex_queue.QueueClient)
    for method in ("thread/read", "thread/turns/list", "thread/queue/add"):
        assert client._rpc(method, {}) == method
    with pytest.raises(ValueError):
        client._rpc("turn/start", {})


@pytest.mark.parametrize("suffix", ["", ".tmp"])
def test_send_refuses_when_response_already_present(tmp_path, suffix):
    root = tmp_path.resolve()
    slug = "queue-present"
    target = str(uuid.uuid4())
    assert app_actions.execute(root, slug, "new-topic", "--topic", "Queue", "--participants", "codex").returncode == 0
    inv = app_actions.prepared_invocation(app_actions.prepare_request(root, slug, "codex", "通常", question="確認"))
    tp = ensure_topic(root, slug)
    save_seats(tp, {f"rt/{slug}/codex": {"participant": "codex", "thread_ref": target, "transport": "codex-queue"}})
    frozen = app_actions.preview(root, slug, "codex", inv, target)
    atomic_write(tp.scratch / f"{inv}.json{suffix}", "{}")
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def _rpc(self, method, params):
            calls.append(method)
            raise AssertionError("must not contact the server")

    with pytest.raises(ValueError):
        codex_queue.send(frozen, root, slug, client_factory=Client)
    record = Journal.load(tp).data["invocations"][inv]
    assert "codex_queue_claim" not in record
    assert record["state"] == "prepared"
    assert calls == []


def _prepared_codex_queue(tmp_path, slug):
    root = tmp_path.resolve()
    target = str(uuid.uuid4())
    assert app_actions.execute(root, slug, "new-topic", "--topic", "Queue", "--participants", "codex").returncode == 0
    inv = app_actions.prepared_invocation(app_actions.prepare_request(root, slug, "codex", "通常", question="確認"))
    tp = ensure_topic(root, slug)
    save_seats(tp, {f"rt/{slug}/codex": {"participant": "codex", "thread_ref": target, "transport": "codex-queue"}})
    return root, tp, inv, target


def test_queue_send_refuses_a_snapshot_changed_outside_the_ledger(tmp_path):
    from roundtable.ledger import LedgerError

    root, tp, inv, target = _prepared_codex_queue(tmp_path, "queue-dirty")
    snapshot = tp.root / "requests" / f"{inv}.snapshot.md"
    snapshot.write_text(snapshot.read_text(encoding="utf-8") + "\n台帳の外の書換え\n", encoding="utf-8")
    frozen = app_actions.preview(root, "queue-dirty", "codex", inv, target)
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def _ensure(self):
            pass

        def close(self):
            pass

        def _rpc(self, method, params):
            calls.append(method)
            return {}

    with pytest.raises(LedgerError):
        codex_queue.send(frozen, root, "queue-dirty", client_factory=Client)
    assert calls == []
    assert "codex_queue_claim" not in Journal.load(tp).data["invocations"][inv]


def test_antigravity_send_refuses_a_packet_changed_outside_the_ledger(tmp_path, monkeypatch):
    from roundtable.ledger import LedgerError

    root = tmp_path.resolve()
    slug = "agy-dirty"
    assert app_actions.execute(root, slug, "new-topic", "--topic", "Queue", "--participants", "antigravity").returncode == 0
    inv = app_actions.prepared_invocation(app_actions.prepare_request(root, slug, "antigravity", "反証", question="確認"))
    tp = ensure_topic(root, slug)
    request = tp.root / "requests" / f"{inv}.md"
    request.write_text(request.read_text(encoding="utf-8") + "台帳の外の書換え\n", encoding="utf-8")
    frozen = app_actions.preview(root, slug, "antigravity", inv, app_actions.ANTIGRAVITY_MODEL)
    agy = str(tmp_path / "agy-fixture")
    monkeypatch.setattr(app_actions.shutil, "which", lambda name: agy)
    ran = []
    real_run = app_actions.subprocess.run

    def run(argv, *args, **kwargs):
        if argv and argv[0] == agy:
            ran.append(argv)
            raise AssertionError("antigravity must not be called")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(app_actions.subprocess, "run", run)
    with pytest.raises(LedgerError):
        frozen.send_antigravity(root, slug)
    assert ran == []
    assert "antigravity_send_claim" not in Journal.load(tp).data["invocations"][inv]


def test_antigravity_send_refuses_an_oversized_prompt_before_claiming(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    slug = "agy-large"
    assert app_actions.execute(root, slug, "new-topic", "--topic", "Queue", "--participants", "antigravity").returncode == 0
    inv = app_actions.prepared_invocation(app_actions.prepare_request(root, slug, "antigravity", "反証", question="確認"))
    tp = ensure_topic(root, slug)
    frozen = app_actions.preview(root, slug, "antigravity", inv, app_actions.ANTIGRAVITY_MODEL)
    agy = str(tmp_path / "agy-fixture")
    monkeypatch.setattr(app_actions.shutil, "which", lambda name: agy)
    monkeypatch.setattr(app_actions.Preview, "antigravity_body",
                        lambda self, root, slug: "議" * (app_actions.ANTIGRAVITY_PROMPT_LIMIT // 3 + 1))
    ran = []
    real_run = app_actions.subprocess.run

    def run(argv, *args, **kwargs):
        if argv and argv[0] == agy:
            ran.append(argv)
            raise AssertionError("antigravity must not be called")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(app_actions.subprocess, "run", run)
    with pytest.raises(ValueError):
        frozen.send_antigravity(root, slug)
    assert ran == []
    record = Journal.load(tp).data["invocations"][inv]
    assert "antigravity_send_claim" not in record
    assert record["state"] == "prepared"
