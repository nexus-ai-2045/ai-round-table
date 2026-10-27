from urllib.parse import parse_qs, urlsplit

import pytest

from roundtable.app_links import claude_new_link, claude_session_link


SESSION = "11111111-2222-4333-8444-abcdef123456"


def test_new_link_roundtrips_japanese_and_reserved_characters():
    prompt = "日本語で相談\n空白 & q=別の値? # + / % 🐈"
    link = claude_new_link(prompt)
    parts = urlsplit(link)
    assert (parts.scheme, parts.netloc, parts.path, parts.fragment) == (
        "claude", "code", "/new", "",
    )
    assert parse_qs(parts.query) == {"q": [prompt]}
    assert "%E6%97%A5" in link
    assert "%26" in link and "%23" in link and "%2B" in link


@pytest.mark.parametrize("folder", [
    "/srv/workspace/日本語 & q=other?# project", "/", "C:\\Workspaces\\My Project",
    "C:/Workspaces/日本語", "\\\\server\\share\\project",
])
def test_explicit_absolute_folder_is_encoded_as_one_parameter(folder):
    assert parse_qs(urlsplit(claude_new_link("相談", folder)).query) == {
        "q": ["相談"], "folder": [folder],
    }


def test_prompt_limit_preserves_every_character():
    prompt = "あ" * 14000
    assert parse_qs(urlsplit(claude_new_link(prompt)).query) == {"q": [prompt]}
    with pytest.raises(ValueError, match="14000"):
        claude_new_link(prompt + "い")


@pytest.mark.parametrize("prompt", ["", " \n\t", None, 123, "bad\x00prompt"])
def test_invalid_prompts_are_rejected(prompt):
    with pytest.raises(ValueError):
        claude_new_link(prompt)


@pytest.mark.parametrize("folder", [
    "", "relative/project", "~/project", "C:relative", "\\relative", 123,
    "file:///srv/workspace", "/tmp/null\x00", "/tmp/new\nline", "/tmp/\t",
    "/tmp/\x7f", "/tmp/\x85",
])
def test_invalid_folders_are_rejected(folder):
    with pytest.raises(ValueError):
        claude_new_link("相談", folder)


@pytest.mark.parametrize("session_id", ["local_" + SESSION, "local_" + SESSION.upper()])
def test_session_links_contain_only_the_canonical_uuid(session_id):
    assert claude_session_link(session_id) == f"claude://claude.ai/code/local_{SESSION}"


@pytest.mark.parametrize("session_id", [
    "", None, 123, SESSION, SESSION.upper(), "not-a-uuid", SESSION + "?q=inject", SESSION + "#fragment",
    SESSION + "/other", SESSION + "\x00", SESSION + "\n", SESSION + "\x7f",
    SESSION + "\x85", " " + SESSION, "{" + SESSION + "}",
    SESSION.replace("-", ""), "urn:uuid:" + SESSION,
    f"claude://claude.ai/code/local_{SESSION}", "local_local_" + SESSION,
])
def test_invalid_session_ids_are_rejected(session_id):
    with pytest.raises(ValueError):
        claude_session_link(session_id)
