"""原本を変更せず、許可されたルートだけを棚卸しする。"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings, Source, validate_settings


@dataclass(frozen=True)
class ScanItem:
    source_id: str
    relative_path: str
    path: Path
    source_uri: str
    document_id: str
    format: str

    @property
    def source_key(self) -> str:
        return self.relative_path


@dataclass
class ScanResult:
    items: list[ScanItem] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    exclusions: list[dict] = field(default_factory=list)
    successful_sources: list[str] = field(default_factory=list)


def _error(result: ScanResult, source: Source, relative_path: str, code: str) -> None:
    # OS例外の文字列はファイル名以外の業務情報を含み得るため保存しない。
    result.errors.append({
        "source_id": source.source_id, "relative_path": relative_path,
        "error_code": code, "message": "収集元の列挙またはファイル情報の取得に失敗しました",
    })


def _excluded(result: ScanResult, source: Source, relative_path: str, reason: str) -> None:
    result.exclusions.append({
        "source_id": source.source_id, "relative_path": relative_path, "reason": reason,
    })


def _excluded_directory(relative_path: str, exclusions: tuple[str, ...]) -> bool:
    normalized = relative_path.casefold()
    return any(normalized == value.rstrip("/").casefold()
               or normalized.startswith(value.rstrip("/").casefold() + "/") for value in exclusions)


def _directory_is_safe(directory: Path, root: Path) -> bool:
    """探索直前にも確認し、列挙後にリンクへ差し替えられたパスへ入らない。"""
    try:
        current = root
        if not stat.S_ISDIR(current.lstat().st_mode):
            return False
        for component in directory.relative_to(root).parts:
            current = current / component
            if not stat.S_ISDIR(current.lstat().st_mode):
                return False
        return True
    except (OSError, ValueError):
        return False


def _entries_without_links(directory: Path) -> list[tuple[str, os.stat_result | None, bool]]:
    """POSIXでは探索対象をfdに固定し、確認と列挙の間のリンク差し替えも拒否する。"""
    descriptor = None
    if os.name != "nt" and os.open in os.supports_dir_fd:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(directory.anchor, flags)
        try:
            for component in directory.parts[1:]:
                next_fd = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_fd
        except BaseException:
            os.close(descriptor)
            raise
    try:
        with os.scandir(descriptor if descriptor is not None else directory) as iterator:
            entries = []
            for entry in iterator:
                try:
                    info = entry.stat(follow_symlinks=False)
                    junction = getattr(entry, "is_junction", lambda: False)()
                except OSError:
                    info, junction = None, False
                entries.append((entry.name, info, junction))
        return sorted(entries, key=lambda entry: (entry[0].casefold(), entry[0]))
    finally:
        if descriptor is not None:
            os.close(descriptor)


def collect(settings: Settings) -> ScanResult:
    validate_settings(settings)
    result = ScanResult()
    extensions = {extension.lower() for extension in settings.include_extensions}
    for source in settings.sources:
        error_count = len(result.errors)
        excluded_directory_names = {name.casefold() for name in source.exclude_directory_names}
        root = source.root.absolute()
        pending = [root]
        while pending:
            directory = pending.pop()
            relative_directory = directory.relative_to(root).as_posix()
            if not _directory_is_safe(directory, root):
                _error(result, source, relative_directory, "unsafe_or_unavailable_directory")
                continue
            try:
                entries = _entries_without_links(directory)
            except OSError:
                _error(result, source, relative_directory, "directory_enumeration_failed")
                continue
            for name, info, junction in entries:
                path = directory / name
                relative_path = path.relative_to(root).as_posix()
                if info is None:
                    _error(result, source, relative_path, "file_stat_failed")
                    continue
                if stat.S_ISLNK(info.st_mode) or junction:
                    _excluded(result, source, relative_path, "symbolic_link")
                    continue
                hidden = name.startswith(".") or bool(
                    getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2)
                )
                if hidden and not settings.include_hidden:
                    _excluded(result, source, relative_path, "hidden")
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if (name.casefold() in excluded_directory_names
                            or _excluded_directory(relative_path, source.exclude_directories)):
                        _excluded(result, source, relative_path, "excluded_directory")
                    elif source.recursive:
                        pending.append(path)
                    else:
                        _excluded(result, source, relative_path, "non_recursive")
                    continue
                if not stat.S_ISREG(info.st_mode):
                    _excluded(result, source, relative_path, "not_regular_file")
                    continue
                if any(fnmatch.fnmatchcase(name.casefold(), pattern.casefold())
                       for pattern in source.exclude_filenames):
                    _excluded(result, source, relative_path, "excluded_filename")
                    continue
                if path.suffix.lower() not in extensions:
                    _excluded(result, source, relative_path, "unsupported_extension")
                    continue
                # 内容の同一性とは別の、所在に対する安定した論理ID。
                identity = (source.source_id + "\x00" + relative_path).encode("utf-8")
                result.items.append(ScanItem(
                    source_id=source.source_id, relative_path=relative_path,
                    path=path, source_uri=path.absolute().as_uri(),
                    document_id="sha256:" + hashlib.sha256(identity).hexdigest(),
                    format=path.suffix.lower().lstrip("."),
                ))
        # 接続不能や途中失敗は削除の根拠にしない。成功sourceを別途返す。
        if len(result.errors) == error_count:
            result.successful_sources.append(source.source_id)
    result.items.sort(key=lambda item: (item.source_id, item.relative_path))
    return result
