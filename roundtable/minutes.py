"""議事録の生成・snapshot・hash・atomic write・merge。

書き込みはすべて tmp → os.replace の atomic 経路 (`atomicio.atomic_write`)。
Windows の一時ロック (エディタ/AV の共有違反) に備えて短い retry を持つ。
merge は hash 照合 fail-closed + escape による予約見出し防御を担う (DESIGN v6 D6/D7)。

**保護の単位を snapshot hash から証跡 + ロックへ移した** (2026-08-07 / レビュー H3):

旧実装は `merge_opinion(..., base_hash)` に *dispatch 開始時の snapshot hash* を渡し、
merge 直前の minutes.md がそれと一致しなければ `MinutesTamperedError` にしていた。
これは並行 dispatch で必ず壊れる: 先に merge した席が minutes.md を伸ばすので、
後続席の base_hash は**正常な運用でも**必ず古くなる。実測 (repro.py R2) では
2 席目が毎回 `failed: tampered` になり、正しく書かれた意見が捨てられていた。
しかも分類が `tampered` = セキュリティ警報なので、正常な並行追記のたびに警報が出て
**本物の改ざんを無視する訓練**になる。fail-closed の意味が消える。

そこで journal.json / seats.json と同じ機構に揃えた:

- minutes.md にも `<root>/.integrity/<slug>/minutes.md.sha256` の証跡を持たせる。
- dispatcher の書き込みはすべて `integrity.write_verified` を通り、証跡を更新する。
- 読み書きは `FileLock(<...>/minutes.md.lock)` 配下で行い、その場で読み直してから
  追記する。他 dispatcher の正当な追記は証跡と一致するので tamper にならず、
  dispatcher 以外の書き換えだけが検知される。

「その場で読み直した hash で照合したら常に一致して無意味では」という指摘 (内部レビュー #2)
は、照合先が *自分が今読んだ値* だった旧設計への指摘である。ここでの照合先は
**別ファイルに記録された、dispatcher の書き込みでしか更新されない証跡**なので、
席が minutes.md を書き換えれば時点によらず検知できる。むしろ「snapshot 採取から
merge までの窓」に限定されていた旧方式より検知範囲は広い。
"""
import hashlib
import re
from pathlib import Path

from . import integrity
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


class MinutesTamperedError(integrity.StateTamperedError):
    """minutes.md が dispatcher 外で変更された (証跡と不一致)。fail-closed の根拠。

    `StateTamperedError` を継承する: merge 経路は watcher が個別に捕まえて
    `failed: tampered` に分類するが、`sync_round` / `write_verdict` /
    `set_background` のような CLI 直呼び経路では CLI の改ざんハンドラ (exit 3) に
    落ちてほしい。継承していないと、これらの経路だけ CEO に traceback が出る。
    """


def _lock(tp: TopicPaths) -> FileLock:
    """minutes.md の read-modify-write を不可分にするロック。

    証跡と同じく議題ディレクトリの外 (`<root>/.integrity/<slug>/`) に置く。
    保持は読み直し〜追記の数ミリ秒だけで、席のターン (最大 900 秒) は握らない。
    """
    return FileLock(tp.lock(_MINUTES_NAME))


def _read_verified(tp: TopicPaths) -> str:
    """証跡と照合してから本文を返す (ロック配下で呼ぶこと)。"""
    try:
        raw = integrity.verify_and_read(tp.minutes, tp.witness(_MINUTES_NAME))
    except integrity.StateTamperedError as exc:
        # 呼び出し側の分類 (watcher の failed:tampered) を維持するため型を寄せる
        raise MinutesTamperedError(str(exc)) from exc
    if raw is None:
        # 「一度も作られていない」窓。消去された場合は verify_and_read が
        # StateTamperedError にするので、ここに来るのは new-topic 前の議題だけ。
        # 改ざん扱いにすると `dispatch` の打ち間違いが exit 3 (改ざん検知) になる。
        raise FileNotFoundError(f"minutes.md が無い: {tp.minutes}")
    return raw.decode("utf-8")


def _write_verified(tp: TopicPaths, text: str) -> None:
    """本文を atomic に書き、証跡を更新する (ロック配下で呼ぶこと)。

    dispatcher の minutes.md 書き込みは **必ずここを通す**。1 経路でも素の
    atomic_write が残ると、そこを通った直後に証跡が古いまま残り、次の merge が
    「誰も改ざんしていないのに tampered」になる (偽陽性の自己生成)。
    """
    integrity.write_verified(tp.minutes, text, tp.witness(_MINUTES_NAME))


def create(tp: TopicPaths, topic: str, participants: list[str], background: str = "") -> None:
    body = TEMPLATE.format(topic=topic, participants=", ".join(participants))
    if background:
        body = body.rstrip() + "\n" + background.rstrip() + "\n"
    with _lock(tp):
        _write_verified(tp, body)


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
        _write_verified(tp, new_text)


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
                f"| {_escape_cell(c['claim'])} | {c['evidence_type']} | "
                f"{_escape_cell(c['evidence'])} |"
            )
        _write_verified(tp, text + "\n".join(s for s in section if s is not None) + "\n")


def write_verdict(tp: TopicPaths, verdict: str) -> None:
    """CEO の裁定を記録して close。裁定はこの機械経路からのみ書かれる (偽装防止)。"""
    with _lock(tp):
        text = _read_verified(tp)
        text = text.replace("status: open", "status: closed", 1)
        text = text.replace("verdict:", f"verdict: {verdict}", 1)
        _write_verified(tp, text)


def sync_round(tp: TopicPaths, round_no: int) -> None:
    """frontmatter の round: 表示を journal と同期 (CEO が議事録を直接見るため)。"""
    with _lock(tp):
        text = _read_verified(tp)
        _write_verified(
            tp,
            re.sub(r"^round: \d+$", f"round: {round_no}", text, count=1, flags=re.MULTILINE),
        )
