"""新しい実PostgreSQLテストも、テスト専用DBの独立schemaに閉じ込める。"""

import os
from uuid import uuid4

import pytest
from sqlalchemy.schema import CreateSchema, DropSchema

from file_scan.db import migrate, open_database


@pytest.fixture
def postgres_database():
    url = os.environ.get("FILE_SCAN_TEST_DATABASE_URL")
    if not url:
        pytest.skip("実PostgreSQLテストにはFILE_SCAN_TEST_DATABASE_URLを指定")
    admin = open_database(url)
    schema = "test_file_scan_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = open_database(url, schema=schema)

    try:
        migrate(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()
