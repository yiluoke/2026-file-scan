"""C++／Qt専用の棚卸し。JSON・設定は参照または明示指定したものだけ収集する。"""

from __future__ import annotations

import fnmatch
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from file_scan.collector import _directory_is_safe, _entries_without_links

from .config import Settings, validate_settings
from .store import ScanError, read_bytes
from .text import decode, literals, masked

CPP_EXTENSIONS = {".c", ".cpp", ".cc", ".cxx", ".c++", ".h", ".hpp", ".hxx", ".hh",
                  ".h++", ".ipp", ".inl", ".tpp"}
CONFIG_EXTENSIONS = {".json", ".ini", ".xml", ".conf", ".cfg", ".yaml", ".yml"}
KINDS = {".ui": "widgets", ".qml": "qml", ".js": "javascript", ".qmltypes": "qmltypes",
         ".qrc": "resource", ".pro": "qmake_reference", ".pri": "qmake_reference",
         ".prf": "qmake_reference", ".md": "reference", ".markdown": "reference",
         ".txt": "reference", ".rst": "reference", ".qss": "reference"}
IGNORED_DIRS = {".git", ".svn", ".hg", ".codewiki", "node_modules", "__pycache__",
                ".venv", "build", "debug", "release", "dist", "cmakefiles", ".qmake.stash"}
TEST_DIRS = {"test", "tests", "testing", "autotests"}
THIRD_PARTY = {"third_party", "thirdparty", "3rdparty", "vendor", "external"}


@dataclass(frozen=True)
class SourceFile:
    relative_path: str
    path: Path
    kind: str


@dataclass
class Inventory:
    items: list[SourceFile] = field(default_factory=list)
    exclusions: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)


def kind_for(path: str) -> str | None:
    p = PurePosixPath(path)
    if p.suffix.lower() in CPP_EXTENSIONS:
        return "cpp"
    if p.name == "qmldir":
        return "qmldir"
    if p.name.casefold() in {"readme", "install", "authors", "changelog"}:
        return "reference"
    return KINDS.get(p.suffix.lower())


def collect(settings: Settings) -> Inventory:
    validate_settings(settings)
    result = Inventory()
    candidates, selected = {}, {}
    pending = [settings.source_root]
    while pending:
        directory = pending.pop()
        name = directory.relative_to(settings.source_root).as_posix()
        if not _directory_is_safe(directory, settings.source_root):
            result.errors.append({"path": name, "code": "unsafe_or_unavailable_directory"})
            continue
        try:
            entries = _entries_without_links(directory)
        except OSError:
            result.errors.append({"path": name, "code": "directory_enumeration_failed"})
            continue
        for filename, info, junction in entries:
            path = directory / filename
            relative = path.relative_to(settings.source_root).as_posix()
            reason = None
            if info is None:
                result.errors.append({"path": relative, "code": "file_stat_failed"})
                continue
            if stat.S_ISLNK(info.st_mode) or junction:
                reason = "symbolic_link"
            elif any(fnmatch.fnmatchcase(relative, p) or fnmatch.fnmatchcase(filename, p)
                     for p in settings.exclude_patterns):
                reason = "configured_exclusion"
            elif stat.S_ISDIR(info.st_mode):
                lowered = filename.casefold()
                if lowered in IGNORED_DIRS or lowered.startswith("build-"):
                    reason = "generated_or_dependency_directory"
                elif lowered in TEST_DIRS and not settings.include_tests:
                    reason = "test_directory"
                elif lowered in THIRD_PARTY and not settings.include_third_party:
                    reason = "third_party_directory"
                elif filename.startswith("."):
                    reason = "hidden_directory"
                else:
                    pending.append(path)
                    continue
            elif not stat.S_ISREG(info.st_mode):
                reason = "not_regular_file"
            elif filename.startswith(("ui_", "moc_", "qrc_")) and path.suffix.lower() in CPP_EXTENSIONS:
                reason = "qt_generated_source"
            elif filename.startswith("."):
                reason = "hidden_file"
            else:
                kind = kind_for(relative)
                if kind:
                    selected[relative] = SourceFile(relative, path, kind)
                elif path.suffix.lower() in CONFIG_EXTENSIONS:
                    candidates[relative] = path
                else:
                    reason = "unsupported_extension"
            if reason:
                result.exclusions.append({"path": relative, "reason": reason})
    # 設定の内容や値をログへ出さず、リテラルによる参照の存在だけを確認する。
    wanted = {p for p in candidates if any(fnmatch.fnmatchcase(p, pattern)
                                         for pattern in settings.include_files)}
    for item in selected.values():
        if item.kind not in {"cpp", "qml", "javascript"}:
            continue
        try:
            text, _ = decode(read_bytes(item.path, settings.max_file_bytes),
                             item.relative_path, settings)
            for _, value in literals(masked(text, strings=False)):
                if PurePosixPath(value).suffix.lower() not in CONFIG_EXTENSIONS:
                    continue
                normalized = (item.path.parent / value).resolve()
                if normalized.is_relative_to(settings.source_root):
                    relative = normalized.relative_to(settings.source_root).as_posix()
                    if relative in candidates:
                        wanted.add(relative)
                    elif value in candidates:
                        wanted.add(value)
            for ref in re.finditer(r'Q_PLUGIN_METADATA\s*\([^)]*\bFILE\s*"([^"]+)"',
                                   masked(text, strings=False)):
                referenced = item.path.parent / ref.group(1)
                if not referenced.is_file():
                    result.errors.append({"path": item.relative_path,
                                          "code": "plugin_metadata_missing"})
        except (OSError, ScanError, RuntimeError):
            # 本コピーでも再確認するが、棚卸しだけの実行でも障害を隠さない。
            result.errors.append({"path": item.relative_path, "code": "reference_read_failed"})
    for name, path in candidates.items():
        if name in wanted:
            selected[name] = SourceFile(name, path, "configuration")
        else:
            result.exclusions.append({"path": name, "reason": "unreferenced_configuration"})
    for pattern in settings.include_files:
        if not any(fnmatch.fnmatchcase(p, pattern) for p in selected):
            result.errors.append({"path": pattern, "code": "explicit_input_not_found"})
    if len(selected) > settings.max_files:
        raise ScanError("source_file_count_limit")
    result.items = [selected[p] for p in sorted(selected)]
    result.exclusions.sort(key=lambda item: item["path"])
    return result
