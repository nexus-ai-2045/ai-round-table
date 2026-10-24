import platform
import subprocess
from unittest.mock import Mock

import pytest

from roundtable import packet


@pytest.fixture
def run(monkeypatch):
    mocked = Mock()
    monkeypatch.setattr(packet.subprocess, "run", mocked)
    return mocked


@pytest.mark.parametrize("text", ["議題 📋\n次の行", "", "引用 ' $() & |"])
@pytest.mark.parametrize(
    "system, command, encoding, prefix",
    [("Darwin", "pbcopy", "utf-8", b""),
     ("Windows", "clip.exe", "utf-16-le", b"\xff\xfe")],
)
def test_clipboard_command_and_bytes(monkeypatch, run, system, command, encoding, prefix, text):
    monkeypatch.setattr(platform, "system", lambda: system)

    packet.to_clipboard(text)

    run.assert_called_once_with(
        [command], input=prefix + text.encode(encoding), check=True, shell=False,
    )


@pytest.mark.parametrize("system", ["Linux", "FreeBSD", "Unknown"])
def test_unsupported_os_raises_without_running_command(monkeypatch, run, system):
    monkeypatch.setattr(platform, "system", lambda: system)

    with pytest.raises(NotImplementedError, match=system):
        packet.to_clipboard("議題")

    run.assert_not_called()


@pytest.mark.parametrize("system, command", [("Darwin", "pbcopy"), ("Windows", "clip.exe")])
@pytest.mark.parametrize("failure", ["missing", "denied", "nonzero"])
def test_clipboard_execution_errors_propagate(monkeypatch, run, system, command, failure):
    monkeypatch.setattr(platform, "system", lambda: system)
    error = {
        "missing": FileNotFoundError(command),
        "denied": PermissionError(command),
        "nonzero": subprocess.CalledProcessError(1, [command]),
    }[failure]
    run.side_effect = error

    with pytest.raises(type(error)) as caught:
        packet.to_clipboard("議題")

    assert caught.value is error
    assert run.call_count == 1
    assert run.call_args.kwargs["check"] is True


def test_clipboard_bounded_timeout(monkeypatch, run):
    monkeypatch.setattr(platform, 'system', lambda: 'Darwin')
    packet.to_clipboard('依頼', timeout_s=3)
    assert run.call_args.kwargs['timeout'] == 3


@pytest.mark.parametrize('timeout', [0, -1, float('nan'), float('inf')])
def test_invalid_clipboard_timeout_has_no_side_effect(run, timeout):
    with pytest.raises(ValueError):
        packet.to_clipboard('依頼', timeout_s=timeout)
    run.assert_not_called()
