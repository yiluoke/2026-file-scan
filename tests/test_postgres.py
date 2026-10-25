"""実PostgreSQL上でpgvectorの保存・距離演算・HNSW索引を確認する。"""

import os

import pytest
from sqlalchemy import text

from file_scan.db import open_database


def test_pgvector_distance_and_hnsw_index():
    url = os.environ.get("FILE_SCAN_TEST_DATABASE_URL")
    if not url:
        pytest.skip("実PostgreSQLテストにはFILE_SCAN_TEST_DATABASE_URLを指定")
    engine = open_database(url)
    try:
        with engine.begin() as connection:
            assert connection.execute(text(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )).scalar_one()
            # 一時表のみを使う。アプリのEmbedding次元数や検索スキーマは変更しない。
            connection.execute(text(
                "CREATE TEMP TABLE vector_smoke (id integer, embedding vector(3)) ON COMMIT DROP"
            ))
            connection.execute(text(
                "INSERT INTO vector_smoke VALUES (1, '[1,0,0]'), (2, '[0,1,0]')"
            ))
            connection.execute(text(
                "CREATE INDEX vector_smoke_hnsw ON vector_smoke "
                "USING hnsw (embedding vector_cosine_ops)"
            ))
            connection.execute(text("SET LOCAL enable_seqscan = off"))
            query = ("SELECT id, embedding <=> '[1,0,0]'::vector AS distance "
                     "FROM vector_smoke ORDER BY embedding <=> '[1,0,0]'::vector LIMIT 1")
            row = connection.execute(text(query)).one()
            assert row.id == 1
            assert row.distance == pytest.approx(0)
            plan = connection.execute(text("EXPLAIN " + query)).scalars().all()
            assert any("vector_smoke_hnsw" in line for line in plan)
    finally:
        engine.dispose()
