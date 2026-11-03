"""専用YAML設定。入力範囲と出力先、外部実行条件を実行前に検証する。"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

import yaml

PINNED_CODEWIKI_COMMIT = "1b4af43d6d20c8dbd758fe1d5f888cc6947ed11d"


class ConfigError(ValueError):
    """設定内容や認証値を含まない利用者向けエラー。"""


@dataclass(frozen=True)
class CodeWikiSettings:
    python: str = sys.executable
    expected_version: str = "2.0.0"
    expected_commit: str = PINNED_CODEWIKI_COMMIT
    timeout_seconds: int = 3600
    max_tokens: int = 8192
    request_limit: int = 30
    max_depth: int = 2


@dataclass(frozen=True)
class DocumentationSettings:
    language: str = "ja"
    provider: str = "codewiki"
    model_env: str = "CPP_SCAN_LLM_MODEL"
    api_key_env: str = "CPP_SCAN_LLM_API_KEY"
    base_url_env: str = "CPP_SCAN_LLM_BASE_URL"


@dataclass(frozen=True)
class Settings:
    project_id: str
    source_root: Path
    artifact_root: Path
    source_encoding: str = "auto"
    fallback_encoding: str = "cp932"
    encoding_overrides: dict[str, str] = field(default_factory=dict)
    ui_mode: str = "auto"
    pro_mode: str = "reference_only"
    include_tests: bool = False
    include_third_party: bool = False
    include_files: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    max_file_bytes: int = 10 * 1024 * 1024
    max_files: int = 50000
    copy_retries: int = 2
    allow_partial_ingest: bool = False
    codewiki: CodeWikiSettings = field(default_factory=CodeWikiSettings)
    documentation: DocumentationSettings = field(default_factory=DocumentationSettings)
    file_scan_config: Path | None = None

    def scan_profile(self) -> dict:
        """接続先・秘密・実行日時を入力版の識別に含めない。"""
        names = ("source_encoding", "fallback_encoding", "encoding_overrides", "ui_mode",
                 "pro_mode", "include_tests", "include_third_party", "include_files",
                 "exclude_patterns", "max_file_bytes", "max_files")
        values = asdict(self)
        return {name: values[name] for name in names}


def _string(value, name):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\x00\r\n"):
        raise ConfigError(f"{name}には空でない1行の文字列が必要です")
    return value


def _mapping(value, name, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ConfigError(f"{name}の形式または項目名が不正です")
    return value


def _integer(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{name}には{minimum}以上の整数が必要です")
    return value


def _bool(value, name):
    if type(value) is not bool:
        raise ConfigError(f"{name}にはtrueまたはfalseを指定してください")
    return value


def relative_path(value: str) -> str:
    _string(value, "相対パス")
    path = PurePosixPath(value)
    if (path.is_absolute() or "\\" in value or ":" in value
            or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise ConfigError("/区切りのルート相対パスを指定してください")
    return path.as_posix()


def _path(value, base, name):
    value = _string(value, name)
    if os.name != "nt" and (value.startswith(("\\\\", "//")) or re.match(r"^[A-Za-z]:", value)):
        raise ConfigError(f"{name}は実行OSから読めるマウント先を指定してください")
    path = Path(value).expanduser()
    path = path if path.is_absolute() else base / path
    # resolveする前に祖先も確認し、リンク経由の入力・出力を黙認しない。
    for parent in (path, *path.parents):
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ConfigError(f"{name}にリンク経由のパスは指定できません")
    return path.resolve()


def validate_settings(settings: Settings) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", settings.project_id):
        raise ConfigError("project_idには英数字・_・-・.で安定したIDを指定してください")
    source, output = settings.source_root.resolve(), settings.artifact_root.resolve()
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ConfigError("source_rootとartifact_rootは親子関係にできません")
    for root in (settings.source_root, settings.artifact_root):
        _path(str(root), Path.cwd(), "ルート")
    if settings.pro_mode != "reference_only":
        raise ConfigError("pro_modeはreference_onlyのみ対応しています")
    if not isinstance(settings.ui_mode, str) or settings.ui_mode not in {"auto", "widgets", "qml", "both"}:
        raise ConfigError("ui_modeはauto/widgets/qml/bothを指定してください")
    if not isinstance(settings.source_encoding, str) or settings.source_encoding not in {"auto", "utf-8", "cp932"}:
        raise ConfigError("source_encodingはauto/utf-8/cp932を指定してください")
    if not isinstance(settings.fallback_encoding, str) or settings.fallback_encoding not in {"utf-8", "cp932"}:
        raise ConfigError("fallback_encodingはutf-8/cp932を指定してください")
    for pattern, encoding in settings.encoding_overrides.items():
        relative_path(pattern)
        if not isinstance(encoding, str) or encoding not in {"utf-8", "cp932"}:
            raise ConfigError("encoding_overridesはutf-8/cp932のみ対応しています")
    for name in ("max_file_bytes", "max_files"):
        _integer(getattr(settings, name), name)
    _integer(settings.copy_retries, "copy_retries", 0)
    for name in ("include_tests", "include_third_party", "allow_partial_ingest"):
        _bool(getattr(settings, name), name)
    for value in (*settings.include_files, *settings.exclude_patterns):
        relative_path(value)
    for name in ("timeout_seconds", "max_tokens", "request_limit", "max_depth"):
        _integer(getattr(settings.codewiki, name), "codewiki." + name)
    _string(settings.codewiki.expected_commit, "codewiki.expected_commit")
    _string(settings.codewiki.expected_version, "codewiki.expected_version")
    _string(settings.codewiki.python, "codewiki.python")
    if not re.fullmatch(r"[0-9a-f]{40}", settings.codewiki.expected_commit):
        raise ConfigError("codewiki.expected_commitには固定コミットの40桁SHAを指定してください")
    doc = settings.documentation
    if doc.language != "ja" or doc.provider != "codewiki":
        raise ConfigError("初期版のdocumentationはlanguage: ja、provider: codewikiです")
    for name in (doc.model_env, doc.api_key_env, doc.base_url_env):
        _string(name, "documentationの環境変数名")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ConfigError("documentationの環境変数名が不正です")


def load_config(path: str | Path) -> Settings:
    path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigError("専用設定を読み込めません。UTF-8とYAML形式を確認してください") from None
    allowed = set(Settings.__dataclass_fields__) - {"file_scan_config"}
    _mapping(raw, "cpp-scan設定", allowed | {"schema_version", "ingestion"})
    if raw.get("schema_version") != "cpp-scan-v1":
        raise ConfigError("schema_version: cpp-scan-v1が必要です")
    for required in ("project_id", "source_root", "artifact_root"):
        if required not in raw:
            raise ConfigError(f"{required}が必要です")
    values = {k: v for k, v in raw.items() if k in allowed}
    for name in ("source_root", "artifact_root"):
        values[name] = _path(raw[name], path.parent, name)
    _string(values["project_id"], "project_id")
    for name in ("include_files", "exclude_patterns"):
        items = values.get(name, [])
        if not isinstance(items, list):
            raise ConfigError(f"{name}には配列が必要です")
        values[name] = tuple(relative_path(v) for v in items)
    overrides = values.get("encoding_overrides", {})
    if not isinstance(overrides, dict):
        raise ConfigError("encoding_overridesにはパスglobと文字コードの対応を指定してください")
    for name, cls in (("codewiki", CodeWikiSettings), ("documentation", DocumentationSettings)):
        data = _mapping(raw.get(name, {}), name, cls.__dataclass_fields__)
        if name == "codewiki" and "python" in data:
            # venvのPythonは通常シンボリックリンク。実行パスはvenv側の所在を保持する。
            executable = Path(_string(data["python"], "codewiki.python")).expanduser()
            if not executable.is_absolute():
                executable = path.parent / executable
            data = dict(data, python=str(executable.absolute()))
        values[name] = cls(**data)
    ingest = _mapping(raw.get("ingestion", {}), "ingestion", {"file_scan_config"})
    if "file_scan_config" in ingest:
        values["file_scan_config"] = _path(ingest["file_scan_config"], path.parent,
                                          "ingestion.file_scan_config")
    result = Settings(**values)
    validate_settings(result)
    return result
