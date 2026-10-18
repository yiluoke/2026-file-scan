"""登録MVPのSQLAlchemy Coreスキーマと非破壊マイグレーション。

01の変換モデルと04のDB設計に従う。第1版には検索公開・リリース対応・
Embedding・ACL同期を含めず、公開状態はdraft、検索範囲は無効に限定する。
原本や画像の実体は外部ストレージに置き、このDBには参照だけを格納する。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine import Engine, make_url

SCHEMA_VERSION = 1

metadata = MetaData()
# SQLiteでも同じCore APIを使う。PostgreSQLでは04のJSONB・text[]に対応する。
_JSON = JSON().with_variant(JSONB(), "postgresql")
_TEXT_ARRAY = JSON().with_variant(ARRAY(Text()), "postgresql")


def _utcnow() -> datetime:
    """DBセッションのローカル時刻に依存しないUTC時刻を生成する。"""
    return datetime.now(timezone.utc)


def _table(name: str, *items, managed: bool = False) -> Table:
    common = [Column("created_at", DateTime(timezone=True), nullable=False, default=_utcnow)]
    if managed:
        common.append(
            Column(
                "updated_at", DateTime(timezone=True), nullable=False,
                default=_utcnow, onupdate=_utcnow,
            )
        )
    return Table(name, metadata, *items, *common)


def _json(name: str, *, array: bool = False) -> Column:
    # defaultにdict/list自体を渡し、行どうしで可変オブジェクトを共有しない。
    return Column(name, _JSON, nullable=False, default=list if array else dict)


def _enum(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    literals = ", ".join("'" + value + "'" for value in values)
    return CheckConstraint(f"{column} IN ({literals})", name=name)


def _draft(table: str) -> CheckConstraint:
    return CheckConstraint("publication_state = 'draft'", name=f"ck_{table}_draft_only")


knowledge_bases = _table(
    "knowledge_bases",
    Column("knowledge_id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("system_id", Text, nullable=False),
    Column("environment", Text, nullable=False),
    # 後続のsystem_releases / embedding_profiles導入時にFKと利用許可を追加する。
    Column("default_release_id", Text, nullable=True, comment="v1はNULLのみ。リリースFKは後続"),
    Column("embedding_profile_id", Text, nullable=True, comment="v1はNULLのみ。Embedding FKは後続"),
    Column("catalog_revision", BigInteger, nullable=False, default=0),
    Column("scan_status", Text, nullable=False, default="unknown"),
    Column("last_successful_scan_at", DateTime(timezone=True), nullable=True),
    Column("enabled", Boolean, nullable=False, default=False),
    CheckConstraint("enabled = false", name="ck_knowledge_bases_disabled"),
    CheckConstraint("default_release_id IS NULL AND embedding_profile_id IS NULL", name="ck_knowledge_bases_future_refs_null"),
    CheckConstraint("catalog_revision >= 0", name="ck_knowledge_bases_revision"),
    _enum("scan_status", ("unknown", "running", "succeeded", "partial", "failed"), "ck_knowledge_bases_scan_status"),
    managed=True,
)

documents = _table(
    "documents",
    Column("document_id", Text, primary_key=True),
    Column("source_system", Text, nullable=False),
    Column("source_key", Text, nullable=False),
    Column("source_uri", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("format", Text, nullable=False),
    Column("document_type", Text, nullable=True),
    Column("owner", Text, nullable=True),
    Column("latest_source_version", Text, nullable=True),
    Column("source_status", Text, nullable=False, default="available"),
    Column("acl_status", Text, nullable=False, default="unknown"),
    Column("acl_revision", BigInteger, nullable=False, default=0),
    Column("source_modified_at", DateTime(timezone=True), nullable=True),
    Column("last_observed_at", DateTime(timezone=True), nullable=True),
    _json("metadata"),
    UniqueConstraint("source_system", "source_key", name="uq_documents_source"),
    UniqueConstraint("document_id", "acl_revision", name="uq_documents_acl_revision"),
    # PostgreSQLでは版テーブル作成後にALTERで張る。登録順は文書(NULL)→版→更新。
    ForeignKeyConstraint(
        ["document_id", "latest_source_version"],
        ["document_versions.document_id", "document_versions.source_version"],
        name="fk_documents_latest_version", use_alter=True, ondelete="RESTRICT",
    ),
    _enum("source_status", ("available", "unavailable", "deleted"), "ck_documents_source_status"),
    CheckConstraint("acl_status = 'unknown'", name="ck_documents_acl_unknown"),
    CheckConstraint("acl_revision >= 0", name="ck_documents_acl_revision"),
    managed=True,
)

knowledge_documents = _table(
    "knowledge_documents",
    Column("knowledge_id", Text, ForeignKey("knowledge_bases.knowledge_id", ondelete="RESTRICT"), primary_key=True),
    Column("document_id", Text, ForeignKey("documents.document_id", ondelete="RESTRICT"), primary_key=True),
    Column("included", Boolean, nullable=False, default=True),
    Column("collection_status", Text, nullable=False, default="pending"),
    Column("last_seen_scan_id", Text, nullable=True),
    Column("last_error_code", Text, nullable=True),
    ForeignKeyConstraint(
        ["knowledge_id", "last_seen_scan_id"], ["index_jobs.knowledge_id", "index_jobs.run_id"],
        name="fk_knowledge_documents_scan", ondelete="RESTRICT",
    ),
    _enum("collection_status", ("pending", "available", "failed", "excluded"), "ck_knowledge_documents_status"),
    managed=True,
)

artifact_assets = _table(
    "artifact_assets",
    Column("asset_id", Text, primary_key=True),
    Column("storage_key", Text, nullable=False),
    Column("mime_type", Text, nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column("width", Integer, nullable=True),
    Column("height", Integer, nullable=True),
    Column("storage_status", Text, nullable=False, default="available"),
    CheckConstraint("byte_size >= 0", name="ck_artifact_assets_byte_size"),
    CheckConstraint("width IS NULL OR width > 0", name="ck_artifact_assets_width"),
    CheckConstraint("height IS NULL OR height > 0", name="ck_artifact_assets_height"),
    _enum("storage_status", ("available", "missing", "pending_delete"), "ck_artifact_assets_status"),
    managed=True,
)

document_versions = _table(
    "document_versions",
    Column("document_id", Text, ForeignKey("documents.document_id", ondelete="RESTRICT"), primary_key=True),
    Column("source_version", Text, primary_key=True),
    Column("asset_id", Text, ForeignKey("artifact_assets.asset_id", ondelete="RESTRICT"), nullable=False),
    Column("source_uri", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("format", Text, nullable=False),
    Column("source_modified_at", DateTime(timezone=True), nullable=True),
    Column("collected_at", DateTime(timezone=True), nullable=False, default=_utcnow),
    Column("repository_uri", Text, nullable=True),
    Column("commit_id", Text, nullable=True),
    Column("repository_path", Text, nullable=True),
    _json("metadata"),
    CheckConstraint("asset_id = source_version", name="ck_document_versions_original_bytes"),
)

conversion_profiles = _table(
    "conversion_profiles",
    Column("conversion_revision", Text, primary_key=True),
    Column("schema_version", Text, nullable=False),
    _json("configuration"),
    Column("profile_status", Text, nullable=False, default="usable"),
    _enum("profile_status", ("usable", "retired"), "ck_conversion_profiles_status"),
    managed=True,
)

extraction_generations = _table(
    "extraction_generations",
    Column("generation_id", Text, primary_key=True),
    Column("document_id", Text, nullable=False),
    Column("source_version", Text, nullable=False),
    Column("conversion_revision", Text, ForeignKey("conversion_profiles.conversion_revision", ondelete="RESTRICT"), nullable=False),
    Column("processing_status", Text, nullable=False, default="staging"),
    Column("publication_state", Text, nullable=False, default="draft"),
    _json("quality_summary"),
    Column("reviewed_by", Text, nullable=True),
    Column("reviewed_at", DateTime(timezone=True), nullable=True),
    Column("published_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(["document_id", "source_version"], ["document_versions.document_id", "document_versions.source_version"], name="fk_generations_version", ondelete="RESTRICT"),
    UniqueConstraint("document_id", "source_version", "conversion_revision", name="uq_generations_input"),
    UniqueConstraint("generation_id", "document_id", name="uq_generations_document"),
    _enum("processing_status", ("staging", "succeeded", "partial", "failed"), "ck_generations_status"),
    _draft("generations"),
    CheckConstraint("published_at IS NULL", name="ck_generations_no_publication_time"),
    managed=True,
)

blocks = _table(
    "blocks",
    Column("generation_id", Text, ForeignKey("extraction_generations.generation_id", ondelete="RESTRICT"), primary_key=True),
    Column("block_id", Text, primary_key=True),
    Column("parent_id", Text, nullable=True),
    Column("element_type", Text, nullable=False),
    Column("reading_order", Integer, nullable=False),
    _json("section_path", array=True),
    Column("content_text", Text, nullable=True),
    _json("content_data"),
    _json("source_locations", array=True),
    Column("artifact_type", Text, nullable=False, default="source"),
    Column("publication_state", Text, nullable=False, default="draft"),
    Column("extraction_method", Text, nullable=False),
    _json("generation_details"),
    _json("quality_flags", array=True),
    UniqueConstraint("generation_id", "block_id", "artifact_type", name="uq_blocks_origin"),
    ForeignKeyConstraint(["generation_id", "parent_id"], ["blocks.generation_id", "blocks.block_id"], name="fk_blocks_parent", ondelete="RESTRICT"),
    CheckConstraint("parent_id IS NULL OR parent_id <> block_id", name="ck_blocks_not_own_parent"),
    CheckConstraint("reading_order >= 0", name="ck_blocks_order"),
    _enum("artifact_type", ("source", "derived"), "ck_blocks_origin"),
    _draft("blocks"),
)

chunks = _table(
    "chunks",
    Column("generation_id", Text, ForeignKey("extraction_generations.generation_id", ondelete="RESTRICT"), primary_key=True),
    Column("chunk_id", Text, primary_key=True),
    Column("title", Text, nullable=False),
    Column("content_text", Text, nullable=False),
    Column("search_text", Text, nullable=False),
    Column("artifact_type", Text, nullable=False, default="source"),
    Column("publication_state", Text, nullable=False, default="draft"),
    Column("identifiers", _TEXT_ARRAY, nullable=False, default=list),
    Column("feature_ids", _TEXT_ARRAY, nullable=False, default=list),
    Column("token_count", Integer, nullable=True),
    _json("quality_flags", array=True),
    UniqueConstraint("generation_id", "chunk_id", "artifact_type", name="uq_chunks_origin"),
    CheckConstraint("token_count IS NULL OR token_count >= 0", name="ck_chunks_token_count"),
    _enum("artifact_type", ("source", "derived"), "ck_chunks_origin"),
    _draft("chunks"),
)

chunk_blocks = _table(
    "chunk_blocks",
    Column("generation_id", Text, primary_key=True),
    Column("chunk_id", Text, primary_key=True),
    Column("sequence_no", Integer, primary_key=True),
    Column("artifact_type", Text, nullable=False),
    Column("block_id", Text, nullable=False),
    Column("chunk_char_start", Integer, nullable=False),
    Column("chunk_char_end", Integer, nullable=False),
    Column("block_char_start", Integer, nullable=True),
    Column("block_char_end", Integer, nullable=True),
    _json("source_slice"),
    ForeignKeyConstraint(["generation_id", "chunk_id", "artifact_type"], ["chunks.generation_id", "chunks.chunk_id", "chunks.artifact_type"], name="fk_chunk_blocks_chunk", ondelete="RESTRICT"),
    ForeignKeyConstraint(["generation_id", "block_id", "artifact_type"], ["blocks.generation_id", "blocks.block_id", "blocks.artifact_type"], name="fk_chunk_blocks_block", ondelete="RESTRICT"),
    CheckConstraint("sequence_no >= 0", name="ck_chunk_blocks_sequence"),
    CheckConstraint("chunk_char_start >= 0 AND chunk_char_end > chunk_char_start", name="ck_chunk_blocks_chunk_range"),
    CheckConstraint(
        "(block_char_start IS NULL AND block_char_end IS NULL) OR "
        "(block_char_start IS NOT NULL AND block_char_end IS NOT NULL "
        "AND block_char_start >= 0 AND block_char_end > block_char_start)",
        name="ck_chunk_blocks_block_range",
    ),
)

asset_occurrences = _table(
    "asset_occurrences",
    Column("occurrence_id", Text, primary_key=True),
    Column("generation_id", Text, nullable=False),
    Column("block_id", Text, nullable=False),
    Column("asset_id", Text, ForeignKey("artifact_assets.asset_id", ondelete="RESTRICT"), nullable=False),
    Column("occurrence_order", Integer, nullable=False),
    Column("asset_role", Text, nullable=False),
    _json("source_locations", array=True),
    _json("rendering_details"),
    UniqueConstraint("generation_id", "block_id", "occurrence_order", name="uq_asset_occurrences_position"),
    ForeignKeyConstraint(["generation_id", "block_id"], ["blocks.generation_id", "blocks.block_id"], name="fk_asset_occurrences_block", ondelete="RESTRICT"),
    CheckConstraint("occurrence_order >= 0", name="ck_asset_occurrences_order"),
)

artifact_relations = _table(
    "artifact_relations",
    Column("relation_id", Text, primary_key=True),
    Column("from_generation_id", Text, nullable=False),
    Column("from_block_id", Text, nullable=False),
    Column("to_generation_id", Text, nullable=False),
    Column("to_block_id", Text, nullable=False),
    Column("relation_type", Text, nullable=False),
    Column("relation_status", Text, nullable=False, default="candidate"),
    Column("origin_type", Text, nullable=False),
    _json("evidence"),
    Column("publication_state", Text, nullable=False, default="draft"),
    ForeignKeyConstraint(["from_generation_id", "from_block_id"], ["blocks.generation_id", "blocks.block_id"], name="fk_relations_from", ondelete="RESTRICT"),
    ForeignKeyConstraint(["to_generation_id", "to_block_id"], ["blocks.generation_id", "blocks.block_id"], name="fk_relations_to", ondelete="RESTRICT"),
    _enum("relation_status", ("confirmed", "candidate", "rejected"), "ck_relations_status"),
    _enum("origin_type", ("source", "derived"), "ck_relations_origin"),
    _draft("relations"),
    managed=True,
)

index_jobs = _table(
    "index_jobs",
    Column("run_id", Text, primary_key=True),
    Column("job_key", Text, nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("stage", Text, nullable=False),
    Column("knowledge_id", Text, ForeignKey("knowledge_bases.knowledge_id", ondelete="RESTRICT"), nullable=False),
    Column("document_id", Text, ForeignKey("documents.document_id", ondelete="RESTRICT"), nullable=True),
    Column("generation_id", Text, nullable=True),
    Column("embedding_profile_id", Text, nullable=True, comment="v1はNULLのみ。Embedding FKは後続"),
    Column("status", Text, nullable=False, default="queued"),
    _json("target_parameters"),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("lease_until", DateTime(timezone=True), nullable=True),
    Column("lease_version", BigInteger, nullable=False, default=0),
    Column("error_code", Text, nullable=True),
    Column("error_summary", Text, nullable=True),
    UniqueConstraint("job_key", "attempt_no", name="uq_index_jobs_attempt"),
    UniqueConstraint("knowledge_id", "run_id", name="uq_index_jobs_scope_run"),
    ForeignKeyConstraint(["generation_id", "document_id"], ["extraction_generations.generation_id", "extraction_generations.document_id"], name="fk_index_jobs_generation", ondelete="RESTRICT"),
    CheckConstraint("attempt_no >= 1 AND lease_version >= 0", name="ck_index_jobs_counters"),
    CheckConstraint("generation_id IS NULL OR document_id IS NOT NULL", name="ck_index_jobs_generation_document"),
    CheckConstraint("embedding_profile_id IS NULL", name="ck_index_jobs_embedding_unavailable"),
    CheckConstraint("finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at", name="ck_index_jobs_times"),
    _enum("stage", ("scan", "collect", "convert", "embed", "publish", "delete", "acl_sync"), "ck_index_jobs_stage"),
    _enum("status", ("queued", "running", "succeeded", "partial", "failed", "cancelled"), "ck_index_jobs_status"),
    managed=True,
)

ingestion_settings = _table(
    "ingestion_settings",
    Column("singleton_id", Integer, primary_key=True),
    Column("storage_root", Text, nullable=False),
    CheckConstraint("singleton_id = 1", name="ck_ingestion_settings_singleton"),
    managed=True,
)
# 実装用の管理表。相対storage_keyが別の保管先を指さないよう、1DBに対して
# 初回ingestで一つの正規化済みstorage_rootを結び付ける。変更拒否は登録側で行う。

schema_migrations = _table(
    "schema_migrations",
    Column("version", Integer, primary_key=True),
    Column("applied_at", DateTime(timezone=True), nullable=False, default=_utcnow),
    CheckConstraint("version >= 1", name="ck_schema_migrations_version"),
)

# FK逆引き・段階処理に使う索引。PK/UQで満たす索引は重複させない。
Index("ix_knowledge_documents_document", knowledge_documents.c.document_id, knowledge_documents.c.included)
Index("ix_document_versions_asset", document_versions.c.asset_id)
Index("ix_blocks_parent_order", blocks.c.generation_id, blocks.c.parent_id, blocks.c.reading_order)
Index("ix_chunks_publication", chunks.c.generation_id, chunks.c.publication_state, chunks.c.artifact_type)
Index("ix_chunk_blocks_block", chunk_blocks.c.generation_id, chunk_blocks.c.block_id, chunk_blocks.c.chunk_id)
Index("ix_asset_occurrences_asset", asset_occurrences.c.asset_id)
Index("ix_artifact_relations_from", artifact_relations.c.from_generation_id, artifact_relations.c.from_block_id, artifact_relations.c.relation_type)
Index("ix_artifact_relations_to", artifact_relations.c.to_generation_id, artifact_relations.c.to_block_id, artifact_relations.c.relation_type)
Index("ix_index_jobs_queue", index_jobs.c.status, index_jobs.c.stage, index_jobs.c.created_at)
_active_job = index_jobs.c.status.in_(("queued", "running"))
Index("uq_index_jobs_active", index_jobs.c.job_key, unique=True, postgresql_where=_active_job, sqlite_where=_active_job)

tables: dict[str, Table] = dict(metadata.tables)


def open_database(url: str) -> Engine:
    """Engineを作る。接続確認やマイグレーションは呼び出し側が明示的に行う。

    PostgreSQLの省略URLはpsycopg 3を使う。SQLiteはテスト・ローカル検証用で、
    FK検証とDDLを含む明示トランザクションを有効にする。
    """
    parsed = make_url(url)
    if parsed.get_backend_name() not in {"postgresql", "sqlite"}:
        raise ValueError("対応DBはPostgreSQLまたはSQLiteです")
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    engine = create_engine(parsed)
    if parsed.get_backend_name() == "sqlite":
        @event.listens_for(engine, "connect")
        def _sqlite_connect(dbapi_connection, _connection_record):
            # pysqliteの暗黙コミットを避け、マイグレーション失敗もrollback可能にする。
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(connection):
            connection.exec_driver_sql("BEGIN")
    return engine


def migrate(engine: Engine) -> None:
    """空の管理領域へv1を適用し、適用済みなら変更しない。

    未管理の同名表・未知の将来版・適用記録と実表の不一致は自動修復しない。
    drop/createや既存データの書き換えは行わず、運用者による確認を求める。
    """
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # 同時起動したバッチどうしの初期DDLを直列化するトランザクションロック。
            connection.execute(text("SELECT pg_advisory_xact_lock(:namespace, :version)"), {"namespace": 20261018, "version": SCHEMA_VERSION})
        existing = set(inspect(connection).get_table_names())
        managed = set(tables) - {"schema_migrations"}
        versions = []
        if "schema_migrations" in existing:
            versions = list(connection.execute(select(schema_migrations.c.version)).scalars())
        if any(version != SCHEMA_VERSION for version in versions):
            raise RuntimeError("未対応のDBスキーマ版です。自動変更しません")
        if versions:
            missing = set(tables) - existing
            if missing:
                raise RuntimeError("移行記録とテーブルが一致しません: " + ", ".join(sorted(missing)))
            return
        if existing & managed:
            raise RuntimeError("未管理の同名テーブルがあります。自動変更しません")
        # PostgreSQLの後付けFK生成はSQLAlchemy内の作成規則を書き換え得る。
        # 公開するmetadataを保持し、同一プロセスの別DBでも同じ定義を使えるよう複製する。
        migration_metadata = MetaData()
        for table in tables.values():
            table.to_metadata(migration_metadata)
        migration_metadata.create_all(connection)
        connection.execute(schema_migrations.insert().values(version=SCHEMA_VERSION))
