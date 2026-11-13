import json
import uuid

import pytest

from roundtable import app_actions
from roundtable.read_model import export_topic


@pytest.mark.parametrize("mode, expected", [("反証", "再現可能な失敗例"), ("振り返り", "再利用できる学び")])
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
