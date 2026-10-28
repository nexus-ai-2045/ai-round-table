"""Inline adapters preserve the captured assignment without legacy file-write steps."""
import uuid

import pytest

from roundtable import browser_seat, claude_socket, handoff, ledger, minutes, packet
from roundtable.journal import Journal
from roundtable.paths import ensure_topic


ORIGINAL = "批判的な観点を担当してください。\n\n  1. 誤った前提を探す。\n2. 代案を示す。\n"
REPLACEMENT = "変更後の役割だけを使ってください。"


def prepared_topic(tmp_path, participant, role_hint=ORIGINAL):
    tp = ensure_topic(tmp_path, "frozen-assignment")
    minutes.create(tp, "捕捉時点の議題を維持する", [participant])
    minutes.make_snapshot(tp)
    journal = Journal.load(tp)
    inv = journal.new_invocation(participant, 1)
    journal.data["invocations"][inv]["role_hint"] = role_hint
    journal.save()
    text = handoff.capture_request(tp, inv, packet.build(tp, participant, inv, role_hint))
    if participant == "claude":
        handoff.prepare(tp, inv, transport="claude-socket", claude_session=str(uuid.uuid4()))
    else:
        handoff.prepare(tp, inv, transport="grok-browser", browser_window=12,
                        browser_tab=34, browser_url="https://grok.com/",
                        browser_account="test-account")
    journal = Journal.load(tp)
    journal.data["invocations"][inv]["role_hint"] = REPLACEMENT
    journal.save()
    assert Journal.load(tp).data["invocations"][inv]["role_hint"] == REPLACEMENT
    return tp, inv, text


@pytest.mark.parametrize("role_hint", ["", "単行の役割", ORIGINAL])
def test_assignment_is_exact_prefix_of_frozen_packet(tmp_path, role_hint):
    tp, inv, text = prepared_topic(tmp_path, "grok", role_hint)
    snapshot = handoff._paths(tp, inv)[1]
    assignment = packet.captured_assignment(tp, "grok", inv, text, snapshot)
    assert assignment == (f"[roundtable packet / invocation: {inv}]\n"
                          f"あなたは roundtable の参加者 (grok) です。{role_hint}\n")
    tail = packet.build(tp, "grok", inv, snapshot_path=snapshot).split("\n", 2)[2]
    assert assignment + tail == text


@pytest.mark.parametrize("participant", ["claude", "grok"])
def test_delivery_keeps_frozen_hint_and_inlines_snapshot(tmp_path, monkeypatch, participant):
    tp, inv, captured = prepared_topic(tmp_path, participant)
    snapshot = handoff._paths(tp, inv)[1]
    frozen_snapshot = snapshot.read_text()
    prompts = []
    if participant == "claude":
        monkeypatch.setattr(claude_socket, "resolve_target", lambda sid: {"session_id": sid})
        def send(target, prompt, message_id):
            assert target["request_marker"] == message_id
            assert message_id in prompt
            prompts.append(prompt)
            return {"state": "frame-written"}
        monkeypatch.setattr(claude_socket, "send", send)
    else:
        monkeypatch.setattr(browser_seat, "inspect", lambda *a: {
            "editor_count": 1, "draft_empty": True, "account": "test-account"})
        def send(window, tab, url, prompt, account):
            prompts.append(prompt)
            return {"state": "submit-clicked"}
        monkeypatch.setattr(browser_seat, "send", send)
    assert handoff.deliver(tp, inv)["state"] == "submitted"
    assert len(prompts) == 1
    prompt = prompts[0]
    assignment = packet.captured_assignment(tp, participant, inv, captured, snapshot)
    assert assignment in prompt
    assert ORIGINAL in prompt and REPLACEMENT not in prompt
    assert prompt.endswith(frozen_snapshot)
    assert f'"invocation_id": "{inv}"' in prompt
    assert f'"participant": "{participant}"' in prompt
    assert '"claims": [{' in prompt and "JSONオブジェクト1個" in prompt
    assert str(snapshot.resolve()) not in prompt
    assert f"{inv}.json.tmp" not in prompt and "リネーム" not in prompt
    assert captured not in prompt


@pytest.mark.parametrize("participant", ["claude", "grok"])
def test_tampered_tail_fails_before_any_target_or_ui_access(tmp_path, monkeypatch, participant):
    tp, inv, captured = prepared_topic(tmp_path, participant)
    request, snapshot, receipt = handoff._paths(tp, inv)
    # Keep hashes consistent to exercise semantic validation after hash verification.
    tampered = captured.replace("3. 議事録本体", "3. 改変された指示")
    assert tampered != captured
    ledger.write_state(request, tampered, "test: altered instruction tail")
    record = handoff._read(receipt)
    record["packet_sha256"] = handoff._hash(tampered.encode("utf-8"))
    handoff._save(receipt, record)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid captured instructions must fail before target/UI access")
    monkeypatch.setattr(claude_socket, "resolve_target", forbidden)
    monkeypatch.setattr(claude_socket, "send", forbidden)
    monkeypatch.setattr(browser_seat, "inspect", forbidden)
    monkeypatch.setattr(browser_seat, "send", forbidden)
    with pytest.raises(ValueError, match="instruction block"):
        handoff.deliver(tp, inv)
    assert handoff.status(tp, inv)["state"] == "prepared"


def test_prompt_helpers_keep_existing_positional_callers():
    inv = uuid.uuid4().hex
    socket_prompt = handoff._socket_prompt(inv, "claude", "snapshot", "marker")
    browser_prompt = browser_seat.build_prompt(inv, "grok", "snapshot")
    for participant, prompt in (("claude", socket_prompt), ("grok", browser_prompt)):
        assert f"参加者 {participant}" in prompt
        assert prompt.endswith("snapshot")
        assert f'"invocation_id": "{inv}"' in prompt
