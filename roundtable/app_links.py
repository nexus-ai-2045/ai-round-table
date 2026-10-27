from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlencode
from uuid import UUID


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)


def claude_new_link(prompt: str, folder: str | None = None) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if len(prompt) > 14000:
        raise ValueError("prompt exceeds 14000 characters")
    if "\x00" in prompt:
        raise ValueError("prompt must not contain null characters")
    params = {"q": prompt}
    if folder is not None:
        if not isinstance(folder, str) or not folder or _has_control(folder):
            raise ValueError("folder must be an absolute path without control characters")
        if not (PurePosixPath(folder).is_absolute() or PureWindowsPath(folder).is_absolute()):
            raise ValueError("folder must be an absolute path")
        params["folder"] = folder
    return "claude://code/new?" + urlencode(params)


def claude_session_link(session_id: str) -> str:
    if not isinstance(session_id, str) or _has_control(session_id) or not session_id.startswith("local_"):
        raise ValueError("session_id must be a Desktop local_<UUID>, not a CLI UUID")
    value = session_id.removeprefix("local_")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("session_id must be a Desktop local_<UUID>") from exc
    if str(parsed) != value.lower():
        raise ValueError("session_id must use the hyphenated UUID format")
    return f"claude://claude.ai/code/local_{parsed}"
