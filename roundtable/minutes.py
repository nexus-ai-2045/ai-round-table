"""議事録の生成・snapshot・hash・atomic write・frontmatter 読み。

書き込みはすべて tmp → os.replace の atomic 経路 (部分書き込みを構造的に排除)。
Windows の一時ロック (エディタ/AV の共有違反 = WinError 32) に備えて短い retry を持つ。
"""
import hashlib
import os
import tempfile
import time
from pathlib import Path

from .paths import TopicPaths

TEMPLATE = """---
topic: {topic}
status: open
round: 1
participants: [{participants}]
verdict:
---

# {topic}

## 背景

"""


def atomic_write(path: Path, text: str) -> None:
    """tmp に書いて os.replace。PermissionError は 5 回まで backoff retry。"""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def create(tp: TopicPaths, topic: str, participants: list[str]) -> None:
    atomic_write(tp.minutes, TEMPLATE.format(topic=topic, participants=", ".join(participants)))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_snapshot(tp: TopicPaths) -> Path:
    snap = tp.snapshot / "minutes.snapshot.md"
    atomic_write(snap, tp.minutes.read_text(encoding="utf-8"))
    return snap


def parse_participants(tp: TopicPaths) -> list[str]:
    """frontmatter の `participants: [a, b]` 行を読む (stdlib のみの素朴 parse)。"""
    for line in tp.minutes.read_text(encoding="utf-8").splitlines():
        if line.startswith("participants:"):
            inner = line.split("[", 1)[1].rsplit("]", 1)[0]
            return [p.strip() for p in inner.split(",") if p.strip()]
    raise ValueError(f"participants 行が見つからない: {tp.minutes}")
