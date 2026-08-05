"""invocation 状態機械。journal.json に atomic 保存し、merge 冪等の根拠になる。

状態遷移 (DESIGN v6 §6):
    prepared → delivered → output-received → validated → merged / failed
Tier3 の delivered は「クリップボード搬出済み」であり席への着信は未知 —
timeout 時に「未貼り付け?」の分岐を出すためにこの状態を区別して記録する。
"""
import json
import uuid

from .minutes import atomic_write
from .paths import TopicPaths

STATES = {"prepared", "delivered", "output-received", "validated", "merged", "failed"}


class Journal:
    def __init__(self, tp: TopicPaths, data: dict):
        self.tp = tp
        self.data = data

    @classmethod
    def load(cls, tp: TopicPaths) -> "Journal":
        if tp.journal.exists():
            return cls(tp, json.loads(tp.journal.read_text(encoding="utf-8")))
        return cls(tp, {"round": 1, "invocations": {}})

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
        rec = self.data["invocations"][inv]
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
