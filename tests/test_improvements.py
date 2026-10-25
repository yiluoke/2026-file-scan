"""調査で再現した条件と、キャッシュ・バッチ・I/Oの改善を回帰検証する。"""

import hashlib
import json
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from file_scan import embeddings, pipeline
from file_scan.chunking import build_chunks
from file_scan.cli import _status
from file_scan.collector import collect
from file_scan.config import EmbeddingSettings, Settings, Source
from file_scan.db import SCHEMA_VERSION, migrate, tables
from file_scan.domain import Block, ExtractionResult
from file_scan.models import ModelIntegrityError, verify_models
from file_scan.profiles import conversion_profile, digest
from file_scan.state import input_hash_sql
from file_scan.storage import Storage
from file_scan.tokens import count_tokens
from file_scan.workers import ParsePool, WorkerError
from test_embeddings import mock_openai, success


@pytest.fixture
def settings(tmp_path):
    source = tmp_path / "share"
    source.mkdir()
    (source / "note.txt").write_text("Short source text.\n")
    return Settings(sources=[Source("share", source)], storage_dir=tmp_path / "storage",
                    report_dir=tmp_path / "reports", pdf_backend="native",
                    embedding=EmbeddingSettings(enabled=True, api_key="sk-private-test-key",
                                                dimensions=3, batch_size=2, read_batch_size=3))


def test_selected_generation_never_falls_back_and_config_must_match(settings, postgres_database, monkeypatch):
    first = pipeline.ingest(settings, postgres_database)
    changed = replace(settings, chunk_max_characters=900)
    save = pipeline._save_payload
    monkeypatch.setattr(pipeline, "_save_payload", lambda *_: (_ for _ in ()).throw(RuntimeError()))
    failed = pipeline.ingest(changed, postgres_database)
    assert failed["summary"]["failed"] == 1
    with postgres_database.connect() as connection:
        assert embeddings._targets(connection, settings.knowledge_id) == []
    mock_openai(monkeypatch, lambda _: pytest.fail("旧世代をAPIへ送ってはいけない"))
    report = embeddings.embed(changed, postgres_database)
    assert report["status"] == "failed" and report["unready_documents"] == {"failed": 1}
    monkeypatch.setattr(pipeline, "_save_payload", save)
    pipeline.ingest(changed, postgres_database)
    mismatch = embeddings.embed(settings, postgres_database)
    assert mismatch["unready_documents"] == {"config_mismatch": 1}
    back = pipeline.ingest(settings, postgres_database)
    assert back["files"][0]["generation_id"] == first["files"][0]["generation_id"]
    with postgres_database.connect() as connection:
        assert {row["generation_id"] for row in embeddings._targets(connection, settings.knowledge_id)} == {
            first["files"][0]["generation_id"]}


def test_stage_caches_reuse_extraction_and_legacy_vectors(settings, postgres_database, monkeypatch):
    calls = []

    def extract(*_):
        calls.append("parse")
        return ExtractionResult(blocks=[Block("p", "paragraph", "Same input", [{"line": 1}])])

    monkeypatch.setattr(pipeline, "extract", extract)
    requests = []
    mock_openai(monkeypatch, lambda request: (requests.append(request), success(request))[1])
    pipeline.ingest(settings, postgres_database)
    assert embeddings.embed(settings, postgres_database)["embedded"] == 1
    # v2の既存ベクトルだけが残る場合も、同じ入力を再送しない。
    with postgres_database.begin() as connection:
        connection.execute(tables["embedding_cache"].delete())
    changed = replace(settings, chunk_max_characters=900)
    second = pipeline.ingest(changed, postgres_database)
    assert second["files"][0]["extraction_cached"] is True and calls == ["parse"]
    result = embeddings.embed(changed, postgres_database)
    assert result["reused"] == 1 and result["embedded"] == 0 and len(requests) == 1
    other_model = replace(changed, embedding=replace(changed.embedding, dimensions=2))
    assert embeddings.embed(other_model, postgres_database)["embedded"] == 1
    assert len(requests) == 2


def test_tables_repeat_headers_keep_rows_and_original_offsets():
    text = "| RatedVoltage | Unit |\n|---|---|\n" + "".join(f"| {i} | V |\n" for i in range(70))
    block = Block("table", "table", text, [{"page_number": 1}], content_data={"table": {
        "table_cells": [{"text": "RatedVoltage", "column_header": True},
                        {"text": "Unit", "column_header": True}]}})
    chunks = build_chunks([block], "spec.pdf", 100)
    assert len(chunks) > 3 and all("RatedVoltage" in chunk.search_text for chunk in chunks)
    assert "".join(chunk.content_text for chunk in chunks) == text
    for chunk in chunks:
        assert chunk.content_text.endswith("\n")
        for mapping in chunk.blocks:
            assert text[mapping.block_char_start:mapping.block_char_end] == chunk.content_text[
                mapping.chunk_char_start:mapping.chunk_char_end]


def test_long_context_and_multibyte_body_obey_full_token_budget():
    text = "日本語🙂と境界。" * 600
    block = Block("p", "paragraph", text, [{"line": 1}], section_path=["heading " * 9000])
    chunks = build_chunks([block], "long title " * 1000, 1800, 256, 64)
    assert len(chunks) > 1
    assert "".join(chunk.content_text for chunk in chunks) == text
    assert all(chunk.token_count == count_tokens(chunk.search_text) <= 256 for chunk in chunks)
    assert all("search_context_truncated" in chunk.quality_flags for chunk in chunks)


def test_tiny_context_budget_still_preserves_complete_body():
    block = Block("table", "table", "| 100 | V |\n", [{"page_number": 1}],
                  content_data={"headers": ["RatedVoltage", "Unit"]})
    chunks = build_chunks([block], "long title " * 100, 100, 32, 1)
    assert chunks[0].content_text == block.content_text
    prefix = chunks[0].search_text[:-len(chunks[0].content_text)]
    assert count_tokens(prefix) <= 1
    assert chunks[0].token_count <= 32


def test_paged_reads_and_short_db_transactions(settings, postgres_database, monkeypatch):
    for number in range(11):
        (settings.sources[0].root / f"extra{number}.txt").write_text(f"Source number {number}.")
    pipeline.ingest(settings, postgres_database)
    sizes = []
    original = embeddings._targets

    def page(*args, **kwargs):
        result = original(*args, **kwargs)
        sizes.append(len(result))
        return result

    monkeypatch.setattr(embeddings, "_targets", page)

    def handle(request):
        assert postgres_database.pool.checkedout() == 1  # バッチ排他専用接続のみ。
        assert len(json.loads(request.content)["input"]) <= 2
        return success(request)

    mock_openai(monkeypatch, handle)
    report = embeddings.embed(settings, postgres_database)
    assert report["status"] == "succeeded" and report["embedded"] == 12
    assert max(sizes) == 3 and report["read_batches"] == 4
    assert embeddings.embed(settings, postgres_database)["unchanged"] == 12


def test_api_batch_total_tokens_are_bounded(settings, postgres_database, monkeypatch):
    for number in range(3):
        (settings.sources[0].root / f"long{number}.txt").write_text("word " * 6000)
    changed = replace(settings, chunk_max_characters=40000, embedding=replace(
        settings.embedding, batch_size=16, batch_max_tokens=8191))
    pipeline.ingest(changed, postgres_database)
    counts = []

    def handle(request):
        texts = json.loads(request.content)["input"]
        counts.append(sum(count_tokens(text) for text in texts))
        assert counts[-1] <= 8191
        return success(request)

    mock_openai(monkeypatch, handle)
    assert embeddings.embed(changed, postgres_database)["status"] == "succeeded"
    assert len(counts) >= 3


def test_model_tampering_and_missing_files_fail_even_after_cached_verification(tmp_path):
    model = tmp_path / "models" / "weights.bin"
    model.parent.mkdir()
    model.write_bytes(b"original")
    record = {"path": "models/weights.bin", "bytes": 8,
              "sha256": hashlib.sha256(b"original").hexdigest()}
    manifest = {"docling": "test", "models": [{"repo_id": "test/model", "revision": "a" * 40,
                                               "files": [record]}]}
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps(manifest))
    (tmp_path / "model-manifest.json").write_text(json.dumps(manifest))
    assert verify_models(tmp_path, lock) == verify_models(tmp_path, lock)
    model.write_bytes(b"tampered")
    with pytest.raises(ModelIntegrityError):
        verify_models(tmp_path, lock)
    model.unlink()
    with pytest.raises(ModelIntegrityError):
        verify_models(tmp_path, lock)


def test_non_pdf_profiles_do_not_depend_on_docling_models(settings, tmp_path):
    changed = replace(settings, pdf_backend="docling", docling_artifacts_path=tmp_path / "missing")
    assert conversion_profile(settings, "txt") == conversion_profile(changed, "txt")


def test_status_separates_excluded_history(settings, postgres_database):
    pipeline.ingest(settings, postgres_database)
    changed = replace(settings, chunk_max_characters=900)
    pipeline.ingest(changed, postgres_database)
    excluded = replace(changed, sources=[replace(changed.sources[0], exclude_filenames=("*.txt",))])
    pipeline.ingest(excluded, postgres_database)
    status = _status(postgres_database, settings.knowledge_id, excluded)
    assert status["documents"] == 0 and status["generations"] == {}
    assert status["historical_generations"] == {"succeeded": 2}
    assert all(value["total"] == 0 for value in status["embeddings"].values())


def test_unchanged_source_has_no_temporary_write_but_same_size_changes_are_detected(settings, postgres_database):
    pipeline.ingest(settings, postgres_database)
    second = pipeline.ingest(settings, postgres_database)["files"][0]
    assert second["original_reused"] is True and second["temporary_bytes_written"] == 0
    path = settings.sources[0].root / "note.txt"
    path.write_bytes(b"x" * path.stat().st_size)
    third = pipeline.ingest(settings, postgres_database)["files"][0]
    assert third["source_version"] != second["source_version"]
    assert third["temporary_bytes_written"] == path.stat().st_size


def test_workers_reuse_and_task_limit_recycles(settings):
    item = collect(settings).items[0]
    asset = Storage(settings.storage_dir).snapshot(item, settings.max_file_bytes, 0)
    changed = replace(settings, parse_worker_max_tasks=2)
    with ParsePool(changed) as pool:
        for _ in range(3):
            assert pool.extract(asset, item).blocks
        assert pool.metrics["started"] == 2 and pool.metrics["reused"] == 1
        assert pool.metrics["recycled"] == 1
    assert all(slot is None for slot in pool.slots)


def test_worker_timeout_is_killed_then_next_document_can_run(settings):
    item = collect(settings).items[0]
    asset = Storage(settings.storage_dir).snapshot(item, settings.max_file_bytes, 0)
    with ParsePool(replace(settings, parse_timeout_seconds=0)) as pool:
        with pytest.raises(WorkerError, match="parse_timeout"):
            pool.extract(asset, item)
        assert all(slot is None for slot in pool.slots)
        pool.settings.parse_timeout_seconds = 30
        assert pool.extract(asset, item).blocks


def test_sql_input_hash_matches_python_for_unicode_and_escapes(postgres_database):
    from sqlalchemy import Text, literal
    text = '日本語🙂 "quoted" \\ newline\n tab\t\x01'
    with postgres_database.connect() as connection:
        assert connection.execute(select(input_hash_sql(connection, literal(text, type_=Text)))).scalar_one() == digest(text)


def test_v2_upgrade_preserves_history_and_requires_explicit_selection(settings, postgres_database):
    pipeline.ingest(settings, postgres_database)
    with postgres_database.begin() as connection:
        for name in ("selected_generations", "extraction_cache", "embedding_cache"):
            tables[name].drop(connection)
        connection.execute(tables["schema_migrations"].update().values(version=2))
    migrate(postgres_database)
    migrate(postgres_database)
    with postgres_database.connect() as connection:
        assert connection.execute(select(tables["schema_migrations"].c.version)).scalar_one() == SCHEMA_VERSION
        assert connection.execute(select(func.count()).select_from(tables["extraction_generations"])).scalar_one() == 1
        assert embeddings._targets(connection, settings.knowledge_id) == []
    pipeline.ingest(settings, postgres_database)
    with postgres_database.connect() as connection:
        assert len(embeddings._targets(connection, settings.knowledge_id)) == 1
