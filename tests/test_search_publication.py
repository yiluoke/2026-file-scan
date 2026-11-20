"""検索公開の取り下げを他の業務版と原本から分離する。"""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from file_scan import pipeline
from file_scan.db import tables
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


def test_reingest_keeps_published_document_searchable_on_interruption(search_db, monkeypatch):
    settings, engine, _, release, factory, _ = search_db
    publication = publish_first(search_db)
    with factory(allow_history=True) as client:
        def assert_available():
            for payload in (body(), _release_request(release)):
                response = client.post("/retrieval", json=payload, headers=AUTH)
                assert response.status_code == 200, response.text
                assert response.json()["records"][0]["metadata"]["publication_id"] == publication

        collect = pipeline.collect

        def observe_scan(config):
            assert_available()
            return collect(config)

        def interrupt_snapshot(*args):
            raise KeyboardInterrupt

        monkeypatch.setattr(pipeline, "collect", observe_scan)
        with monkeypatch.context() as patch:
            patch.setattr(pipeline.Storage, "snapshot", interrupt_snapshot)
            with pytest.raises(KeyboardInterrupt):
                pipeline.ingest(settings, engine)
        assert_available()
        assert pipeline.ingest(settings, engine)["summary"]["unchanged"] == 1
        assert_available()


@pytest.mark.parametrize("failure", ["size_limit", "incomplete_scan"])
def test_collection_failure_is_scoped_to_knowledge(search_db, monkeypatch, failure):
    settings, engine, _, release, factory, _ = search_db
    publication = publish_first(search_db)
    other = replace(settings, knowledge_id="other")
    pipeline.ingest(other, engine)
    if failure == "size_limit":
        other = replace(other, max_file_bytes=1)
    else:
        monkeypatch.setattr(pipeline, "collect", lambda _: pipeline.ScanResult(errors=[
            {"source_id": "share", "relative_path": ".", "error_code": "directory_enumeration_failed"}]))
    assert pipeline.ingest(other, engine)["status"] == "partial"
    members = tables["knowledge_documents"]
    with engine.connect() as conn:
        states = dict(conn.execute(select(members.c.knowledge_id, members.c.collection_status)).all())
        assert states == {settings.knowledge_id: "available", "other": "failed"}
    with factory(allow_history=True) as client:
        for payload in (body(), _release_request(release)):
            response = client.post("/retrieval", json=payload, headers=AUTH)
            assert response.status_code == 200, response.text
            assert response.json()["records"][0]["metadata"]["publication_id"] == publication


def test_catalog_revision_tracks_collection_failure_and_cached_recovery(search_db):
    settings, engine, _, _, _, _ = search_db
    catalog = tables["knowledge_bases"]

    def revision():
        with engine.connect() as conn:
            return conn.scalar(select(catalog.c.catalog_revision)
                               .where(catalog.c.knowledge_id == settings.knowledge_id))

    before = revision()
    assert pipeline.ingest(settings, engine)["summary"]["unchanged"] == 1
    assert revision() == before
    assert pipeline.ingest(replace(settings, max_file_bytes=1), engine)["summary"]["failed"] == 1
    failed = revision()
    assert failed > before
    assert pipeline.ingest(settings, engine)["summary"]["unchanged"] == 1
    assert revision() > failed
