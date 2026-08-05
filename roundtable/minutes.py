"""議事録の生成・snapshot・hash・atomic write・merge。

書き込みはすべて tmp → os.replace の atomic 経路 (部分書き込みを構造的に排除)。
Windows の一時ロック (エディタ/AV の共有違反 = WinError 32) に備えて短い retry を持つ。
merge は hash 照合 fail-closed + escape による予約見出し防御を担う (DESIGN v6 D6/D7)。
"""
import hashlib
import os
import re
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


class MinutesTamperedError(Exception):
    """merge 前 hash 照合に失敗 (議事録が dispatcher 外で変更された)。fail-closed の根拠。"""


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


def _escape_body(body: str) -> str:
    """本文用 escape: 見出しに化けうる行をすべて無効化する (予約見出し防御, レビュー M1)。

    対象: ATX 見出し (#) / frontmatter・setext H2 (---) / setext H1 (= のみの行) /
    コードフェンス (``` ~~~ — 未閉フェンスは以降の描画を乗っ取る) / 引用 (> — 引用内見出し)。
    """
    out = []
    for line in body.splitlines():
        s = line.strip()
        head = line.lstrip()
        if (
            head.startswith("#")
            or s == "---"
            or (s != "" and set(s) == {"="})
            or head.startswith("```")
            or head.startswith("~~~")
            or head.startswith(">")
        ):
            line = "\\" + line
        out.append(line)
    return "\n".join(out)


def _escape_cell(value: str) -> str:
    """表セル用: | を \\| に、改行を <br> に。改行→行頭# の見出し注入を構造的に不能にする。"""
    return value.replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def merge_opinion(tp: TopicPaths, opinion: dict, round_no: int, base_hash: str) -> None:
    """検証済み意見を議事録へ追記する。

    base_hash は snapshot 採取時のものを渡すこと。その場で再計算した hash を
    渡すと照合が常に一致し、改ざん検知が無力化する (内部レビュー #2 の罠)。
    """
    # check/use を単一 read に統一 (TOCTOU 防止): 照合した bytes と同じ bytes から本文を得る。
    # 別々に read すると「照合時は原本・読取時は改ざん版」を踏むレースが成立する (レビュー H1)。
    raw = tp.minutes.read_bytes()
    if hashlib.sha256(raw).hexdigest() != base_hash:
        raise MinutesTamperedError(f"minutes hash mismatch: {tp.minutes}")
    text = raw.decode("utf-8")
    # Round 見出しの存在判定は行頭完全一致で行う。部分文字列だと escape 済み本文中の
    # 「\## Round N」に誤反応し、敵対 opinion が本物の見出し生成を抑止できる (レビュー M2)。
    round_heading_exists = re.search(rf"^## Round {round_no}$", text, re.MULTILINE)
    section = [
        None if round_heading_exists else f"\n## Round {round_no}",
        f"\n### {opinion['participant']} (invocation: {opinion['invocation_id']})",
        "",
        _escape_body(opinion["opinion"]),
        "",
        "| claim | evidence_type | evidence |",
        "|---|---|---|",
    ]
    for c in opinion["claims"]:
        section.append(
            f"| {_escape_cell(c['claim'])} | {c['evidence_type']} | {_escape_cell(c['evidence'])} |"
        )
    atomic_write(tp.minutes, text + "\n".join(s for s in section if s is not None) + "\n")


def write_verdict(tp: TopicPaths, verdict: str) -> None:
    """CEO の裁定を記録して close。裁定はこの機械経路からのみ書かれる (偽装防止)。"""
    text = tp.minutes.read_text(encoding="utf-8")
    text = text.replace("status: open", "status: closed", 1)
    text = text.replace("verdict:", f"verdict: {verdict}", 1)
    atomic_write(tp.minutes, text)


def sync_round(tp: TopicPaths, round_no: int) -> None:
    """frontmatter の round: 表示を journal と同期 (CEO が議事録を直接見るため)。"""
    text = tp.minutes.read_text(encoding="utf-8")
    atomic_write(
        tp.minutes,
        re.sub(r"^round: \d+$", f"round: {round_no}", text, count=1, flags=re.MULTILINE),
    )
