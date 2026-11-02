"""既存v3の取込・検索テーブルを、原本・行データを保持したままwinnapへ移す。"""

import argparse
import json

from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateSchema

from file_scan.config import load_config
from file_scan.db import SCHEMA_VERSION, open_database, tables
from file_scan.namespace import database_schema, validate_namespace
from file_scan.pipeline import ingestion_lock
from search.schema import metadata as search_metadata


def migrate_namespace(engine, *, source_schema="public"):
    """対象表の完全性・版・衝突を先に検査し、全表を1トランザクションで移す。"""
    if engine.dialect.name != "postgresql":
        raise RuntimeError("スキーマ移行はPostgreSQL専用です")
    validate_namespace(source_schema)
    ingestion_names = set(tables)
    search_names = {table.name for table in search_metadata.tables.values()}
    managed = ingestion_names | search_names
    with ingestion_lock(engine), engine.begin() as connection:
        target = database_schema(connection)
        if source_schema == target:
            raise RuntimeError("移行元と移行先は別のスキーマにしてください")
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        # 取込init-db・検索init-dbと同じ順序でDDL排他を取得する。
        connection.execute(text("SELECT pg_advisory_xact_lock(20261018, 1)"))
        connection.execute(text("SELECT pg_advisory_xact_lock(20261018, 2)"))
        inspector = inspect(connection)
        source_tables = set(inspector.get_table_names(schema=source_schema))
        target_tables = set(inspector.get_table_names(schema=target))
        legacy = source_tables & managed
        quote = connection.dialect.identifier_preparer.quote_identifier

        def versions(schema, table):
            return list(connection.exec_driver_sql(
                f"SELECT version FROM {quote(schema)}.{quote(table)}").scalars())

        def check_complete(schema, names):
            if not ingestion_names <= names:
                raise RuntimeError("取込テーブルが不足しています。移行前の環境でv3へ更新してください")
            if versions(schema, "schema_migrations") != [SCHEMA_VERSION]:
                raise RuntimeError("移行対象は取込スキーマv3だけです")
            if names & search_names:
                if not search_names <= names or versions(schema, "search_schema_migrations") != [1]:
                    raise RuntimeError("検索テーブルと検索スキーマ版が一致しません")

        if not legacy:
            check_complete(target, target_tables)
            return {"status": "unchanged", "schema": target, "moved_tables": 0}
        check_complete(source_schema, source_tables)
        target_objects = connection.execute(text(
            "SELECT c.relname FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema"
        ), {"schema": target}).first()
        if target_objects:
            raise RuntimeError("移行先スキーマに既存オブジェクトがあります。自動統合しません")
        connection.execute(CreateSchema(target, if_not_exists=True))
        for name in sorted(legacy):
            connection.exec_driver_sql(
                f"ALTER TABLE {quote(source_schema)}.{quote(name)} SET SCHEMA {quote(target)}")
        # ALTER TABLEは索引・所有シーケンス・FK参照を保持して移動する。
        check_complete(target, set(inspect(connection).get_table_names(schema=target)))
        return {"status": "migrated", "schema": target, "moved_tables": len(legacy)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    engine = None
    try:
        settings = load_config(args.config)
        engine = open_database(settings.database_url)
        print(json.dumps(migrate_namespace(engine), ensure_ascii=False))
        return 0
    except Exception as error:
        # 自作の検査メッセージ以外の例外原文には接続先・SQL値が含まれ得る。
        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print("スキーマ移行を完了できません: " + message)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
