import pytest

from roundtable.paths import ensure_topic
from roundtable.journal import InvalidTransitionError, Journal


def _advance_to_merged(j: Journal, inv: str) -> None:
    """正規の遷移経路で merged まで進める (遷移表が入ったため直行できない)。"""
    for state in ("delivered", "output-received", "validated", "merged"):
        j.set_state(inv, state)


def test_invocation_lifecycle_and_idempotent_merge(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    _advance_to_merged(j, inv)
    j2 = Journal.load(tp)
    assert j2.is_merged(inv)  # 別プロセス相当の再ロードでも merge 済み判定できる


def test_round_advances_only_when_all_merged(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    a = j.new_invocation("codex", 1)
    b = j.new_invocation("cc", 1)
    _advance_to_merged(j, a)
    j.advance_round_if_complete(["codex", "cc"])
    assert j.round_no == 1  # cc 未完了なので据え置き
    _advance_to_merged(j, b)
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


# --- 軸 C: 状態遷移の検証 (v0.2 detector) ---


def test_merged_is_terminal_no_regression(tmp_path):
    """merged からの逆行を拒否する。close 済みを後から書き換える経路を塞ぐ。"""
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    _advance_to_merged(j, inv)
    for state in ("prepared", "delivered", "output-received", "validated", "failed", "merged"):
        with pytest.raises(InvalidTransitionError):
            j.set_state(inv, state)
    assert j.is_merged(inv)  # 拒否後も状態は merged のまま (副作用なし)


def test_failed_is_terminal(tmp_path):
    """failed も終端。失敗を「なかったこと」にして merged へ進めない。"""
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    j.set_state(inv, "failed", "timeout")
    with pytest.raises(InvalidTransitionError):
        j.set_state(inv, "merged")
    assert j.data["invocations"][inv]["state"] == "failed"


def test_skipping_validation_is_rejected(tmp_path):
    """output-received から validated を飛ばして merged にはできない。"""
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    j.set_state(inv, "output-received")
    with pytest.raises(InvalidTransitionError):
        j.set_state(inv, "merged")


def test_delivered_cannot_go_back_to_prepared(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    j.set_state(inv, "delivered", "tier3")
    with pytest.raises(InvalidTransitionError):
        j.set_state(inv, "prepared")


def test_invalid_transition_is_not_persisted(tmp_path):
    """拒否した遷移が journal.json に書かれていないことを再ロードで確認する。"""
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    _advance_to_merged(j, inv)
    with pytest.raises(InvalidTransitionError):
        j.set_state(inv, "failed", "後から失敗にする")
    assert Journal.load(tp).data["invocations"][inv]["state"] == "merged"


def test_corrupted_current_state_rejected(tmp_path):
    """journal.json が手編集された場合、KeyError でなく明示的な ValueError で落とす。"""
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    j.data["invocations"][inv]["state"] = "謎の状態"
    with pytest.raises(ValueError):
        j.set_state(inv, "failed", "timeout")


def test_every_state_reachable_path_allowed(tmp_path):
    """prepared → failed / delivered → failed など、失敗分岐は各段階から可能。"""
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    for setup in ([], ["delivered"], ["delivered", "output-received"],
                  ["delivered", "output-received", "validated"]):
        inv = j.new_invocation("codex", 1)
        for s in setup:
            j.set_state(inv, s)
        j.set_state(inv, "failed", "timeout")
        assert j.data["invocations"][inv]["state"] == "failed"


# --- 軸 A: 人間の操作回数 (KPI) ---


def test_human_actions_counted_and_broken_down(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    assert j.human_actions == 0
    j.record_human_action("topic")
    j.record_human_action("nominate")
    j.record_human_action("paste")
    j.record_human_action("paste")
    assert j.human_actions == 4
    assert j.human_action_counts == {"topic": 1, "nominate": 1, "paste": 2}


def test_human_actions_persisted_across_reload(tmp_path):
    """別プロセス相当の再ロードでも計測値が残る (自己申告でなく機械計測)。"""
    tp = ensure_topic(tmp_path, "t1")
    Journal.load(tp).record_human_action("topic")
    Journal.load(tp).record_human_action("verdict")
    assert Journal.load(tp).human_actions == 2


def test_unknown_human_action_kind_rejected(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    with pytest.raises(ValueError):
        j.record_human_action("なんとなく")
    assert j.human_actions == 0


def test_v01_journal_without_human_actions_loads(tmp_path):
    """v0.1 が書いた human_actions なしの journal.json も読める (後方互換)。"""
    import json

    tp = ensure_topic(tmp_path, "t1")
    tp.journal.write_text(
        json.dumps({"round": 1, "invocations": {}}), encoding="utf-8"
    )
    j = Journal.load(tp)
    assert j.human_actions == 0
    j.record_human_action("topic")
    assert Journal.load(tp).human_actions == 1


# --- 軸 B: 失敗分類の集計 ---


def test_failure_counts_groups_by_category(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    for detail in ("timeout", "timeout", "parse", "schema: claims が空",
                   "schema: evidence_type 不正"):
        inv = j.new_invocation("codex", 1)
        j.set_state(inv, "failed", detail)
    merged = j.new_invocation("cc", 1)
    _advance_to_merged(j, merged)
    # schema 違反は詳細で散らばらせず 1 分類に畳む
    assert j.failure_counts() == {"timeout": 2, "parse": 1, "schema": 2}


def test_failure_counts_empty_when_no_failures(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    _advance_to_merged(j, inv)
    assert j.failure_counts() == {}


def test_failure_counts_handles_empty_detail(tmp_path):
    """detail 空の failed も件数から落とさない (黙って消えるのが一番まずい)。"""
    tp = ensure_topic(tmp_path, "t1")
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    j.set_state(inv, "failed")
    assert j.failure_counts() == {"(分類なし)": 1}
