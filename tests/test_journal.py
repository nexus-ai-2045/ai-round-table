import pytest

from roundtable.paths import ensure_topic
from roundtable.journal import Journal


def test_invocation_lifecycle_and_idempotent_merge(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    j.set_state(inv, "merged")
    j2 = Journal.load(tp)
    assert j2.is_merged(inv)  # 別プロセス相当の再ロードでも merge 済み判定できる


def test_round_advances_only_when_all_merged(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    a = j.new_invocation("codex", 1)
    b = j.new_invocation("cc", 1)
    j.set_state(a, "merged")
    j.advance_round_if_complete(["codex", "cc"])
    assert j.round_no == 1  # cc 未完了なので据え置き
    j.set_state(b, "merged")
    j.advance_round_if_complete(["codex", "cc"])
    assert j.round_no == 2


def test_failures_listed(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    j.set_state(inv, "failed", "timeout")
    assert j.failures()[0]["detail"] == "timeout"


def test_invalid_state_rejected(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    with pytest.raises(ValueError):
        j.set_state(inv, "definitely-not-a-state")
