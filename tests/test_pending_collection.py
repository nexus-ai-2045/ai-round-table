"""再起動時の発見、有限待機、取消と再開能力の境界をCLIで確認する。"""
import json

import pytest

from roundtable import minutes
from roundtable.cli import main
from roundtable.journal import Journal
from roundtable.paths import ensure_topic


def setup_topic(root):
    tp = ensure_topic(root, 't1')
    minutes.create(tp, '回収検証', ['codex'])
    inv = Journal.load(tp).new_invocation('codex', 1)
    return tp, inv


def write_reply(tp, inv, **overrides):
    opinion = {'invocation_id': inv, 'participant': 'codex', 'opinion': '回答',
               'claims': [{'claim': 'A', 'evidence_type': 'argument', 'evidence': 'B'}]}
    opinion.update(overrides)
    (tp.scratch / f'{inv}.json').write_text(json.dumps(opinion), encoding='utf-8')


def scan(root, *extra):
    return main(['collect-pending', 't1', '--root', str(root), *extra])


def test_restart_discovers_existing_reply_and_never_claims_resume(tmp_path):
    tp, inv = setup_topic(tmp_path)
    write_reply(tp, inv)
    assert scan(tmp_path) == 0
    receipt = json.loads(tp.last_result.read_text(encoding="utf-8"))
    assert receipt['merged'] == [inv]
    assert receipt['follow_up']['resume_executed'] is False
    original = tp.minutes.read_bytes()
    # 別呼出しはdiskから読み直す。mtimeや前回のメモリには依存しない。
    assert scan(tmp_path) == 0
    assert tp.minutes.read_bytes() == original
    assert json.loads(tp.last_result.read_text(encoding="utf-8"))['follow_up']['invocations'] == [inv]


def test_pending_is_not_success_and_late_reply_is_discovered(tmp_path):
    tp, inv = setup_topic(tmp_path)
    assert scan(tmp_path) == 2
    assert json.loads(tp.last_result.read_text(encoding="utf-8"))['pending'] == [inv]
    write_reply(tp, inv)
    assert scan(tmp_path) == 0
    assert Journal.load(tp).is_merged(inv)


def test_one_bad_reply_does_not_hide_other_ready_reply(tmp_path):
    tp, inv = setup_topic(tmp_path)
    other = Journal.load(tp).new_invocation('codex', 1)
    write_reply(tp, inv, invocation_id='wrong')
    write_reply(tp, other)
    assert scan(tmp_path) == 1
    receipt = json.loads(tp.last_result.read_text(encoding="utf-8"))
    assert receipt['failed'][0]['invocation'] == inv
    assert other in receipt['merged']


def test_cancelled_late_reply_is_not_collected(tmp_path):
    tp, inv = setup_topic(tmp_path)
    assert main(['cancel', 't1', '--invocation', inv, '--root', str(tmp_path)]) == 0
    write_reply(tp, inv)
    assert scan(tmp_path) == 0
    receipt = json.loads(tp.last_result.read_text(encoding="utf-8"))
    assert receipt['cancelled'] == [inv]
    assert receipt['merged'] == []


def test_legacy_timeout_is_reported_not_automatically_reopened(tmp_path):
    tp, inv = setup_topic(tmp_path)
    Journal.load(tp).set_state(inv, 'failed', 'timeout')
    write_reply(tp, inv)
    assert scan(tmp_path) == 1
    assert not Journal.load(tp).is_merged(inv)


@pytest.mark.parametrize('flag,value', [('--timeout', 'nan'), ('--timeout', 'inf'),
                                      ('--timeout', '-1'), ('--poll-interval', '0')])
def test_invalid_wait_is_rejected_before_creating_topic(tmp_path, flag, value):
    with pytest.raises(SystemExit) as exc:
        scan(tmp_path, flag, value)
    assert exc.value.code == 2
    assert not (tmp_path / 'minutes').exists()


def test_unhashable_evidence_type_returns_schema_error(tmp_path):
    tp, inv = setup_topic(tmp_path)
    write_reply(tp, inv, claims=[{'claim': 'A', 'evidence_type': [], 'evidence': 'B'}])
    assert scan(tmp_path) == 1
    assert json.loads(tp.last_result.read_text(encoding="utf-8"))['failed'][0]['detail'].startswith('schema')


@pytest.mark.parametrize('state', ['output-received', 'validated'])
def test_incomplete_reply_after_crash_is_rechecked_within_same_wait(tmp_path, monkeypatch, state):
    from roundtable import cli

    tp, inv = setup_topic(tmp_path)
    Journal.load(tp).set_state(inv, state)
    (tp.scratch / f'{inv}.json.tmp').write_text('{', encoding='utf-8')

    class Clock:
        now = 0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds
            write_reply(tp, inv)

    monkeypatch.setattr(cli, 'time', Clock())
    assert scan(tmp_path, '--timeout', '3', '--poll-interval', '1') == 0
    assert Journal.load(tp).is_merged(inv)


def test_mistyped_root_is_not_reported_settled_or_created(tmp_path, capsys):
    assert scan(tmp_path) == 2
    assert 'unknown-topic' in capsys.readouterr().out
    assert not (tmp_path / 'minutes').exists()


@pytest.mark.parametrize("route", ["relay", "stdout-only", None])
def test_repeated_collect_preserves_delivery_guidance(tmp_path, capsys, route):
    tp, inv = setup_topic(tmp_path)
    journal = Journal.load(tp)
    if route is not None:
        journal.data["invocations"][inv]["delivery_route"] = route
        journal.save()
    if route == "relay":
        journal.set_state(inv, "delivered", "tier1:sent")
    for _ in range(2):
        assert main(["collect", "t1", "--root", str(tmp_path),
                     "--invocation", inv, "--timeout", "0"]) == 1
        output = capsys.readouterr().out
        assert Journal.load(tp).data["invocations"][inv]["state"] == "waiting"
        if route == "relay":
            assert "搬出済み" in output
            assert "クリップボード搬出なし" not in output
            assert "再送しない" in output
        elif route == "stdout-only":
            assert "クリップボード搬出なし" in output
    if route is None:
        assert "搬出履歴を確定できません" in output
        assert "再送しない" in output


def test_unknown_delivery_never_becomes_confirmed_on_collect_retry(tmp_path, capsys):
    tp, inv = setup_topic(tmp_path)
    journal = Journal.load(tp)
    journal.data["invocations"][inv]["delivery_route"] = "relay"
    journal.save()
    journal.set_state(inv, "delivery-unknown", "transport-timeout")
    for _ in range(2):
        assert main(["collect", "t1", "--root", str(tmp_path),
                     "--invocation", inv, "--timeout", "0"]) == 1
        output = capsys.readouterr().out
        assert "搬出履歴を確定できません" in output
        assert "再送しない" in output
        assert "搬出済み" not in output
        assert not Journal.load(tp).data["invocations"][inv].get("delivery_confirmed")
