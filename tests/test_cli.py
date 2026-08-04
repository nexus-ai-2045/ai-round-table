"""cli の統合テスト。subprocess ではなく main(argv) を直接呼ぶ。"""
from roundtable.cli import main


def test_new_topic_and_status(tmp_path, capsys):
    main(["new-topic", "t1", "--topic", "X", "--participants", "codex,cc",
          "--root", str(tmp_path)])
    main(["status", "t1", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "round: 1" in out


def test_dispatch_no_clipboard_and_close_shows_failures(tmp_path, capsys, monkeypatch):
    main(["new-topic", "t1", "--topic", "X", "--participants", "codex",
          "--root", str(tmp_path)])
    # collect を即 timeout させる (席がいないので出力は現れない)
    main(["dispatch", "t1", "--participant", "codex", "--no-clipboard",
          "--timeout", "0.1", "--root", str(tmp_path)])
    main(["close", "t1", "--verdict", "見送り", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "timeout" in out  # 失敗一覧が隠れず表示される
