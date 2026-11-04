"""Bounded reads of user-selected local files."""
import os
from contextlib import ExitStack
from pathlib import Path
import stat


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _regular_path(path: Path) -> os.stat_result:
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("symlink")
        if part == path:
            file_info = info
    if not stat.S_ISREG(file_info.st_mode):
        raise ValueError("not-regular-file")
    return file_info


def read_local_file(path: Path, limit: int) -> bytes:
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError("invalid-path")
    before = _regular_path(path)
    if before.st_size > limit:
        raise ValueError("file-too-large")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    with ExitStack() as cleanup:
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open(path.anchor, dir_flags)
            cleanup.callback(os.close, parent)
            for name in path.parts[1:-1]:
                parent = os.open(name, dir_flags, dir_fd=parent)
                cleanup.callback(os.close, parent)
            fd = os.open(path.name, flags | os.O_NOFOLLOW, dir_fd=parent)
        else:
            fd = os.open(path, flags)
        stream = cleanup.enter_context(os.fdopen(fd, "rb"))
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise ValueError("source-changed")
        if _identity(_regular_path(path)) != _identity(opened):
            raise ValueError("source-changed")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file-too-large")
        if _identity(os.fstat(fd)) != _identity(opened) or _identity(_regular_path(path)) != _identity(opened):
            raise ValueError("source-changed")
    return data
