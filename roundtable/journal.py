"""invocation 状態機械。journal.json に atomic 保存し、merge 冪等の根拠になる。

状態遷移 (DESIGN v6 §6 + v0.2 軸 C detector):
    prepared → delivered → output-received → validated → merged / failed
前進のみ。merged / failed からの逆行は ValueError。

並行 dispatch での記録消失対策 (2026-08-07 実測バグ / 診断 fix_options A を採用):
    save() は「ロード時のスナップショットを全文上書き」ではなく
    「ディスクの最新状態に自分の記録を重ねる」read-modify-write にする。
    詳細と選定理由は save() の docstring を読む。
"""
import json
import uuid
from collections import Counter
from datetime import datetime, timezone

from . import integrity
from .filelock import FileLock
from .paths import TopicPaths

STATES = {"prepared", "delivered", "output-received", "validated", "merged", "failed"}

# 軸 C: 前進のみ (ジャンプ可) / 逆行禁止。同一状態は detail 更新を許可。
# failed は任意の非終端から到達可。merged / failed は終端。
_FORWARD_ORDER = ["prepared", "delivered", "output-received", "validated", "merged"]
_FORWARD_INDEX = {s: i for i, s in enumerate(_FORWARD_ORDER)}

_JOURNAL_NAME = "journal.json"


def _read_verified(tp: TopicPaths) -> bytes | None:
    """ロックを取ってから証跡照合 + 本文読取を行う (読みもロック配下、レビュー H1/H2)。

    `integrity.verify_and_read` は純粋な読み取りではない (pending の巻き戻しと
    baseline 採用で証跡を書く)。ロック外の reader がこれをやると:

    - writer の「予告 → 本文 → 確定」の隙間に割り込んで証跡を巻き戻し、その書き込みが
      writer の確定より後に着地する → 誰も改ざんしていないのに恒久的な
      `StateTamperedError`。fail-closed なので議題が二度と開けない。
    - reader が journal.json / 証跡を開いている間、writer の `os.replace` が Windows で
      WinError 5 になり dispatch ごと落ちる (repro3 で実測: writer 4 / reader 4)。

    ロック配下に寄せると pending 窓は reader から観測不能になり、対象ファイルを
    同時に開く経路も消えるので、両方が同じ 1 手で閉じる。保持時間は読み取りの
    数ミリ秒だけで、dispatch の 900 秒を握るわけではない。
    """
    with FileLock(tp.lock(_JOURNAL_NAME)):
        return integrity.verify_and_read(tp.journal, tp.witness(_JOURNAL_NAME))


def _entry_id(entry) -> str | None:
    return entry.get("id") if isinstance(entry, dict) else None


def _entry_key(entry) -> str:
    return json.dumps(entry, ensure_ascii=False, sort_keys=True)


def _merge_lists(disk: list, local: list) -> list:
    """append-only リスト (human_actions / conflicts) を、記録を落とさずに重ねる。

    単純連結だと「自分が前回書いた分をディスクから読み直した」ものを二重計上する。
    逆に集合和にすると、同一内容の操作を 2 回した記録が 1 回に減る。どちらも
    軸 A KPI (人間の手数) を静かに嘘にする。

    そこで各要素に発行時の id を持たせ、id があるものは id で和を取る (完全に一意)。
    id を持たない要素はこの機構より前に書かれた記録なので、内容ごとの多重度 max で
    畳む。後者は「同一内容の操作 2 回」を区別できない — 過去は観測できないという
    限界であって、隠さずここに書いておく。
    """
    out = list(disk)
    seen_ids = {i for i in (_entry_id(a) for a in disk) if i}
    legacy_counts = Counter(_entry_key(a) for a in disk if not _entry_id(a))
    used: Counter[str] = Counter()
    for a in local:
        eid = _entry_id(a)
        if eid is not None:
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            out.append(a)
            continue
        key = _entry_key(a)
        used[key] += 1
        if used[key] > legacy_counts[key]:
            out.append(a)
    return sorted(out, key=lambda a: a.get("at", "") if isinstance(a, dict) else "")


def _merge_invocations(disk: dict, local: dict, conflicts: list) -> dict:
    """invocation を key 単位で重ねる。どちらの key も消さない。

    同じ invocation が両方に在って状態が違う場合は、状態機械上「先にある方」を採る。
    どちらへも遷移できない組み合わせ (merged と failed 等) は本来起きない異常なので、
    ディスク側 (先に書いた方) を残しつつ conflicts に両方を積む。落とした記録を
    黙って消さないための append-only な置き場である (失敗を隠さない)。
    """
    out = dict(disk)
    for inv, lrec in local.items():
        drec = out.get(inv)
        if drec is None:
            out[inv] = lrec  # 相手が知らない invocation — これが消えていた記録
            continue
        if drec == lrec:
            continue
        ds, ls = drec.get("state"), lrec.get("state")
        if ds == ls:
            out[inv] = lrec  # detail だけの差 → 自分の最新を採る
        elif _transition_allowed(ds, ls):
            out[inv] = lrec  # 自分が前へ進めた
        elif _transition_allowed(ls, ds):
            pass  # ディスクの方が先。自分は遅れているだけなので何も落ちない
        else:
            conflicts.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "invocation": inv,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "kept": drec,
                    "dropped": lrec,
                }
            )
    return out


def _merge_data(disk: dict, local: dict) -> dict:
    """ディスクの記録に自分の記録を重ねた**新しい** dict を返す (既存 dict は変えない)。"""
    conflicts = _merge_lists(disk.get("conflicts", []), local.get("conflicts", []))
    merged = dict(disk)
    merged.update({k: v for k, v in local.items() if k not in merged})
    merged["round"] = max(disk.get("round", 1), local.get("round", 1))  # round は前進のみ
    merged["invocations"] = _merge_invocations(
        disk.get("invocations", {}), local.get("invocations", {}), conflicts
    )
    merged["human_actions"] = _merge_lists(
        disk.get("human_actions", []), local.get("human_actions", [])
    )
    merged["conflicts"] = conflicts
    return merged


def _transition_allowed(current: str, new: str) -> bool:
    if new == current:
        return True
    if current in {"merged", "failed"}:
        return False  # 終端からの逆行・離脱は不可
    if new == "failed":
        return True
    if current not in _FORWARD_INDEX or new not in _FORWARD_INDEX:
        return False
    return _FORWARD_INDEX[new] >= _FORWARD_INDEX[current]


class Journal:
    def __init__(self, tp: TopicPaths, data: dict):
        self.tp = tp
        self.data = data
        self.data.setdefault("human_actions", [])
        self.data.setdefault("invocations", {})
        self.data.setdefault("round", 1)
        self.data.setdefault("conflicts", [])

    @classmethod
    def load(cls, tp: TopicPaths) -> "Journal":
        raw = _read_verified(tp)
        if raw is None:
            return cls(tp, {"round": 1, "invocations": {}, "human_actions": []})
        return cls(tp, json.loads(raw.decode("utf-8")))

    def refresh(self) -> None:
        """ディスクの最新状態を取り込む。自分の未保存の記録は重ね合わせで残る。

        並行 dispatch では、自分がロードした後に他プロセスが merged を書いている。
        状態遷移の検証や round 判定を古いスナップショットで行うと、他プロセスの
        成果が見えないまま「まだ揃っていない」と誤判定する。
        """
        raw = _read_verified(self.tp)
        if raw is not None:
            self.data = _merge_data(json.loads(raw.decode("utf-8")), self.data)

    @property
    def round_no(self) -> int:
        return self.data["round"]

    def new_invocation(self, participant: str, round_no: int) -> str:
        inv = uuid.uuid4().hex[:12]
        self.data["invocations"][inv] = {
            "participant": participant,
            "round": round_no,
            "state": "prepared",
            "detail": "",
        }
        self.save()
        return inv

    def set_state(self, inv: str, state: str, detail: str = "") -> None:
        if state not in STATES:
            raise ValueError(f"unknown state: {state}")
        # 遷移検証はディスクの最新状態に対して行う。古いスナップショットで検証すると
        # 「別プロセスが failed にした invocation を merged に進める」が通ってしまい、
        # 終端の不変条件がプロセス境界を越えた瞬間に消える。
        self.refresh()
        rec = self.data["invocations"][inv]
        current = rec["state"]
        if not self._transition_allowed(current, state):
            raise ValueError(f"invalid transition: {current} -> {state}")
        rec["state"] = state
        rec["detail"] = detail
        self.save()

    @staticmethod
    def _transition_allowed(current: str, new: str) -> bool:
        return _transition_allowed(current, new)

    def is_merged(self, inv: str) -> bool:
        return self.data["invocations"].get(inv, {}).get("state") == "merged"

    def advance_round_if_complete(self, order: list[str]) -> None:
        """指名バッチ (order) の全員が現 round で merged なら round を進める。"""
        # 他プロセスが merge した分を見ないと、揃っているのに進まない (逆に、
        # 自分の古い round を書き戻して巻き戻す) — 実測バグの再発点そのもの。
        self.refresh()
        r = self.round_no
        done = {
            v["participant"]
            for v in self.data["invocations"].values()
            if v["round"] == r and v["state"] == "merged"
        }
        if set(order) <= done:
            self.data["round"] = r + 1
            self.save()

    def failures(self) -> list[dict]:
        return [
            {"invocation": k, **v}
            for k, v in self.data["invocations"].items()
            if v["state"] == "failed"
        ]

    def unresolved(self) -> list[dict]:
        """merged に到達していない全 invocation。"""
        return [
            {"invocation": k, **v}
            for k, v in self.data["invocations"].items()
            if v["state"] != "merged"
        ]

    def conflicts(self) -> list[dict]:
        """並行書き込みで解決できなかった記録 (どちらも捨てずに積んである)。"""
        return list(self.data.get("conflicts", []))

    def record_human_action(self, action: str, detail: str = "") -> None:
        """軸 A KPI: 人間操作を機械記録 (自己申告にしない)。"""
        self.data.setdefault("human_actions", []).append(
            {
                # id は並行 dispatch で記録を取り違えないための identity。
                # 同じ操作を 2 プロセスが同時刻に記録しても、片方が消えない。
                "id": uuid.uuid4().hex[:12],
                "action": action,
                "detail": detail,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.save()

    def human_action_count(self) -> int:
        return len(self.data.get("human_actions", []))

    def failure_stats(self) -> dict[str, int]:
        """軸 B detector: 失敗分類の集計。detail 先頭トークン (timeout / parse / ...) で数える。"""
        counts: Counter[str] = Counter()
        for v in self.data["invocations"].values():
            if v["state"] != "failed":
                continue
            detail = (v.get("detail") or "unknown").strip()
            key = detail.split(":", 1)[0].strip() or "unknown"
            counts[key] += 1
        return dict(counts)

    def save(self) -> None:
        """ディスクの最新状態に自分の記録を重ねて書く (read-modify-write)。

        **なぜ全文上書きをやめたか** (2026-08-07 実測): 旧実装は load 時の
        スナップショットを save のたびに全文書き戻していた。dispatch は
        「起動時に load → 最大 900 秒後に save」なので、read-modify-write の窓が
        プロセスの全寿命に等しく、並行 dispatch の片方が他方の invocation を
        丸ごと消していた (minutes.md には merge 済みなのに journal から消滅)。

        **なぜ fix_options A (重ね合わせ + hash) を採ったか**
        - B (append-only event log) が根治だが、journal.json の schema・status・
          close・既存テストに全面波及する。観測された 1 事象に対して変更が大きい。
        - C (排他ロック単独) は 900 秒握れないので、古いスナップショットで
          上書きする問題そのものが残る。
        - A は minutes.md で既に使っている「hash 照合 + fail-closed」を
          そのまま持ち込む形で、この repo の思想と既存 API を壊さない。
        B は v0.3 の設計課題として docs/review-backlog.md に残す。

        重ね合わせの規則は _merge_data を読む。ロックは書き込みの数ミリ秒だけ握り、
        ロード〜save の 900 秒は握らない (握れない)。ロックは「重ね合わせを不可分に
        する」ためだけで、失われた更新を防ぐ本体は重ね合わせ側にある。

        読み取り (`load` / `refresh`) も同じロックを取る (`_read_verified`)。
        証跡の巻き戻しが writer と競合して偽の改ざん検知を作るのと、reader が
        開いている間の `os.replace` が Windows で失敗するのを同時に防ぐ。
        """
        witness = self.tp.witness(_JOURNAL_NAME)
        with FileLock(self.tp.lock(_JOURNAL_NAME)):
            raw = integrity.verify_and_read(self.tp.journal, witness)
            merged = (
                self.data
                if raw is None
                else _merge_data(json.loads(raw.decode("utf-8")), self.data)
            )
            integrity.write_verified(
                self.tp.journal,
                json.dumps(merged, ensure_ascii=False, indent=1),
                witness,
            )
            self.data = merged
