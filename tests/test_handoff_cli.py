"""Macの依頼準備から回答回収まで、外部AIを起動せずCLIで確認する。"""
import json

import pytest

from roundtable import handoff, packet
from roundtable.cli import main
from roundtable.paths import ensure_topic


def setup(root):
    assert main(['new-topic', 'mac', '--topic', '接続検証', '--participants', 'cc',
                 '--root', str(root)]) == 0
    assert main(['dispatch', 'mac', '--participant', 'cc', '--role-hint', '製品は変更しない',
                 '--no-clipboard', '--async', '--root', str(root)]) == 0
    tp = ensure_topic(root, 'mac')
    inv = json.loads(tp.last_result.read_text())['invocation']
    return tp, inv


def test_desktop_prepare_apply_collect(tmp_path, monkeypatch):
    monkeypatch.setattr(packet.platform, 'system', lambda: 'Darwin')
    original = handoff.shutil.which
    monkeypatch.setattr(handoff.shutil, 'which',
                        lambda name: '/mock/pbcopy' if name == 'pbcopy' else original(name))
    sent = []
    monkeypatch.setattr(packet, 'to_clipboard', lambda text, **kwargs: sent.append(text))
    tp, inv = setup(tmp_path)
    args = ['handoff', 'mac', '--transport', 'claude-desktop', '--invocation', inv,
            '--root', str(tmp_path)]
    assert main(args) == 0
    assert sent == []
    assert main(args + ['--apply']) == 0
    assert len(sent) == 1 and '製品は変更しない' in sent[0]
    assert main(['handoff-status', 'mac', '--invocation', inv, '--root', str(tmp_path)]) == 0
    assert main(args + ['--apply']) == 1
    assert len(sent) == 1
    reply = {'invocation_id': inv, 'participant': 'cc', 'opinion': '模擬確認',
             'claims': [{'claim': '契約を確認', 'evidence_type': 'argument', 'evidence': '模擬'}]}
    (tp.scratch / f'{inv}.json').write_text(json.dumps(reply), encoding='utf-8')
    assert main(['collect-pending', 'mac', '--root', str(tmp_path)]) == 0
    assert json.loads(tp.last_result.read_text())['merged'] == [inv]


def test_handoff_invalid_target_has_no_clipboard_effect(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(packet, 'to_clipboard', lambda text, **kwargs: sent.append(text))
    tp, inv = setup(tmp_path)
    before = tp.last_result.read_bytes()
    with pytest.raises(SystemExit) as exc:
        main(['handoff', 'mac', '--transport', 'cmux', '--invocation', inv,
              '--apply', '--root', str(tmp_path)])
    assert exc.value.code == 2
    assert not sent
    assert tp.last_result.read_bytes() == before
    assert not (tp.root / 'deliveries').exists()


def test_missing_topic_does_not_create_delivery(tmp_path):
    assert main(['handoff', 'missing', '--transport', 'claude-desktop',
                 '--invocation', 'unknown', '--root', str(tmp_path)]) == 2
    assert not (tmp_path/'minutes').exists()
