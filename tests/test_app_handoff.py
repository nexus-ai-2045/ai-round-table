import json
import subprocess

import pytest

from roundtable import browser_seat, handoff, minutes, packet
from roundtable.journal import Journal
from roundtable.paths import ensure_topic


def topic(tmp_path, participant="grok"):
    tp = ensure_topic(tmp_path, "app-seat")
    minutes.create(tp, "Visible app review", [participant])
    minutes.make_snapshot(tp)
    journal = Journal.load(tp)
    inv = journal.new_invocation(participant, 1)
    handoff.capture_request(tp, inv, packet.build(tp, participant, inv))
    return tp, inv


def browser(tp, inv):
    return handoff.prepare(tp, inv, transport="grok-browser", browser_window=12,
                           browser_tab=34, browser_url="https://grok.com/", browser_account="test-account")


@pytest.mark.parametrize("url", ["http://grok.com/", "https://evil.test/", "https://grok.com@evil.test/", "https://grok.com/settings"])
def test_browser_target_rejects_other_origins(url):
    with pytest.raises(ValueError):
        browser_seat.validate_target(12, 34, url)


def test_browser_draft_is_preserved_before_any_send(tmp_path, monkeypatch):
    tp, inv = topic(tmp_path)
    browser(tp, inv)
    monkeypatch.setattr(browser_seat, "inspect", lambda *a: {"editor_count": 1, "draft_empty": False, "account": "test-account"})
    monkeypatch.setattr(browser_seat, "send", lambda *a: pytest.fail("existing draft must not be touched"))
    assert handoff.deliver(tp, inv)["reason"] == "browser-not-ready-draft-preserved"
    assert handoff.status(tp, inv)["state"] == "prepared"


def test_uncertain_dom_send_never_retries(tmp_path, monkeypatch):
    tp, inv = topic(tmp_path)
    browser(tp, inv)
    monkeypatch.setattr(browser_seat, "inspect", lambda *a: {"editor_count": 1, "draft_empty": True, "account": "test-account"})
    calls = []
    def send(*args):
        calls.append(args)
        raise RuntimeError("DOM connection lost")
    monkeypatch.setattr(browser_seat, "send", send)
    assert handoff.deliver(tp, inv)["state"] == "delivery-unknown"
    assert handoff.deliver(tp, inv)["reason"] == "resend-forbidden"
    assert len(calls) == 1


def test_visible_reply_uses_existing_collector_once(tmp_path, monkeypatch):
    tp, inv = topic(tmp_path)
    browser(tp, inv)
    monkeypatch.setattr(browser_seat, "inspect", lambda *a: {"editor_count": 1, "draft_empty": True, "account": "test-account"})
    monkeypatch.setattr(browser_seat, "send", lambda *a: {"state": "submit-clicked"})
    assert handoff.deliver(tp, inv)["state"] == "submitted"
    answer = {"invocation_id": inv, "participant": "grok", "opinion": "Test reply",
              "claims": [{"claim": "Use native links", "evidence_type": "argument", "evidence": "Visible chat"}]}
    monkeypatch.setattr(browser_seat, "reply", lambda *a: {"state": "answer-visible", "url": "https://grok.com/c/test", "text": json.dumps(answer)})
    assert handoff.collect_browser(tp, inv)["state"] == "answer-observed"
    handoff.collect_browser(tp, inv)
    assert Journal.load(tp).data["invocations"][inv]["state"] == "merged"
    assert handoff.collect_browser(tp, inv)["already_collected"]


def test_reply_with_wrong_id_is_not_saved(tmp_path, monkeypatch):
    tp, inv = topic(tmp_path)
    browser(tp, inv)
    monkeypatch.setattr(browser_seat, "inspect", lambda *a: {"editor_count": 1, "draft_empty": True, "account": "test-account"})
    monkeypatch.setattr(browser_seat, "send", lambda *a: {"state": "submit-clicked"})
    handoff.deliver(tp, inv)
    monkeypatch.setattr(browser_seat, "reply", lambda *a: {"state": "answer-visible", "text": '{"invocation_id":"other"}'})
    with pytest.raises(ValueError):
        handoff.collect_browser(tp, inv)
    assert not (tp.scratch / f"{inv}.json").exists()


def test_claude_link_is_only_composer_not_execution(tmp_path, monkeypatch):
    tp, inv = topic(tmp_path, "claude")
    handoff.prepare(tp, inv, transport="claude-link")
    monkeypatch.setattr(handoff.sys, "platform", "darwin")
    original = subprocess.run
    calls = []
    def run(args, **kwargs):
        if args[0] == "open":
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, b"")
        return original(args, **kwargs)
    monkeypatch.setattr(handoff.subprocess, "run", run)
    assert handoff.deliver(tp, inv)["state"] == "composer-ready"
    assert calls[0][1].startswith("claude://code/new?q=")
    assert handoff.deliver(tp, inv)["reason"] == "resend-forbidden"
    assert Journal.load(tp).data["invocations"][inv]["state"] != "merged"


def test_changed_browser_account_blocks_before_send(tmp_path, monkeypatch):
    tp, inv = topic(tmp_path)
    browser(tp, inv)
    monkeypatch.setattr(browser_seat, "inspect", lambda *a: {"editor_count": 1, "draft_empty": True, "account": "other-account"})
    monkeypatch.setattr(browser_seat, "send", lambda *a: pytest.fail("wrong account must not receive packet"))
    assert handoff.deliver(tp, inv)["reason"] == "browser-account-unverified-or-changed"
    assert handoff.status(tp, inv)["state"] == "prepared"


def test_unknown_delivery_can_collect_late_reply_without_resending(tmp_path, monkeypatch):
    tp, inv = topic(tmp_path)
    browser(tp, inv)
    monkeypatch.setattr(browser_seat, "inspect", lambda *a: {"editor_count": 1, "draft_empty": True, "account": "test-account"})
    def lost(*a):
        raise RuntimeError("response lost after possible click")
    monkeypatch.setattr(browser_seat, "send", lost)
    assert handoff.deliver(tp, inv)["state"] == "delivery-unknown"
    answer = {"invocation_id": inv, "participant": "grok", "opinion": "Late reply",
              "claims": [{"claim": "No resend needed", "evidence_type": "argument", "evidence": "ID matches"}]}
    monkeypatch.setattr(browser_seat, "reply", lambda *a: {"state": "answer-visible", "url": "https://grok.com/c/test", "text": json.dumps(answer)})
    assert handoff.collect_browser(tp, inv)["state"] == "answer-observed"
    handoff.collect_browser(tp, inv)
    assert Journal.load(tp).data["invocations"][inv]["state"] == "merged"
