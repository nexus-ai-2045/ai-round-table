"""Official Codex queue delivery with a durable claim and read-only recovery."""
from __future__ import annotations

from hashlib import sha256
import time
import uuid

from .app_actions import preview
from .journal import Journal
from .paths import ensure_topic
from .relay import load_seats
from .relay_codex import CodexAppServerRelay
from .watcher import _invocation_lock


class QueueClient(CodexAppServerRelay):
    def _rpc(self, method, params, timeout=15):
        if method == "initialize":
            params = {**params, "capabilities": {"experimentalApi": True}}
        elif method not in ("thread/read", "thread/queue/add"):
            raise ValueError("unsupported queue operation")
        return super()._rpc(method, params, timeout=timeout)


def _claim(tp, inv):
    rec = Journal.load(tp).data["invocations"].get(inv, {})
    claim = rec.get("codex_queue_claim")
    if not isinstance(claim, dict):
        raise ValueError("キュー送信の予約がありません")
    return rec, claim


def observe(tp, inv, client):
    """Only an exact user item in the intended thread proves arrival."""
    _, claim = _claim(tp, inv)
    thread = client._rpc("thread/read", {"threadId": claim["thread_id"], "includeTurns": True})["thread"]
    if thread.get("id") != claim["thread_id"]:
        raise ValueError("履歴の対象が一致しません")
    matches = []
    for turn in thread.get("turns", []):
        if turn.get("itemsView", "full") != "full":
            continue
        items = turn.get("items", [])
        if not items:
            continue
        user = items[0]
        if user.get("type") != "userMessage" or user.get("clientId") != claim["client_id"]:
            continue
        content = user.get("content", [])
        if len(content) != 1 or content[0].get("type") != "text":
            continue
        text = content[0].get("text")
        if not isinstance(text, str) or sha256(text.encode()).hexdigest() != claim["body_sha256"]:
            continue
        matches.append(turn)
    if len(matches) != 1:
        return {"state": "unknown" if not matches else "ambiguous"}
    turn = matches[0]
    return {"state": "completed" if turn.get("status") == "completed" else "arrived",
            "turn_id": turn["id"]}


def recover(root, slug, inv, *, timeout=120, client_factory=QueueClient):
    """A second call observes existing work. It never adds to the queue."""
    from .app_actions import execute, report

    tp = ensure_topic(root, slug)
    client = client_factory(cwd=str(tp.root.resolve()))
    deadline = time.monotonic() + timeout
    try:
        client._ensure()
        while True:
            observed = observe(tp, inv, client)
            if observed["state"] in ("arrived", "completed"):
                with _invocation_lock(tp, inv):
                    journal = Journal.load(tp)
                    record = journal.data["invocations"][inv]
                    record["codex_queue_arrival"] = observed
                    journal.save()
                if observed["state"] == "completed":
                    result = execute(root, slug, "collect", "--invocation", inv, "--timeout", "0")
                    return report(result)
            if time.monotonic() >= deadline:
                return "送信結果は照合待ちです。同じ依頼の回収を続けてください。"
            time.sleep(1)
    finally:
        client.close()


def send(frozen, root, slug, *, client_factory=QueueClient):
    """Reserve before the one and only queue add. Uncertain replies are not retried."""
    tp = ensure_topic(root, slug)
    inv = frozen.invocation
    with _invocation_lock(tp, inv):
        journal = Journal.load(tp)
        record = journal.data["invocations"].get(inv, {})
        seat = load_seats(tp).get(f"rt/{slug}/codex", {})
        if (record.get("state") != "prepared" or record.get("participant") != "codex"
                or record.get("delivery_route") != "stdout-only"
                or any(key in record for key in ("codex_send_claim", "codex_queue_claim"))
                or seat.get("thread_ref") != frozen.target or seat.get("transport") != "codex-queue"):
            raise ValueError("登録席または依頼の状態が変わりました")
        if preview(root, slug, "codex", inv, frozen.target) != frozen:
            raise ValueError("プレビュー後に内容が変わりました")
        body = frozen.desktop_body(root, slug)
        claim = {"thread_id": frozen.target, "client_id": str(uuid.uuid4()),
                 "packet_sha256": frozen.packet_hash, "snapshot_sha256": frozen.snapshot_hash,
                 "body_sha256": sha256(body.encode()).hexdigest()}
        record["codex_queue_claim"] = claim
        record["delivery_route"] = "relay"
        journal.save()
        if Journal.load(tp).data["invocations"][inv].get("codex_queue_claim") != claim:
            raise ValueError("送信予約を保存できません")
        client = client_factory(cwd=str(tp.root.resolve()))
        try:
            client._ensure()
            metadata = client._rpc("thread/read", {"threadId": frozen.target, "includeTurns": False})
            if metadata.get("thread", {}).get("id") != frozen.target:
                raise ValueError("会話を照合できません")
            response = client._rpc("thread/queue/add", {
                "threadId": frozen.target, "input": [{"type": "text", "text": body}],
                "clientUserMessageId": claim["client_id"],
            })
            receipt = response.get("queuedSubmission", {})
            if (not isinstance(receipt.get("id"), str)
                    or receipt.get("threadId", frozen.target) != frozen.target
                    or receipt.get("clientUserMessageId", claim["client_id"]) != claim["client_id"]):
                raise ValueError("受付の対応を確認できません")
            journal.data["invocations"][inv]["codex_queue_acceptance"] = {"queue_id": receipt["id"], "client_id": claim["client_id"]}
            journal.set_state(inv, "delivered", "codex-queue-accepted")
        except Exception:
            journal.set_state(inv, "delivery-unknown", "公式キューの応答を確認できません")
        finally:
            client.close()
    return recover(root, slug, inv, client_factory=client_factory)
