"""E2E mock テスト — 模擬席 (別スレッド) を相手に 2 round 回して close まで通す。

模擬席は journal.json を poll して未 merge の invocation を特定し (sleep 決め打ち禁止)、
契約 JSON を scratch に置く。dispatcher 側は cli.main を直接呼ぶ。
"""
import json
import threading
import time

from roundtable.cli import main
from roundtable.paths import ensure_topic, topic_dir


def _find_pending_invocation(tp, deadline_s: float = 5.0) -> str:
    """journal.json を poll し、未 merge の invocation id を返す。

    deadline 内に見つからなければ AssertionError。journal は atomic_write
    (os.replace) 中に一時的に読めないことがあるため、transient なエラーは
    握って poll を継続する。
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            if tp.journal.exists():
                data = json.loads(tp.journal.read_text(encoding="utf-8"))
                pending = [
                    k for k, v in data["invocations"].items()
                    if v["state"] != "merged"
                ]
                if pending:
                    return pending[-1]
        except (json.JSONDecodeError, OSError):
            pass  # 書き込み途中 / 一時ロック — 次の poll で再試行
        time.sleep(0.05)
    raise AssertionError("invocation が発行されなかった (deadline 5s)")


def _seat_thread(tp, rnd: int, errors: list[BaseException]) -> None:
    """模擬席の本体: invocation を特定して契約 JSON を scratch に置く。

    スレッド内の assert 失敗は pytest に伝わらないため errors に収集し、
    join 後に main スレッド側で検査する。
    """
    try:
        inv = _find_pending_invocation(tp)
        opinion = {
            "invocation_id": inv,
            "participant": "codex",
            "opinion": f"round{rnd} の意見",
            "claims": [
                {"claim": "C", "evidence_type": "argument", "evidence": "E"}
            ],
        }
        (tp.scratch / f"{inv}.json").write_text(
            json.dumps(opinion, ensure_ascii=False), encoding="utf-8"
        )
    except BaseException as e:  # noqa: BLE001 — assert 含め全部 main 側へ運ぶ
        errors.append(e)


def test_full_topic_two_rounds(tmp_path, capsys):
    """new-topic → (dispatch + 模擬席) x 2 round → close の全経路。"""
    main([
        "new-topic", "t1", "--topic", "X", "--participants", "codex",
        "--root", str(tmp_path),
    ])
    for rnd in (1, 2):
        tp = ensure_topic(tmp_path, "t1")
        errors: list[BaseException] = []
        t = threading.Thread(target=_seat_thread, args=(tp, rnd, errors))
        t.start()
        rc = main([
            "dispatch", "t1", "--participant", "codex", "--no-clipboard",
            "--timeout", "5", "--root", str(tmp_path),
        ])
        t.join()
        assert not errors, f"模擬席スレッドで失敗: {errors[0]}"
        assert rc == 0, f"round {rnd} の dispatch が失敗した"

    text = (topic_dir(tmp_path, "t1") / "minutes.md").read_text(encoding="utf-8")
    assert "round1 の意見" in text and "round2 の意見" in text

    main(["close", "t1", "--verdict", "採用", "--root", str(tmp_path)])
    closed = (topic_dir(tmp_path, "t1") / "minutes.md").read_text(encoding="utf-8")
    assert "status: closed" in closed
    assert "verdict: 採用" in closed
