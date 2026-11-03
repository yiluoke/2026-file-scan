"""版の保存、成果物検証、プロジェクト排他。DBや外部APIに依存しない。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from file_scan.storage import _assert_no_links, _open_no_follow

from .config import relative_path


class ScanError(RuntimeError):
    """固定コードのみを外へ返す工程エラー。"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def bytes_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path: Path, limit: int = 100 * 1024 * 1024) -> bytes:
    _assert_no_links(path)
    with _open_no_follow(path) as handle:
        result = handle.read(limit + 1)
    if len(result) > limit:
        raise ScanError("artifact_size_limit")
    return result


def read_json(path: Path):
    try:
        return json.loads(read_bytes(path).decode("utf-8"))
    except (ValueError, UnicodeError, OSError):
        raise ScanError("invalid_or_missing_manifest") from None


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_links(path.parent)
    if path.exists() or path.is_symlink():
        _assert_no_links(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path: Path, value) -> None:
    write_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())


def version_path(root: Path, kind: str, identifier: str) -> Path:
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-z]+-[0-9a-f]{32,64}", identifier):
        raise ScanError("invalid_version_id")
    path = root / kind / identifier
    if path.exists():
        _assert_no_links(path)
    return path


def file_manifest(root: Path) -> list[dict]:
    result = []
    for path in sorted(root.rglob("*")):
        _assert_no_links(path)
        if path.is_file():
            data = read_bytes(path)
            result.append({"path": path.relative_to(root).as_posix(),
                           "sha256": bytes_hash(data), "size": len(data)})
    return result


def verify_files(root: Path, files: list[dict], *, exact: bool = True) -> None:
    expected = set()
    for item in files:
        name = relative_path(item["path"])
        if name in expected:
            raise ScanError("duplicate_artifact_path")
        expected.add(name)
        data = read_bytes(root / name)
        if bytes_hash(data) != item["sha256"] or len(data) != item["size"]:
            raise ScanError("artifact_integrity_failed")
    if exact and {f["path"] for f in file_manifest(root)} != expected:
        raise ScanError("artifact_file_set_changed")


@contextmanager
def project_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    _assert_no_links(root)
    path = root / ".cpp-scan.lock"
    if path.exists() or path.is_symlink():
        _assert_no_links(path)
    with path.open("a+b") as handle:
        locked = False
        try:
            try:
                if os.name == "nt":
                    import msvcrt
                    if path.stat().st_size == 0:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                raise ScanError("project_already_running") from None
            yield
        finally:
            if locked:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
