"""座長通知の不変宛先、改変拒否、一度だけのclaimを実ledgerで確認する。"""
import json
from pathlib import Path

import pytest

from roundtable import followup, ledger, minutes, watcher
from roundtable.journal import Journal
from roundtable.paths import ensure_topic

TARGET = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def setup(tmp_path, merged=True):
    tp = ensure_topic(tmp_path, "followup")
    minutes.create(tp, "通知検証", ["codex"])
    journal = Journal.load(tp)
    inv = journal.new_invocation("codex", 1)
    if merged:
        data = dict(invocation_id=inv, participant="codex", opinion="秘密の回答本文",
                    claims=[dict(claim="A", evidence_type="argument", evidence="B")])
        (tp.scratch / f"{inv}.json").write_text(json.dumps(data), encoding="utf-8")
        assert watcher.collect(tp, journal, inv, "codex", timeout_s=0)["ok"]
    return tp, inv


def test_prepare_is_idempotent_and_target_immutable(tmp_path):
    tp, inv = setup(tmp_path)
    record = followup.prepare(tp, inv, TARGET)
    assert followup.prepare(tp, inv, TARGET) == record
    text = Path(record["message_path"]).read_text(encoding="utf-8")
    assert "秘密の回答本文" not in text
    assert str(tp.minutes.resolve()) in text
    with pytest.raises(ValueError, match="different target"):
        followup.prepare(tp, inv, OTHER)


def test_claim_is_durable_and_never_repeats(tmp_path):
    tp, inv = setup(tmp_path)
    followup.prepare(tp, inv, TARGET)
    claimed = followup.claim(tp, inv)
    assert claimed["ok"] and claimed["state"] == "sending"
    assert followup.status(tp, inv)["state"] == "delivery-unknown"
    assert followup.claim(tp, inv)["reason"] == "resend-forbidden"
    receipt = dict(accepted=True, action="send_message_to_thread", thread_id=TARGET,
                   message_sha256=claimed["message_sha256"], source_call_id="actual-tool-call")
    result = followup.record_delivery(tp, inv, TARGET, claimed["message_sha256"], receipt)
    assert result["state"] == "submitted" and not result["resume_executed"]
    assert followup.claim(tp, inv)["reason"] == "resend-forbidden"


@pytest.mark.parametrize("field", ["target", "hash", "receipt"])
def test_receipt_identity_mismatch_rejected(tmp_path, field):
    tp, inv = setup(tmp_path)
    followup.prepare(tp, inv, TARGET)
    claimed = followup.claim(tp, inv)
    digest = claimed["message_sha256"]
    receipt = dict(accepted=True, action="send_message_to_thread", thread_id=TARGET,
                   message_sha256=digest, source_call_id="actual-tool-call")
    if field == "receipt":
        receipt["accepted"] = False
    with pytest.raises(ValueError):
        followup.record_delivery(tp, inv, OTHER if field == "target" else TARGET,
                                 "0" * 64 if field == "hash" else digest, receipt)
    assert followup.status(tp, inv)["state"] == "delivery-unknown"


def test_nonmerged_rejected(tmp_path):
    tp, inv = setup(tmp_path, merged=False)
    with pytest.raises(ValueError, match="merged"):
        followup.prepare(tp, inv, TARGET)
    assert not (tp.root / "followups").exists()


def test_tampered_message_blocks_claim(tmp_path):
    tp, inv = setup(tmp_path)
    record = followup.prepare(tp, inv, TARGET)
    Path(record["message_path"]).write_text("tampered", encoding="utf-8")
    with pytest.raises(ledger.LedgerDirtyError):
        followup.claim(tp, inv)


def test_missing_minutes_evidence_rejected(tmp_path):
    tp, inv = setup(tmp_path)
    with minutes._lock(tp):
        minutes._write_verified(tp, minutes.TEMPLATE.format(topic="通知", participants="codex"), "fixture")
    with pytest.raises(ValueError, match="evidence"):
        followup.prepare(tp, inv, TARGET)


@pytest.mark.parametrize("target_form,receipt_form", [
    ("upper", "braced"), ("braced", "upper"),
])
def test_delivery_accepts_equivalent_uuid_spellings(tmp_path, target_form, receipt_form):
    tp, inv = setup(tmp_path)
    canonical = "abcdefab-abcd-4abc-8abc-abcdefabcdef"
    forms = {"upper": canonical.upper(), "braced": "{" + canonical + "}"}
    followup.prepare(tp, inv, forms[target_form])
    claimed = followup.claim(tp, inv)
    receipt = dict(accepted=True, action="send_message_to_thread",
                   thread_id=forms[receipt_form], message_sha256=claimed["message_sha256"],
                   source_call_id="actual-tool-call")
    result = followup.record_delivery(tp, inv, forms[target_form],
                                     claimed["message_sha256"], receipt)
    assert result["state"] == "submitted"
    assert result["target_thread_id"] == canonical
    assert result["receipt"]["thread_id"] == canonical
    assert receipt["thread_id"] == forms[receipt_form]


@pytest.mark.parametrize("field,value", [
    ("target", "invalid"), ("receipt", "invalid"),
    ("receipt", OTHER), ("receipt", None),
])
def test_delivery_invalid_or_different_uuid_rejected(tmp_path, field, value):
    tp, inv = setup(tmp_path)
    followup.prepare(tp, inv, TARGET)
    claimed = followup.claim(tp, inv)
    receipt = dict(accepted=True, action="send_message_to_thread",
                   thread_id=value if field == "receipt" else TARGET,
                   message_sha256=claimed["message_sha256"], source_call_id="actual-tool-call")
    with pytest.raises(ValueError):
        followup.record_delivery(tp, inv, value if field == "target" else TARGET,
                                 claimed["message_sha256"], receipt)
    assert followup.status(tp, inv)["state"] == "delivery-unknown"
