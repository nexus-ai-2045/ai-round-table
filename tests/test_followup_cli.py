"""CLIから回収・元担当固定・送信記録までを通し、再送を拒否する。"""
import json

from roundtable import minutes
from roundtable.cli import main
from roundtable.journal import Journal
from roundtable.paths import ensure_topic


def test_followup_cli_round_trip(tmp_path, capsys):
    tp = ensure_topic(tmp_path, "reply")
    minutes.create(tp, "CLI返却検証", ["codex"])
    inv = Journal.load(tp).new_invocation("codex", 1)
    reply = dict(invocation_id=inv, participant="codex", opinion="確認結果",
                 claims=[dict(claim="確認", evidence_type="argument", evidence="理由")])
    (tp.scratch / f"{inv}.json").write_text(json.dumps(reply))
    assert main(["collect-pending", "reply", "--root", str(tmp_path)]) == 0
    capsys.readouterr()
    base = ["followup", "reply", "--root", str(tmp_path), "--invocation", inv]
    target = "11111111-1111-4111-8111-111111111111"
    assert main(base + ["--action", "prepare", "--target-thread", target]) == 0
    capsys.readouterr()
    assert main(base + ["--action", "claim"]) == 0
    claimed = json.loads(capsys.readouterr().out)
    receipt = tmp_path / "native-receipt.json"
    receipt.write_text(json.dumps(dict(accepted=True, action="send_message_to_thread",
        thread_id=target, message_sha256=claimed["message_sha256"], source_call_id="fixture-call")))
    assert main(base + ["--action", "record", "--target-thread", target,
        "--message-sha256", claimed["message_sha256"], "--receipt", str(receipt)]) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["state"] == "submitted"
    assert recorded["resume_executed"] is False
    assert main(base + ["--action", "claim"]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "resend-forbidden"


def test_unknown_followup_does_not_create_topic(tmp_path, capsys):
    assert main(["followup", "missing", "--root", str(tmp_path),
                 "--invocation", "missing", "--action", "claim"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "unknown-topic"
    assert not (tmp_path / "minutes").exists()
