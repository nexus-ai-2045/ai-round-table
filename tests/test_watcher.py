import json

from roundtable import minutes, watcher
from roundtable.journal import Journal
from roundtable.paths import ensure_topic


def _setup(tmp_path):
    tp = ensure_topic(tmp_path, "t1")
    minutes.create(tp, "X", ["codex"])
    minutes.make_snapshot(tp)
    j = Journal.load(tp)
    inv = j.new_invocation("codex", 1)
    return tp, j, inv


def _opinion(inv, participant="codex"):
    return {
        "invocation_id": inv,
        "participant": participant,
        "opinion": "意見",
        "claims": [{"claim": "A", "evidence_type": "argument", "evidence": "B"}],
    }


def test_collect_success(tmp_path):
    tp, j, inv = _setup(tmp_path)
    (tp.scratch / f"{inv}.json").write_text(json.dumps(_opinion(inv)), encoding="utf-8")
    assert watcher.collect(tp, j, inv, "codex", timeout_s=1)["ok"]
    assert j.is_merged(inv)
    assert "### codex" in tp.minutes.read_text(encoding="utf-8")


def test_collect_timeout(tmp_path):
    tp, j, inv = _setup(tmp_path)
    r = watcher.collect(tp, j, inv, "codex", timeout_s=0.1, poll_s=0.05)
    assert r == {"ok": False, "reason": "timeout"}
    assert j.failures()


def test_collect_id_mismatch(tmp_path):
    tp, j, inv = _setup(tmp_path)
    (tp.scratch / f"{inv}.json").write_text(json.dumps(_opinion("other")), encoding="utf-8")
    assert watcher.collect(tp, j, inv, "codex", timeout_s=1)["reason"] == "id-mismatch"


def test_collect_detects_tamper(tmp_path):
    tp, j, inv = _setup(tmp_path)
    tp.minutes.write_text(tp.minutes.read_text(encoding="utf-8") + "改ざん", encoding="utf-8")
    # snapshot 採取後に原本が変わった → merge は fail-closed
    (tp.scratch / f"{inv}.json").write_text(json.dumps(_opinion(inv)), encoding="utf-8")
    assert watcher.collect(tp, j, inv, "codex", timeout_s=1)["reason"] == "tampered"


def test_collect_parse_failure_on_non_dict(tmp_path):
    tp, j, inv = _setup(tmp_path)
    # JSON としては valid だが top-level が配列 → dict でないため parse 失敗として弾く
    (tp.scratch / f"{inv}.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    r = watcher.collect(tp, j, inv, "codex", timeout_s=1)
    assert r == {"ok": False, "reason": "parse"}
    assert j.failures()[0]["detail"] == "parse"
