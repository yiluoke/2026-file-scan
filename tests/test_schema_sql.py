"""配布用SQLだけで現行スキーマを導入し、実アプリから利用できることを確認する。"""

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.schema import CreateSchema, DropSchema

from file_scan.config import Settings, Source
from file_scan.db import SCHEMA_VERSION, metadata, migrate, open_database, tables
from file_scan.pipeline import ingest, require_schema

ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = ROOT / "sql/postgresql/schema-v3.sql"
spec = importlib.util.spec_from_file_location("export_postgres_schema", ROOT / "scripts/export_postgres_schema.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def test_deployment_sql_matches_current_implementation():
    assert SQL_PATH.read_text(encoding="utf-8") == exporter.render_schema()
    assert exporter.render_schema() == exporter.render_schema()


@pytest.fixture
def empty_postgres():
    url = os.environ.get("FILE_SCAN_TEST_DATABASE_URL")
    if not url:
        pytest.skip("実PostgreSQLテストにはFILE_SCAN_TEST_DATABASE_URLを指定")
    admin = open_database(url)
    schema = "test_file_scan_ddl_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = open_database(url)

    @event.listens_for(engine, "connect")
    def search_path(dbapi, _):
        with dbapi.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}", public')
        dbapi.commit()

    try:
        yield engine, schema
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


def install_sql(engine, schema, sql=None):
    script = SQL_PATH.read_text(encoding="utf-8") if sql is None else sql
    # DDLはそのまま使い、作成先だけを自分で生成した一時schemaへ限定する。
    original = "SET LOCAL search_path = public, pg_catalog;"
    assert script.count(original) == 1
    script = script.replace(original, f'SET LOCAL search_path = "{schema}", public, pg_catalog;')
    raw = engine.raw_connection()
    dbapi = raw.driver_connection
    try:
        dbapi.autocommit = True
        try:
            with dbapi.cursor() as cursor:
                cursor.execute(script)
        except Exception:
            dbapi.execute("ROLLBACK")
            raise
    finally:
        dbapi.autocommit = False
        raw.close()


def describe_schema(engine):
    inspector = inspect(engine)
    schema = inspector.default_schema_name

    def normalized(value):
        if isinstance(value, dict):
            return {key: normalized(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalized(item) for item in value]
        if isinstance(value, str):
            return value.replace(f"{schema}.", "<schema>.").replace(schema, "<schema>")
        return value

    result = {}
    for name in sorted(metadata.tables):
        columns = [{key: str(column[key]) if key == "type" else column[key]
                    for key in ("name", "type", "nullable", "default", "comment")}
                   for column in inspector.get_columns(name)]
        result[name] = normalized({
            "columns": columns,
            "primary_key": inspector.get_pk_constraint(name),
            "unique": sorted(inspector.get_unique_constraints(name), key=lambda row: row["name"]),
            "checks": sorted(inspector.get_check_constraints(name), key=lambda row: row["name"]),
            "foreign_keys": sorted(inspector.get_foreign_keys(name), key=lambda row: row["name"]),
            "indexes": sorted(inspector.get_indexes(name), key=lambda row: row["name"]),
        })
    return result


def test_sql_schema_matches_migration_and_app_can_ingest(empty_postgres, postgres_database, tmp_path):
    engine, schema = empty_postgres
    install_sql(engine, schema)
    assert set(inspect(engine).get_table_names()) == set(metadata.tables)
    assert describe_schema(engine) == describe_schema(postgres_database)
    require_schema(engine)
    migrate(engine)  # SQLの版登録が現行アプリにも認識され、再初期化されない。
    source = tmp_path / "source"
    source.mkdir()
    (source / "readme.txt").write_text("別環境への新規導入を検証する。\n", encoding="utf-8")
    settings = Settings(sources=[Source("ddl-validation", source)],
                        storage_dir=tmp_path / "storage", report_dir=tmp_path / "reports")
    assert ingest(settings, engine)["summary"]["registered"] == 1
    assert ingest(settings, engine)["summary"]["unchanged"] == 1
    with engine.connect() as connection:
        assert connection.execute(select(tables["schema_migrations"].c.version)).scalar_one() == SCHEMA_VERSION
        document = connection.execute(select(tables["documents"].c.document_id)).scalar_one()
    with pytest.raises(psycopg.errors.RaiseException, match="already exist"):
        install_sql(engine, schema)
    with engine.connect() as connection:
        assert connection.execute(select(tables["documents"].c.document_id)).scalar_one() == document


def test_failed_installation_rolls_back_all_tables(empty_postgres):
    engine, schema = empty_postgres
    script = SQL_PATH.read_text(encoding="utf-8").replace(
        "\nCOMMIT;", "\nSELECT file_scan_deliberately_missing_function();\nCOMMIT;")
    with pytest.raises(psycopg.errors.UndefinedFunction):
        install_sql(engine, schema, script)
    assert inspect(engine).get_table_names() == []
    install_sql(engine, schema)
    require_schema(engine)
