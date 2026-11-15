"""DB・外部APIが使えない環境でも設定から固定応答へ切り替えられることを確認。"""

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from file_scan.config import ConfigError
from search import api, cli, config, models

AUTH = {"Authorization": "Bearer test-mode-key"}
CONTENT = "これは検索APIのテスト応答です。DB検索は実行していません。"


def request_body(query="テスト質問"):
    return {
        "knowledge_id": "test",
        "query": query,
        "retrieval_setting": {"top_k": 1, "score_threshold": 1.0},
    }


@pytest.fixture
def test_config(tmp_path, monkeypatch):
    path = tmp_path / "search.yaml"
    path.write_text("search:\n  test_mode: true\n", encoding="utf-8")
    monkeypatch.setenv("FILE_SCAN_SEARCH_API_KEY", "test-mode-key")
    monkeypatch.setattr(models, "count_tokens", forbidden)
    return path


def forbidden(*args, **kwargs):
    pytest.fail("テストモードでDB・検索サービス・取込設定を使用しました")


def test_fixed_response_without_database_or_embedding(test_config, monkeypatch):
    monkeypatch.setattr(config, "load_config", forbidden)
    monkeypatch.setattr(api, "async_database", forbidden)
    monkeypatch.setattr(api, "RetrievalService", forbidden)

    class ForbiddenEngine:
        def __getattr__(self, name):
            forbidden()

    settings = config.load_search_settings(test_config)
    assert settings.test_mode is True
    assert settings.ingestion is None
    app = api.create_app(settings, engine=ForbiddenEngine(), embedder=forbidden)
    assert app.state.service is None
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["execution_mode"] == "test"
        assert health.json()["database"] == "not_used"
        assert client.get("/openapi.json").status_code == 200
        expected_records = None
        request_ids = set()
        for query in ("DB未接続の質問", "別の質問", "機密テスト入力"):
            for route in ("/search-with-status", "/retrieval"):
                response = client.post(route, json=request_body(query), headers=AUTH)
                assert response.status_code == 200, response.text
                data = response.json()
                assert len(data["records"]) == 1
                record = data["records"][0]
                assert record["content"] == CONTENT
                assert record["metadata"]["test_mode"] is True
                assert "source_ref" not in record["metadata"]
                assert query not in response.text
                if expected_records is None:
                    expected_records = data["records"]
                assert data["records"] == expected_records
                if route == "/search-with-status":
                    request_ids.add(data["request_id"])
                    status = data["retrieval_status"]
                    assert status["execution_mode"] == "test"
                    assert status["coverage_state"] == "unknown"
                    assert status["resolved_release_id"] is None
                    assert status["embedding_profile_id"] is None
                    assert status["pending_updates"] is None
                else:
                    assert set(data) == {"records"}
        assert len(request_ids) == 3
        assert client.get("/sources/any-publication", headers=AUTH).status_code == 404


@pytest.mark.parametrize("route", ["/search-with-status", "/retrieval"])
def test_test_mode_preserves_authentication_and_validation(test_config, route):
    with TestClient(api.create_app(config.load_search_settings(test_config))) as client:
        assert client.post(route, json=request_body()).status_code == 401
        assert client.post(route, json={}, headers=AUTH).status_code == 422
        invalid = request_body() | {"test_mode": False}
        assert client.post(route, json=invalid, headers=AUTH).status_code == 422
        assert client.post(route, content="not JSON", headers=AUTH).status_code == 415


@pytest.mark.parametrize("section", [{}, {"search": {}}, {"search": {"test_mode": False}}])
def test_normal_mode_is_default(tmp_path, monkeypatch, section):
    path = tmp_path / "config.yaml"
    raw = {"sources": [{"source_id": "sample", "root": "share"}]} | section
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("FILE_SCAN_SEARCH_API_KEY", "test-mode-key")
    settings = config.load_search_settings(path)
    assert settings.test_mode is False
    assert settings.ingestion.sources[0].source_id == "sample"


@pytest.mark.parametrize("mode", ["true", "false", 1, 0, None, [], {}])
def test_mode_requires_yaml_boolean(test_config, mode):
    test_config.write_text(yaml.safe_dump({"search": {"test_mode": mode}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="search.test_mode"):
        config.load_search_settings(test_config)


@pytest.mark.parametrize("minimum", [-0.1, 1.1, "0.5", True, float("nan")])
def test_semantic_minimum_rejects_invalid_yaml_values(test_config, minimum):
    text = yaml.safe_dump({"search": {"test_mode": True, "semantic_min_cosine_similarity": minimum}})
    test_config.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="search.semantic_min_cosine_similarity"):
        config.load_search_settings(test_config)


@pytest.mark.parametrize("section", [None, "true", [], True])
def test_search_section_requires_mapping(test_config, section):
    test_config.write_text(yaml.safe_dump({"search": section}), encoding="utf-8")
    with pytest.raises(ConfigError, match="search"):
        config.load_search_settings(test_config)


def test_connection_key_is_required_in_test_mode(test_config, monkeypatch):
    monkeypatch.delenv("FILE_SCAN_SEARCH_API_KEY")
    with pytest.raises(ConfigError, match="FILE_SCAN_SEARCH_API_KEY"):
        config.load_search_settings(test_config)


def test_cli_serves_test_mode_without_ingestion_config(test_config, monkeypatch):
    import uvicorn

    calls = []
    monkeypatch.setattr(cli, "load_config", forbidden)
    monkeypatch.setattr(cli, "open_database", forbidden)
    monkeypatch.setattr(api, "async_database", forbidden)
    monkeypatch.setattr(api, "RetrievalService", forbidden)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    assert cli.main(["serve", "--config", str(test_config)]) == 0
    assert len(calls) == 1
    app, options = calls[0]
    assert options["host"] == "127.0.0.1"
    with TestClient(app) as client:
        response = client.post("/retrieval", json=request_body(), headers=AUTH)
        assert response.status_code == 200
        assert response.json()["records"][0]["content"] == CONTENT
