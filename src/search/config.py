"""検索用のI/O上限と接続キー。取り込みSettingsの内容は変更しない。"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from file_scan.config import ConfigError, load_config
from file_scan.namespace import APPLICATION_SCHEMA, connection_arguments


@dataclass(frozen=True)
class SearchSettings:
    ingestion: object = field(repr=False)
    api_key: str = field(repr=False)
    total_timeout: float = 10.0
    embedding_timeout: float = 4.0
    statement_timeout_ms: int = 2000
    token_budget: int = 6000
    record_token_budget: int = 2000
    response_bytes: int = 262144
    candidate_limit: int = 50
    config_version: str = "poc-v1"
    test_mode: bool = False


def load_search_settings(path):
    try:
        raw = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
    except OSError, UnicodeError, yaml.YAMLError:
        raise ConfigError(
            "設定ファイルを読み込めません。文字コードとYAML形式を確認してください"
        ) from None
    if not isinstance(raw, dict):
        raise ConfigError("設定ファイルの最上位はマッピングにしてください")
    search = raw.get("search", {})
    if not isinstance(search, dict):
        raise ConfigError("searchにはマッピングを指定してください")
    test_mode = search.get("test_mode", False)
    if not isinstance(test_mode, bool):
        raise ConfigError("search.test_modeにはtrueまたはfalseが必要です")
    # 接続試験には収集元・DB・モデル設定を要求しない。
    settings = None if test_mode else load_config(path)
    key = os.environ.get("FILE_SCAN_SEARCH_API_KEY", "")
    if not key or any(c in key for c in "\r\n\x00"):
        raise ConfigError("FILE_SCAN_SEARCH_API_KEYを設定してください")
    return SearchSettings(settings, key, test_mode=test_mode)


def async_database(url, *, schema=APPLICATION_SCHEMA, **kwargs):
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        raise ConfigError("検索APIにはPostgreSQLが必要です")
    connection_options = connection_arguments(
        schema, {"connect_timeout": 1} | kwargs.pop("connect_args", {}))
    execution_options = kwargs.pop("execution_options", {}) | {
        "schema_translate_map": {APPLICATION_SCHEMA: schema}}
    return create_async_engine(
        parsed.set(drivername="postgresql+psycopg"),
        pool_size=4,
        max_overflow=0,
        pool_timeout=1,
        connect_args=connection_options,
        execution_options=execution_options,
        **kwargs,
    )
