"""scratch 監視 → 検証 → merge。dispatcher の回収側の心臓部。

失敗分類 (journal.set_state の detail / 戻り値 reason に使う):
    timeout      出力ファイルが timeout_s 以内に出現しなかった
    parse        JSON parse 不能、または parse 後の型が dict でない (配列/null 等)
    id-mismatch  invocation_id / participant が指名と一致しない
    schema       schema.validate_opinion が違反を検出
    tampered     merge 時の hash 照合が snapshot 採取時と不一致 (議事録が dispatcher 外で変更された)
"""
import json
import time

from . import minutes as m
from .journal import Journal
from .paths import TopicPaths
from .schema import validate_opinion


def collect(
    tp: TopicPaths,
    journal: Journal,
    inv_id: str,
    participant: str,
    timeout_s: float = 900,
    poll_s: float = 2.0,
    clock=time,
) -> dict:
    """invocation の出力を待ち、検証してから議事録へ merge する。

    merge_opinion へ渡す base_hash は snapshot 採取時 (この関数の開始時点) に
    計算したものをそのまま使う。参加者ターン中に発生した hash をその場で
    再計算して渡すと照合が常に一致し、改ざん検知が無力化する (内部レビュー #2)。
    """
    if journal.is_merged(inv_id):
        return {"ok": True}  # 冪等: 既に merge 済みなら再実行しない

    base_hash = m.sha256(tp.snapshot / "minutes.snapshot.md")
    out = tp.scratch / f"{inv_id}.json"

    deadline = clock.monotonic() + timeout_s
    while not out.exists():
        if clock.monotonic() >= deadline:
            journal.set_state(inv_id, "failed", "timeout")
            return {"ok": False, "reason": "timeout"}
        clock.sleep(poll_s)

    journal.set_state(inv_id, "output-received")

    data = None
    for attempt in range(3):  # grace retry: 部分書き込み・AV 一時ロックの吸収
        try:
            # utf-8-sig: Windows 席が BOM 付きで書いた場合も受理 (L6 detector 側の耐性)
            data = json.loads(out.read_text(encoding="utf-8-sig"))
            break
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            clock.sleep(0.2)

    if not isinstance(data, dict):  # parse 不能 or 非 dict (配列/null 等) をまとめて弾く
        journal.set_state(inv_id, "failed", "parse")
        return {"ok": False, "reason": "parse"}

    if data.get("invocation_id") != inv_id or data.get("participant") != participant:
        journal.set_state(inv_id, "failed", "id-mismatch")
        return {"ok": False, "reason": "id-mismatch"}

    errs = validate_opinion(data)
    if errs:
        journal.set_state(inv_id, "failed", "schema: " + "; ".join(errs))
        return {"ok": False, "reason": "schema"}

    journal.set_state(inv_id, "validated")
    try:
        m.merge_opinion(tp, data, journal.round_no, base_hash)  # snapshot 時 hash をそのまま渡す
    except m.MinutesTamperedError:
        journal.set_state(inv_id, "failed", "tampered")
        return {"ok": False, "reason": "tampered"}

    journal.set_state(inv_id, "merged")
    return {"ok": True}
