"""scratch 監視 → 検証 → merge。dispatcher の回収側の心臓部。

失敗分類 (journal.set_state の detail / 戻り値 reason に使う):
    timeout      出力ファイルが timeout_s 以内に出現しなかった (`.tmp` も無い = 席が無応答)
    stalled-tmp  `<inv>.json.tmp` は在るのに確定 (rename) されず、中身も採用条件を満たさない
    parse        JSON parse 不能、または parse 後の型が dict でない (配列/null 等)
    id-mismatch  invocation_id / participant が指名と一致しない
    schema       schema.validate_opinion が違反を検出
    tampered     merge 時に minutes.md が hash 証跡と不一致 (議事録が dispatcher 外で変更された)

来歴 (merged 側の detail):
    recovered-from-tmp  `.tmp` から回収して merge した。通常経路 (.json 確定) ではない

`.tmp` 救済の根拠 (2026-08-07 実測): 席が `<inv>.json.tmp` を**完成した状態で**書いた後、
rename せずに turn ごと止まる事象を観測した。watcher は `.json` しか見ていなかったため
「席は正しく応答したのに timeout (失敗) と記録される」偽陰性になっていた。
失敗を隠さない原則の裏返しで、**成功も来歴を隠さない**: 救済したら merged の detail と
CEO 出力の両方に必ず残す。書きかけを拾わないよう、grace 中に内容が変化しないことを
確認した上で JSON parse + id 照合 + schema 検証を全部通す。
"""
import json
import time

from . import minutes as m
from .journal import Journal
from .paths import TopicPaths
from .schema import validate_opinion

RECOVERED_FROM_TMP = "recovered-from-tmp"
"""merged の detail に必ず入る来歴トークン。通常経路と機械的に区別できるようにする。"""


def _read_stable(path, clock, stable_s: float) -> tuple[bytes | None, str]:
    """grace を挟んで 2 回読み、内容が変化しないことを確かめる。

    書きかけ (席がまだ書き足している最中) を「完成品」と誤認しないための門。
    戻り値は (bytes, "") か (None, 不採用理由)。
    """
    try:
        first = path.read_bytes()
    except OSError as exc:
        return None, f"読み取れない ({exc.__class__.__name__})"
    clock.sleep(stable_s)
    try:
        second = path.read_bytes()
    except OSError as exc:
        # grace 中に消えた = 席が rename を完了した可能性。呼び出し側が .json を見直す。
        return None, f"grace 中に読めなくなった ({exc.__class__.__name__})"
    if first != second:
        return None, "grace 中に内容が変化 (まだ書き込み中)"
    return second, ""


def _load_tmp_candidate(
    tmp, inv_id: str, participant: str, clock, stable_s: float
) -> tuple[dict | None, str]:
    """`.tmp` を採用してよいか判定する。採用可なら (data, "")、不可なら (None, 理由)。

    採用条件は `.json` 経路と同じ厳しさにする (安定 → parse → dict → id 照合 → schema)。
    ここを緩めると「半端な .tmp を成功として記録する」= 失敗を隠す方向の穴になる。
    """
    raw, why = _read_stable(tmp, clock, stable_s)
    if raw is None:
        return None, why
    try:
        # utf-8-sig: Windows 席の BOM 付き出力も .json 経路と同じく受理する
        data = json.loads(raw.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "JSON として不完全 (書きかけの可能性)"
    if not isinstance(data, dict):
        return None, "top-level が object でない"
    if data.get("invocation_id") != inv_id or data.get("participant") != participant:
        return None, "invocation_id / participant が指名と不一致"
    errs = validate_opinion(data)
    if errs:
        return None, "schema 違反: " + "; ".join(errs)
    return data, ""


def collect(
    tp: TopicPaths,
    journal: Journal,
    inv_id: str,
    participant: str,
    timeout_s: float = 900,
    poll_s: float = 2.0,
    clock=time,
    tmp_stable_s: float = 1.0,
) -> dict:
    """invocation の出力を待ち、検証してから議事録へ merge する。

    改ざん判定は `minutes.merge_opinion` 側に寄せてある。**snapshot 採取時の hash を
    渡す方式はやめた** (レビュー H3): 並行 dispatch では先に merge した席が
    minutes.md を伸ばすので、後続席の base_hash は正常運用でも必ず古くなり、
    正しく書かれた意見が毎回 `failed: tampered` として捨てられていた (repro R2 で実測)。
    現在は minutes.md 自身の hash 証跡 (`.integrity/<slug>/minutes.md.sha256`) と
    ロックで、「他 dispatcher の正当な追記」と「dispatcher 外の書き換え」を分ける。

    timeout に達した時は `.json` が無いというだけで failed:timeout にせず、
    `<inv>.json.tmp` の有無と中身を見て「無応答」と「rename 漏れ」を分ける。
    tmp_stable_s は書きかけ判定の grace (この間に内容が変われば採用しない)。
    """
    if journal.is_merged(inv_id):
        return {"ok": True}  # 冪等: 既に merge 済みなら再実行しない

    out = tp.scratch / f"{inv_id}.json"
    tmp = tp.scratch / f"{inv_id}.json.tmp"

    data = None
    recovered = False
    deadline = clock.monotonic() + timeout_s
    while not out.exists():
        if clock.monotonic() < deadline:
            clock.sleep(poll_s)
            continue
        # --- timeout 到達。ここで .tmp を見る (見ないと偽陰性になる) ---
        if not tmp.exists():
            journal.set_state(inv_id, "failed", "timeout")
            return {"ok": False, "reason": "timeout"}
        candidate, why = _load_tmp_candidate(tmp, inv_id, participant, clock, tmp_stable_s)
        if out.exists():
            break  # grace 中に席が rename を完了した → 通常経路で読み直す
        if candidate is None:
            journal.set_state(inv_id, "failed", f"stalled-tmp: {why}")
            return {"ok": False, "reason": "stalled-tmp", "tmp": str(tmp), "detail": why}
        data = candidate
        recovered = True
        break

    provenance = f"{RECOVERED_FROM_TMP}: {tmp.name}" if recovered else ""
    journal.set_state(inv_id, "output-received", provenance)

    if data is None:  # 通常経路 (.json が確定している)
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

    journal.set_state(inv_id, "validated", provenance)
    try:
        m.merge_opinion(tp, data, journal.round_no)  # 照合はロック配下で証跡と行う
    except m.MinutesTamperedError:
        journal.set_state(inv_id, "failed", "tampered")
        return {"ok": False, "reason": "tampered"}

    # 来歴を merged の detail に残す。「成功に見える」変更なので、通常経路と
    # 区別できない形で入れると DESIGN の「失敗を隠さない」を裏側から壊す。
    journal.set_state(inv_id, "merged", provenance)
    if recovered:
        # .tmp は消さない: 何を採用したかの物証を残す (dispatcher は席の契約成果物を捏造しない)。
        return {"ok": True, "recovered": "tmp", "tmp": str(tmp)}
    return {"ok": True}
