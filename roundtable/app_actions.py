"""Actions selected in the desktop app, backed by the existing roundtable ledger."""
from dataclasses import dataclass
from hashlib import sha256
import json
import shutil
import tempfile
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import UUID

from .local_read import read_local_file
from .paths import topic_dir


ANTIGRAVITY_MODEL = "gemini-4-argon-medium"

REVIEW_MODES = {
    "通常": "",
    "反証": " 既存案の弱い前提、再現可能な失敗例、影響と検証方法を独立に示してください。"
            "確認できた事実と推測を分け、対応・検討・記録・却下の候補と理由を示してください。",
    "振り返り": " 議事録の根拠を示し、判断、道具、見落とした代替案の観点で再利用できる学びを抽出してください。"
                "採用・却下・保留の候補を分け、テストや仕組みに落とせる項目を示してください。",
}

RESEARCH_MODES = {
    "先行実装調査": " GitHubの先行実装と公式資料を調べ、対象版、既存の仕組み、再利用条件と不足を示してください。",
    "技術判断の裏取り": " 公式資料と論文・文献で技術判断を検証し、反例、比較対象、適用範囲と限界を示してください。",
}
RESEARCH_EVIDENCE = (
    " 各主張の根拠には原典URL、公開・更新日（不明なら不明）、確認日、確認範囲を記録してください。"
    " 検索結果と原典で確認した事実を区別し、同じ原典の再引用を独立した裏取りとして数えず、未確認点も示してください。"
)


def prepare_request(root: Path, slug: str, participant: str, mode: str, *, question: str = "") -> subprocess.CompletedProcess:
    hint = (RESEARCH_MODES[mode] + RESEARCH_EVIDENCE
            if mode in RESEARCH_MODES else REVIEW_MODES[mode])
    if question.strip():
        hint += " 今回の依頼。 " + question.strip()
    return execute(root, slug, "dispatch", "--participant", participant, "--tier", "3",
                   "--no-clipboard", "--async", "--role-hint", hint)


def prepared_invocation(result: subprocess.CompletedProcess) -> str:
    if result.returncode:
        raise RuntimeError("依頼を準備できませんでした。議題の状態を確認してください。")
    matches = re.findall(r"^\[async\] invocation: ([a-f0-9]{12}) — ", result.stdout, re.MULTILINE)
    if len(matches) != 1:
        raise RuntimeError("依頼の準備結果を確認できませんでした。")
    return matches[0]


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

    def _topic_paths(self, root: Path, slug: str):
        from .paths import TopicPaths

        directory = topic_dir(root, slug)
        paths = TopicPaths(directory, directory / "minutes.md", directory / "journal.json",
                           directory / "scratch", directory / "snapshot")
        return paths, directory / "requests" / f"{self.invocation}.snapshot.md"

    def desktop_body(self, root: Path, slug: str) -> str:
        from .packet import inline_snapshot

        paths, snapshot_path = self._topic_paths(root, slug)
        return inline_snapshot(paths, "codex", self.invocation, self.packet, snapshot_path, self.snapshot)

    def reserve_desktop(self, root: Path, slug: str) -> str:
        from .journal import Journal
        from .paths import ensure_topic
        from .relay import load_seats
        from .watcher import _invocation_lock

        tp = ensure_topic(root, slug)
        with _invocation_lock(tp, self.invocation):
            journal = Journal.load(tp)
            record = journal.data["invocations"].get(self.invocation, {})
            seat = load_seats(tp).get(f"rt/{slug}/codex", {})
            if (self.provider != "codex" or seat.get("thread_ref") != self.target
                    or seat.get("transport") != "codex-desktop"
                    or record.get("participant") != "codex" or record.get("state") != "prepared"
                    or record.get("delivery_route") != "stdout-only" or "codex_send_claim" in record):
                raise ValueError("宛先または依頼の状態が変わりました")
            if preview(root, slug, "codex", self.invocation, self.target) != self:
                raise ValueError("プレビュー後に内容が変わりました")
            claim = {"thread_id": self.target, "packet_sha256": self.packet_hash,
                     "snapshot_sha256": self.snapshot_hash, "transport": "codex-desktop"}
            record["codex_send_claim"] = claim
            record["delivery_route"] = "relay"
            journal.save()
            if Journal.load(tp).data["invocations"][self.invocation].get("codex_send_claim") != claim:
                raise ValueError("送信依頼を保存できませんでした")
        return "Desktop連携側への送信依頼を保存しました。送信受付は別途確認します。"

    def antigravity_body(self, root: Path, slug: str) -> str:
        from .packet import captured_assignment

        paths, snapshot_path = self._topic_paths(root, slug)
        captured = captured_assignment(paths, "antigravity", self.invocation, self.packet, snapshot_path)
        assignment = captured.split("\n", 1)[1].rstrip("\n")
        return (assignment + "\n議事録を参考に回答してください。ファイル操作は行わず、回答をJSONで返してください。"
                "\n\n── 議事録の固定版 ──\n\n" + self.snapshot)

    def send_antigravity(self, root: Path, slug: str) -> str:
        from .atomicio import atomic_write
        from .journal import Journal
        from .paths import ensure_topic
        from .schema import validate_opinion
        from .watcher import _invocation_lock

        binary = shutil.which("agy")
        if binary is None:
            raise ValueError("Antigravity CLIが見つかりません")
        tp = ensure_topic(root, slug)
        inv = self.invocation
        output = tp.scratch / f"{inv}.json"
        with _invocation_lock(tp, inv):
            journal = Journal.load(tp)
            record = journal.data["invocations"].get(inv, {})
            if (record.get("state") != "prepared" or record.get("participant") != "antigravity"
                    or record.get("delivery_route") != "stdout-only" or "antigravity_send_claim" in record
                    or output.exists()):
                raise ValueError("準備済みの未送信依頼を選んでください")
            if preview(root, slug, self.provider, inv, self.target) != self:
                raise ValueError("プレビュー後に内容が変わりました")
            record["antigravity_send_claim"] = {"model": self.target, "packet_sha256": self.packet_hash,
                                               "snapshot_sha256": self.snapshot_hash}
            record["delivery_route"] = "relay"
            journal.save()
            claim_fields = {"claim": {"type": "string"}, "evidence_type": {"type": "string"},
                            "evidence": {"type": "string"}}
            schema = {
                "type": "object", "required": ["opinion", "claims"],
                "properties": {"opinion": {"type": "string"}, "claims": {
                    "type": "array", "items": {"type": "object", "properties": claim_fields,
                                               "required": list(claim_fields)}}},
            }
            try:
                with tempfile.TemporaryDirectory(prefix="roundtable-agy-") as work:
                    result = subprocess.run(
                        [binary, "--mode", "plan", "--sandbox", "--disable-slash-commands",
                         "--model", self.target, "--output-format", "json", "--json-schema",
                         json.dumps(schema), "--print", self.antigravity_body(root, slug)],
                        cwd=work, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                        timeout=300, check=False,
                    )
                envelope = json.loads(result.stdout)
                if result.returncode or envelope.get("status") != "SUCCESS":
                    raise ValueError("公式CLIの認証、利用枠、または実行状態を確認してください")
                answer = envelope.get("structured_output")
                if not isinstance(answer, dict):
                    raise ValueError("構造化された回答がありません")
                answer = {**answer, "invocation_id": inv, "participant": "antigravity"}
                if validate_opinion(answer):
                    raise ValueError("回答の形式を確認できません")
                atomic_write(output, json.dumps(answer, ensure_ascii=False))
                journal.set_state(inv, "delivered", "antigravity-response")
            except (OSError, subprocess.SubprocessError, ValueError, AttributeError) as exc:
                journal.set_state(inv, "delivery-unknown", "Antigravityの結果を確認できません")
                raise ValueError("Antigravityの結果を確認できません。公式CLIの状態を確認してください。") from exc
        return report(execute(root, slug, "collect", "--invocation", inv, "--timeout", "0"))

    def send(self, root: Path, slug: str) -> str:
        if self.provider == "antigravity":
            return self.send_antigravity(root, slug)
        if self.provider == "codex":
            from .paths import ensure_topic
            from .relay import load_seats

            seat = load_seats(ensure_topic(root, slug)).get(f"rt/{slug}/codex", {})
            if seat.get("transport") == "codex-queue":
                from .codex_queue import send

                return send(self, root, slug)
            if seat.get("transport") == "codex-desktop":
                return self.reserve_desktop(root, slug)
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
    if provider not in ("codex", "claude", "antigravity") or not re.fullmatch(r"[A-Za-z0-9_-]+", invocation):
        raise ValueError("席または依頼IDが不正です")
    if provider == "antigravity":
        if target != ANTIGRAVITY_MODEL:
            raise ValueError("Antigravityのモデルが一致しません")
    else:
        target = str(UUID(target))
    directory = topic_dir(root, slug) / "requests"
    packet = read_local_file(directory / f"{invocation}.md", 1024 * 1024)
    snapshot = read_local_file(directory / f"{invocation}.snapshot.md", 8 * 1024 * 1024)
    return Preview(provider, invocation, target, packet.decode("utf-8"), snapshot.decode("utf-8"),
                   sha256(packet).hexdigest(), sha256(snapshot).hexdigest())


def report(result: subprocess.CompletedProcess) -> str:
    return f"終了コード {result.returncode}\n{(result.stdout + result.stderr)[-2400:]}"
