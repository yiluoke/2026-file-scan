"""検索公開の取り下げを他の業務版と原本から分離する。"""

import json

import pytest
from sqlalchemy import select

from search import cli, schema
from search.management import PublicationError, create_release, publish, unpublish
from test_search import AUTH, body, publish_first
from test_search import search_db as search_db


def _release_request(release_id):
    return body(
        metadata_condition={
            "conditions": [
                {"name": "target_release", "comparison_operator": "is", "value": release_id},
            ]
        }
    )


def test_unpublish_preserves_original_and_other_release(search_db, monkeypatch, capsys):
    settings, engine, generation, release_id, factory, _ = search_db
    document_id = generation["document_id"]
    current_publication = publish_first(search_db)
    other_release_id = create_release(engine, settings, "release-2")
    other_publication = publish(
        engine, settings, other_release_id, document_id, generation["generation_id"], "reviewer"
    )
    with factory(allow_history=True) as client:
        result = client.post("/search-with-status", json=body(), headers=AUTH)
        original_ref = result.json()["records"][0]["metadata"]["source_ref"]
        assert client.get(original_ref, headers=AUTH).status_code == 200

    with engine.connect() as c:
        previous_revision = c.execute(select(schema.catalogs.c.revision)).scalar_one()
        previous_binding = dict(
            c.execute(select(schema.bindings).where(schema.bindings.c.release_id == release_id))
            .mappings()
            .one()
        )

    monkeypatch.setattr(cli, "load_config", lambda _: settings)
    monkeypatch.setattr(cli, "open_database", lambda _: engine)
    assert (
        cli.main(
            [
                "unpublish",
                "--config",
                "unused.yaml",
                "--release",
                "release-1",
                "--document-id",
                document_id,
                "--withdrawn-by",
                "publication-manager",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "release_id": release_id,
        "document_id": document_id,
        "withdrawn_publication_id": current_publication,
        "changed": True,
    }
    assert (
        unpublish(engine, settings, release_id, document_id, "second-attempt")["changed"] is False
    )
    with pytest.raises(PublicationError, match="release_document_not_found"):
        unpublish(engine, settings, release_id, "unknown-document", "publication-manager")

    with engine.connect() as c:
        assert c.execute(select(schema.catalogs.c.revision)).scalar_one() == previous_revision + 1
        binding = dict(
            c.execute(select(schema.bindings).where(schema.bindings.c.release_id == release_id))
            .mappings()
            .one()
        )
        assert binding == previous_binding | {"publication_id": None}
        assert set(c.execute(select(schema.publications.c.publication_id)).scalars()) == {
            current_publication,
            other_publication,
        }
        event = (
            c.execute(
                select(schema.events).where(
                    schema.events.c.result_status == "publication_withdrawn"
                )
            )
            .mappings()
            .one()
        )
        assert event["release_id"] == release_id
        assert event["summary"] == {
            "document_id": document_id,
            "publication_id": current_publication,
            "withdrawn_by": "publication-manager",
        }

    with factory(allow_history=True) as client:
        for request in (body(), _release_request(release_id)):
            result = client.post("/search-with-status", json=request, headers=AUTH)
            assert result.status_code == 200, result.text
            assert result.json()["records"] == []
            assert result.json()["retrieval_status"]["pending_updates"] == 1
        assert client.get(original_ref, headers=AUTH).status_code == 404
        result = client.post(
            "/search-with-status", json=_release_request(other_release_id), headers=AUTH
        )
        assert result.status_code == 200, result.text
        record = result.json()["records"][0]["metadata"]
        assert record["publication_id"] == other_publication
        assert client.get(record["source_ref"], headers=AUTH).status_code == 200

    assert publish_first(search_db) == current_publication
    with factory() as client:
        result = client.post("/search-with-status", json=body(), headers=AUTH)
        assert result.json()["records"][0]["metadata"]["publication_id"] == current_publication
