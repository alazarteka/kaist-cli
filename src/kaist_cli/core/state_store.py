from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


@contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """
    Process-level advisory lock for file-backed state.

    Uses flock on Unix. The lock file is separate from the data file to keep
    read/write call sites straightforward.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        try:
            import fcntl
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Process file locking requires fcntl on this platform") from exc
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        handle.close()


def read_json_file(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return data if isinstance(data, dict) else default


def write_json_file_atomic(path: Path, data: dict[str, Any], *, chmod_mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp_path.write_text(text, encoding="utf-8")
    if chmod_mode is not None:
        try:
            os.chmod(tmp_path, chmod_mode)
        except PermissionError:
            pass
    os.replace(tmp_path, path)
    if chmod_mode is not None:
        try:
            os.chmod(path, chmod_mode)
        except PermissionError:
            pass


def update_json_file(
    path: Path,
    *,
    default: dict[str, Any],
    updater: Callable[[dict[str, Any]], dict[str, Any]],
    chmod_mode: int | None = None,
) -> dict[str, Any]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    with file_lock(lock_path):
        current = read_json_file(path, default=default)
        updated = updater(current)
        write_json_file_atomic(path, updated, chmod_mode=chmod_mode)
        return updated
