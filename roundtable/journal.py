"""invocation 状態機械。journal.json に atomic 保存し、merge 冪等の根拠になる。

状態遷移 (DESIGN v6 §6 + v0.2 軸 C detector):
    prepared → delivered → output-received → validated → merged / failed
前進のみ。merged / failed からの逆行は ValueError。
"""
import json
import uuid
from collections import Counter
from datetime import datetime, timezone

from .minutes import atomic_write
from .paths import TopicPaths

STATES = {"prepared", "delivered", "output-received", "validated", "merged", "failed"}

# 軸 C: 前進のみ (ジャンプ可) / 逆行禁止。同一状態は detail 更新を許可。
# failed は任意の非終端から到達可。merged / failed は終端。
_FORWARD_ORDER = ["prepared", "delivered", "output-received", "validated", "merged"]
_FORWARD_INDEX = {s: i for i, s in enumerate(_FORWARD_ORDER)}


class Journal:
    def __init__(self, tp: TopicPaths, data: dict):
        self.tp = tp
        self.data = data
        self.data.setdefault("human_actions", [])
        self.data.setdefault("invocations", {})
        self.data.setdefault("round", 1)

    @classmethod
    def load(cls, tp: TopicPaths) -> "Journal":
        if tp.journal.exists():
            return cls(tp, json.loads(tp.journal.read_text(encoding="utf-8")))
        return cls(tp, {"round": 1, "invocations": {}, "human_actions": []})

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
        current = rec["state"]
        if not self._transition_allowed(current, state):
            raise ValueError(f"invalid transition: {current} -> {state}")
        rec["state"] = state
        rec["detail"] = detail
        self.save()

    @staticmethod
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
        """merged に到達していない全 invocation。"""
        return [
            {"invocation": k, **v}
            for k, v in self.data["invocations"].items()
            if v["state"] != "merged"
        ]

    def record_human_action(self, action: str, detail: str = "") -> None:
        """軸 A KPI: 人間操作を機械記録 (自己申告にしない)。"""
        self.data.setdefault("human_actions", []).append(
            {
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
        atomic_write(self.tp.journal, json.dumps(self.data, ensure_ascii=False, indent=1))
