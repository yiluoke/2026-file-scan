"""検索公開・HTTP契約・更新時の隔離と既存取り込みの互換性を実DBで検証する。"""

import asyncio
import json

import httpx
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("greenlet")

from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, text

from file_scan import embeddings
from file_scan.config import EmbeddingSettings, Settings, Source
from file_scan.db import metadata as ingestion_metadata, migrate as ingest_migrate, tables
from file_scan.pipeline import ingest
from search import schema
from search.api import create_app
from search.config import SearchSettings, async_database
from search.management import PublicationError, create_release, publish
from search.models import SearchRequest
from search.ranking import fuse, identifiers

AUTH = {"Authorization": "Bearer search-test-key"}


def body(**updates):
    return {
        "knowledge_id": "test",
        "query": "日次締め E1203",
        "retrieval_setting": {"top_k": 5, "score_threshold": 0.0},
        **updates,
    }


@pytest.fixture
def search_db(tmp_path, postgres_database, monkeypatch):
    root = tmp_path / "share"
    root.mkdir()
    (root / "manual.txt").write_text("日次締め E1203 の発生条件は締め状態が確定済みの場合です。\n")
    settings = Settings(
        sources=[Source("share", root)],
        knowledge_id="test",
        storage_dir=tmp_path / "storage",
        report_dir=tmp_path / "reports",
        embedding=EmbeddingSettings(enabled=True, api_key="test-api-key", dimensions=3),
    )
    ingest(settings, postgres_database)
    original = embeddings.openai.OpenAI

    def response(request):
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [1.0, 0.0, 0.0]}
                    for i, _ in enumerate(payload["input"])
                ],
                "model": payload["model"],
                "object": "list",
            },
        )

    monkeypatch.setattr(
        embeddings.openai,
        "OpenAI",
        lambda **kwargs: original(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(response))
        ),
    )
    assert embeddings.embed(settings, postgres_database)["embedded"] == 1
    before = set(ingestion_metadata.tables)
    schema.migrate(postgres_database)
    schema.migrate(postgres_database)
    assert set(ingestion_metadata.tables) == before
    # 既存CLIのinit-dbも検索表を残したまま再実行できる。
    ingest_migrate(postgres_database)
    release = create_release(postgres_database, settings, "release-1")
    with postgres_database.connect() as c:
        generation = dict(c.execute(select(tables["extraction_generations"])).mappings().one())
        namespace = c.execute(text("SELECT current_schema() ")).scalar_one()
    calls = []

    async def embedder(query, profile):
        calls.append((query, profile["dimensions"]))
        return [1.0, 0.0, 0.0]

    config = SearchSettings(settings, "search-test-key")

    def client_factory(config=config, embedder=embedder, allow_history=False):
        engine = async_database(
            postgres_database.url, schema=namespace
        )
        return TestClient(
            create_app(config, engine=engine, embedder=embedder, allow_history=allow_history)
        )

    yield settings, postgres_database, generation, release, client_factory, calls


def publish_first(fixture, **kwargs):
    settings, engine, gen, release, _, _ = fixture
    return publish(
        engine,
        settings,
        release,
        gen["document_id"],
        gen["generation_id"],
        "test-reviewer",
        **kwargs,
    )


def test_publish_search_and_ingestion_remains_draft(search_db):
    settings, engine, gen, release, factory, calls = search_db
    pub = publish_first(search_db)
    assert publish_first(search_db) == pub
    with factory() as client:
        assert client.get("/healthz").status_code == 200
        contract = client.get("/openapi.json").json()
        assert "SearchResponse" in contract["components"]["schemas"]
        assert "HTTPBearer" in contract["components"]["securitySchemes"]
        result = client.post("/search-with-status", json=body(), headers=AUTH)
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["records"][0]["metadata"]["publication_id"] == pub
        assert data["records"][0]["content"].startswith("日次締め")
        assert data["records"][0]["score"] == pytest.approx(1)
        assert data["retrieval_status"]["coverage_state"] == "complete"
        assert data["retrieval_status"]["access_control"] == "not_enforced"
        assert data["retrieval_status"]["policy_revision"] is None
        assert len(calls) == 1
        compat = client.post("/retrieval", json=body(), headers=AUTH)
        assert compat.status_code == 200 and set(compat.json()) == {"records"}
        metadata = data["records"][0]["metadata"]
        source = client.get(metadata["source_ref"], headers=AUTH)
        assert source.status_code == 200
        assert source.content == (settings.sources[0].root / "manual.txt").read_bytes()
    with engine.connect() as c:
        assert (
            c.execute(select(tables["extraction_generations"].c.publication_state)).scalar_one()
            == "draft"
        )
        assert c.execute(select(tables["knowledge_bases"].c.enabled)).scalar_one() is False
        assert c.execute(select(schema.events.c.request_id)).first()
        assert "E1203" not in str(c.execute(select(schema.events)).all())
    assert ingest(settings, engine)["summary"]["unchanged"] == 1
    assert embeddings.embed(settings, engine)["unchanged"] == 1
    with factory() as client:
        assert client.post("/search-with-status", json=body(), headers=AUTH).status_code == 200


def test_unpublished_not_returned_and_compat_rejects_incomplete(search_db):
    _, _, _, _, factory, calls = search_db
    with factory() as client:
        result = client.post("/search-with-status", json=body(), headers=AUTH)
        assert result.status_code == 200, result.text
        assert result.json()["records"] == []
        assert result.json()["retrieval_status"]["pending_updates"] == 1
        assert client.post("/retrieval", json=body(), headers=AUTH).status_code == 409
    assert calls == []


def test_update_excludes_old_current_and_history_is_explicit(search_db):
    settings, engine, gen, release, factory, calls = search_db
    pub = publish_first(search_db)
    old = settings.sources[0].root / "manual.txt"
    old.write_text("更新後の仕様 E9999。\n")
    ingest(settings, engine)
    with factory(allow_history=True) as client:
        current = client.post("/search-with-status", json=body(), headers=AUTH)
        assert current.status_code == 200, current.text
        assert current.json()["records"] == []
        assert current.json()["retrieval_status"]["pending_updates"] == 1
        assert calls == []
        history = body(
            metadata_condition={
                "conditions": [
                    {"name": "target_release", "comparison_operator": "is", "value": release}
                ]
            }
        )
        response = client.post("/search-with-status", json=history, headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.json()["records"][0]["metadata"]["publication_id"] == pub
    with factory() as client:
        assert client.post("/search-with-status", json=history, headers=AUTH).status_code == 404


def test_deleted_source_removed_even_from_history(search_db):
    settings, engine, _, release, factory, calls = search_db
    pub = publish_first(search_db)
    (settings.sources[0].root / "manual.txt").unlink()
    ingest(settings, engine)
    with factory(allow_history=True) as client:
        response = client.post("/search-with-status", json=body(), headers=AUTH)
        assert response.status_code == 200
        assert response.json()["records"] == []
        assert response.json()["retrieval_status"]["failed_documents"] == 1
        assert client.get("/sources/" + pub, headers=AUTH).status_code == 404
    assert calls == []


def test_missing_vectors_are_reported_as_incomplete_coverage(search_db):
    settings, engine, gen, _, factory, _ = search_db
    with engine.begin() as c:
        c.execute(tables["chunk_embeddings"].delete())
    publish_first(search_db)
    with factory() as client:
        response = client.post("/search-with-status", json=body(), headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.json()["records"]
        assert response.json()["retrieval_status"]["index_pending_documents"] == 1
        assert client.post("/retrieval", json=body(), headers=AUTH).status_code == 409


def test_unusable_fulltext_index_rejects_readiness_and_search_before_embedding(search_db):
    _, engine, _, _, factory, calls = search_db
    publish_first(search_db)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_search_chunks_fulltext"))
        # A same-name index using a different access method does not restore the PGroonga lane.
        connection.execute(text(
            "CREATE INDEX ix_search_chunks_fulltext ON search_chunks (search_text)"
        ))
    with pytest.raises(schema.SearchIndexError, match="ix_search_chunks_fulltext"):
        schema.migrate(engine)
    with factory() as client:
        assert client.get("/healthz").status_code == 503
        for route in ("/search-with-status", "/retrieval"):
            result = client.post(route, json=body(), headers=AUTH)
            assert result.status_code == 503
            assert result.json()["code"] == "SEARCH_UNAVAILABLE"
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        body(knowledge_id="other"),
        body(
            metadata_condition={
                "conditions": [{"name": "feature_id", "comparison_operator": "is", "value": "x"}]
            }
        ),
    ],
)
def test_scope_and_unconfigured_feature_fail_before_embedding(search_db, payload):
    _, _, _, _, factory, calls = search_db
    with factory() as client:
        response = client.post("/search-with-status", json=payload, headers=AUTH)
        assert response.status_code in {404, 422}
    assert calls == []


def test_validation_auth_size_and_no_secret_echo(search_db):
    _, _, _, _, factory, _ = search_db
    with factory() as client:
        assert client.post("/search-with-status", json=body()).status_code == 401
        invalid = body(query="secret-text", retrieval_setting={"top_k": True, "score_threshold": 0})
        response = client.post("/search-with-status", json=invalid, headers=AUTH)
        assert response.status_code == 422 and "secret-text" not in response.text
        assert (
            client.post(
                "/search-with-status",
                content="x" * 32769,
                headers=AUTH | {"Content-Type": "application/json"},
            ).status_code
            == 413
        )
        assert client.post("/search-with-status", content="text", headers=AUTH).status_code == 415
        assert (
            client.post(
                "/search-with-status",
                content="{",
                headers=AUTH | {"Content-Type": "application/json"},
            ).status_code
            == 422
        )


def test_embedding_failure_and_deadline_are_not_empty_results(search_db, caplog):
    settings, engine, _, _, factory, _ = search_db
    publish_first(search_db)

    async def fail(query, profile):
        raise RuntimeError("secret-failure-body")

    with factory(embedder=fail) as client:
        result = client.post("/search-with-status", json=body(), headers=AUTH)
        assert result.status_code == 500 and "secret-failure-body" not in result.text

    async def slow(query, profile):
        await asyncio.sleep(1)
        return [1.0, 0.0, 0.0]

    gateway_id = "ab" * 16
    for total, embedding, kind in [(10, 0.02, "embedding_timeout"), (0.5, 4, "request_deadline")]:
        config = SearchSettings(
            settings, "search-test-key", total_timeout=total, embedding_timeout=embedding
        )
        with factory(config=config, embedder=slow) as client:
            result = client.post(
                "/search-with-status", json=body(), headers=AUTH | {"X-Gateway-Request-ID": gateway_id}
            )
            assert result.status_code == 504
            data = result.json()
            request_id = data["request_id"]
            assert data["code"] == "SEARCH_TIMEOUT" and data["error_code"] == 5003
            assert data["stage"] == "query_embedding" and data["timeout_kind"] == kind
            assert data["elapsed_ms"] >= min(total, embedding) * 1000
            assert result.headers["X-Request-ID"] == request_id
            assert (
                f"request_id={request_id} gateway_request_id={gateway_id} stage=query_embedding"
                in caplog.text
            )
            assert body()["query"] not in caplog.text and AUTH["Authorization"] not in caplog.text
            assert client.get("/healthz").status_code == 200
        with engine.connect() as c:
            event = c.execute(
                select(schema.events).where(schema.events.c.request_id == request_id)
            ).mappings().one()
            assert event["error_code"] == "SEARCH_TIMEOUT"
            assert event["summary"]["stage"] == "query_embedding"
            assert event["summary"]["timeout_kind"] == kind


def test_pool_timeout_is_traceable_when_event_cannot_be_written(search_db, caplog):
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    class ExhaustedPool:
        def connect(self):
            raise PoolTimeout("private-db-details")

        def begin(self):
            raise PoolTimeout("private-db-details")

    _, _, _, _, factory, _ = search_db
    with factory() as client:
        client.app.state.service.engine = ExhaustedPool()
        response = client.post("/search-with-status", json=body(), headers=AUTH)
    data = response.json()
    assert response.status_code == 504
    assert data["stage"] == "db_connection" and data["timeout_kind"] == "db_pool_timeout"
    assert f"search_event_write_failed request_id={data['request_id']}" in caplog.text
    assert f"search_timeout request_id={data['request_id']}" in caplog.text
    assert "private-db-details" not in caplog.text + response.text


def test_partial_approval_is_explicit(search_db):
    _, engine, gen, _, _, _ = search_db
    with engine.begin() as c:
        c.execute(
            tables["extraction_generations"]
            .update()
            .where(tables["extraction_generations"].c.generation_id == gen["generation_id"])
            .values(
                processing_status="partial",
                quality_summary=gen["quality_summary"] | {"pending_images": 1},
            )
        )
    with pytest.raises(PublicationError, match="partial_requires"):
        publish_first(search_db)
    assert publish_first(search_db, allow_partial=True)


def test_rrf_identifiers_and_input_boundaries():
    assert fuse([[("g", "a")]] + [[]] * 2)[0][1] == pytest.approx(1 / 3)
    assert fuse([[("g", "a")]] * 3)[0][1] == pytest.approx(1)
    assert identifiers("`Do_Work` E1203 src/a.py") == ["Do_Work", "E1203", "src/a.py"]
    for conditions in (None, {"conditions": []}):
        assert SearchRequest.model_validate(body(metadata_condition=conditions))
    for changes in (
        {"retrieval_setting": {"top_k": "5", "score_threshold": 0}},
        {"retrieval_setting": {"top_k": 21, "score_threshold": 0}},
        {"retrieval_setting": {"top_k": 5, "score_threshold": float("nan")}},
        {"metadata_condition": {"logical_operator": "or", "conditions": []}},
        {"query": " "},
        {"query": "x" * 4001},
        {"user_id": "admin"},
    ):
        with pytest.raises(ValidationError):
            SearchRequest.model_validate(body(**changes))


def test_context_and_token_budget_keep_original_positions(search_db):
    settings, engine, _, release, factory, _ = search_db
    (settings.sources[0].root / "note.md").write_text("# 前提条件\n\n手順 E5555 を確認します。\n")
    ingest(settings, engine)
    embeddings.embed(settings, engine)
    with engine.connect() as c:
        docs = tables["documents"]
        gen = (
            c.execute(
                select(tables["extraction_generations"])
                .join(docs, docs.c.document_id == tables["extraction_generations"].c.document_id)
                .where(docs.c.source_key == "note.md")
            )
            .mappings()
            .one()
        )
    publish(engine, settings, release, gen["document_id"], gen["generation_id"], "reviewer")
    payload = body(
        query="E5555",
        retrieval_setting={"top_k": 3, "score_threshold": 0},
        metadata_condition={
            "conditions": [
                {"name": "document_id", "comparison_operator": "is", "value": gen["document_id"]}
            ]
        },
    )
    with factory() as client:
        response = client.post("/search-with-status", json=payload, headers=AUTH)
        assert response.status_code == 200, response.text
        records = response.json()["records"]
        assert len(records) == 2
        context = next(r for r in records if r["metadata"]["evidence_role"] == "context")
        assert context["content"] == "# 前提条件"
        assert context["metadata"]["source_locations"][0]["start_line"] == 1
        assert context["metadata"]["supporting_hit_ids"]
    from file_scan.tokens import count_tokens

    config = SearchSettings(settings, "search-test-key", token_budget=10, record_token_budget=8)
    with factory(config=config) as client:
        response = client.post("/search-with-status", json=body(), headers=AUTH)
        assert response.status_code == 200, response.text
        records = response.json()["records"]
        assert sum(count_tokens(r["content"]) for r in records) <= 10
        assert all(count_tokens(r["content"]) <= 8 for r in records)
        assert "token_budget" in response.json()["retrieval_status"]["limit_reasons"]


def test_freshness_change_inside_snapshot_discards_results(search_db, monkeypatch):
    _, engine, gen, _, factory, _ = search_db
    publish_first(search_db)
    from search import repository

    original = repository.lanes

    async def change_after_candidates(*args, **kwargs):
        result = await original(*args, **kwargs)
        with engine.begin() as c:
            c.execute(
                tables["documents"]
                .update()
                .where(tables["documents"].c.document_id == gen["document_id"])
                .values(source_status="unavailable")
            )
        return result

    monkeypatch.setattr(repository, "lanes", change_after_candidates)
    with factory() as client:
        response = client.post("/search-with-status", json=body(), headers=AUTH)
        assert response.status_code == 503, response.text
        assert response.json()["retryable"] is True
        assert "records" not in response.json()


def test_external_embedding_does_not_hold_database_connection(search_db):
    _, _, _, _, factory, _ = search_db
    publish_first(search_db)
    engines = []

    async def spy(query, profile):
        assert engines[0].pool.checkedout() == 0
        return [1.0, 0.0, 0.0]

    with factory(embedder=spy) as client:
        engines.append(client.app.state.service.engine)
        assert client.post("/search-with-status", json=body(), headers=AUTH).status_code == 200
        assert engines[0].pool.checkedout() == 0


def test_threshold_no_match_and_feature_filter(search_db):
    publish_first(search_db, feature_ids=["closing"])
    *_, factory, calls = search_db
    with factory() as client:
        request = body(query="ZZZ_NO_MATCH", retrieval_setting={"top_k": 5, "score_threshold": 1})
        response = client.post("/search-with-status", json=request, headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.json()["records"] == []
        assert response.json()["retrieval_status"]["result_state"] == "no_match"
        calls.clear()
        filtered = body(
            metadata_condition={
                "conditions": [
                    {"name": "feature_id", "comparison_operator": "is", "value": "other-feature"}
                ]
            }
        )
        assert client.post("/retrieval", json=filtered, headers=AUTH).json() == {"records": []}
        assert calls == []


@pytest.mark.parametrize(
    ("vector", "minimum"), [([-1.0, 0.0, 0.0], 0.0), ([0.1, 1.0, 0.0], 0.5)]
)
def test_weak_semantic_neighbors_do_not_become_evidence_at_zero_rrf_threshold(
    search_db, vector, minimum
):
    settings, _, _, _, factory, _ = search_db
    publish_first(search_db)

    async def embed(query, profile):
        return vector

    config = SearchSettings(settings, "search-test-key", semantic_min_cosine_similarity=minimum)
    with factory(config=config, embedder=embed) as client:
        response = client.post(
            "/search-with-status", json=body(query="恒星物理学超新星爆発"), headers=AUTH
        )
        assert response.status_code == 200, response.text
        assert response.json()["retrieval_status"]["coverage_state"] == "complete"
        assert response.json()["retrieval_status"]["result_state"] == "no_match"
        assert response.json()["records"] == []


def test_source_corruption_fails_before_streaming(search_db):
    settings, _, gen, _, factory, _ = search_db
    pub = publish_first(search_db)
    checksum = gen["source_version"].split(":")[1]
    path = settings.storage_dir / "objects" / "sha256" / checksum[:2] / checksum
    path.write_bytes(b"corrupted-private-body")
    with factory() as client:
        response = client.get("/sources/" + pub, headers=AUTH)
        assert response.status_code == 503
        assert "corrupted-private-body" not in response.text


def test_disconnect_cancels_embedding_and_releases_connections(search_db):
    settings, engine, _, _, _, _ = search_db
    publish_first(search_db)
    with engine.connect() as c:
        namespace = c.execute(text("SELECT current_schema()")).scalar_one()

    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def slow(query, profile):
            entered.set()
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()

        asynchronous = async_database(
            engine.url, schema=namespace
        )
        app = create_app(
            SearchSettings(settings, "search-test-key"), engine=asynchronous, embedder=slow
        )
        consumed = False

        async def receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {
                    "type": "http.request",
                    "body": json.dumps(body()).encode(),
                    "more_body": False,
                }
            await entered.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "POST",
            "path": "/search-with-status",
            "raw_path": b"/search-with-status",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"authorization", b"Bearer search-test-key"),
            ],
            "server": ("localhost", 8000),
            "client": ("localhost", 123),
            "scheme": "http",
        }
        try:
            try:
                await asyncio.wait_for(app(scope, receive, send), timeout=2)
            except asyncio.CancelledError:
                pass
            assert cancelled.is_set()
            assert asynchronous.pool.checkedout() == 0
        finally:
            await asynchronous.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("stage,sql_marker", [
    ("vector_search", "<=>"), ("fulltext_search", "pgroonga_tokenize"),
])
def test_database_timeout_cancels_statement_and_pool_recovers(search_db, stage, sql_marker):
    settings, _, _, _, factory, _ = search_db
    publish_first(search_db)
    from sqlalchemy import event

    config = SearchSettings(settings, "search-test-key", statement_timeout_ms=100)
    with factory(config=config) as client:
        engine = client.app.state.service.engine

        @event.listens_for(engine.sync_engine, "before_cursor_execute", retval=True)
        def delay_lane(conn, cursor, statement, parameters, context, executemany):
            if sql_marker in statement:
                return "SELECT pg_sleep(0.5)", ()
            return statement, parameters

        response = client.post("/retrieval", json=body(), headers=AUTH)
        assert response.status_code == 504, response.text
        assert response.json()["stage"] == stage
        assert response.json()["timeout_kind"] == "db_query_cancelled"
        assert client.app.state.service.engine.pool.checkedout() == 0
        assert client.get("/healthz").status_code == 200


def test_query_embedding_uses_saved_profile_and_does_not_retry(search_db, monkeypatch):
    settings, *_ = search_db
    from search.errors import SearchError
    from search.service import QueryEmbedding
    import openai

    original = openai.AsyncOpenAI
    calls = []
    failure = None

    async def handle(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["model"] == "text-embedding-3-large"
        assert payload["dimensions"] == 2
        assert payload["input"] == ["質問文だけ"]
        if failure == "timeout":
            raise httpx.ReadTimeout("private-timeout-detail", request=request)
        if failure == "server_error":
            return httpx.Response(500, json={"error": {"message": "private-error-body"}})
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [1.0, 0.0]}],
                "object": "list",
                "model": payload["model"],
            },
        )

    def factory(**kwargs):
        assert kwargs["max_retries"] == 0 and kwargs["timeout"] == 4
        return original(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )

    monkeypatch.setattr(openai, "AsyncOpenAI", factory)
    profile = {
        "provider": "openai",
        "model": "text-embedding-3-large",
        "dimensions": 2,
        "input_version": "search_text-v1",
    }
    embedder = QueryEmbedding(SearchSettings(settings, "search-test-key"))
    assert asyncio.run(embedder("質問文だけ", profile)) == [1.0, 0.0]
    failure = "server_error"
    with pytest.raises(SearchError) as error:
        asyncio.run(embedder("質問文だけ", profile))
    assert error.value.code == "SEARCH_UNAVAILABLE" and error.value.retryable
    assert "private-error-body" not in str(error.value)
    failure = "timeout"
    with pytest.raises(SearchError) as error:
        asyncio.run(embedder("質問文だけ", profile))
    assert error.value.code == "SEARCH_TIMEOUT" and error.value.timeout_kind == "embedding_timeout"
    assert len(calls) == 3
