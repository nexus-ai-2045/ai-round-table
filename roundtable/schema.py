"""意見 JSON の検証。stdlib のみ、違反理由をすべて返す (最初の 1 個で止めない)。

契約 (DESIGN v6 §4.5):
    {"invocation_id": str, "participant": str, "opinion": str,
     "claims": [{"claim": str, "evidence_type": <型>, "evidence": str}, ...]}
claims は 1 件以上必須 —「同意します」だけの応答を構造的に無効化する。
evidence_type は自己申告のラベルであり「検証済み」を意味しない (Codex1st#8)。
"""

EVIDENCE_TYPES = {"observed", "log", "diff", "source", "argument", "none"}


def validate_opinion(data) -> list[str]:
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["top-level must be object"]
    for key in ("invocation_id", "participant", "opinion"):
        if not isinstance(data.get(key), str) or not data.get(key):
            errs.append(f"{key}: 非空文字列が必要")
    claims = data.get("claims")
    if not isinstance(claims, list) or len(claims) == 0:
        errs.append("claims: 1 件以上必要 (『同意します』だけは無効)")
        return errs
    for i, c in enumerate(claims):
        if not isinstance(c, dict):
            errs.append(f"claims[{i}]: object が必要")
            continue
        if not isinstance(c.get("claim"), str) or not c.get("claim"):
            errs.append(f"claims[{i}].claim: 非空文字列が必要")
        if c.get("evidence_type") not in EVIDENCE_TYPES:
            errs.append(f"claims[{i}].evidence_type: {sorted(EVIDENCE_TYPES)} のいずれか")
        if not isinstance(c.get("evidence"), str):
            errs.append(f"claims[{i}].evidence: 文字列が必要")
    return errs
