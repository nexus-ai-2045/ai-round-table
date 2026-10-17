"""scratch 監視 → 検証 → merge。dispatcher の回収側の心臓部。

待機・失敗分類 (journal.set_state の detail / 戻り値 reason に使う):
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
import math
import re
import time

from . import minutes as m
from .filelock import AdvisoryFileLock
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


def _collect_ready(
    tp: TopicPaths,
    journal: Journal,
    inv_id: str,
    participant: str,
    timeout_s: float = 900,
    poll_s: float = 2.0,
    clock=time,
    tmp_stable_s: float = 1.0,
    preserve_delivery_unknown: bool = False,
) -> dict:
    """invocation の出力を待ち、検証してから議事録へ merge する。

    改ざん判定は `minutes.merge_opinion` 側に寄せてある。**snapshot 採取時の hash を
    渡す方式はやめた** (レビュー H3): 並行 dispatch では先に merge した席が
    minutes.md を伸ばすので、後続席の base_hash は正常運用でも必ず古くなり、
    正しく書かれた意見が毎回 `failed: tampered` として捨てられていた (repro R2 で実測)。
    現在は git の clean 検査 (D12 / roundtable.ledger) とロックで、「他 dispatcher の
    正当な追記 (commit 済み = clean)」と「dispatcher 外の書き換え (dirty)」を分ける。

    timeout に達した時は `.json` が無いというだけで終端失敗にせず、
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
            if journal.data["invocations"][inv_id]["state"] in {"validated", "output-received"}:
                return {"ok": False, "reason": "missing-response"}
            if preserve_delivery_unknown:
                # turn/start の応答を失った呼び出しは、後から成果物が届きうる。
                # failed 終端へ進めず、明示 collect で回収できる状態を保つ。
                return {"ok": False, "reason": "delivery-unknown"}
            journal.set_state(inv_id, "waiting", "timeout")
            return {"ok": False, "reason": "timeout"}
        candidate, why = _load_tmp_candidate(tmp, inv_id, participant, clock, tmp_stable_s)
        if out.exists():
            break  # grace 中に席が rename を完了した → 通常経路で読み直す
        if candidate is None:
            if journal.data["invocations"][inv_id]["state"] not in {"validated", "output-received"}:
                journal.set_state(inv_id, "waiting", f"stalled-tmp: {why}")
            return {"ok": False, "reason": "stalled-tmp", "tmp": str(tmp), "detail": why}
        data = candidate
        recovered = True
        break

    provenance = f"{RECOVERED_FROM_TMP}: {tmp.name}" if recovered else ""
    if journal.data["invocations"][inv_id]["state"] != "validated":
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

    rec = journal.data["invocations"][inv_id]
    digest = m.opinion_hash(data)
    if rec.get("response_sha256") not in {None, digest}:
        journal.set_state(inv_id, "failed", "response-changed")
        return {"ok": False, "reason": "response-changed"}
    rec["response_sha256"] = digest
    journal.set_state(inv_id, "validated", provenance)
    try:
        m.merge_opinion(tp, data, rec["round"])  # 照合はロック配下で証跡と行う
    except m.OpinionConflictError:
        journal.set_state(inv_id, "failed", "response-conflict")
        return {"ok": False, "reason": "response-conflict"}
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


def _invocation_lock(tp: TopicPaths, inv_id: str, **kwargs) -> AdvisoryFileLock:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", inv_id):
        raise ValueError("invalid invocation id")
    return AdvisoryFileLock(tp.lock(f"collect-{inv_id}"), **kwargs)


def cancel(tp: TopicPaths, inv_id: str) -> dict:
    """回収を取り消す。席のプロセスや休止中 Codex は操作しない。"""
    with _invocation_lock(tp, inv_id):
        journal = Journal.load(tp)
        rec = journal.data["invocations"].get(inv_id)
        if rec is None:
            return {"ok": False, "reason": "unknown-invocation"}
        if rec["state"] == "cancelled":
            return {"ok": True, "reason": "cancelled"}
        if rec["state"] in {"merged", "failed"}:
            return {"ok": False, "reason": rec["state"]}
        if rec.get("response_sha256") and m.has_response(
            tp, inv_id, rec["participant"], rec["response_sha256"]
        ):
            journal.set_state(inv_id, "merged", "recovered-committed-response")
            return {"ok": False, "reason": "merged"}
        journal.set_state(inv_id, "cancelled", "collection-cancelled; seat-not-stopped")
        return {"ok": True, "reason": "cancelled"}


def collect(
    tp: TopicPaths,
    journal: Journal,
    inv_id: str,
    participant: str,
    timeout_s: float = 900,
    poll_s: float = 2.0,
    clock=time,
    tmp_stable_s: float = 1.0,
    preserve_delivery_unknown: bool = False,
    lock_timeout_s: float | None = None,
) -> dict:
    """bounded poll。待機中は lock を解放し、取消と別 collector を受け付ける。

    再起動後は同じ invocation を指定して再実行する。merge と journal 更新の
    間で中断しても、minutes の同一書込み receipt で追記を重複させない。
    """
    if not all(math.isfinite(v) for v in (timeout_s, poll_s, tmp_stable_s)):
        raise ValueError("wait values must be finite")
    if timeout_s < 0 or poll_s <= 0 or tmp_stable_s < 0:
        raise ValueError("timeout/stability must be nonnegative and poll must be positive")
    deadline = clock.monotonic() + timeout_s
    lock_options = {} if lock_timeout_s is None else {"timeout_s": lock_timeout_s}
    if lock_timeout_s is not None and (not math.isfinite(lock_timeout_s) or lock_timeout_s < 0):
        raise ValueError("lock timeout must be finite and nonnegative")
    while True:
        with _invocation_lock(tp, inv_id, **lock_options):
            # 未保存の古い snapshot を重ねず、ここから先は disk が正本。
            journal.data = Journal.load(tp).data
            rec = journal.data["invocations"].get(inv_id)
            if rec is None:
                return {"ok": False, "reason": "unknown-invocation"}
            if rec["participant"] != participant:
                return {"ok": False, "reason": "participant-mismatch"}
            if rec["state"] == "merged":
                return {"ok": True}
            if rec["state"] == "cancelled":
                return {"ok": False, "reason": "cancelled"}
            if rec["state"] == "failed" and not journal.reopen_wait_failure(inv_id):
                return {"ok": False, "reason": "failed", "detail": rec.get("detail", "")}
            # commit済みの採用結果をscratchより先に照合する。停止後にscratchが
            # 差し替わったり消えても、採用済み回答を失敗へ巻き戻さない。
            if rec["state"] == "validated" and rec.get("response_sha256") and m.has_response(
                tp, inv_id, participant, rec["response_sha256"]
            ):
                journal.set_state(inv_id, "merged", "recovered-committed-response")
                return {"ok": True, "reason": "recovered-committed-response"}
            out = tp.scratch / f"{inv_id}.json"
            if out.exists() or clock.monotonic() >= deadline:
                return _collect_ready(tp, journal, inv_id, participant, timeout_s=0,
                                      poll_s=poll_s, clock=clock, tmp_stable_s=tmp_stable_s,
                                      preserve_delivery_unknown=preserve_delivery_unknown)
        clock.sleep(min(poll_s, max(0, deadline - clock.monotonic())))
