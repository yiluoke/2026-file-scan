"""登録用スキーマの境界を検証する。実データ・外部DBは使用しない。"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import AddConstraint, CreateIndex, CreateTable

from file_scan.db import SCHEMA_VERSION, metadata, migrate, open_database, tables

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
REVISION = "sha256:" + "c" * 64


@pytest.fixture
def engine():
    database = open_database("sqlite+pysqlite:///:memory:")
    migrate(database)
    try:
        yield database
    finally:
        database.dispose()


def _seed(connection):
    connection.execute(tables["knowledge_bases"].insert().values(
        knowledge_id="kb", name="検証", system_id="poc", environment="test",
    ))
    connection.execute(tables["conversion_profiles"].insert().values(
        conversion_revision=REVISION, schema_version="1", configuration={"parser": "test"},
    ))
    for number, content_hash in ((1, HASH_A), (2, HASH_B)):
        document_id, generation_id = f"d{number}", f"g{number}"
        connection.execute(tables["documents"].insert().values(
            document_id=document_id, source_system="share", source_key=f"/{number}.txt",
            source_uri=f"file:///share/{number}.txt", title=f"資料{number}", format="txt",
        ))
        connection.execute(tables["artifact_assets"].insert().values(
            asset_id=content_hash, storage_key=f"assets/{number}",
            mime_type="text/plain", byte_size=6,
        ))
        connection.execute(tables["document_versions"].insert().values(
            document_id=document_id, source_version=content_hash, asset_id=content_hash,
            source_uri=f"file:///share/{number}.txt", title=f"資料{number}", format="txt",
        ))
        connection.execute(tables["documents"].update().where(
            tables["documents"].c.document_id == document_id,
        ).values(latest_source_version=content_hash))
        connection.execute(tables["extraction_generations"].insert().values(
            generation_id=generation_id, document_id=document_id,
            source_version=content_hash, conversion_revision=REVISION,
        ))
        connection.execute(tables["blocks"].insert().values(
            generation_id=generation_id, block_id="root", element_type="document",
            reading_order=0, extraction_method="native", source_locations=[{"path": f"{number}.txt"}],
        ))
        connection.execute(tables["blocks"].insert().values(
            generation_id=generation_id, block_id="source", parent_id="root",
            element_type="paragraph", reading_order=1, content_text="本文",
            extraction_method="native", source_locations=[{"line_start": 1, "line_end": 1}],
        ))
        connection.execute(tables["chunks"].insert().values(
            generation_id=generation_id, chunk_id="chunk", title=f"資料{number}",
            content_text="本文", search_text=f"資料{number} 本文", identifiers=["ID_001"],
        ))
    connection.execute(tables["blocks"].insert().values(
        generation_id="g1", block_id="derived", parent_id="root", element_type="paragraph",
        reading_order=2, content_text="説明", artifact_type="derived", extraction_method="image_api",
    ))
    connection.execute(tables["blocks"].insert().values(
        generation_id="g2", block_id="only_g2", element_type="paragraph", reading_order=2,
        extraction_method="native",
    ))
    connection.execute(tables["chunks"].insert().values(
        generation_id="g1", chunk_id="derived", title="生成説明", content_text="説明",
        search_text="生成説明", artifact_type="derived",
    ))


@pytest.fixture
def connection(engine):
    with engine.begin() as connected:
        _seed(connected)
        yield connected


def _reject(connection, statement):
    # PostgreSQLでも失敗後にトランザクション全体を破棄しないようsavepointを使う。
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            connection.execute(statement)


def _mapping(**overrides):
    values = dict(
        generation_id="g1", chunk_id="chunk", sequence_no=0, artifact_type="source",
        block_id="source", chunk_char_start=0, chunk_char_end=2,
        block_char_start=0, block_char_end=2,
    )
    values.update(overrides)
    return values


def _job(**overrides):
    values = dict(run_id="run-1", job_key="collect:d1", attempt_no=1, stage="collect", knowledge_id="kb")
    values.update(overrides)
    return values


def test_migrate_replay_preserves_rows_and_unrelated_tables(engine):
    with engine.begin() as connection:
        _seed(connection)
        connection.exec_driver_sql("CREATE TABLE unrelated (value TEXT NOT NULL)")
        connection.exec_driver_sql("INSERT INTO unrelated VALUES ('keep')")
    migrate(engine)
    migrate(engine)
    with engine.connect() as connection:
        assert connection.execute(select(tables["schema_migrations"].c.version)).scalars().all() == [SCHEMA_VERSION]
        assert connection.execute(select(tables["documents"].c.document_id)).scalars().all() == ["d1", "d2"]
        assert connection.exec_driver_sql("SELECT value FROM unrelated").scalar_one() == "keep"


def test_migrate_refuses_unmanaged_name_collision():
    database = open_database("sqlite:///:memory:")
    try:
        with database.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE documents (original INTEGER)")
        with pytest.raises(RuntimeError, match="未管理"):
            migrate(database)
        assert [column["name"] for column in inspect(database).get_columns("documents")] == ["original"]
        assert "schema_migrations" not in inspect(database).get_table_names()
    finally:
        database.dispose()


def test_migrate_refuses_unknown_future_version(engine):
    with engine.begin() as connection:
        connection.execute(tables["schema_migrations"].update().values(version=SCHEMA_VERSION + 1))
    with pytest.raises(RuntimeError, match="未対応"):
        migrate(engine)
    with engine.connect() as connection:
        assert connection.execute(select(tables["schema_migrations"].c.version)).scalar_one() == SCHEMA_VERSION + 1


def test_v1_upgrade_adds_embeddings_without_losing_documents(engine):
    with engine.begin() as connection:
        _seed(connection)
        for name in ("selected_generations", "extraction_cache", "embedding_cache"):
            tables[name].drop(connection)
        tables["chunk_embeddings"].drop(connection)
        tables["embedding_profiles"].drop(connection)
        connection.execute(tables["schema_migrations"].update().values(version=1))
    migrate(engine)
    migrate(engine)
    with engine.connect() as connection:
        assert connection.execute(select(tables["schema_migrations"].c.version)).scalar_one() == SCHEMA_VERSION
        assert set(connection.execute(select(tables["documents"].c.document_id)).scalars()) == {"d1", "d2"}
        assert "chunk_embeddings" in inspect(connection).get_table_names()


def test_ingestion_settings_has_one_storage_root_and_timestamps(engine):
    settings = tables["ingestion_settings"]
    with engine.begin() as connection:
        connection.execute(settings.insert().values(singleton_id=1, storage_root="/approved/objects"))
        row = connection.execute(select(settings)).mappings().one()
        assert row["storage_root"] == "/approved/objects"
        assert row["created_at"] is not None and row["updated_at"] is not None
        _reject(connection, settings.insert().values(singleton_id=2, storage_root="/other/objects"))
        _reject(connection, settings.insert().values(singleton_id=1, storage_root="/other/objects"))
    migrate(engine)
    with engine.connect() as connection:
        assert connection.execute(select(settings.c.storage_root)).scalar_one() == "/approved/objects"


def test_migrate_detects_missing_ingestion_settings_without_recreating(engine):
    # 一時DBだけを意図的に破損させ、v1記録があっても自動修復しないことを確認。
    with engine.begin() as connection:
        tables["ingestion_settings"].drop(connection)
    with pytest.raises(RuntimeError, match="ingestion_settings"):
        migrate(engine)
    assert "ingestion_settings" not in inspect(engine).get_table_names()


def test_sqlite_foreign_keys_and_ddl_rollback_are_enabled(engine):
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    with pytest.raises(RuntimeError, match="rollback"):
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE rolled_back (value INTEGER)")
            raise RuntimeError("rollback")
    assert "rolled_back" not in inspect(engine).get_table_names()


def test_metadata_compiles_for_postgresql_without_missing_foreign_tables():
    dialect = postgresql.dialect()
    statements = []
    for table in metadata.tables.values():
        statements.append(str(CreateTable(table).compile(dialect=dialect)))
        statements.extend(str(CreateIndex(index).compile(dialect=dialect)) for index in table.indexes)
        # mockでcreate_allするとSQLAlchemyがFKの作成規則を書き換えるため、
        # 共有metadataを変更せず、PostgreSQL固有の後付け制約だけをコンパイルする。
        statements.extend(
            str(AddConstraint(constraint, isolate_from_table=False).compile(dialect=dialect))
            for constraint in table.foreign_key_constraints if constraint.use_alter
        )
    ddl = "\n".join(statements)
    assert "JSONB" in ddl
    assert "TEXT[]" in ddl
    assert "ALTER TABLE documents ADD CONSTRAINT fk_documents_latest_version" in ddl
    assert "WHERE status IN ('queued', 'running')" in ddl
    assert "embedding_profiles" in {fk.column.table.name for table in tables.values() for fk in table.foreign_keys}


def test_defaults_and_document_version_cycle(connection):
    row = connection.execute(select(tables["documents"]).where(tables["documents"].c.document_id == "d1")).mappings().one()
    assert row["latest_source_version"] == HASH_A
    assert row["acl_status"] == "unknown"
    assert row["created_at"] is not None and row["updated_at"] is not None
    assert row["metadata"] == {}
    _reject(connection, tables["documents"].update().where(
        tables["documents"].c.document_id == "d1",
    ).values(latest_source_version=HASH_B))
    _reject(connection, tables["document_versions"].insert().values(
        document_id="d1", source_version=HASH_B, asset_id=HASH_A,
        source_uri="file:///bad", title="不一致", format="txt",
    ))


def test_logical_documents_may_share_bytes_and_local_block_ids(connection):
    connection.execute(tables["document_versions"].insert().values(
        document_id="d2", source_version=HASH_A, asset_id=HASH_A,
        source_uri="file:///share/2.txt", title="別原本", format="txt",
    ))
    assert len(connection.execute(select(tables["blocks"]).where(tables["blocks"].c.block_id == "source")).all()) == 2
    _reject(connection, tables["extraction_generations"].insert().values(
        generation_id="duplicate", document_id="d1", source_version=HASH_A, conversion_revision=REVISION,
    ))


@pytest.mark.parametrize("overrides", [
    {"parent_id": "only_g2"},
    {"parent_id": "new"},
    {"reading_order": -1},
])
def test_block_parent_and_order_constraints(connection, overrides):
    values = dict(generation_id="g1", block_id="new", element_type="paragraph", reading_order=3, extraction_method="native")
    values.update(overrides)
    _reject(connection, tables["blocks"].insert().values(**values))


@pytest.mark.parametrize("overrides", [
    {"block_id": "derived"},
    {"block_id": "only_g2"},
    {"artifact_type": "derived"},
    {"sequence_no": -1},
    {"chunk_char_start": -1},
    {"chunk_char_end": 0},
    {"block_char_start": None},
    {"block_char_end": 0},
])
def test_chunk_block_same_generation_origin_and_ranges(connection, overrides):
    _reject(connection, tables["chunk_blocks"].insert().values(**_mapping(**overrides)))


def test_chunk_may_reference_multiple_blocks_in_order(connection):
    connection.execute(tables["chunk_blocks"].insert().values(**_mapping()))
    connection.execute(tables["chunk_blocks"].insert().values(**_mapping(sequence_no=1, block_id="root", block_char_start=None, block_char_end=None)))
    rows = connection.execute(select(tables["chunk_blocks"].c.block_id).order_by(tables["chunk_blocks"].c.sequence_no)).scalars().all()
    assert rows == ["source", "root"]


def test_asset_sharing_keeps_separate_occurrences(connection):
    values = dict(asset_id=HASH_A, block_id="source", occurrence_order=0, asset_role="original_image")
    connection.execute(tables["asset_occurrences"].insert().values(occurrence_id="o1", generation_id="g1", **values))
    connection.execute(tables["asset_occurrences"].insert().values(occurrence_id="o2", generation_id="g2", **values))
    _reject(connection, tables["asset_occurrences"].insert().values(occurrence_id="o3", generation_id="g1", **values))
    _reject(connection, tables["asset_occurrences"].insert().values(
        occurrence_id="bad", generation_id="g1", block_id="only_g2", asset_id=HASH_A,
        occurrence_order=1, asset_role="original_image",
    ))
    _reject(connection, tables["artifact_assets"].delete().where(tables["artifact_assets"].c.asset_id == HASH_A))


def test_cross_document_relations_have_real_block_foreign_keys(connection):
    values = dict(
        from_generation_id="g1", from_block_id="derived", to_generation_id="g2",
        to_block_id="source", relation_type="derived_from", origin_type="derived",
    )
    connection.execute(tables["artifact_relations"].insert().values(relation_id="r1", **values))
    values["to_block_id"] = "missing"
    _reject(connection, tables["artifact_relations"].insert().values(relation_id="r2", **values))


def test_job_active_uniqueness_retry_and_generation_owner(connection):
    connection.execute(tables["index_jobs"].insert().values(**_job(status="running")))
    _reject(connection, tables["index_jobs"].insert().values(**_job(run_id="run-2", attempt_no=2)))
    connection.execute(tables["index_jobs"].update().where(tables["index_jobs"].c.run_id == "run-1").values(status="failed"))
    connection.execute(tables["index_jobs"].insert().values(**_job(run_id="run-2", attempt_no=2)))
    _reject(connection, tables["index_jobs"].insert().values(**_job(run_id="run-3", attempt_no=2, status="failed")))
    _reject(connection, tables["index_jobs"].insert().values(**_job(
        run_id="wrong-owner", job_key="convert:wrong", stage="convert", generation_id="g1", document_id="d2",
    )))
    _reject(connection, tables["index_jobs"].insert().values(**_job(
        run_id="no-owner", job_key="convert:none", stage="convert", generation_id="g1",
    )))


def test_last_seen_scan_is_bound_to_same_knowledge(connection):
    connection.execute(tables["knowledge_bases"].insert().values(knowledge_id="other", name="別範囲", system_id="poc", environment="test"))
    connection.execute(tables["index_jobs"].insert().values(**_job(
        run_id="scan", job_key="scan:other", stage="scan", knowledge_id="other",
    )))
    _reject(connection, tables["knowledge_documents"].insert().values(
        knowledge_id="kb", document_id="d1", last_seen_scan_id="scan",
    ))


@pytest.mark.parametrize("table_name,values", [
    ("knowledge_bases", {"enabled": True}),
    ("knowledge_bases", {"default_release_id": "future"}),
    ("knowledge_bases", {"embedding_profile_id": "future"}),
    ("documents", {"acl_status": "synchronized"}),
    ("extraction_generations", {"publication_state": "published"}),
    ("blocks", {"publication_state": "reviewed"}),
    ("chunks", {"publication_state": "excluded"}),
])
def test_v1_cannot_bypass_staging_safety(connection, table_name, values):
    _reject(connection, tables[table_name].update().values(**values))


def test_unsupported_database_is_rejected_before_connect():
    with pytest.raises(ValueError, match="PostgreSQL"):
        open_database("mysql://example.invalid/db")
