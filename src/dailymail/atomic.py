"""One atomic JSON writer, used everywhere DailyMail publishes a file.

Extracted verbatim in behaviour from `collect.write_artifact`, which has written
the daily collection artifact in production since Phase 1: `mkstemp` in the
destination directory, serialize, `os.replace`. A single implementation matters
more than it looks. The status snapshot is read by ControlPanel's collector on a
timer while DailyMail may be writing it, so "a reader never sees half a file" is
a property of this function, and two subtly different copies of it would mean
that property held in one place and not the other.

`os.replace` is atomic within a filesystem, which is why the temporary file is
created in the destination's own directory rather than in `/tmp`.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class UnsafePathError(OSError):
    """The destination is not a plain file we are willing to replace."""


def _check_destination(path: Path) -> None:
    """Refuse a destination that is a symlink or anything but a regular file.

    A status file is written by a scheduled job and read by another service, so
    the one thing it must never do is follow a link somebody else planted into
    a directory this process can write.
    """
    if path.is_symlink():
        raise UnsafePathError(f"refusing to write through a symlink: {path}")
    if path.exists() and not path.is_file():
        raise UnsafePathError(f"refusing to replace a non-regular file: {path}")
    parent = path.parent
    if parent.is_symlink():
        raise UnsafePathError(f"refusing to write into a symlinked directory: {parent}")


def atomic_write_json(
    path: Path,
    document: dict,
    *,
    mode: int | None = None,
    indent: int | None = 1,
    fsync: bool = False,
    sort_keys: bool = True,
) -> Path:
    """Serialize `document` and put it at `path` atomically.

    A reader either sees the previous file or the new one, never a partial
    write, and an interrupted call leaves no debris behind.

    `mode` sets the final permissions; `fsync` additionally forces the bytes to
    disk before the rename, which is what a crash-safety-critical file wants and
    what a large collection artifact does not need to pay for.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_destination(path)

    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(
                document,
                stream,
                ensure_ascii=False,
                indent=indent,
                sort_keys=sort_keys,
            )
            stream.write("\n")
            if fsync:
                stream.flush()
                os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return path
