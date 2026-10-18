"""原本の安定したスナップショットと、内容ハッシュ単位の原子的保存。"""

from __future__ import annotations

import errno
import hashlib
import mimetypes
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .collector import ScanItem


class StorageError(RuntimeError):
    code = "storage_error"


class SourceChangedError(StorageError):
    code = "source_changed_during_copy"


class UnsafePathError(StorageError):
    code = "unsafe_symbolic_link"


class FileTooLargeError(StorageError):
    code = "file_too_large"


class AssetIntegrityError(StorageError):
    code = "stored_asset_integrity_failed"


class StoredAssetMissingError(StorageError):
    code = "stored_asset_missing"


@dataclass(frozen=True)
class StoredAsset:
    asset_id: str
    storage_key: str
    mime_type: str
    byte_size: int
    path: Path
    source_modified_at: datetime | None = None


def _signature(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _assert_no_links(path: Path) -> None:
    # resolve()でリンクをたどる前に、経路上の差し替えを検査する。
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
        ):
            raise UnsafePathError("シンボリックリンク経由の読み書きを拒否しました")


def _open_no_follow(path: Path) -> BinaryIO:
    """POSIXでは各ディレクトリをdir_fdで固定してリンク差し替えを防ぐ。"""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if os.name != "nt" and os.open in os.supports_dir_fd:
        directory_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | nofollow)
        try:
            for component in path.parts[1:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            descriptor = os.open(path.name, flags | nofollow, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
    else:
        _assert_no_links(path)
        descriptor = os.open(path, flags | nofollow)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise UnsafePathError("通常ファイル以外の読み込みを拒否しました")
    return os.fdopen(descriptor, "rb")


class Storage:
    def __init__(self, root: str | Path):
        requested = Path(root).expanduser().absolute()
        if requested.is_symlink() or getattr(requested, "is_junction", lambda: False)():
            raise UnsafePathError("保管先にシンボリックリンクは指定できません")
        self.root = requested.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._temporary = self.root / ".tmp"
        self._temporary.mkdir(exist_ok=True)
        _assert_no_links(self._temporary)

    def _copy_stream(self, source: BinaryIO, target: BinaryIO, max_file_bytes: int) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_file_bytes:
                raise FileTooLargeError("原本が設定されたサイズ上限を超えています")
            target.write(chunk)
            digest.update(chunk)
        return digest.hexdigest(), size

    def _verify_existing(self, path: Path, digest: str, size: int) -> None:
        try:
            _assert_no_links(path)
            with _open_no_follow(path) as source:
                before = os.fstat(source.fileno())
                if before.st_size != size:
                    raise AssetIntegrityError("保存済みオブジェクトのサイズが一致しません")
                actual = hashlib.sha256()
                actual_size = 0
                while True:
                    data = source.read(1024 * 1024)
                    if not data:
                        break
                    actual_size += len(data)
                    if actual_size > size:
                        raise AssetIntegrityError("検証中に保存済みオブジェクトの変更を検出しました")
                    actual.update(data)
                after = os.fstat(source.fileno())
                if (_signature(before) != _signature(after) or after.st_size != size
                        or actual.hexdigest() != digest or _signature(path.lstat()) != _signature(after)):
                    raise AssetIntegrityError("保存済みオブジェクトのハッシュが一致しません")
        except UnsafePathError:
            raise
        except FileNotFoundError:
            raise StoredAssetMissingError("保存済みオブジェクトが見つかりません") from None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise UnsafePathError("保存済みオブジェクトの経路変更を検出しました") from None
            raise AssetIntegrityError("保存済みオブジェクトを検証できません") from None

    def verify(self, asset_id: str, storage_key: str, byte_size: int) -> StoredAsset:
        """再利用前にDBの実体参照を検証する。欠落物の作成・修復は行わない。

        拡張子や原本の型はハッシュから分からないため、戻り値のMIMEは
        application/octet-streamとする。既存のDB側MIMEは変更しない。
        """
        if (not isinstance(asset_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset_id)
                or isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0):
            raise AssetIntegrityError("保存済みオブジェクトの参照情報が不正です")
        digest = asset_id.split(":", 1)[1]
        key = (Path("objects") / "sha256" / digest[:2] / digest).as_posix()
        # DB値をそのままパス結合せず、IDから導出した保管規則と照合する。
        if not isinstance(storage_key, str) or storage_key != key:
            raise AssetIntegrityError("保存済みオブジェクトの保存先参照が一致しません")
        destination = self.root / key
        self._verify_existing(destination, digest, byte_size)
        return StoredAsset(asset_id, key, "application/octet-stream", byte_size, destination)

    def _install(self, temporary: Path, digest: str, size: int, mime_type: str,
                 modified_at: datetime | None = None) -> StoredAsset:
        key = Path("objects") / "sha256" / digest[:2] / digest
        destination = self.root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        _assert_no_links(destination.parent)
        try:
            # hard linkは既存オブジェクトを上書きせず、完成したファイルを一度に公開する。
            os.link(temporary, destination)
        except FileExistsError:
            # サイズだけを信用せず、再利用するたびに内容ハッシュを検査する。
            self._verify_existing(destination, digest, size)
        return StoredAsset(
            asset_id="sha256:" + digest, storage_key=key.as_posix(),
            mime_type=mime_type, byte_size=size, path=destination,
            source_modified_at=modified_at,
        )

    def put_bytes(self, data: bytes, mime_type: str) -> StoredAsset:
        if not isinstance(data, bytes):
            raise TypeError("保存データにはbytesを指定してください")
        temporary = None
        try:
            _assert_no_links(self._temporary)
            with tempfile.NamedTemporaryFile(dir=self._temporary, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            return self._install(temporary, hashlib.sha256(data).hexdigest(), len(data), mime_type)
        except OSError:
            raise StorageError("オブジェクトの保存に失敗しました") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _snapshot_once(self, item: ScanItem, max_file_bytes: int) -> StoredAsset:
        temporary = None
        path = item.path.absolute()
        try:
            _assert_no_links(path)
            before_path = path.lstat()
            if not stat.S_ISREG(before_path.st_mode):
                raise UnsafePathError("通常ファイル以外の原本は取得できません")
            if before_path.st_size > max_file_bytes:
                raise FileTooLargeError("原本が設定されたサイズ上限を超えています")
            with _open_no_follow(path) as source:
                before = os.fstat(source.fileno())
                if _signature(before_path) != _signature(before):
                    raise SourceChangedError("取得開始時に原本の変更を検出しました")
                _assert_no_links(self._temporary)
                with tempfile.NamedTemporaryFile(dir=self._temporary, delete=False) as target:
                    temporary = Path(target.name)
                    digest, size = self._copy_stream(source, target, max_file_bytes)
                    target.flush()
                    os.fsync(target.fileno())
                after = os.fstat(source.fileno())
                try:
                    _assert_no_links(path)
                    after_path = path.lstat()
                except FileNotFoundError:
                    raise SourceChangedError("コピー中に原本の移動または削除を検出しました") from None
                if (_signature(before) != _signature(after) or _signature(after) != _signature(after_path)
                        or size != after.st_size):
                    raise SourceChangedError("コピー中に原本の変更を検出しました")
            mime_type = mimetypes.guess_type(item.path.name)[0] or "application/octet-stream"
            modified_at = datetime.fromtimestamp(after.st_mtime, tz=timezone.utc)
            return self._install(temporary, digest, size, mime_type, modified_at)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise UnsafePathError("原本の経路が変更されたため取得を拒否しました") from None
            raise StorageError("原本の読み取りまたは保存に失敗しました") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def snapshot(self, item: ScanItem, max_file_bytes: int, retries: int) -> StoredAsset:
        if isinstance(max_file_bytes, bool) or max_file_bytes < 1 or isinstance(retries, bool) or retries < 0:
            raise ValueError("サイズ上限と再試行回数が不正です")
        for attempt in range(retries + 1):
            try:
                return self._snapshot_once(item, max_file_bytes)
            except SourceChangedError:
                if attempt == retries:
                    raise SourceChangedError("原本の変更が継続したため取得を保留しました") from None
        raise AssertionError("unreachable")
