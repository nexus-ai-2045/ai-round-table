"""doctor は実 codex を必須にしない。ソケット不在と推奨 tier の性質を固定する。"""
from roundtable.cli import main
from roundtable.doctor import DoctorReport, default_control_socket, format_report, run_doctor


def test_default_control_socket_under_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sock = default_control_socket()
    assert sock == tmp_path / "app-server-control" / "app-server-control.sock"
    assert sock.exists() is False


def test_format_report_includes_recommendation():
    r = DoctorReport(
        codex_binary="codex",
        desktop_control_socket="/tmp/x.sock",
        desktop_socket_exists=False,
        proxy_connect="missing",
        spawn_initialize="ok",
        thread_list="ok:3",
        thread_start="timeout",
        recommended_tier=3,
        real_seat_path="tier3-clipboard-paste",
        notes=["no desktop socket"],
    )
    text = format_report(r)
    assert "recommended_tier: 3" in text
    assert "tier3-clipboard-paste" in text
    assert "no desktop socket" in text


def test_run_doctor_without_codex_binary(monkeypatch):
    monkeypatch.setattr("roundtable.doctor._which_codex", lambda binary="codex": None)
    report = run_doctor()
    assert report.codex_binary is None
    assert report.recommended_tier == 3
    assert report.real_seat_path == "tier3-clipboard-paste"
    assert report.spawn_initialize == "skipped"


def test_run_doctor_recommends_tier1_when_start_ok(monkeypatch, tmp_path):
    """start 成功時だけ tier1 (stdio セッションをモック)。"""
    monkeypatch.setattr("roundtable.doctor._which_codex", lambda binary="codex": "codex")
    monkeypatch.setattr("roundtable.doctor._probe_proxy", lambda *a, **k: "missing")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    class FakeProc:
        def __init__(self):
            self.stdin = self
            self.stdout = iter([])
            self._alive = True

        def write(self, _s):
            return None

        def flush(self):
            return None

        def poll(self):
            return None if self._alive else 0

        def kill(self):
            self._alive = False

    responses = {
        1: {"id": 1, "result": {"userAgent": "x"}},
        2: {"id": 2, "result": {"data": [{"id": "t1"}]}},
        3: {"id": 3, "result": {"thread": {"id": "new"}}},
    }

    def fake_session(binary, cwd=None):
        return FakeProc(), None

    def fake_rpc(proc, q, rid, method, params, timeout):
        return responses.get(rid)

    def fake_notify(proc, method, params):
        return None

    monkeypatch.setattr("roundtable.doctor._stdio_session", fake_session)
    monkeypatch.setattr("roundtable.doctor._rpc", fake_rpc)
    monkeypatch.setattr("roundtable.doctor._notify", fake_notify)

    report = run_doctor(probe_start=True)
    assert report.spawn_initialize == "ok"
    assert report.thread_list == "ok:1"
    assert report.thread_start == "ok"
    assert report.recommended_tier == 1
    assert report.real_seat_path == "tier1-app-server"


def test_cli_doctor_skip_start_without_codex(monkeypatch, capsys):
    monkeypatch.setattr("roundtable.doctor._which_codex", lambda binary="codex": None)
    rc = main(["doctor", "--skip-start"])
    assert rc == 0  # 診断は常に 0、推奨は本文
    out = capsys.readouterr().out
    assert "recommended_tier: 3" in out
    assert "not found" in out
