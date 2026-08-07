"""dispatcher の状態ファイル (journal.json / seats.json) の改ざん検知。

**塞ぐ穴**: docs/review-backlog.md「未対応で残す穴 (v0.3)」— minutes.md は
snapshot 時 hash と照合して fail-closed にしているのに、journal.json と seats.json
には同等の保護が無かった。Tier1 の席は sandbox=workspace-write / cwd=議題ディレクトリ
で動くので、席自身がこの 2 ファイルを書ける。書ける以上「書かれたことを検知できる」
必要がある (DESIGN の思想: 防止でなく検知)。

**証跡の置き場**: 証跡は議題ディレクトリの **外** (`<root>/.integrity/<slug>/`) に置く。
対象と同じ場所に置くと、対象を書き換えられる相手が証跡も書き換えられ、照合が
成立しない。席の cwd は議題ディレクトリに限定してあるので、そこから外へ逃がす。

ただし **「席がそこへ届かない」は未検証の前提** である (2026-08-07 / レビュー H4)。
`workspace-write` の実効書込範囲を決めるのはサーバ側で、repo 内に裏取りが無い。
前提が破れた場合、この module の検知は丸ごと無効になる (席が本文と証跡を両方
書ける)。検証手順は `TopicPaths.integrity` の docstring と docs/review-backlog.md。

**中断した書き込みを改ざんと呼ばないための 2 段記録**: 本文と証跡は別ファイルなので、
その間でプロセスが死ぬと必ず不一致になる。それを改ざん扱いにすると、事故のたびに
議題が二度と開けなくなる (fail-closed の副作用が本来の失敗より重い)。そこで証跡には
「書き込み後の hash」と「書き込み前の hash (pending_from)」の 2 つを持たせ、
実物が前者なら正常、後者なら *書き込みが届かなかった* と判定して証跡を巻き戻す。
残る穴は「攻撃者が直前の内容へ戻す」ケースだけで、これは中断と区別できない。
隠さず明記しておく。

**呼び出し規約 (2026-08-07 / レビュー H1・H2 で追加)**: `verify_and_read` も
`write_verified` も、対象ファイルの `FileLock` を **保持したまま** 呼ぶこと。
読みだけだから安全、は成り立たない:

- `verify_and_read` は `pending_from` 一致時と証跡不在時に証跡を**書く**。ロック外の
  reader がこれをやると、writer の「予告 → 本文 → 確定」の隙間に割り込んで証跡を
  巻き戻し、その書き込みが writer の確定より後に着地する。誰も改ざんしていないのに
  以後ずっと `StateTamperedError` になる (fail-closed なので議題が開けなくなる)。
- ロック外の reader が対象を開いていると、writer 側の `os.replace` が Windows で
  WinError 5 になり dispatch ごと落ちる (repro3 で実測)。

ロック配下に寄せると、pending 窓は常にロックの内側なので reader からは観測不能になり、
巻き戻し経路そのものが競合しなくなる。読み書きを同じロックで直列化するのが
この 2 つを同時に閉じる最小の手当である。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .atomicio import atomic_write


class StateTamperedError(ValueError):
    """状態ファイルが dispatcher 以外に書き換えられた / 消された。

    ValueError を継承する: 呼び出し側の既存 `except ValueError` を壊さずに済み、
    かつ CEO には「壊れた入力」ではなく「改ざん検知」として提示できる。
    """


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_witness(witness: Path) -> dict | None:
    """証跡を読む。壊れていたら fail-closed (読めない証跡は無いより危険)。"""
    if not witness.exists():
        return None
    try:
        data = json.loads(witness.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise StateTamperedError(f"hash 証跡が読めない: {witness} ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("sha256"), str):
        raise StateTamperedError(f"hash 証跡の形式が不正: {witness}")
    return data


def _write_witness(
    witness: Path, sha: str, pending_from: str | None = None, adopted: bool = False
) -> None:
    witness.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        witness,
        json.dumps(
            {
                "sha256": sha,
                "pending_from": pending_from,
                "adopted": adopted,
                "at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=1,
        ),
    )


def verify_and_read(path: Path, witness: Path) -> bytes | None:
    """証跡と照合してから本文 bytes を返す。ファイルが無ければ None。

    **対象ファイルのロックを保持したまま呼ぶこと** (module docstring の呼び出し規約)。
    この関数は純粋な読み取りではなく、証跡の巻き戻し / baseline 採用で書き込む。

    照合した bytes をそのまま返すのが要点 (TOCTOU 対策)。「照合用に読む」
    「利用のために読み直す」を分けると、その隙間で差し替えられる。
    """
    rec = _read_witness(witness)
    if not path.exists():
        if rec is None:
            return None
        if rec.get("pending_from") is None and rec["sha256"]:
            raise StateTamperedError(
                f"{path} が消えている (hash 記録あり: {rec['sha256']})。証跡: {witness}"
            )
        return None  # 新規作成が中断した窓。記録ゼロが正しい状態
    raw = path.read_bytes()
    actual = digest(raw)
    if rec is None:
        # この機構より前から在る議題。観測していない過去は検証しようがない。
        # 現状を baseline として採用し、「観測ではなく採用」であることを証跡に残す。
        _write_witness(witness, actual, adopted=True)
        return raw
    if actual == rec["sha256"]:
        return raw
    if rec.get("pending_from") and actual == rec["pending_from"]:
        _write_witness(witness, actual)  # 書き込みが届いていない = 直前の内容が正
        return raw
    raise StateTamperedError(
        f"{path} が dispatcher 以外に書き換えられた (sha256 不一致)\n"
        f"  記録: {rec['sha256']}\n"
        f"  実物: {actual}\n"
        f"  証跡: {witness}\n"
        "  内容を確認し、正しいと判断したら証跡ファイルを削除して再実行する"
        " (現状が採用として再記録される)。"
    )


def write_verified(path: Path, text: str, witness: Path) -> None:
    """本文を atomic に書き、hash を証跡へ記録する。書き込みロックの中で呼ぶこと。

    順序は「予告 (pending) → 本文 → 確定」。どこで落ちても、次回の verify が
    改ざんと中断を区別できる状態しか残らない。
    """
    new = digest(text.encode("utf-8"))  # atomic_write は newline="\n" 固定 = 無変換
    rec = _read_witness(witness)
    prev = rec["sha256"] if rec else None
    _write_witness(witness, new, pending_from=prev)
    atomic_write(path, text)
    _write_witness(witness, new)
