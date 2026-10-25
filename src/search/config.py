"""検索用のI/O上限と接続キー。取り込みSettingsの内容は変更しない。"""

import os
from dataclasses import dataclass, field

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from file_scan.config import ConfigError, load_config


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


def load_search_settings(path):
    settings = load_config(path)
    key = os.environ.get("FILE_SCAN_SEARCH_API_KEY", "")
    if not key or any(c in key for c in "\r\n\x00"):
        raise ConfigError("FILE_SCAN_SEARCH_API_KEYを設定してください")
    return SearchSettings(settings, key)


def async_database(url, **kwargs):
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        raise ConfigError("検索APIにはPostgreSQLが必要です")
    connection_options = {"connect_timeout": 1} | kwargs.pop("connect_args", {})
    return create_async_engine(
        parsed.set(drivername="postgresql+psycopg"),
        pool_size=4,
        max_overflow=0,
        pool_timeout=1,
        connect_args=connection_options,
        **kwargs,
    )
