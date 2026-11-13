"""Actions selected in the desktop app, backed by the existing roundtable ledger."""
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import UUID

from .local_read import read_local_file
from .paths import topic_dir


REVIEW_MODES = {
    "通常": "",
    "反証": " 既存案の弱い前提、再現可能な失敗例、影響と検証方法を独立に示してください。"
            "確認できた事実と推測を分け、対応・検討・記録・却下の候補と理由を示してください。",
    "振り返り": " 議事録の根拠を示し、判断、道具、見落とした代替案の観点で再利用できる学びを抽出してください。"
                "採用・却下・保留の候補を分け、テストや仕組みに落とせる項目を示してください。",
}


def prepare_request(root: Path, slug: str, participant: str, mode: str) -> subprocess.CompletedProcess:
    return execute(root, slug, "dispatch", "--participant", participant, "--tier", "3",
                   "--no-clipboard", "--async", "--role-hint", REVIEW_MODES[mode])


def command(root: Path, slug: str, action: str, *options: str) -> list[str]:
    return [sys.executable, "-B", "-m", "roundtable.cli", action, slug, "--root", str(root), *options]


def execute(root: Path, slug: str, action: str, *options: str) -> subprocess.CompletedProcess:
    return subprocess.run(command(root, slug, action, *options), capture_output=True,
                          encoding="utf-8", errors="replace", check=False,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})


@dataclass(frozen=True)
class Preview:
    provider: str
    invocation: str
    target: str
    packet: str
    snapshot: str
    packet_hash: str
    snapshot_hash: str

    def send(self, root: Path, slug: str) -> str:
        if self.provider == "codex":
            result = execute(root, slug, "codex-send", "--invocation", self.invocation,
                             "--thread-id", self.target, "--packet-sha256", self.packet_hash,
                             "--snapshot-sha256", self.snapshot_hash, "--timeout", "300")
            return report(result)
        if self.provider != "claude":
            raise ValueError("非対応の送信先です")
        from . import handoff
        from .paths import ensure_topic

        tp = ensure_topic(root, slug)
        prepared = handoff.prepare(tp, self.invocation, transport="claude-socket",
                                   claude_session=self.target)
        if not prepared.get("ok"):
            return f"送信準備不可 ({prepared.get('reason', prepared.get('state', 'unknown'))})"
        result = handoff.deliver(tp, self.invocation, expected_preview=(
            self.target, self.packet_hash, self.snapshot_hash,
        ))
        return f"搬送記録 ({result.get('state', result.get('reason', 'unknown'))})。回答は別途回収してください。"


def preview(root: Path, slug: str, provider: str, invocation: str, target: str) -> Preview:
    if provider not in ("codex", "claude") or not re.fullmatch(r"[A-Za-z0-9_-]+", invocation):
        raise ValueError("席または依頼IDが不正です")
    target = str(UUID(target))
    directory = topic_dir(root, slug) / "requests"
    packet = read_local_file(directory / f"{invocation}.md", 1024 * 1024)
    snapshot = read_local_file(directory / f"{invocation}.snapshot.md", 8 * 1024 * 1024)
    return Preview(provider, invocation, target, packet.decode("utf-8"), snapshot.decode("utf-8"),
                   sha256(packet).hexdigest(), sha256(snapshot).hexdigest())


def report(result: subprocess.CompletedProcess) -> str:
    return f"終了コード {result.returncode}\n{(result.stdout + result.stderr)[-2400:]}"
