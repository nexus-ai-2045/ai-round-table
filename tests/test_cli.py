"""cli の統合テスト。subprocess ではなく main(argv) を直接呼ぶ。"""
import pytest

from roundtable.cli import main
from roundtable.journal import Journal
from roundtable.paths import ensure_topic


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
    main(["new-topic", "t1", "--topic", "X", "--participants", "codex",
          "--root", str(tmp_path)])
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    j.new_invocation("codex", 1)  # prepared のまま放置 (Ctrl+C 相当)
    capsys.readouterr()
    main(["close", "t1", "--verdict", "見送り", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "未解決一覧" in out and "prepared" in out


# --- v0.2: KPI 計測 / detector の CLI 露出 ---


def test_status_shows_human_action_count(tmp_path, capsys):
    """new-topic で 1 回、--no-clipboard の dispatch で 1 回 (貼り付けは発生しない)。"""
    main(["new-topic", "t1", "--topic", "X", "--participants", "codex",
          "--root", str(tmp_path)])
    main(["dispatch", "t1", "--participant", "codex", "--no-clipboard",
          "--timeout", "0.1", "--root", str(tmp_path)])
    capsys.readouterr()
    main(["status", "t1", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "人間の操作: 2 回" in out
    assert "topic: 1" in out and "nominate: 1" in out
    assert "paste" not in out  # Tier1 相当 (貼り付けなし) では paste を計上しない


def test_status_shows_failure_breakdown(tmp_path, capsys):
    main(["new-topic", "t1", "--topic", "X", "--participants", "codex",
          "--root", str(tmp_path)])
    for _ in range(2):
        main(["dispatch", "t1", "--participant", "codex", "--no-clipboard",
              "--timeout", "0.1", "--root", str(tmp_path)])
    capsys.readouterr()
    main(["status", "t1", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "失敗分類: timeout: 2" in out


def test_status_on_fresh_topic_shows_zero_failures(tmp_path, capsys):
    main(["new-topic", "t1", "--topic", "X", "--participants", "codex",
          "--root", str(tmp_path)])
    capsys.readouterr()
    main(["status", "t1", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "人間の操作: 1 回" in out
    assert "失敗分類" not in out  # 失敗ゼロなら行自体を出さない


def test_close_records_verdict_action_and_totals(tmp_path, capsys):
    main(["new-topic", "t1", "--topic", "X", "--participants", "codex",
          "--root", str(tmp_path)])
    capsys.readouterr()
    main(["close", "t1", "--verdict", "採用", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "人間の操作 (この議題 合計): 2 回" in out  # topic + verdict
    tp = ensure_topic(tmp_path, "t1")
    assert Journal.load(tp).human_action_counts == {"topic": 1, "verdict": 1}


def test_dispatch_with_clipboard_counts_paste(tmp_path, capsys, monkeypatch):
    """Tier3 の貼り付けを 1 回として計上する (clip.exe は呼ばずに差し替える)。"""
    from roundtable import packet

    monkeypatch.setattr(packet, "to_clipboard", lambda text: None)
    main(["new-topic", "t1", "--topic", "X", "--participants", "codex",
          "--root", str(tmp_path)])
    main(["dispatch", "t1", "--participant", "codex",
          "--timeout", "0.1", "--root", str(tmp_path)])
    capsys.readouterr()
    tp = ensure_topic(tmp_path, "t1")
    assert Journal.load(tp).human_action_counts == {
        "topic": 1, "nominate": 1, "paste": 1
    }


def test_invalid_slug_rejected_by_cli(tmp_path):
    """CLI 引数の slug も検証経路を通る (軸 D は入口で効く)。"""
    with pytest.raises(ValueError):
        main(["new-topic", "../evil", "--topic", "X", "--participants", "codex",
              "--root", str(tmp_path)])
    assert not (tmp_path / "minutes").exists()
