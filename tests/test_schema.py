from roundtable.schema import validate_opinion

VALID = {
    "invocation_id": "abc",
    "participant": "codex",
    "opinion": "本文",
    "claims": [{"claim": "X", "evidence_type": "argument", "evidence": "理由"}],
}


def test_valid_passes():
    assert validate_opinion(VALID) == []


def test_missing_claims_fails():
    assert any("claims" in e for e in validate_opinion({**VALID, "claims": []}))


def test_bad_evidence_type_fails():
    bad = {**VALID, "claims": [{"claim": "X", "evidence_type": "vibes", "evidence": ""}]}
    assert any("evidence_type" in e for e in validate_opinion(bad))


def test_non_dict_fails():
    assert validate_opinion([]) != []


def test_empty_strings_fail():
    assert any("invocation_id" in e for e in validate_opinion({**VALID, "invocation_id": ""}))
    assert any("opinion" in e for e in validate_opinion({**VALID, "opinion": ""}))
