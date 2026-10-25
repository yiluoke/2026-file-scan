"""実DBの一時表で日本語検索・正規化・pgvectorとの同居を検証する。"""

import os

import pytest
from sqlalchemy import text

from file_scan.db import open_database


def test_pgroonga_japanese_index_and_vector_coexist():
    url = os.environ.get("FILE_SCAN_TEST_DATABASE_URL")
    if not url:
        pytest.skip("実PGroongaテストにはFILE_SCAN_TEST_DATABASE_URLを指定")
    engine = open_database(url)
    try:
        with engine.begin() as connection:
            extensions = dict(connection.execute(text(
                "SELECT extname, extversion FROM pg_extension "
                "WHERE extname IN ('vector', 'pgroonga')"
            )).all())
            assert set(extensions) == {"vector", "pgroonga"}
            connection.execute(text(
                "CREATE TEMP TABLE lexical_smoke "
                "(id integer, content text, embedding vector(3)) ON COMMIT DROP"
            ))
            connection.execute(text(
                "INSERT INTO lexical_smoke VALUES "
                "(1, '日次締め処理で E1203 が発生する条件', '[1,0,0]'), "
                "(2, '在庫照会の操作手順 E12030', '[0,1,0]'), "
                "(3, '監視のエラーコード Ｅ１２０３', '[0,0,1]')"
            ))
            connection.execute(text(
                "CREATE INDEX lexical_smoke_pgroonga ON lexical_smoke USING pgroonga "
                "(content) WITH (tokenizer='TokenBigram', normalizers='NormalizerAuto')"
            ))
            connection.execute(text("SET LOCAL enable_seqscan = off"))
            query = (
                "SELECT id, pgroonga_score(tableoid, ctid) AS score FROM lexical_smoke "
                "WHERE content &@ :keyword ORDER BY id"
            )
            rows = connection.execute(text(query), {"keyword": "日次締め"}).all()
            assert len(rows) == 1 and rows[0].id == 1 and rows[0].score > 0
            normalized = connection.execute(text(query), {"keyword": "E1203"}).all()
            assert [row.id for row in normalized] == [1, 3]
            assert connection.execute(text(query), {"keyword": "未登録の用語"}).all() == []
            plan = connection.execute(text("EXPLAIN " + query),
                                      {"keyword": "日次締め"}).scalars().all()
            assert any("lexical_smoke_pgroonga" in line for line in plan)
            assert connection.execute(text(
                "SELECT id FROM lexical_smoke ORDER BY embedding <=> '[1,0,0]'::vector "
                "LIMIT 1"
            )).scalar_one() == 1
    finally:
        engine.dispose()
