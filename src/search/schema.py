"""検索用の独立スキーマ。取り込みv3のmetadata・制約・版を変更しない。"""

from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from file_scan.db import tables as ingestion
from file_scan.pipeline import require_schema

metadata = MetaData()


def now():
    return datetime.now(timezone.utc)


releases = Table(
    "search_releases",
    metadata,
    Column("release_id", Text, primary_key=True),
    Column(
        "knowledge_id",
        Text,
        ForeignKey(ingestion["knowledge_bases"].c.knowledge_id),
        nullable=False,
    ),
    Column("name", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, default=now),
    UniqueConstraint("knowledge_id", "name"),
)
catalogs = Table(
    "search_catalogs",
    metadata,
    Column(
        "knowledge_id",
        Text,
        ForeignKey(ingestion["knowledge_bases"].c.knowledge_id),
        primary_key=True,
    ),
    Column("default_release_id", Text, ForeignKey(releases.c.release_id)),
    Column(
        "embedding_profile_id",
        Text,
        ForeignKey(ingestion["embedding_profiles"].c.embedding_profile_id),
        nullable=False,
    ),
    Column("revision", BigInteger, nullable=False, default=0),
    Column("enabled", Boolean, nullable=False, default=False),
    Column("feature_filter_enabled", Boolean, nullable=False, default=False),
)
publications = Table(
    "search_publications",
    metadata,
    Column("publication_id", Text, primary_key=True),
    Column("release_id", Text, ForeignKey(releases.c.release_id), nullable=False),
    Column("document_id", Text, ForeignKey(ingestion["documents"].c.document_id), nullable=False),
    Column("generation_id", Text, nullable=False),
    Column("source_version", Text, nullable=False),
    Column("source_path", Text, nullable=False),
    Column("conversion_revision", Text, nullable=False),
    Column(
        "embedding_profile_id",
        Text,
        ForeignKey(ingestion["embedding_profiles"].c.embedding_profile_id),
        nullable=False,
    ),
    Column("generation_updated_at", DateTime(timezone=True), nullable=False),
    Column("approved_by", Text, nullable=False),
    Column("approved_at", DateTime(timezone=True), nullable=False, default=now),
    Column("partial", Boolean, nullable=False),
    Column("quality_summary", JSONB, nullable=False),
    Column("relations", JSONB, nullable=False),
    UniqueConstraint("publication_id", "document_id", "release_id"),
    ForeignKeyConstraint(
        ["generation_id", "document_id"],
        [
            ingestion["extraction_generations"].c.generation_id,
            ingestion["extraction_generations"].c.document_id,
        ],
    ),
)
bindings = Table(
    "search_release_bindings",
    metadata,
    Column("release_id", Text, ForeignKey(releases.c.release_id), primary_key=True),
    Column("document_id", Text, ForeignKey(ingestion["documents"].c.document_id), primary_key=True),
    Column("expected_source_version", Text),
    Column("expected_conversion_revision", Text),
    Column("publication_id", Text),
    ForeignKeyConstraint(
        ["publication_id", "document_id", "release_id"],
        [publications.c.publication_id, publications.c.document_id, publications.c.release_id],
    ),
)
blocks = Table(
    "search_blocks",
    metadata,
    Column("publication_id", Text, ForeignKey(publications.c.publication_id), primary_key=True),
    Column("block_id", Text, primary_key=True),
    Column("parent_id", Text),
    Column("reading_order", Integer, nullable=False),
    Column("element_type", Text, nullable=False),
    Column("content_text", Text),
    Column("content_data", JSONB, nullable=False),
    Column("section_path", JSONB, nullable=False),
    Column("source_locations", JSONB, nullable=False),
    Column("quality_flags", JSONB, nullable=False),
    Column("assets", JSONB, nullable=False),
)
chunks = Table(
    "search_chunks",
    metadata,
    Column("publication_id", Text, ForeignKey(publications.c.publication_id), primary_key=True),
    Column("chunk_id", Text, primary_key=True),
    Column("content_text", Text, nullable=False),
    Column("search_text", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("identifiers", ARRAY(Text), nullable=False),
    Column("feature_ids", ARRAY(Text), nullable=False),
    Column("mappings", JSONB, nullable=False),
    Column("quality_flags", JSONB, nullable=False),
    Column("embedding", Vector()),
    Column("dimensions", Integer, nullable=False),
    Column("input_hash", Text, nullable=False),
    CheckConstraint("embedding IS NULL OR vector_dims(embedding) = dimensions"),
)
events = Table(
    "search_events",
    metadata,
    Column("request_id", Text, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False, default=now),
    Column("knowledge_id", Text),
    Column("release_id", Text),
    Column("snapshot_id", Text),
    Column("result_status", Text, nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("summary", JSONB, nullable=False),
    Column("error_code", Text),
)
versions = Table("search_schema_migrations", metadata, Column("version", Integer, primary_key=True))


def migrate(engine):
    """既存v3へ検索専用表だけを追加。未管理・未知版の検索表は修復しない。"""
    require_schema(engine)
    if engine.dialect.name != "postgresql":
        raise RuntimeError("検索にはPostgreSQLが必要です")
    with engine.begin() as c:
        c.execute(text("SELECT pg_advisory_xact_lock(20261018, 2)"))
        available = set(
            c.execute(
                text("SELECT extname FROM pg_extension WHERE extname IN ('vector','pgroonga')")
            ).scalars()
        )
        if available != {"vector", "pgroonga"}:
            raise RuntimeError("vectorとpgroonga拡張を先に有効化してください")
        existing = set(inspect(c).get_table_names())
        if versions.name in existing:
            if (
                list(c.execute(select(versions.c.version)).scalars()) != [1]
                or set(metadata.tables) - existing
            ):
                raise RuntimeError("検索スキーマと適用版が一致しません")
            return
        if set(metadata.tables) & existing:
            raise RuntimeError("未管理の検索テーブルがあります")
        metadata.create_all(c)
        c.exec_driver_sql(
            "CREATE INDEX ix_search_chunks_fulltext ON search_chunks USING pgroonga "
            "(search_text) WITH (tokenizer='TokenBigram', normalizers='NormalizerAuto')"
        )
        c.exec_driver_sql(
            "CREATE INDEX ix_search_chunks_identifiers ON search_chunks USING gin (identifiers)"
        )
        c.exec_driver_sql(
            "CREATE INDEX ix_search_chunks_features ON search_chunks USING gin (feature_ids)"
        )
        c.execute(versions.insert().values(version=1))
