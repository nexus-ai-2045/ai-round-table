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


@pytest.mark.parametrize("suffix", ["", ".tmp"])
def test_antigravity_send_refuses_when_an_answer_is_already_present(tmp_path, monkeypatch, suffix):
    from roundtable.journal import Journal
    from roundtable.paths import ensure_topic

    root = tmp_path.resolve()
    slug = "test-antigravity-present"
    result = app_actions.execute(root, slug, "new-topic", "--topic", "架空議題", "--participants", "antigravity")
    assert result.returncode == 0, result.stderr
    result = app_actions.prepare_request(root, slug, "antigravity", "反証", question="問い")
    assert result.returncode == 0, result.stderr
    inv = app_actions.prepared_invocation(result)
    frozen = app_actions.preview(root, slug, "antigravity", inv, app_actions.ANTIGRAVITY_MODEL)
    tp = ensure_topic(root, slug)
    (tp.scratch / f"{inv}.json{suffix}").write_text("{}", encoding="utf-8")
    fake_cli = str(tmp_path / "fake-agy")
    monkeypatch.setattr(app_actions.shutil, "which", lambda name: fake_cli)
    real_run = app_actions.subprocess.run

    def guarded_run(args, *rest, **kwargs):
        if args and args[0] == fake_cli:
            raise AssertionError("must not call the CLI")
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(app_actions.subprocess, "run", guarded_run)
    with pytest.raises(ValueError):
        frozen.send_antigravity(root, slug)
    record = Journal.load(tp).data["invocations"][inv]
    assert "antigravity_send_claim" not in record
    assert record["delivery_route"] == "stdout-only"


def test_collection_plan_collects_a_manually_delivered_prepared_codex_request(tmp_path):
    root = tmp_path.resolve()
    slug = "manual-codex"
    assert app_actions.execute(root, slug, "new-topic", "--topic", "架空議題", "--participants", "codex").returncode == 0
    inv = app_actions.prepared_invocation(app_actions.prepare_request(root, slug, "codex", "反証"))
    record = {"id": inv, "participant": "codex", "state": "prepared"}
    assert app_actions.collection_plan(root, slug, record)[0] == "blocked"
    for suffix in ("", ".tmp"):
        answer = root / "minutes" / slug / "scratch" / f"{inv}.json{suffix}"
        answer.write_text("{}", encoding="utf-8")
        kind, args = app_actions.collection_plan(root, slug, record)
        assert kind == "execute"
        assert args == ("collect", "--invocation", inv, "--timeout", "0")
        answer.unlink()


def test_collection_plan_routes_claude_and_queued_codex(tmp_path):
    root = tmp_path.resolve()
    slug = "routes"
    assert app_actions.execute(root, slug, "new-topic", "--topic", "架空議題", "--participants", "codex").returncode == 0
    inv = app_actions.prepared_invocation(app_actions.prepare_request(root, slug, "codex", "反証"))
    from roundtable.journal import Journal
    from roundtable.paths import ensure_topic

    tp = ensure_topic(root, slug)
    journal = Journal.load(tp)
    journal.data["invocations"][inv]["codex_queue_claim"] = {"thread_id": "t"}
    journal.save()
    assert app_actions.collection_plan(root, slug, {"id": inv, "participant": "codex", "state": "delivered"}) == ("recover", None)
    assert app_actions.collection_plan(root, slug, {"id": "abc", "participant": "claude", "state": "delivered"}) == (
        "execute", ("socket-collect", "--invocation", "abc"))


@pytest.mark.parametrize("suffix", ["", ".tmp"])
def test_desktop_reservation_refuses_when_an_answer_already_exists(tmp_path, suffix):
    from roundtable.journal import Journal
    from roundtable.paths import ensure_topic
    from roundtable.relay import save_seats

    root = tmp_path.resolve()
    slug = "desktop-answered"
    target = str(uuid.uuid4())
    assert app_actions.execute(root, slug, "new-topic", "--topic", "架空議題", "--participants", "codex").returncode == 0
    inv = app_actions.prepared_invocation(app_actions.prepare_request(root, slug, "codex", "反証"))
    tp = ensure_topic(root, slug)
    save_seats(tp, {f"rt/{slug}/codex": {"participant": "codex", "thread_ref": target, "transport": "codex-desktop"}})
    frozen = app_actions.preview(root, slug, "codex", inv, target)
    (tp.scratch / f"{inv}.json{suffix}").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        frozen.reserve_desktop(root, slug)
    record = Journal.load(tp).data["invocations"][inv]
    assert "codex_send_claim" not in record
    assert record["delivery_route"] == "stdout-only"


def test_topic_paths_has_answer_sees_final_and_temporary_output(tmp_path):
    from roundtable.paths import ensure_topic

    tp = ensure_topic(tmp_path, "answers")
    assert not tp.has_answer("abc")
    (tp.scratch / "abc.json.tmp").write_text("{}", encoding="utf-8")
    assert tp.has_answer("abc")
    (tp.scratch / "abc.json.tmp").unlink()
    (tp.scratch / "abc.json").write_text("{}", encoding="utf-8")
    assert tp.has_answer("abc")
    assert not tp.has_answer("other")
