"""Real ledger with mocked side effects: target binding and no duplicate sends."""
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from roundtable import handoff, ledger, minutes, packet, watcher
from roundtable.journal import Journal
from roundtable.paths import ensure_topic

WS = '11111111-1111-4111-8111-111111111111'
SURFACE = '22222222-2222-4222-8222-222222222222'


@pytest.fixture(autouse=True)
def cmux_on_path(monkeypatch):
    original = handoff.shutil.which
    monkeypatch.setattr(handoff.shutil, 'which',
                        lambda name: '/mock/cmux' if name == 'cmux' else original(name))


def setup(tmp_path, participant='codex'):
    tp = ensure_topic(tmp_path, 'handoff')
    minutes.create(tp, 'Handoff', [participant])
    minutes.make_snapshot(tp)
    journal = Journal.load(tp)
    inv = journal.new_invocation(participant, 1)
    script = tmp_path / 'cmux_file_signal.py'
    script.write_text('# mock-only adapter', encoding='utf-8')
    return tp, inv, dict(workspace=WS, surface=SURFACE, script=str(script))


def test_frozen_snapshot_and_role_survive_next_dispatch(tmp_path):
    tp, inv, opts = setup(tmp_path)
    text = handoff.capture_request(tp, inv, packet.build(tp, 'codex', inv, 'SPECIAL ROLE'))
    fixed = (tp.root / 'requests' / f'{inv}.snapshot.md').read_bytes()
    (tp.snapshot / 'minutes.snapshot.md').write_text('later snapshot', encoding='utf-8')
    result = handoff.prepare(tp, inv, **opts)
    assert result['source'] == 'dispatch-captured'
    assert Path(result['snapshot']).read_bytes() == fixed
    assert 'SPECIAL ROLE' in Path(result['packet']).read_text(encoding="utf-8")
    assert str(Path(result['snapshot'])) in text
    assert handoff.prepare(tp, inv, **opts) == result
    with pytest.raises(ValueError, match='different destination'):
        handoff.prepare(tp, inv, **{**opts, 'surface': WS})


def test_submission_is_not_collection_and_never_repeats(tmp_path, monkeypatch):
    tp, inv, opts = setup(tmp_path)
    handoff.prepare(tp, inv, **opts)
    original = subprocess.run
    calls = []
    def run(args, **kwargs):
        if '--message-file' not in args:
            return original(args, **kwargs)
        calls.append(args)
        assert kwargs['shell'] is False and kwargs['timeout'] == 30
        receipt = json.loads((tp.root / 'deliveries' / f'{inv}.json').read_text(encoding="utf-8"))
        assert receipt['state'] == 'sending'
        return subprocess.CompletedProcess(args, 0, b'{"status":"working"}')
    monkeypatch.setattr(handoff.subprocess, 'run', run)
    result = handoff.deliver(tp, inv)
    assert result['state'] == 'submitted'
    assert Journal.load(tp).data['invocations'][inv]['state'] == 'prepared'
    assert handoff.deliver(tp, inv)['reason'] == 'resend-forbidden'
    assert len(calls) == 1
    assert '--require-process' in calls[0] and '--verify-submit-json' in calls[0]
    assert '--packet-gate' not in calls[0]  # fail remains the wrapper default


@pytest.mark.parametrize('failure', ['timeout', 'nonzero', 'oserror'])
def test_unknown_delivery_blocks_retry_after_reload(tmp_path, monkeypatch, failure):
    tp, inv, opts = setup(tmp_path)
    handoff.prepare(tp, inv, **opts)
    original = subprocess.run
    def run(args, **kwargs):
        if '--message-file' not in args:
            return original(args, **kwargs)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args, 1)
        if failure == 'oserror':
            raise OSError('transport lost')
        return subprocess.CompletedProcess(args, 3, b'')
    monkeypatch.setattr(handoff.subprocess, 'run', run)
    assert handoff.deliver(tp, inv)['state'] == 'delivery-unknown'
    assert handoff.status(tp, inv)['state'] == 'delivery-unknown'
    assert handoff.deliver(tp, inv)['reason'] == 'resend-forbidden'


def test_crash_after_sending_receipt_never_retries(tmp_path, monkeypatch):
    tp, inv, opts = setup(tmp_path)
    handoff.prepare(tp, inv, **opts)
    original = subprocess.run
    def run(args, **kwargs):
        if '--message-file' in args:
            raise KeyboardInterrupt()
        return original(args, **kwargs)
    monkeypatch.setattr(handoff.subprocess, 'run', run)
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


def test_invalid_target_and_unknown_process_fail_before_artifacts(tmp_path):
    tp, inv, opts = setup(tmp_path, 'unknown')
    with pytest.raises(ValueError, match='process mapping'):
        handoff.prepare(tp, inv, **opts)
    with pytest.raises(ValueError):
        handoff.prepare(tp, inv, **{**opts, 'surface': 'surface:1'})
    with pytest.raises(ValueError, match='absolute'):
        handoff.prepare(tp, inv, **{**opts, 'script': 'cmux_file_signal.py'})
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
    handoff.capture_request(tp, inv, packet.build(tp, 'codex', inv))
    assert handoff.prepare(tp, inv, **opts)['reason'] == 'response-present'


@pytest.mark.parametrize('failure', ['missing-cmux', 'missing-dependency', 'timeout', 'oserror'])
def test_preflight_failure_preserves_prepared_and_never_sends(tmp_path, monkeypatch, failure):
    tp, inv, opts = setup(tmp_path)
    handoff.prepare(tp, inv, **opts)
    original = subprocess.run
    calls = []
    def run(args, **kwargs):
        if str(opts['script']) not in args:
            return original(args, **kwargs)
        calls.append(args)
        assert args == [handoff.sys.executable, opts['script'], '--help']
        assert kwargs['shell'] is False
        assert json.loads((tp.root / 'deliveries' / f'{inv}.json').read_text(encoding="utf-8"))['state'] == 'prepared'
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args, 1, stderr=b'private error')
        if failure == 'oserror':
            raise OSError('private error')
        return subprocess.CompletedProcess(args, 1, b'', b'private dependency error')
    if failure == 'missing-cmux':
        monkeypatch.setattr(handoff.shutil, 'which', lambda name: None)
    monkeypatch.setattr(handoff.subprocess, 'run', run)
    result = handoff.deliver(tp, inv)
    assert not result['ok'] and result['reason'] == 'cmux-preflight-failed'
    assert result['state'] == 'prepared'
    assert 'private' not in json.dumps(result)
    assert handoff.status(tp, inv)['state'] == 'prepared'
    assert len(calls) == (0 if failure == 'missing-cmux' else 1)
