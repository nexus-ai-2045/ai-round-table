"""席台帳 (seats.json) — 席ごとの tier と thread の記録 (DESIGN v6 §7)。

配置は `<root>/seats.json` (topic 配下ではない: 席は議題をまたいで CEO が管理する)。
key は `rt/<topic>/<participant>`、値は DESIGN v6 §7 の形:

    {"participant": "codex", "topic": "<slug>", "surface": "codex-app",
     "tier": 3, "thread_ref": null, "last_thread_ref": null,
     "created_at": "...", "note": ""}

## 既定 tier を 3 にしている理由

Tier3 (人間 relay) は「常に利用可能な縮退運転」であり、Tier1 は
「spike で実測できた席にだけ、seats.json へ明示的に記録してから使う」
(DESIGN v6 §4 / §8-3「spike の結果を seats.json の tier に反映」)。

既定を 1 にすると、codex が入っていない環境や未検証の環境でも dispatcher が
勝手に app-server を起こしにいき、「届いたつもり」の議題が生まれる。tier の
昇格は必ず人間の明示操作 (`dispatch --tier 1`) を経る。

## thread_ref に書かず last_thread_ref に書く理由

Tier1 で thread を作ると id が返るが、これを `thread_ref` に書き戻すと、次の
dispatch で「既存 thread への再接続 (thread/resume)」が要求される。resume は
2026-08-05 spike で一度も実測していないため relay_codex が NotImplementedError で
落ち、結果として 2 回目以降の Tier1 が常に Tier3 へ縮退する。

そこで **記録用 (`last_thread_ref`) と接続指示 (`thread_ref`) を分ける**。
CEO が resume を意図する時だけ `thread_ref` を手で入れる (現状は未実測なので
縮退の説明つきで Tier3 に落ちる)。
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .minutes import atomic_write
from .paths import validate_slug

SEATS_FILENAME = "seats.json"

# Tier3 = 人間 relay。常に成立する縮退運転を既定にする (本 module docstring 参照)。
DEFAULT_TIER = 3
VALID_TIERS = (1, 2, 3)

# 参加者名: 台帳 key と packet 本文に入るだけで path にはならないが、`/` や空白を
# 許すと key が壊れて席の同定ができなくなる (軸 D: 境界で検証する)。
PARTICIPANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def seats_path(root: Path) -> Path:
    return Path(root) / SEATS_FILENAME


def validate_participant(participant: str) -> str:
    """participant を検証してそのまま返す。違反は ValueError。"""
    if not isinstance(participant, str) or not PARTICIPANT_RE.fullmatch(participant):
        raise ValueError(
            f"invalid participant: {participant!r} (期待: ^[A-Za-z0-9][A-Za-z0-9._-]*$)"
        )
    return participant


def validate_tier(tier: int) -> int:
    """tier を検証してそのまま返す。1/2/3 以外は ValueError。"""
    if tier not in VALID_TIERS or isinstance(tier, bool):
        raise ValueError(f"invalid tier: {tier!r} (1/2/3 のみ)")
    return tier


def seat_key(topic: str, participant: str) -> str:
    """台帳の key。topic / participant は合成前に検証する。"""
    return f"rt/{validate_slug(topic)}/{validate_participant(participant)}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Seats:
    """seats.json の read / update。書き込みは atomic_write 経由。"""

    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data

    @classmethod
    def load(cls, root: Path) -> "Seats":
        """seats.json を読む。無ければ空台帳 (ファイルはまだ作らない)。

        壊れた JSON は握りつぶさず ValueError にする — 台帳は CEO が手で編集する
        前提のファイルなので、黙って初期化すると tier 設定を失う。
        """
        path = seats_path(root)
        if not path.exists():
            return cls(path, {})
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"seats.json が壊れている: {path} ({e})") from e
        if not isinstance(data, dict):
            raise ValueError(f"seats.json は object でなければならない: {path}")
        for key, seat in data.items():
            if not isinstance(seat, dict):
                raise ValueError(f"seats.json の {key} が object でない: {path}")
            validate_tier(seat.get("tier"))
        return cls(path, data)

    # --- 参照 ---------------------------------------------------------
    def get(self, topic: str, participant: str) -> dict | None:
        return self.data.get(seat_key(topic, participant))

    def for_topic(self, topic: str) -> dict[str, dict]:
        """topic の席を {participant: seat} で返す (status 表示用)。"""
        prefix = f"rt/{validate_slug(topic)}/"
        return {
            seat["participant"]: seat
            for key, seat in self.data.items()
            if key.startswith(prefix)
        }

    # --- 更新 ---------------------------------------------------------
    def ensure(self, topic: str, participant: str, tier: int | None = None) -> dict:
        """席を取得する。無ければ作る。tier 指定があれば作成・更新して保存する。

        tier=None (指定なし) の時は既存の記録を尊重する — dispatch のたびに
        既定値で上書きすると、CEO が seats.json に入れた Tier1 設定が消える。
        """
        key = seat_key(topic, participant)
        seat = self.data.get(key)
        if seat is None:
            seat = {
                "participant": participant,
                "topic": topic,
                "surface": f"{participant}-app",
                "tier": validate_tier(tier) if tier is not None else DEFAULT_TIER,
                "thread_ref": None,  # 再接続 (resume) 指示。未実測なので既定は空
                "last_thread_ref": None,  # Tier1 で作った thread の記録 (履歴の所在)
                "created_at": _now(),
                "note": "",  # CEO が作成したチャット名を書く欄 (DESIGN v6 §7)
            }
            self.data[key] = seat
            self.save()
            return seat
        if tier is not None and seat.get("tier") != validate_tier(tier):
            seat["tier"] = tier
            self.save()
        return seat

    def record_thread(self, topic: str, participant: str, thread_ref: str) -> None:
        """Tier1 で作った thread id を記録する (`thread_ref` は書き換えない)。"""
        seat = self.data.get(seat_key(topic, participant))
        if seat is None:
            raise KeyError(f"未登録の席: rt/{topic}/{participant}")
        if seat.get("last_thread_ref") == thread_ref:
            return
        seat["last_thread_ref"] = thread_ref
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))
