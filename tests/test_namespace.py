"""専用スキーマの配置・旧配置からのデータ保持・衝突時のrollbackを検証する。"""

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.schema import DropSchema

from file_scan.config import Settings, Source
from file_scan.db import metadata, migrate, open_database, tables
from file_scan.namespace import APPLICATION_SCHEMA, database_schema
from file_scan.pipeline import ingest, require_schema
from file_scan.profiles import digest
from file_scan.tokens import count_tokens
from search import schema as search
from search.management import create_release, publish

spec = importlib.util.spec_from_file_location(
    "migrate_postgres_schema", Path(__file__).resolve().parents[1] / "scripts/migrate_postgres_schema.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_application_metadata_and_connection_options_are_bound_to_winnap():
    assert {table.schema for table in metadata.tables.values()} == {"winnap"}
    assert {table.schema for table in search.metadata.tables.values()} == {"winnap"}
    engine = open_database("postgresql+psycopg://localhost/unconnected")
    try:
        assert engine.get_execution_options()["schema_translate_map"] == {"winnap": "winnap"}
    finally:
        engine.dispose()


@pytest.fixture
def namespaces(tmp_path):
    url = os.environ.get("FILE_SCAN_TEST_DATABASE_URL")
    if not url:
        pytest.skip("実PostgreSQLテストにはFILE_SCAN_TEST_DATABASE_URLを指定")
    source_name, target_name = "legacy_" + uuid4().hex, "target_" + uuid4().hex
    source, target = open_database(url, schema=source_name), open_database(url, schema=target_name)
    migrate(source)
    root = tmp_path / "source"
    root.mkdir()
    (root / "manual.txt").write_text("移行後も保持する本文 E001", encoding="utf-8")
    settings = Settings(sources=[Source("schema-validation", root)], storage_dir=tmp_path / "storage",
                        report_dir=tmp_path / "reports")
    ingest(settings, source)
    try:
        yield source, target, source_name, target_name, settings
    finally:
        target.dispose()
        with source.begin() as c:
            c.execute(DropSchema(source_name, cascade=True, if_exists=True))
            c.execute(DropSchema(target_name, cascade=True, if_exists=True))
        source.dispose()


def test_migration_keeps_documents_vectors_publications_indexes_and_sequences(namespaces):
    source, target, source_name, target_name, settings = namespaces
    settings.embedding.dimensions = 3
    profile = {"provider": "openai", "model": settings.embedding.model, "dimensions": 3,
               "input_version": "search_text-v1"}
    profile_id = digest(profile)
    with source.begin() as c:
        c.exec_driver_sql("CREATE TABLE unrelated_application (id integer)")
        chunk = c.execute(select(tables["chunks"])).mappings().one()
        c.execute(tables["embedding_profiles"].insert().values(embedding_profile_id=profile_id, **profile))
        c.execute(tables["chunk_embeddings"].insert().values(
            generation_id=chunk["generation_id"], chunk_id=chunk["chunk_id"],
            embedding_profile_id=profile_id, dimensions=3, embedding=[1.0, 0.0, 0.0],
            input_hash=digest(chunk["search_text"]), input_tokens=count_tokens(chunk["search_text"])))
        document_id = c.execute(select(tables["documents"].c.document_id)).scalar_one()
    search.migrate(source)
    release = create_release(source, settings, "schema-test")
    publication = publish(source, settings, release, document_id, chunk["generation_id"], "tester")
    all_tables = [*tables.values(), *search.metadata.tables.values()]
    with source.connect() as c:
        counts = {table.name: c.execute(select(func.count()).select_from(table)).scalar_one()
                  for table in all_tables}
    moved = migration.migrate_namespace(target, source_schema=source_name)
    assert moved == {"status": "migrated", "schema": target_name, "moved_tables": len(all_tables)}
    require_schema(target)
    migrate(target)
    search.migrate(target)
    with target.connect() as c:
        assert database_schema(c) == target_name
        assert {table.name: c.execute(select(func.count()).select_from(table)).scalar_one()
                for table in all_tables} == counts
        assert c.execute(select(tables["documents"].c.document_id)).scalar_one() == document_id
        assert list(c.execute(select(tables["chunk_embeddings"].c.embedding)).scalar_one()) == [1, 0, 0]
        assert c.execute(select(search.publications.c.publication_id)).scalar_one() == publication
        sequence_schema = c.execute(text(
            "SELECT n.nspname FROM pg_class s JOIN pg_namespace n ON n.oid=s.relnamespace "
            "WHERE s.oid=pg_get_serial_sequence(:table, 'version')::regclass"
        ), {"table": f"{target_name}.schema_migrations"}).scalar_one()
        assert sequence_schema == target_name
        assert inspect(c).get_foreign_keys("documents", schema=target_name)[0]["referred_schema"] == target_name
        assert "ix_search_chunks_fulltext" in {
            row["name"] for row in inspect(c).get_indexes("search_chunks", schema=target_name)}
        assert inspect(c).get_table_names(schema=source_name) == ["unrelated_application"]
        assert c.execute(text(
            "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace "
            "WHERE e.extname='vector'"
        )).scalar_one() == "public"
    assert migration.migrate_namespace(target, source_schema=source_name)["status"] == "unchanged"


def test_migration_rejects_target_collisions_without_changing_source(namespaces):
    source, target, source_name, _, _ = namespaces
    migrate(target)
    with pytest.raises(RuntimeError, match="自動統合"):
        migration.migrate_namespace(target, source_schema=source_name)
    with source.connect() as c:
        assert c.execute(select(func.count()).select_from(tables["documents"])).scalar_one() == 1
    with target.connect() as c:
        assert c.execute(select(func.count()).select_from(tables["documents"])).scalar_one() == 0


def test_migration_failure_rolls_back_every_moved_table(namespaces):
    source, target, source_name, target_name, _ = namespaces
    moved = []

    def fail_second_move(conn, cursor, statement, parameters, context, many):
        if statement.startswith("ALTER TABLE"):
            moved.append(statement)
            if len(moved) == 2:
                raise RuntimeError("injected_move_failure")

    event.listen(target, "before_cursor_execute", fail_second_move)
    try:
        with pytest.raises(RuntimeError, match="injected_move_failure"):
            migration.migrate_namespace(target, source_schema=source_name)
    finally:
        event.remove(target, "before_cursor_execute", fail_second_move)
    with source.connect() as c:
        assert set(inspect(c).get_table_names(schema=source_name)) == set(tables)
        assert not inspect(c).has_schema(target_name)


def test_migration_rejects_unknown_version_without_moving_tables(namespaces):
    source, target, source_name, target_name, _ = namespaces
    with source.begin() as c:
        c.execute(tables["schema_migrations"].update().values(version=99))
    with pytest.raises(RuntimeError, match="v3"):
        migration.migrate_namespace(target, source_schema=source_name)
    with source.connect() as c:
        assert set(inspect(c).get_table_names(schema=source_name)) == set(tables)
        assert not inspect(c).has_schema(target_name)


def test_migration_rejects_partial_search_installation(namespaces):
    source, target, source_name, target_name, _ = namespaces
    with source.begin() as c:
        c.exec_driver_sql("CREATE TABLE search_schema_migrations (version integer)")
        c.exec_driver_sql("INSERT INTO search_schema_migrations VALUES (1)")
    with pytest.raises(RuntimeError, match="検索テーブル"):
        migration.migrate_namespace(target, source_schema=source_name)
    with source.connect() as c:
        assert set(tables) <= set(inspect(c).get_table_names(schema=source_name))
        assert not inspect(c).has_schema(target_name)


def test_qualified_tables_never_fall_back_to_another_schema(namespaces):
    source, target, source_name, target_name, _ = namespaces
    with target.connect() as c:
        c.exec_driver_sql(f'SET search_path = "{target_name}", "{source_name}", public')
        # 無修飾なら旧配置を参照できる状態でも、新配置がなければ明示的に失敗する。
        assert c.exec_driver_sql("SELECT count(*) FROM documents").scalar_one() == 1
        with pytest.raises(ProgrammingError):
            c.execute(select(tables["documents"]))


@pytest.mark.parametrize("schema", ["winnap;DROP SCHEMA public", "has space", "a,b", '"other"'])
def test_invalid_namespace_is_rejected_before_connect(schema):
    with pytest.raises(ValueError, match="スキーマ"):
        open_database("postgresql+psycopg://localhost/unconnected", schema=schema)


def test_sqlite_schema_translation_keeps_local_tests_working():
    engine = open_database("sqlite:///:memory:")
    try:
        assert engine.get_execution_options()["schema_translate_map"] == {APPLICATION_SCHEMA: None}
        migrate(engine)
        require_schema(engine)
    finally:
        engine.dispose()
