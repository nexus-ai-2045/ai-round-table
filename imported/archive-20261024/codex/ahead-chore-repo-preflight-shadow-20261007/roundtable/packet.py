"""議題 packet の生成とクリップボード搬出 (Tier3)。

dispatcher は AI を実行しない。参加者への依頼文 (packet) を作って
クリップボードに載せるところまでが役目で、貼り付け先の chat 操作は
人間 (CEO) が行う。
"""
import subprocess

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


def build(tp: TopicPaths, participant: str, inv_id: str, role_hint: str = "") -> str:
    """参加者に渡す packet 文字列を組み立てる (副作用なし)。"""
    out = tp.scratch / f"{inv_id}.json"
    snap = tp.snapshot / "minutes.snapshot.md"
    return _TEMPLATE.format(
        inv=inv_id, participant=participant, role_hint=role_hint, snapshot=snap, out=out
    )


def to_clipboard(text: str) -> None:
    """clip.exe へ UTF-16LE + BOM で流し込む。

    BOM (\\xff\\xfe) 必須: clip.exe はパイプ入力のエンコーディング判定を
    先頭バイト列に依存するため、BOM が無いと日本語が文字化けする。
    """
    subprocess.run(
        ["clip.exe"],
        input=(chr(0xFEFF) + text).encode("utf-16-le"),
        check=True,
        shell=False,
    )
