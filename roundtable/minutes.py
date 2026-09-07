"""議事録の生成・snapshot・hash・atomic write・merge。

書き込みはすべて tmp → os.replace の atomic 経路 (`atomicio.atomic_write`)。
Windows の一時ロック (エディタ/AV の共有違反) に備えて短い retry を持つ。
merge は hash 照合 fail-closed + escape による予約見出し防御を担う (DESIGN v6 D6/D7)。

**保護の単位の変遷** (詳細は git 履歴と DESIGN D12):

1. snapshot hash (`base_hash` 引数) — 並行 dispatch で 2 席目が必ず
   `failed: tampered` になる欠陥 (2026-08-07 実測)。正常運用が警報を出し続け、
   本物の改ざんを無視する訓練になっていた。
2. witness 証跡 (`.integrity/<slug>/*.sha256`) — 「席は証跡に届かない」前提が
   grok 席で実測破綻 (PowerShell 任意実行)。届く相手には照合が成立しない。
3. **git (現行, D12)** — dispatcher の書込は commit で履歴になり、外部の書込は
   次操作の clean 検査で dirty として検知される。検知の実体は `roundtable.ledger`。
   他 dispatcher の正当な追記は commit 済みなので clean のまま = 警報を出さない
   (1 の欠陥を再発させない)。裁くのは CEO が読む `git diff` (人間判断がメイン)。
"""
import hashlib
import json
import re
from pathlib import Path

from . import ledger
from .atomicio import atomic_write
from .filelock import FileLock
from .paths import TopicPaths

__all__ = [
    "TEMPLATE",
    "MinutesTamperedError",
    "atomic_write",
    "create",
    "set_background",
    "sha256",
    "make_snapshot",
    "parse_participants",
    "merge_opinion",
    "write_verdict",
    "sync_round",
]

_MINUTES_NAME = "minutes.md"

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


class MinutesTamperedError(ledger.LedgerDirtyError):
    """minutes.md が dispatcher 外で変更された (working tree が dirty)。fail-closed の根拠。

    `LedgerDirtyError` を継承する: merge 経路は watcher が個別に捕まえて
    `failed: tampered` に分類するが、`sync_round` / `write_verdict` /
    `set_background` のような CLI 直呼び経路では CLI の改ざんハンドラ (exit 3) に
    落ちてほしい。継承していないと、これらの経路だけ CEO に traceback が出る。
    """


def _lock(tp: TopicPaths) -> FileLock:
    """minutes.md の read-modify-write を不可分にするロック。

    議題ディレクトリの外 (`<root>/.locks/<slug>/`) に置く (paths.TopicPaths.lock)。
    保持は読み直し〜追記の数ミリ秒だけで、席のターン (最大 900 秒) は握らない。
    """
    return FileLock(tp.lock(_MINUTES_NAME))


def _read_verified(tp: TopicPaths) -> str:
    """clean 検査してから本文を返す (ロック配下で呼ぶこと)。"""
    try:
        raw = ledger.read_state(tp.minutes)
    except ledger.LedgerDirtyError as exc:
        # 呼び出し側の分類 (watcher の failed:tampered) を維持するため型を寄せる
        raise MinutesTamperedError(str(exc)) from exc
    if raw is None:
        # 「一度も作られていない」= new-topic 前の議題。改ざん扱いにすると
        # `dispatch` の打ち間違いが exit 3 (改ざん検知) になる。
        # (commit 済み minutes.md の消去は read_state が dirty として検知する)
        raise FileNotFoundError(f"minutes.md が無い: {tp.minutes}")
    return raw.decode("utf-8")


def _write_verified(tp: TopicPaths, text: str, op: str) -> None:
    """本文を atomic に書き、commit する (ロック配下で呼ぶこと)。

    dispatcher の minutes.md 書き込みは **必ずここを通す**。1 経路でも素の
    atomic_write が残ると、そこを通った書込が commit されず、次操作の clean 検査で
    「誰も改ざんしていないのに dirty」になる (偽陽性の自己生成)。
    """
    ledger.write_state(tp.minutes, text, f"minutes({tp.root.name}): {op}")


def create(tp: TopicPaths, topic: str, participants: list[str], background: str = "") -> None:
    body = TEMPLATE.format(topic=topic, participants=", ".join(participants))
    if background:
        body = body.rstrip() + "\n" + background.rstrip() + "\n"
    with _lock(tp):
        _write_verified(tp, body, "create")


def set_background(tp: TopicPaths, background: str) -> None:
    """## 背景 セクション直後に本文を書く (S1: python 直書きを不要にする)。"""
    with _lock(tp):
        text = _read_verified(tp)
        marker = "## 背景\n"
        if marker not in text:
            raise ValueError("minutes に ## 背景 見出しが無い")
        # 次の ## 見出しまでを差し替える (無ければ末尾)
        head, rest = text.split(marker, 1)
        next_h = re.search(r"^## ", rest, re.MULTILINE)
        if next_h:
            rest_after = rest[next_h.start() :]
            new_text = head + marker + "\n" + background.rstrip() + "\n\n" + rest_after
        else:
            new_text = head + marker + "\n" + background.rstrip() + "\n"
        _write_verified(tp, new_text, "set-background")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_snapshot(tp: TopicPaths) -> Path:
    """席に渡す読み取り用スナップショットを作る。

    snapshot 自体は議題に 1 本しかなく、dispatch のたびに上書きされる (v0.3 の穴)。
    merge の改ざん判定には **使わない** (この module の docstring 参照) ので、
    並行 dispatch で上書きされても正しい意見が捨てられることはない。
    """
    with _lock(tp):
        text = _read_verified(tp)
    snap = tp.snapshot / "minutes.snapshot.md"
    atomic_write(snap, text)
    return snap


def parse_participants(tp: TopicPaths) -> list[str]:
    """frontmatter の `participants: [a, b]` 行を読む (stdlib のみの素朴 parse)。"""
    with _lock(tp):
        text = _read_verified(tp)
    for line in text.splitlines():
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


class OpinionConflictError(ValueError):
    """同じ invocation に別の内容、または検証不能な旧 receipt が存在する。"""


def opinion_hash(opinion: dict) -> str:
    """採用回答の正規化 JSON SHA256。空白/BOM と意味上の変更を区別する。"""
    return hashlib.sha256(json.dumps(opinion, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def has_response(tp: TopicPaths, inv_id: str, participant: str, digest: str) -> bool:
    """journal 更新直前に中断した merge の receipt を clean な議事録から読む。"""
    with _lock(tp):
        text = _read_verified(tp)
        heading = f"### {participant} (invocation: {inv_id})"
        return bool(re.search(r"^" + re.escape(heading) + r"\n"
                              + re.escape(f"<!-- roundtable-response-sha256: {digest} -->")
                              + r"$", text, re.MULTILINE))


def merge_opinion(tp: TopicPaths, opinion: dict, round_no: int) -> None:
    """検証済み意見を議事録へ追記する。

    ロックを取り、証跡と照合してから、**照合した bytes そのもの**に追記する
    (TOCTOU 対策: 照合用の read と利用のための read を分けると隙間で差し替えられる)。
    他 dispatcher が先に追記していても、それが dispatcher 経由である限り証跡と
    一致するので tamper にならない — 並行 dispatch で 2 席目が必ず落ちていた
    旧 base_hash 方式との違いはここ (module docstring / レビュー H3)。
    """
    with _lock(tp):
        text = _read_verified(tp)
        heading = f"### {opinion['participant']} (invocation: {opinion['invocation_id']})"
        receipt = f"<!-- roundtable-response-sha256: {opinion_hash(opinion)} -->"
        existing = re.search(r"^### .* \(invocation: " + re.escape(opinion["invocation_id"])
                             + r"\)$", text, re.MULTILINE)
        if existing:
            # receipt は見出し直後の機械生成行。本文から注入した一致は認めない。
            tail = text[existing.end():]
            if existing.group() == heading and tail.startswith("\n" + receipt + "\n"):
                return
            raise OpinionConflictError("invocation already exists with a different or legacy receipt")
        # Round 見出しの存在判定は行頭完全一致で行う。部分文字列だと escape 済み本文中の
        # 「\## Round N」に誤反応し、敵対 opinion が本物の見出し生成を抑止できる (レビュー M2)。
        round_heading_exists = re.search(rf"^## Round {round_no}$", text, re.MULTILINE)
        section = [
            None if round_heading_exists else f"\n## Round {round_no}",
            f"\n{heading}",
            receipt,
            "",
            _escape_body(opinion["opinion"]),
            "",
            "| claim | evidence_type | evidence |",
            "|---|---|---|",
        ]
        for c in opinion["claims"]:
            section.append(
                f"| {_escape_cell(c['claim'])} | {c['evidence_type']} | "
                f"{_escape_cell(c['evidence'])} |"
            )
        addition = "\n".join(s for s in section if s is not None) + "\n"
        insert_at = len(text)
        if round_heading_exists:
            next_heading = re.search(r"^## ", text[round_heading_exists.end():], re.MULTILINE)
            if next_heading:
                insert_at = round_heading_exists.end() + next_heading.start()
        _write_verified(tp, text[:insert_at] + addition + text[insert_at:],
                        f"merge round {round_no} {opinion['participant']}")


def write_verdict(tp: TopicPaths, verdict: str) -> None:
    """CEO の裁定を記録して close。裁定はこの機械経路からのみ書かれる (偽装防止)。"""
    with _lock(tp):
        text = _read_verified(tp)
        text = text.replace("status: open", "status: closed", 1)
        text = text.replace("verdict:", f"verdict: {verdict}", 1)
        _write_verified(tp, text, "verdict")


def sync_round(tp: TopicPaths, round_no: int) -> None:
    """frontmatter の round: 表示を journal と同期 (CEO が議事録を直接見るため)。"""
    with _lock(tp):
        text = _read_verified(tp)
        _write_verified(
            tp,
            re.sub(r"^round: \d+$", f"round: {round_no}", text, count=1, flags=re.MULTILINE),
            f"sync round {round_no}",
        )
