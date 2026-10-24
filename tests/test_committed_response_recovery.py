"""採用commit直後の停止は、差替えscratchではなく採用証跡から復旧する。"""
import json

import pytest

from roundtable import followup, minutes, watcher
from roundtable.journal import Journal
from roundtable.paths import ensure_topic


@pytest.mark.parametrize("replacement", ["changed", "malformed", "missing"])
def test_committed_response_recovers_without_reusing_scratch(tmp_path, monkeypatch, replacement):
    tp = ensure_topic(tmp_path, "committed-recovery")
    minutes.create(tp, "採用済み回答の復旧", ["codex"])
    journal = Journal.load(tp)
    inv = journal.new_invocation("codex", 1)
    data = dict(invocation_id=inv, participant="codex", opinion="採用済み回答",
                claims=[dict(claim="確認", evidence_type="argument", evidence="根拠")])
    scratch = tp.scratch / f"{inv}.json"
    scratch.write_text(json.dumps(data), encoding="utf-8")
    original = Journal.set_state

    def crash(self, inv_id, state, detail=""):
        if state == "merged":
            raise RuntimeError("停止: 採用commit後")
        return original(self, inv_id, state, detail)

    with monkeypatch.context() as patch:
        patch.setattr(Journal, "set_state", crash)
        with pytest.raises(RuntimeError, match="採用commit後"):
            watcher.collect(tp, journal, inv, "codex", timeout_s=0)
    assert Journal.load(tp).data["invocations"][inv]["state"] == "validated"
    committed = tp.minutes.read_bytes()
    if replacement == "missing":
        scratch.unlink()
    elif replacement == "malformed":
        scratch.write_text("{invalid", encoding="utf-8")
    else:
        data["opinion"] = "差替え回答"
        scratch.write_text(json.dumps(data), encoding="utf-8")

    result = watcher.collect(tp, Journal.load(tp), inv, "codex", timeout_s=0)
    assert result == {"ok": True, "reason": "recovered-committed-response"}
    assert tp.minutes.read_bytes() == committed
    record = Journal.load(tp).data["invocations"][inv]
    assert record["state"] == "merged"
    assert record["detail"] == "recovered-committed-response"
    assert followup.prepare(tp, inv, "11111111-1111-4111-8111-111111111111")["ok"]
    assert watcher.collect(tp, Journal.load(tp), inv, "codex", timeout_s=0)["ok"]
    assert tp.minutes.read_bytes() == committed
