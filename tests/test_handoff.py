"""Real ledger with mocked side effects: target binding and no duplicate sends."""
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from roundtable import handoff, ledger, minutes, packet, watcher
from roundtable.journal import Journal
from roundtable.paths import ensure_topic

@pytest.fixture(autouse=True)
def clipboard_on_path(monkeypatch):
    original = handoff.shutil.which
    monkeypatch.setattr(packet.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(handoff.shutil, 'which',
                        lambda name: '/mock/' + name if name == 'pbcopy' else original(name))


def setup(tmp_path, participant='claude'):
    tp = ensure_topic(tmp_path, 'handoff')
    minutes.create(tp, 'Handoff', [participant])
    minutes.make_snapshot(tp)
    journal = Journal.load(tp)
    inv = journal.new_invocation(participant, 1)
    return tp, inv, dict(transport='claude-desktop')


def test_frozen_snapshot_and_role_survive_next_dispatch(tmp_path):
    tp, inv, opts = setup(tmp_path)
    text = handoff.capture_request(tp, inv, packet.build(tp, 'claude', inv, 'SPECIAL ROLE'))
    fixed = (tp.root / 'requests' / f'{inv}.snapshot.md').read_bytes()
    (tp.snapshot / 'minutes.snapshot.md').write_text('later snapshot', encoding='utf-8')
    result = handoff.prepare(tp, inv, **opts)
    assert result['source'] == 'dispatch-captured'
    assert Path(result['snapshot']).read_bytes() == fixed
    assert 'SPECIAL ROLE' in Path(result['packet']).read_text(encoding="utf-8")
    assert str(Path(result['snapshot'])) in text
    assert handoff.prepare(tp, inv, **opts) == result
    with pytest.raises(ValueError, match='different destination'):
        target_session = str(uuid.uuid4())
        handoff.prepare(tp, inv, transport='claude-socket', claude_session=target_session)


def test_clipboard_submission_is_not_collection_and_never_repeats(tmp_path, monkeypatch):
    tp, inv, opts = setup(tmp_path)
    handoff.prepare(tp, inv, **opts)
    calls = []
    def copy(text, **kwargs):
        receipt = json.loads((tp.root / 'deliveries' / f'{inv}.json').read_text())
        assert receipt['state'] == 'sending'
        calls.append(text)
    monkeypatch.setattr(packet, 'to_clipboard', copy)
    assert handoff.deliver(tp, inv)['state'] == 'clipboard-ready'
    assert Journal.load(tp).data['invocations'][inv]['state'] == 'prepared'
    assert handoff.deliver(tp, inv)['reason'] == 'resend-forbidden'
    assert len(calls) == 1


def test_crash_after_sending_receipt_never_retries(tmp_path, monkeypatch):
    tp, inv, opts = setup(tmp_path)
    handoff.prepare(tp, inv, **opts)
    def copy(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(packet, 'to_clipboard', copy)
    with pytest.raises(KeyboardInterrupt):
        handoff.deliver(tp, inv)
    assert handoff.status(tp, inv)['reason'] == 'interrupted-send'
    assert handoff.deliver(tp, inv)['state'] == 'delivery-unknown'


@pytest.mark.parametrize('state', ['cancelled', 'merged', 'failed', 'delivered', 'waiting'])
def test_terminal_or_already_dispatched_never_sends(tmp_path, monkeypatch, state):
    tp, inv, opts = setup(tmp_path)
    handoff.prepare(tp, inv, **opts)
    Journal.load(tp).set_state(inv, state)
    assert handoff.deliver(tp, inv)['reason'] == state


@pytest.mark.parametrize('suffix', ['', '.tmp'])
def test_existing_response_requires_collection(tmp_path, suffix):
    tp, inv, opts = setup(tmp_path)
    handoff.prepare(tp, inv, **opts)
    (tp.scratch / f'{inv}.json{suffix}').write_text('{}', encoding='utf-8')
    assert handoff.deliver(tp, inv)['reason'] == 'response-present'


def test_clipboard_is_human_paste_not_desktop_delivery(tmp_path, monkeypatch):
    tp, inv, _ = setup(tmp_path, 'claude')
    copied = Mock()
    monkeypatch.setattr(packet, 'to_clipboard', copied)
    handoff.prepare(tp, inv, transport='claude-desktop')
    copied.assert_not_called()
    result = handoff.deliver(tp, inv)
    assert result['state'] == 'clipboard-ready'
    assert result['next_action'] == 'human-paste-required'
    assert copied.call_count == 1
    assert copied.call_args.kwargs == {'timeout_s': 30}
    assert handoff.status(tp, inv)['collection_state'] == 'prepared'
    assert not handoff.deliver(tp, inv)['ok']
    assert copied.call_count == 1


def test_dirty_or_committed_tamper_blocks_delivery(tmp_path):
    tp, inv, opts = setup(tmp_path)
    result = handoff.prepare(tp, inv, **opts)
    request = Path(result['packet'])
    request.write_text('tampered', encoding='utf-8')
    with pytest.raises(ledger.LedgerDirtyError):
        handoff.deliver(tp, inv)
    ledger.commit(ledger.require_root(tp.root), [request], 'simulated committed edit')
    with pytest.raises(ValueError, match='hash mismatch'):
        handoff.deliver(tp, inv)


def test_unknown_participant_fails_before_artifacts(tmp_path):
    tp, inv, opts = setup(tmp_path, 'unknown')
    with pytest.raises(ValueError, match='requires participant claude'):
        handoff.prepare(tp, inv, **opts)
    assert not (tp.root / 'deliveries').exists()


def test_cancel_before_delivery_uses_same_invocation_lock(tmp_path):
    tp, inv, opts = setup(tmp_path)
    handoff.prepare(tp, inv, **opts)
    assert watcher.cancel(tp, inv)['ok']
    assert handoff.deliver(tp, inv)['reason'] == 'cancelled'


def test_waiting_stdout_route_can_deliver_but_relay_cannot(tmp_path, monkeypatch):
    tp, inv, _ = setup(tmp_path, 'claude')
    journal = Journal.load(tp)
    journal.data['invocations'][inv]['delivery_route'] = 'stdout-only'
    journal.save()
    journal.set_state(inv, 'waiting')
    handoff.prepare(tp, inv, transport='claude-desktop')
    monkeypatch.setattr(packet, 'to_clipboard', Mock())
    assert handoff.deliver(tp, inv)['state'] == 'clipboard-ready'
    second = journal.new_invocation('claude', 1)
    journal.data['invocations'][second]['delivery_route'] = 'relay'
    journal.save()
    handoff.capture_request(tp, second, packet.build(tp, 'claude', second))
    assert handoff.prepare(tp, second, transport='claude-desktop')['reason'] == 'relay-route'


def test_unprepared_status_and_deliver(tmp_path):
    tp, inv, _ = setup(tmp_path)
    assert handoff.status(tp, inv)['reason'] == 'handoff-not-prepared'
    assert handoff.deliver(tp, inv)['reason'] == 'handoff-not-prepared'


@pytest.mark.parametrize('participant', ['cc', 'claude-code'])
def test_desktop_claude_aliases(tmp_path, participant):
    tp, inv, _ = setup(tmp_path, participant)
    assert handoff.prepare(tp, inv, transport='claude-desktop')['state'] == 'prepared'


def test_capture_can_freeze_after_fast_response_but_cannot_send(tmp_path):
    tp, inv, opts = setup(tmp_path)
    (tp.scratch / f'{inv}.json').write_text('{}', encoding='utf-8')
    handoff.capture_request(tp, inv, packet.build(tp, 'claude', inv))
    assert handoff.prepare(tp, inv, **opts)['reason'] == 'response-present'


def test_retired_cmux_prepare_has_no_artifacts_or_external_calls(tmp_path, monkeypatch):
    tp, inv, _ = setup(tmp_path)
    run = Mock(side_effect=AssertionError('external invocation forbidden'))
    copied = Mock(side_effect=AssertionError('clipboard forbidden'))
    monkeypatch.setattr(handoff.subprocess, 'run', run)
    monkeypatch.setattr(packet, 'to_clipboard', copied)
    with pytest.raises(ValueError, match='retired'):
        handoff.prepare(tp, inv, transport='cmux')
    assert not (tp.root / 'requests').exists()
    assert not (tp.root / 'deliveries').exists()
    run.assert_not_called()
    copied.assert_not_called()


@pytest.mark.parametrize('state', ['prepared', 'sending', 'submitted'])
def test_historical_cmux_receipt_cannot_send_and_is_preserved(tmp_path, monkeypatch, state):
    tp, inv, opts = setup(tmp_path)
    handoff.prepare(tp, inv, **opts)
    receipt = tp.root / 'deliveries' / f'{inv}.json'
    record = json.loads(receipt.read_text())
    record.update(transport='cmux', state=state)
    ledger.write_state(receipt, json.dumps(record), 'historical cmux receipt')
    before = receipt.read_bytes()
    original = subprocess.run
    calls = []
    def run(args, **kwargs):
        # Existing ledger verification may invoke git; no transport may run.
        assert Path(str(args[0])).name == 'git'
        calls.append(args)
        return original(args, **kwargs)
    copied = Mock()
    monkeypatch.setattr(handoff.subprocess, 'run', run)
    monkeypatch.setattr(packet, 'to_clipboard', copied)
    result = handoff.deliver(tp, inv)
    assert not result['ok'] and result['reason'] == 'transport-retired'
    assert receipt.read_bytes() == before
    assert handoff.status(tp, inv)['transport'] == 'cmux'
    copied.assert_not_called()


def test_role_hint_snapshot_path_does_not_steal_frozen_reference(tmp_path):
    tp, inv, _ = setup(tmp_path)
    original = str((tp.snapshot / 'minutes.snapshot.md').resolve())
    role = f'参考パス: {original}\n自由記述にも同じパス: {original}'
    captured = handoff.capture_request(tp, inv, packet.build(tp, 'claude', inv, role))
    frozen = tp.root / 'requests' / f'{inv}.snapshot.md'
    assert role in captured
    assert captured == packet.build(tp, 'claude', inv, role, snapshot_path=frozen)
    assert captured.count(original) == 2


def test_capture_rejects_changed_instruction_block_before_artifact_write(tmp_path):
    tp, inv, _ = setup(tmp_path)
    text = packet.build(tp, 'claude', inv) + '\n変更された末尾'
    with pytest.raises(ValueError, match='instruction block'):
        handoff.capture_request(tp, inv, text)
    assert not (tp.root / 'requests' / f'{inv}.md').exists()
    assert not (tp.root / 'requests' / f'{inv}.snapshot.md').exists()


@pytest.mark.parametrize('system,command', [('Linux', None), ('Darwin', 'pbcopy'), ('Windows', 'clip.exe')])
def test_clipboard_preflight_failure_keeps_prepared_and_can_retry(tmp_path, monkeypatch, system, command):
    tp, inv, _ = setup(tmp_path, 'claude')
    handoff.prepare(tp, inv, transport='claude-desktop')
    monkeypatch.setattr(packet.platform, 'system', lambda: system)
    monkeypatch.setattr(handoff.shutil, 'which', lambda name: None)
    copied = Mock()
    monkeypatch.setattr(packet, 'to_clipboard', copied)
    result = handoff.deliver(tp, inv)
    assert result['reason'] == 'clipboard-preflight-failed'
    assert result['state'] == 'prepared'
    assert handoff.status(tp, inv)['state'] == 'prepared'
    copied.assert_not_called()
    monkeypatch.setattr(packet.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(handoff.shutil, 'which', lambda name: '/mock/pbcopy')
    assert handoff.deliver(tp, inv)['state'] == 'clipboard-ready'
    copied.assert_called_once()


def test_clipboard_failure_after_preflight_remains_unknown(tmp_path, monkeypatch):
    tp, inv, _ = setup(tmp_path, 'claude')
    handoff.prepare(tp, inv, transport='claude-desktop')
    monkeypatch.setattr(packet.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(handoff.shutil, 'which', lambda name: '/mock/pbcopy')
    copied = Mock(side_effect=subprocess.TimeoutExpired('pbcopy', 1))
    monkeypatch.setattr(packet, 'to_clipboard', copied)
    assert handoff.deliver(tp, inv)['state'] == 'delivery-unknown'
    assert handoff.deliver(tp, inv)['reason'] == 'resend-forbidden'
    copied.assert_called_once()
