"""議題 packet の生成とクリップボード搬出 (Tier3)。

dispatcher は AI を実行しない。参加者への依頼文 (packet) を作って
クリップボードに載せるところまでが役目で、貼り付け先の chat 操作は
人間 (CEO) が行う。
"""
import math
import platform
import subprocess
from pathlib import Path

from .paths import TopicPaths

_TEMPLATE = """[roundtable packet / invocation: {inv}]
あなたは roundtable の参加者 ({participant}) です。{role_hint}
1. 議事録スナップショットを読む: {snapshot}
2. 意見を JSON で書く。まず一時ファイル {out}.tmp に書き、完成後 {out} にリネームする。
   形式: {{"invocation_id": "{inv}", "participant": "{participant}",
          "opinion": "本文 (markdown 可)",
          "claims": [{{"claim": "主張", "evidence_type": "observed|log|diff|source|argument|none",
                      "evidence": "根拠"}}]}}
   claims は 1 件以上必須。「同意します」だけの応答は無効として却下されます。
3. 議事録本体や他のファイルは変更しないこと。"""


def build(tp: TopicPaths, participant: str, inv_id: str, role_hint: str = "",
          *, snapshot_path: Path | None = None) -> str:
    """参加者に渡す packet 文字列を組み立てる (副作用なし)。"""
    out = (tp.scratch / f"{inv_id}.json").resolve()
    snap = (snapshot_path if snapshot_path is not None else tp.snapshot / "minutes.snapshot.md").resolve()
    return _TEMPLATE.format(
        inv=inv_id, participant=participant, role_hint=role_hint, snapshot=snap, out=out
    )


def clipboard_command() -> tuple[str, str, str]:
    """搬出と事前確認で共有するOS別のコマンド・文字コード・接頭文字。"""
    system = platform.system()
    if system == "Darwin":
        return "pbcopy", "utf-8", ""
    if system == "Windows":
        return "clip.exe", "utf-16-le", chr(0xFEFF)
    raise NotImplementedError(f"Clipboard export is not supported on {system}")


def to_clipboard(text: str, timeout_s: float | None = None) -> None:
    """macOS は pbcopy へ UTF-8、Windows は clip.exe へ UTF-16LE + BOM。

    BOM (\\xff\\xfe) 必須: clip.exe はパイプ入力のエンコーディング判定を
    先頭バイト列に依存するため、BOM が無いと日本語が文字化けする。
    その他の OS は未対応として例外を送出し、実行失敗も呼び出し元へ伝える。
    """
    if timeout_s is not None and (not math.isfinite(timeout_s) or timeout_s <= 0):
        raise ValueError("timeout must be positive and finite")
    command, encoding, prefix = clipboard_command()
    data = (prefix + text).encode(encoding)

    subprocess.run(
        [command],
        input=data,
        check=True,
        shell=False,
        **({"timeout": timeout_s} if timeout_s is not None else {}),
    )
