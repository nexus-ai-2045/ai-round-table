"""invocation 状態機械 + 人間の操作回数 (KPI)。journal.json に atomic 保存する。

状態遷移 (DESIGN v6 §6):
    prepared → delivered → output-received → validated → merged / failed
Tier3 の delivered は「クリップボード搬出済み」であり席への着信は未知 —
timeout 時に「未貼り付け?」の分岐を出すためにこの状態を区別して記録する。

v0.2 で 2 つの detector を足した (計画 §4 Phase 2 — 修正ではなく検知):
- 軸 C: TRANSITIONS による遷移検証。merged/failed からの逆行を ValueError にする
- 軸 A: human_actions カウンタ。受け入れ基準「1 議題あたり人間の操作 ≤ 3」を
  自己申告でなく機械的に計測する
"""
import json
import uuid

from .minutes import atomic_write
from .paths import TopicPaths

STATES = {"prepared", "delivered", "output-received", "validated", "merged", "failed"}

# 許可された遷移 (軸 C の Invariant「状態は前進のみ」)。
# merged / failed は終端 — 空集合なのでそこから先へは動かせない。
TRANSITIONS: dict[str, set[str]] = {
    "prepared": {"delivered", "output-received", "failed"},
    "delivered": {"output-received", "failed"},
    "output-received": {"validated", "failed"},
    "validated": {"merged", "failed"},
    "merged": set(),
    "failed": set(),
}

# 人間が 1 回触ったことを表す種別 (軸 A の計測単位)。
#   topic    議題宣言 (new-topic)
#   nominate 指名 (dispatch の起動)
#   paste    Tier3 の貼り付け (Tier1 では発生しない)
#   verdict  裁定 (close)
#   command  上記に当てはまらない手動コマンド実行
HUMAN_ACTION_KINDS = {"topic", "nominate", "paste", "verdict", "command"}


class InvalidTransitionError(ValueError):
    """許可されていない状態遷移。ValueError 派生なので既存の握り方を壊さない。"""


class Journal:
    def __init__(self, tp: TopicPaths, data: dict):
        self.tp = tp
        self.data = data

    @classmethod
    def load(cls, tp: TopicPaths) -> "Journal":
        if tp.journal.exists():
            data = json.loads(tp.journal.read_text(encoding="utf-8"))
            data.setdefault("human_actions", {})  # v0.1 で作られた journal との互換
            return cls(tp, data)
        return cls(tp, {"round": 1, "invocations": {}, "human_actions": {}})

    @property
    def round_no(self) -> int:
        return self.data["round"]

    def record_human_action(self, kind: str) -> None:
        """人間が 1 回操作したことを記録する (軸 A の KPI 計測)。

        「議事録が正しいか」ではなく「人間が何回触ったか」を測る。dispatcher が
        自動でやったことは数えない — 数えると KPI が甘くなり、手数削減の圧力が消える。
        """
        if kind not in HUMAN_ACTION_KINDS:
            raise ValueError(f"unknown human action kind: {kind}")
        counts = self.data.setdefault("human_actions", {})
        counts[kind] = counts.get(kind, 0) + 1
        self.save()

    @property
    def human_actions(self) -> int:
        """人間の操作の合計回数。受け入れ基準 (≤ 3) と直接比較する値。"""
        return sum(self.data.get("human_actions", {}).values())

    @property
    def human_action_counts(self) -> dict[str, int]:
        """種別ごとの内訳。Tier3 席の +1 がどこで発生したかを追える (計画 §5)。"""
        return dict(self.data.get("human_actions", {}))

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
        """状態を進める。遷移表にない移動は InvalidTransitionError (軸 C の detector)。

        merged / failed は終端。close 済みの invocation を後から書き換えて
        「成功したことにする」経路を構造的に塞ぐ。
        """
        if state not in STATES:
            raise ValueError(f"unknown state: {state}")
        rec = self.data["invocations"][inv]
        current = rec["state"]
        if current not in TRANSITIONS:  # journal.json 側が壊れている (手編集・別版)
            raise ValueError(f"unknown current state in journal: {current!r} (inv: {inv})")
        if state not in TRANSITIONS[current]:
            raise InvalidTransitionError(f"invalid transition: {current} -> {state} (inv: {inv})")
        rec["state"] = state
        rec["detail"] = detail
        self.save()

    def is_merged(self, inv: str) -> bool:
        return self.data["invocations"].get(inv, {}).get("state") == "merged"

    def advance_round_if_complete(self, order: list[str]) -> None:
        """指名バッチ (order) の全員が現 round で merged なら round を進める。"""
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

    def failure_counts(self) -> dict[str, int]:
        """失敗分類ごとの件数 (軸 B の detector)。

        detail は watcher が付ける分類 (timeout / parse / id-mismatch / tampered) か、
        schema 違反時の `schema: <詳細>`。先頭のコロンまでを分類キーとして畳む —
        違反内容ごとに散らばると「何で落ちているか」の分布が読めなくなる。
        """
        counts: dict[str, int] = {}
        for rec in self.data["invocations"].values():
            if rec["state"] != "failed":
                continue
            key = rec.get("detail", "").split(":", 1)[0].strip() or "(分類なし)"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def unresolved(self) -> list[dict]:
        """merged に到達していない全 invocation。

        close 時の偽装成功防止 (レビュー M3): failed だけでなく、Ctrl+C や clip 失敗で
        prepared / delivered / output-received / validated に残ったものも CEO に見せる。
        """
        return [
            {"invocation": k, **v}
            for k, v in self.data["invocations"].items()
            if v["state"] != "merged"
        ]

    def save(self) -> None:
        atomic_write(self.tp.journal, json.dumps(self.data, ensure_ascii=False, indent=1))
