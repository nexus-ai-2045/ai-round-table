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
    capsys.readouterr()  # dispatch 自身の [failed] 出力を破棄 (レビュー M4: close の出力だけを検査する)
    main(["close", "t1", "--verdict", "見送り", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "timeout" in out  # close 自身が未解決一覧を表示している
    assert "未解決一覧" in out


def test_close_lists_stuck_invocations_not_only_failed(tmp_path, capsys):
    """failed 以外の途中状態 (prepared 等) も close で提示される (レビュー M3)。"""
    from roundtable.journal import Journal
    from roundtable.paths import ensure_topic

    main(["new-topic", "t1", "--topic", "X", "--participants", "codex",
          "--root", str(tmp_path)])
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    j.new_invocation("codex", 1)  # prepared のまま放置 (Ctrl+C 相当)
    capsys.readouterr()
    main(["close", "t1", "--verdict", "見送り", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "未解決一覧" in out and "prepared" in out
