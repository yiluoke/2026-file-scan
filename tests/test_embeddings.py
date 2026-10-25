"""OpenAI SDKのHTTP要求・失敗・再実行と、実pgvectorへの保存を検証する。"""

import json
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import func, select

from file_scan import embeddings, pipeline
from file_scan.config import EmbeddingSettings, Settings, Source, load_config
from file_scan.db import tables


def test_embedding_config_supports_yaml_and_environment_without_exposing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text("sources: [{source_id: share, root: share}]\n"
                      "embedding: {enabled: true, api_key: sk-file-secret, dimensions: 512}\n")
    settings = load_config(config)
    assert settings.embedding.api_key == "sk-file-secret"
    assert "sk-file-secret" not in repr(settings)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-environment-secret")
    assert load_config(config).embedding.api_key == "sk-environment-secret"
    assert "sk-environment-secret" not in repr(load_config(config))


def test_disabled_embedding_and_placeholder_do_not_make_requests(monkeypatch):
    def unexpected(**kwargs):
        pytest.fail("APIへ接続してはいけません")
    monkeypatch.setattr(embeddings.openai, "OpenAI", unexpected)
    assert embeddings.embed(Settings(), None)["status"] == "disabled"
    with pytest.raises(embeddings.EmbeddingError, match="仮キー"):
        embeddings.embed(Settings(embedding=EmbeddingSettings(
            enabled=True, api_key="sk-placeholder-replace-me")), None)


@pytest.fixture
def configured(tmp_path, postgres_database):
    source = tmp_path / "share"
    source.mkdir()
    (source / "manual.txt").write_text("登録してから確認する。\n", encoding="utf-8")
    settings = Settings(sources=[Source("file-server", source)],
                        storage_dir=tmp_path / "storage", report_dir=tmp_path / "reports",
                        embedding=EmbeddingSettings(enabled=True, api_key="sk-private-test-key",
                                                    dimensions=3, batch_size=2))
    assert pipeline.ingest(settings, postgres_database)["status"] == "succeeded"
    return settings


def mock_openai(monkeypatch, handler):
    original = embeddings.openai.OpenAI

    def client(**kwargs):
        return original(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(embeddings.openai, "OpenAI", client)


def success(request):
    assert str(request.url) == "https://api.openai.com/v1/embeddings"
    assert request.headers["authorization"] == "Bearer sk-private-test-key"
    payload = json.loads(request.content)
    assert payload["model"].startswith("text-embedding-3-")
    assert payload["encoding_format"] == "float"
    return httpx.Response(200, json={
        "object": "list", "model": payload["model"],
        "data": [{"index": index, "object": "embedding",
                  "embedding": [1.0] + [0.0] * (payload["dimensions"] - 1)}
                 for index in reversed(range(len(payload["input"])))],
        "usage": {"prompt_tokens": 10, "total_tokens": 10},
    })


def test_sdk_pgvector_idempotency_and_model_profile(configured, postgres_database, monkeypatch):
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return success(request)
    mock_openai(monkeypatch, handle)
    first = embeddings.embed(configured, postgres_database)
    assert first["embedded"] == 1 and first["status"] == "succeeded"
    second = embeddings.embed(configured, postgres_database)
    assert second["unchanged"] == 1 and len(calls) == 1
    with postgres_database.begin() as connection:
        row = connection.execute(select(tables["chunk_embeddings"])).mappings().one()
        assert list(row["embedding"]) == [1, 0, 0]
        assert row["input_tokens"] > 0
        connection.execute(tables["chunks"].update().values(search_text="変更された検索用の文脈"))
    assert embeddings.embed(configured, postgres_database)["embedded"] == 1
    changed = replace(configured, embedding=replace(configured.embedding,
                                                   model="text-embedding-3-large", dimensions=2))
    result = embeddings.embed(changed, postgres_database)
    assert result["embedded"] == 1
    assert result["embedding_profile_id"] != first["embedding_profile_id"]
    with postgres_database.connect() as connection:
        assert connection.execute(select(func.count()).select_from(tables["chunk_embeddings"])).scalar_one() == 2
    assert "sk-private-test-key" not in next(configured.report_dir.glob("embed-*.json")).read_text()


def test_authentication_failure_preserves_chunks_and_redacts_errors(configured, postgres_database, monkeypatch):
    def handle(request):
        return httpx.Response(401, json={"error": {"message": "sk-private-test-key secret content",
                                                   "type": "invalid_request_error",
                                                   "code": "invalid_api_key"}})
    mock_openai(monkeypatch, handle)
    report = embeddings.embed(configured, postgres_database)
    assert report["status"] == "failed"
    assert report["error_code"] == "openai_authentication_error"
    with postgres_database.connect() as connection:
        assert connection.execute(select(func.count()).select_from(tables["chunks"])).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(tables["chunk_embeddings"])).scalar_one() == 0
        assert connection.execute(select(tables["index_jobs"].c.status).where(
            tables["index_jobs"].c.stage == "embed")).scalar_one() == "failed"
    text = next(configured.report_dir.glob("embed-*.json")).read_text()
    assert "sk-private-test-key" not in text and "secret content" not in text


def test_invalid_response_is_not_saved_and_retry_succeeds(configured, postgres_database, monkeypatch):
    valid = False

    def handle(request):
        if valid:
            return success(request)
        return httpx.Response(200, json={"object": "list", "model": "text-embedding-3-small",
            "data": [{"index": 0, "object": "embedding", "embedding": [1, 0]}],
            "usage": {"prompt_tokens": 10, "total_tokens": 10}})
    mock_openai(monkeypatch, handle)
    first = embeddings.embed(configured, postgres_database)
    assert first["status"] == "failed" and first["error_code"] == "embedding_invalid_vector"
    valid = True
    assert embeddings.embed(configured, postgres_database)["embedded"] == 1


def test_oversized_input_is_reported_without_sending(configured, postgres_database, monkeypatch):
    with postgres_database.begin() as connection:
        connection.execute(tables["chunks"].update().values(search_text="hello " * 9000))
    mock_openai(monkeypatch, lambda request: pytest.fail("上限超過の本文は送信しない"))
    report = embeddings.embed(configured, postgres_database)
    assert report["status"] == "failed" and report["failed"] == 1
    assert report["files"][0]["error_code"] == "embedding_input_token_limit"


def test_completed_batches_survive_failure_and_resume(configured, postgres_database, monkeypatch):
    for number in range(4):
        (configured.sources[0].root / f"extra{number}.txt").write_text(f"追加文書 {number}。\n")
    pipeline.ingest(configured, postgres_database)
    calls = []
    fail = True

    def handle(request):
        calls.append(json.loads(request.content))
        if fail and len(calls) == 2:
            return httpx.Response(401, json={"error": {"message": "denied",
                "type": "invalid_request_error", "code": "invalid_api_key"}})
        return success(request)
    mock_openai(monkeypatch, handle)
    first = embeddings.embed(configured, postgres_database)
    assert first["status"] == "partial"
    assert first["embedded"] == 2 and first["failed"] == 3
    assert len(calls) == 2
    fail = False
    resumed = embeddings.embed(configured, postgres_database)
    assert resumed["embedded"] == 3 and resumed["unchanged"] == 2
    assert resumed["status"] == "succeeded"
    with postgres_database.connect() as connection:
        assert connection.execute(select(func.count()).select_from(tables["chunk_embeddings"])).scalar_one() == 5
