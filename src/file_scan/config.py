"""収集設定の読み込みと、二重収集・自己取り込みを防ぐための検証。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


class ConfigError(ValueError):
    """設定値そのものを表示せずに通知できる設定エラー。"""


DEFAULT_EXTENSIONS = (
    ".docx", ".xlsx", ".xlsm", ".pdf", ".txt", ".md", ".py",
    ".sql", ".json", ".yaml", ".yml", ".csv", ".log",
)


@dataclass(frozen=True)
class Source:
    source_id: str
    root: Path
    recursive: bool = True
    exclude_directories: tuple[str, ...] = ()
    exclude_filenames: tuple[str, ...] = ()


@dataclass
class Settings:
    sources: list[Source] = field(default_factory=list)
    knowledge_id: str = "default"
    system_id: str = "unspecified"
    environment: str = "unspecified"
    # repr(settings)や通常のログに接続文字列を出さない。
    database_url: str = field(default="sqlite:///var/knowledge.sqlite3", repr=False)
    database_url_env: str = "FILE_SCAN_DATABASE_URL"
    storage_dir: Path = field(default_factory=lambda: Path("var/storage"))
    report_dir: Path = field(default_factory=lambda: Path("var/reports"))
    include_extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    max_file_bytes: int = 100 * 1024 * 1024
    copy_retries: int = 2
    pdf_backend: str = "native"
    include_hidden: bool = False
    chunk_max_characters: int = 1800
    parse_timeout_seconds: int = 120


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError(f"{field_name}には空でない文字列が必要です")
    return value


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name}にはtrueまたはfalseが必要です")
    return value


def _integer(value: Any, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{field_name}には{minimum}以上の整数が必要です")
    return value


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{field_name}には文字列の配列が必要です")
    return tuple(_string(item, field_name) for item in value)


def _path(value: Any, base: Path, field_name: str) -> Path:
    text = _string(value, field_name)
    # UNIX上でUNCを相対パスと誤認して、空の収集に見せかけない。
    if text.startswith(("\\\\", "//")) and os.name != "nt":
        raise ConfigError(f"{field_name}のUNCパスはWindowsでのみ使用できます。マウント先を設定してください")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ConfigError(f"{field_name}にシンボリックリンクは指定できません")
    return path.resolve()


def _directory_exclusions(values: tuple[str, ...]) -> tuple[str, ...]:
    result = []
    for value in values:
        # ディレクトリ除外はglobではなく、ルート相対パスのprefixで扱う。
        path = PurePosixPath(value.rstrip("/"))
        if ("\\" in value or path.is_absolute() or not path.parts
                or any(part in (".", "..") for part in path.parts)
                or any(char in value for char in "*?[]")):
            raise ConfigError("exclude_directoriesには/区切りのルート相対ディレクトリを指定してください")
        result.append(path.as_posix())
    return tuple(result)


def _database_url(value: str, base: Path) -> str:
    """SQLiteの相対パスだけを設定ファイル基準にする。認証情報は表示しない。"""
    match = re.match(r"^(sqlite(?:\+[a-zA-Z0-9_]+)?:///)(.*)$", value)
    if match:
        location, separator, query = match.group(2).partition("?")
        if location == ":memory:":
            return value
        if not location:
            raise ConfigError("database_urlのSQLite保存先がありません")
        path = Path(location).expanduser()
        if not path.is_absolute():
            path = base / path
        return match.group(1) + path.resolve().as_posix() + (separator + query if separator else "")
    if "://" not in value:
        raise ConfigError("database_urlの形式が不正です")
    return value


def validate_settings(settings: Settings) -> None:
    """直接生成されたSettingsでも、危険な収集範囲を実行前に拒否する。"""
    _integer(settings.chunk_max_characters, "chunk_max_characters", 100)
    ids: set[str] = set()
    roots: list[Path] = []
    for source in settings.sources:
        if source.source_id in ids:
            raise ConfigError("source_idが重複しています")
        ids.add(source.source_id)
        if source.root.is_symlink() or getattr(source.root, "is_junction", lambda: False)():
            raise ConfigError("収集ルートにシンボリックリンクは指定できません")
        root = source.root.resolve()
        for previous in roots:
            if root == previous or root.is_relative_to(previous) or previous.is_relative_to(root):
                raise ConfigError("収集ルートが重複、または親子関係になっています")
        roots.append(root)
        for output in (settings.storage_dir, settings.report_dir):
            if output.resolve().is_relative_to(root):
                raise ConfigError("storage_dirとreport_dirは収集ルートの外に設定してください")


def load_config(path: str | Path) -> Settings:
    config_path = Path(path).expanduser().resolve()
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except (OSError, UnicodeError, yaml.YAMLError):
        # YAMLエラーの原文には設定行や接続情報が含まれ得るため転記しない。
        raise ConfigError("設定ファイルを読み込めません。文字コードとYAML形式を確認してください") from None
    if not isinstance(raw, dict):
        raise ConfigError("設定ファイルの最上位はマッピングにしてください")
    base = config_path.parent
    source_values = raw.get("sources")
    if not isinstance(source_values, list) or not source_values:
        raise ConfigError("sourcesには1件以上の収集元が必要です")
    sources = []
    for value in source_values:
        if not isinstance(value, dict):
            raise ConfigError("sourcesの各要素はマッピングにしてください")
        source_id = _string(value.get("source_id"), "source_id")
        if any(char in source_id for char in "/\\\r\n"):
            raise ConfigError("source_idにパス区切りや改行は使用できません")
        filenames = _strings(value.get("exclude_filenames", []), "exclude_filenames")
        if any("/" in name or "\\" in name for name in filenames):
            raise ConfigError("exclude_filenamesはディレクトリを含まないファイル名globにしてください")
        sources.append(Source(
            source_id=source_id,
            root=_path(value.get("root"), base, "root"),
            recursive=_boolean(value.get("recursive", True), "recursive"),
            exclude_directories=_directory_exclusions(_strings(value.get("exclude_directories", []), "exclude_directories")),
            exclude_filenames=filenames,
        ))
    extensions = _strings(raw.get("include_extensions", list(DEFAULT_EXTENSIONS)), "include_extensions")
    if any(not extension.startswith(".") or len(extension) < 2
           or any(char in extension for char in "/\\*?[]") for extension in extensions):
        raise ConfigError("include_extensionsにはドットから始まる拡張子を指定してください")
    env_name = _string(raw.get("database_url_env", "FILE_SCAN_DATABASE_URL"), "database_url_env")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise ConfigError("database_url_envの環境変数名が不正です")
    database = (os.environ.get("FILE_SCAN_DATABASE_URL") or os.environ.get(env_name)
                or raw.get("database_url", "sqlite:///var/knowledge.sqlite3"))
    settings = Settings(
        sources=sources,
        knowledge_id=_string(raw.get("knowledge_id", "default"), "knowledge_id"),
        system_id=_string(raw.get("system_id", "unspecified"), "system_id"),
        environment=_string(raw.get("environment", "unspecified"), "environment"),
        database_url=_database_url(_string(database, "database_url"), base),
        database_url_env=env_name,
        storage_dir=_path(raw.get("storage_dir", "var/storage"), base, "storage_dir"),
        report_dir=_path(raw.get("report_dir", "var/reports"), base, "report_dir"),
        include_extensions=tuple(dict.fromkeys(extension.lower() for extension in extensions)),
        max_file_bytes=_integer(raw.get("max_file_bytes", 100 * 1024 * 1024), "max_file_bytes", 1),
        copy_retries=_integer(raw.get("copy_retries", 2), "copy_retries", 0),
        pdf_backend=_string(raw.get("pdf_backend", "native"), "pdf_backend"),
        include_hidden=_boolean(raw.get("include_hidden", False), "include_hidden"),
        chunk_max_characters=_integer(raw.get("chunk_max_characters", 1800), "chunk_max_characters", 100),
        parse_timeout_seconds=_integer(raw.get("parse_timeout_seconds", 120), "parse_timeout_seconds", 1),
    )
    if settings.pdf_backend not in ("native", "docling"):
        raise ConfigError("pdf_backendはnativeまたはdoclingを指定してください")
    validate_settings(settings)
    return settings
