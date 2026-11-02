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
    ".docx", ".xlsx", ".xlsm", ".pdf", ".txt", ".md",
)
SUPPORTED_EXTENSIONS = frozenset((*DEFAULT_EXTENSIONS, ".markdown"))
DEFAULT_DATABASE_URL = "postgresql+psycopg://file_scan@127.0.0.1:5432/file_scan"


@dataclass(frozen=True)
class Source:
    source_id: str
    root: Path
    recursive: bool = True
    exclude_directories: tuple[str, ...] = ()
    exclude_filenames: tuple[str, ...] = ()


@dataclass
class EmbeddingSettings:
    enabled: bool = False
    model: str = "text-embedding-3-small"
    dimensions: int = 1536
    api_key: str = field(default="", repr=False)
    api_key_env: str = "OPENAI_API_KEY"
    batch_size: int = 16
    read_batch_size: int = 128
    batch_max_tokens: int = 262112
    timeout_seconds: int = 60


@dataclass(frozen=True)
class PdfOcrSettings:
    """日本語・ONNX Runtime CPU固定。autoはDoclingによる必要領域の選択。"""

    enabled: bool = True
    mode: str = "auto"
    scale: float = 3.0
    timeout_seconds: int = 1800


@dataclass
class Settings:
    sources: list[Source] = field(default_factory=list)
    knowledge_id: str = "default"
    system_id: str = "unspecified"
    environment: str = "unspecified"
    # repr(settings)や通常のログに接続文字列を出さない。
    database_url: str = field(default=DEFAULT_DATABASE_URL, repr=False)
    database_url_env: str = "FILE_SCAN_DATABASE_URL"
    storage_dir: Path = field(default_factory=lambda: Path("var/storage"))
    report_dir: Path = field(default_factory=lambda: Path("var/reports"))
    include_extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    max_file_bytes: int = 100 * 1024 * 1024
    copy_retries: int = 2
    pdf_backend: str = "docling"
    docling_artifacts_path: Path = field(default_factory=lambda: Path("data/models/docling"))
    pdf_ocr: PdfOcrSettings = field(default_factory=PdfOcrSettings)
    include_hidden: bool = False
    chunk_max_characters: int = 1800
    chunk_max_tokens: int = 8191
    chunk_context_max_tokens: int = 1024
    parse_timeout_seconds: int = 120
    parse_workers: int = 1
    parse_worker_max_tasks: int = 100
    parse_worker_max_memory_mb: int = 1536
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)


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
    if any(not isinstance(extension, str) or extension.lower() not in SUPPORTED_EXTENSIONS
           for extension in settings.include_extensions):
        raise ConfigError("include_extensionsには文書形式（.docx / .xlsx / .xlsm / .pdf / .txt / .md / .markdown）のみ指定できます")
    _integer(settings.chunk_max_characters, "chunk_max_characters", 100)
    if _integer(settings.chunk_max_tokens, "chunk_max_tokens", 32) > 8191:
        raise ConfigError("chunk_max_tokensは8191以下にしてください")
    if _integer(settings.chunk_context_max_tokens, "chunk_context_max_tokens", 1) >= settings.chunk_max_tokens:
        raise ConfigError("chunk_context_max_tokensはchunk_max_tokens未満にしてください")
    if _integer(settings.parse_workers, "parse_workers", 1) > 4:
        raise ConfigError("parse_workersは4以下にしてください")
    _integer(settings.parse_worker_max_tasks, "parse_worker_max_tasks", 1)
    _integer(settings.parse_worker_max_memory_mb, "parse_worker_max_memory_mb", 128)
    ocr = settings.pdf_ocr
    _boolean(ocr.enabled, "pdf_ocr.enabled")
    if ocr.mode not in {"auto", "full_page"}:
        raise ConfigError("pdf_ocr.modeはautoまたはfull_pageを指定してください")
    if isinstance(ocr.scale, bool) or not isinstance(ocr.scale, (int, float)) or not 1 <= ocr.scale <= 4:
        raise ConfigError("pdf_ocr.scaleは1以上4以下の数値にしてください")
    _integer(ocr.timeout_seconds, "pdf_ocr.timeout_seconds", 1)
    _integer(settings.parse_timeout_seconds, "parse_timeout_seconds", 1)
    embedding = settings.embedding
    _boolean(embedding.enabled, "embedding.enabled")
    maximum = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}
    if embedding.model not in maximum:
        raise ConfigError("embedding.modelにはtext-embedding-3-smallまたはlargeを指定してください")
    dimensions = _integer(embedding.dimensions, "embedding.dimensions", 1)
    if dimensions > maximum[embedding.model]:
        raise ConfigError("embedding.dimensionsがモデルの最大次元数を超えています")
    if _integer(embedding.batch_size, "embedding.batch_size", 1) > 32:
        raise ConfigError("embedding.batch_sizeは32以下にしてください")
    _integer(embedding.timeout_seconds, "embedding.timeout_seconds", 1)
    if _integer(embedding.read_batch_size, "embedding.read_batch_size", 1) > 1000:
        raise ConfigError("embedding.read_batch_sizeは1000以下にしてください")
    if _integer(embedding.batch_max_tokens, "embedding.batch_max_tokens", 8191) > 300000:
        raise ConfigError("embedding.batch_max_tokensは300000以下にしてください")
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
                or raw.get("database_url", DEFAULT_DATABASE_URL))
    ocr = raw.get("pdf_ocr", {})
    if not isinstance(ocr, dict):
        raise ConfigError("pdf_ocrにはマッピングを指定してください")
    if set(ocr) - {"enabled", "mode", "scale", "timeout_seconds"}:
        raise ConfigError("pdf_ocrに未対応の項目があります。言語・エンジン・モデルは日本語CPU構成に固定です")
    embedding = raw.get("embedding", {})
    if not isinstance(embedding, dict):
        raise ConfigError("embeddingにはマッピングを指定してください")
    key_env = _string(embedding.get("api_key_env", "OPENAI_API_KEY"), "embedding.api_key_env")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise ConfigError("embedding.api_key_envの環境変数名が不正です")
    api_key = os.environ.get(key_env) or embedding.get("api_key", "")
    if not isinstance(api_key, str) or any(char in api_key for char in "\r\n\x00"):
        raise ConfigError("embedding.api_keyの形式が不正です")
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
        pdf_backend=_string(raw.get("pdf_backend", "docling"), "pdf_backend"),
        docling_artifacts_path=_path(raw.get("docling_artifacts_path", "data/models/docling"),
                                    base, "docling_artifacts_path"),
        pdf_ocr=PdfOcrSettings(
            enabled=_boolean(ocr.get("enabled", True), "pdf_ocr.enabled"),
            mode=_string(ocr.get("mode", "auto"), "pdf_ocr.mode"),
            scale=ocr.get("scale", 3.0),
            timeout_seconds=_integer(ocr.get("timeout_seconds", 1800), "pdf_ocr.timeout_seconds", 1),
        ),
        include_hidden=_boolean(raw.get("include_hidden", False), "include_hidden"),
        chunk_max_characters=_integer(raw.get("chunk_max_characters", 1800), "chunk_max_characters", 100),
        chunk_max_tokens=_integer(raw.get("chunk_max_tokens", 8191), "chunk_max_tokens", 32),
        chunk_context_max_tokens=_integer(raw.get("chunk_context_max_tokens", 1024), "chunk_context_max_tokens", 1),
        parse_timeout_seconds=_integer(raw.get("parse_timeout_seconds", 120), "parse_timeout_seconds", 1),
        parse_workers=_integer(raw.get("parse_workers", 1), "parse_workers", 1),
        parse_worker_max_tasks=_integer(raw.get("parse_worker_max_tasks", 100), "parse_worker_max_tasks", 1),
        parse_worker_max_memory_mb=_integer(raw.get("parse_worker_max_memory_mb", 1536), "parse_worker_max_memory_mb", 128),
        embedding=EmbeddingSettings(
            enabled=_boolean(embedding.get("enabled", False), "embedding.enabled"),
            model=_string(embedding.get("model", "text-embedding-3-small"), "embedding.model"),
            dimensions=_integer(embedding.get("dimensions", 1536), "embedding.dimensions", 1),
            api_key=api_key, api_key_env=key_env,
            batch_size=_integer(embedding.get("batch_size", 16), "embedding.batch_size", 1),
            read_batch_size=_integer(embedding.get("read_batch_size", 128), "embedding.read_batch_size", 1),
            batch_max_tokens=_integer(embedding.get("batch_max_tokens", 262112), "embedding.batch_max_tokens", 8191),
            timeout_seconds=_integer(embedding.get("timeout_seconds", 60), "embedding.timeout_seconds", 1),
        ),
    )
    if settings.pdf_backend not in ("native", "docling"):
        raise ConfigError("pdf_backendはnativeまたはdoclingを指定してください")
    validate_settings(settings)
    return settings
