import json
import uuid

import pytest

from roundtable import app_actions
from roundtable.read_model import export_topic


@pytest.mark.parametrize("mode, expected", [
    ("反証", "再現可能な失敗例"), ("振り返り", "再利用できる学び"),
    ("先行実装調査", "GitHubの先行実装と公式資料"),
    ("技術判断の裏取り", "公式資料と論文・文献"),
])
def test_review_mode_uses_frozen_packet_and_existing_collection(tmp_path, mode, expected):
    root = tmp_path.resolve()
    slug = "test-review"
    result = app_actions.execute(root, slug, "new-topic", "--topic", "架空議題", "--participants", "codex")
    assert result.returncode == 0, result.stderr
    result = app_actions.prepare_request(root, slug, "codex", mode)
    assert result.returncode == 0, result.stderr
    record, = export_topic(root, slug)["invocations"]
    assert record["state"] == "prepared"
    inv = record["id"]
    frozen = app_actions.preview(root, slug, "codex", inv, str(uuid.uuid4()))
    assert expected in frozen.packet
    if mode in app_actions.RESEARCH_MODES:
        assert "原典URL" in frozen.packet
        assert "確認日" in frozen.packet
    assert "架空議題" in frozen.snapshot
    topic = root / "minutes" / slug
    (topic / "scratch" / f"{inv}.json").write_text(json.dumps({
        "invocation_id": inv, "participant": "codex", "opinion": "検証可能な架空の所見",
        "claims": [{"claim": "架空の主張", "evidence_type": "argument", "evidence": "根拠"}],
    }), encoding="utf-8")
    result = app_actions.execute(root, slug, "collect", "--invocation", inv, "--timeout", "0")
    assert result.returncode == 0, result.stdout + result.stderr
    result = app_actions.execute(root, slug, "close", "--verdict=保留。実機試験後に決める。")
    assert result.returncode == 0, result.stderr
    final = export_topic(root, slug)
    assert final["invocations"][0]["state"] == "merged"
    assert "保留。実機試験後に決める。" in final["minutes"]

    repeated = app_actions.execute(root, slug, "close", "--verdict=別の判断")
    assert repeated.returncode != 0
    assert export_topic(root, slug)["minutes"] == final["minutes"]


def test_chair_note_is_in_next_preview_without_closing(tmp_path):
    root = tmp_path.resolve()
    slug = "chair-loop"
    assert app_actions.execute(root, slug, "new-topic", "--topic", "架空議題",
                               "--participants", "codex").returncode == 0
    note = "先に最小構成を比較。\n## 次の論点は失敗時の復旧"
    assert app_actions.execute(root, slug, "chair-note", "--note=" + note).returncode == 0
    assert app_actions.prepare_request(root, slug, "codex", "反証").returncode == 0
    record, = export_topic(root, slug)["invocations"]
    frozen = app_actions.preview(root, slug, "codex", record["id"], str(uuid.uuid4()))
    assert "status: open" in frozen.snapshot
    assert "> ## 次の論点は失敗時の復旧" in frozen.snapshot
    assert app_actions.execute(root, slug, "close", "--verdict=採用").returncode == 0
    before = export_topic(root, slug)["minutes"]
    assert app_actions.execute(root, slug, "chair-note", "--note=閉会後").returncode != 0
    assert export_topic(root, slug)["minutes"] == before


def test_antigravity_body_keeps_every_line_of_a_multiline_assignment(tmp_path):
    root = tmp_path.resolve()
    slug = "test-antigravity"
    result = app_actions.execute(root, slug, "new-topic", "--topic", "架空議題", "--participants", "antigravity")
    assert result.returncode == 0, result.stderr
    question = "一行目の問い。\n二行目の制約を必ず守る。"
    result = app_actions.prepare_request(root, slug, "antigravity", "反証", question=question)
    assert result.returncode == 0, result.stderr
    inv = app_actions.prepared_invocation(result)
    frozen = app_actions.preview(root, slug, "antigravity", inv, app_actions.ANTIGRAVITY_MODEL)
    assert "二行目の制約を必ず守る。" in frozen.packet
    body = frozen.antigravity_body(root, slug)
    assert "一行目の問い。" in body
    assert "二行目の制約を必ず守る。" in body
    assert "[roundtable packet" not in body
    assert ".json.tmp" not in body
    assert body.endswith(frozen.snapshot)
