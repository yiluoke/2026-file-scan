"""CLIからの登録と、実データを失いやすい更新・失敗経路を検証する。"""

import os
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.schema import CreateSchema, DropSchema

from file_scan import pipeline
from file_scan.cli import main
from file_scan.collector import ScanResult, collect
from file_scan.config import Settings, Source
from file_scan.db import migrate, open_database, tables
from file_scan.domain import Block, ExtractedAsset, ExtractionResult
from file_scan.storage import Storage


@pytest.fixture(params=["sqlite", "postgresql"])
def database(request, tmp_path):
    """PostgreSQLも使える場合は同じ契約テストを独立した一時schemaで実行する。"""
    if request.param == "sqlite":
        engine = open_database("sqlite:///" + str(tmp_path / "database.sqlite3"))
        migrate(engine)
        yield engine
        engine.dispose()
        return
    url = os.environ.get("FILE_SCAN_TEST_DATABASE_URL")
    if not url:
        pytest.skip("実PostgreSQLテストにはFILE_SCAN_TEST_DATABASE_URLを指定")
    admin = open_database(url)
    schema = "test_file_scan_" + uuid4().hex
    with admin.begin() as conn:
        conn.execute(CreateSchema(schema))
    engine = open_database(url, schema=schema)

    try:
        migrate(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            # このテストで作ったランダムなschemaだけを削除し、他の表には触れない。
            conn.execute(DropSchema(schema, cascade=True))
        admin.dispose()


@pytest.fixture
def settings(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    (source / "manual.md").write_text("# 手順\n\n登録ボタンを押す。\n", encoding="utf-8")
    return Settings(sources=[Source("manuals", source)], storage_dir=tmp_path / "store",
                    report_dir=tmp_path / "reports", include_extensions=(".md", ".txt", ".docx"))


def count(engine, name):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(tables[name])).scalar_one()


def test_ingest_idempotency_change_and_provenance(database, settings):
    first = pipeline.ingest(settings, database)
    assert first["status"] == "succeeded"
    assert first["summary"]["registered"] == 1
    second = pipeline.ingest(settings, database)
    assert second["summary"]["unchanged"] == 1
    assert count(database, "document_versions") == 1
    assert count(database, "extraction_generations") == 1
    (settings.sources[0].root / "manual.md").write_text("# 手順\n\n承認してから登録する。\n", encoding="utf-8")
    third = pipeline.ingest(settings, database)
    assert third["summary"]["registered"] == 1
    assert count(database, "documents") == 1
    assert count(database, "document_versions") == 2
    assert count(database, "extraction_generations") == 2
    with database.connect() as conn:
        assert set(conn.execute(select(tables["chunks"].c.publication_state)).scalars()) == {"draft"}
        assert conn.execute(select(tables["knowledge_bases"].c.enabled)).scalar_one() is False
        assert conn.execute(select(tables["documents"].c.acl_status)).scalar_one() == "unknown"
        for mapping in conn.execute(select(tables["chunk_blocks"])).mappings():
            chunk = conn.execute(select(tables["chunks"]).where(
                tables["chunks"].c.generation_id == mapping["generation_id"],
                tables["chunks"].c.chunk_id == mapping["chunk_id"])).mappings().one()
            block = conn.execute(select(tables["blocks"]).where(
                tables["blocks"].c.generation_id == mapping["generation_id"],
                tables["blocks"].c.block_id == mapping["block_id"])).mappings().one()
            assert chunk["content_text"][mapping["chunk_char_start"]:mapping["chunk_char_end"]] == (
                block["content_text"][mapping["block_char_start"]:mapping["block_char_end"]])


def test_same_bytes_are_distinct_documents(database, settings):
    path = settings.sources[0].root
    (path / "copy.md").write_bytes((path / "manual.md").read_bytes())
    report = pipeline.ingest(settings, database)
    assert report["summary"]["registered"] == 2
    assert count(database, "documents") == 2
    assert count(database, "artifact_assets") == 1


def test_atomic_rollback_and_retry(database, settings, monkeypatch):
    real_save = pipeline._save_payload

    def fail_after_insert(*args):
        real_save(*args)
        raise RuntimeError("secret document text must not reach reports")

    monkeypatch.setattr(pipeline, "_save_payload", fail_after_insert)
    report = pipeline.ingest(settings, database)
    assert report["summary"]["failed"] == 1
    assert "secret document" not in str(report)
    assert count(database, "blocks") == count(database, "chunks") == 0
    monkeypatch.setattr(pipeline, "_save_payload", real_save)
    assert pipeline.ingest(settings, database)["status"] == "succeeded"
    assert count(database, "extraction_generations") == 1
    with database.connect() as conn:
        attempts = conn.execute(select(tables["index_jobs"].c.attempt_no).where(
            tables["index_jobs"].c.stage == "convert")).scalars().all()
        assert attempts == [1, 2]


def test_scan_failure_exclusion_and_real_deletion(database, settings, monkeypatch):
    pipeline.ingest(settings, database)
    real_collect = collect
    monkeypatch.setattr(pipeline, "collect", lambda _: ScanResult(errors=[{
        "source_id": "manuals", "relative_path": ".", "error_code": "unavailable"}]))
    assert pipeline.ingest(settings, database)["status"] == "partial"
    with database.connect() as conn:
        assert conn.execute(select(tables["documents"].c.source_status)).scalar_one() != "deleted"
    monkeypatch.setattr(pipeline, "collect", real_collect)
    excluded = replace(settings, sources=[replace(settings.sources[0], exclude_filenames=("*.md",))])
    pipeline.ingest(excluded, database)
    with database.connect() as conn:
        assert conn.execute(select(tables["knowledge_documents"].c.included)).scalar_one() is False
    pipeline.ingest(settings, database)
    (settings.sources[0].root / "manual.md").unlink()
    report = pipeline.ingest(settings, database)
    assert report["summary"]["deleted"] == 1
    assert count(database, "document_versions") == 1  # 履歴を消さない。


def test_storage_root_is_bound_to_database(database, settings):
    pipeline.ingest(settings, database)
    changed = replace(settings, storage_dir=settings.storage_dir.parent / "other-store")
    with pytest.raises(pipeline.PipelineError, match="storage_dir"):
        pipeline.ingest(changed, database)


def test_excluding_directory_names_preserves_history_and_can_be_reversed(database, settings):
    backup = settings.sources[0].root / "team/design/BK/backup.txt"
    backup.parent.mkdir(parents=True)
    backup.write_text("backup document", encoding="utf-8")
    assert pipeline.ingest(settings, database)["summary"]["registered"] == 2
    excluded = replace(settings, sources=[replace(settings.sources[0], exclude_directory_names=("BK",))])
    report = pipeline.ingest(excluded, database)
    assert report["status"] == "succeeded"
    assert report["summary"]["deleted"] == 0
    with database.connect() as conn:
        rows = conn.execute(select(tables["documents"].c.source_key,
                                   tables["documents"].c.source_status,
                                   tables["knowledge_documents"].c.included,
                                   tables["knowledge_documents"].c.collection_status).join(
                                       tables["knowledge_documents"])).mappings().all()
    by_path = {row["source_key"]: row for row in rows}
    assert by_path["team/design/BK/backup.txt"]["included"] is False
    assert by_path["team/design/BK/backup.txt"]["collection_status"] == "excluded"
    assert by_path["team/design/BK/backup.txt"]["source_status"] != "deleted"
    assert by_path["manual.md"]["included"] is True
    assert count(database, "document_versions") == 2
    assert count(database, "extraction_generations") == 2
    assert pipeline.ingest(settings, database)["summary"]["unchanged"] == 2
    with database.connect() as conn:
        assert all(conn.execute(select(tables["knowledge_documents"].c.included)).scalars())


def test_export_failure_is_repaired_from_database(database, settings, monkeypatch):
    real_export = pipeline._export
    monkeypatch.setattr(pipeline, "_export", lambda *args: (_ for _ in ()).throw(OSError()))
    first = pipeline.ingest(settings, database)
    assert first["status"] == "partial"
    monkeypatch.setattr(pipeline, "_export", real_export)
    monkeypatch.setattr(pipeline, "extract", lambda *args: (_ for _ in ()).throw(AssertionError()))
    second = pipeline.ingest(settings, database)
    assert second["status"] == "succeeded"
    assert second["summary"]["unchanged"] == 1
    assert list(settings.report_dir.glob("generations/*/review.md"))


def test_missing_image_is_reextracted_but_corruption_is_reported(database, settings, monkeypatch):
    image = b"synthetic-image-bytes"

    def fake_parse(*_):
        return ExtractionResult(
            blocks=[Block("figure", "figure", None, [{"format": "md", "line": 1}])],
            assets=[ExtractedAsset("figure", image, "image/png", [{"format": "md", "line": 1}])],
            pending_images=1,
        )

    monkeypatch.setattr(pipeline, "extract", fake_parse)
    assert pipeline.ingest(settings, database)["summary"]["partial"] == 1
    stored = Storage(settings.storage_dir).put_bytes(image, "image/png")
    stored.path.unlink()
    assert pipeline.ingest(settings, database)["summary"]["registered"] == 1
    assert stored.path.read_bytes() == image
    stored.path.write_bytes(b"x" * len(image))
    failed = pipeline.ingest(settings, database)
    assert failed["summary"]["failed"] == 1
    assert failed["files"][0]["error_code"] == "stored_asset_integrity_failed"


def test_expired_collect_job_does_not_lose_final_report(database, settings, monkeypatch):
    original = Storage.snapshot
    expired_once = False

    def delayed(storage, *args):
        nonlocal expired_once
        result = original(storage, *args)
        if not expired_once:
            with database.begin() as conn:
                conn.execute(tables["index_jobs"].update().where(
                    tables["index_jobs"].c.stage == "collect",
                    tables["index_jobs"].c.status == "running",
                ).values(lease_until=pipeline.utcnow() - timedelta(seconds=1)))
            expired_once = True
        return result

    (settings.sources[0].root / "second.txt").write_text("別ファイルの処理は続ける。", encoding="utf-8")
    monkeypatch.setattr(Storage, "snapshot", delayed)
    result = pipeline.ingest(settings, database)
    assert result["summary"]["failed"] == 1
    assert result["summary"]["registered"] == 1
    assert list(settings.report_dir.glob("ingest-*.json"))


def test_batch_lock_rejects_second_writer(database):
    with pipeline.ingestion_lock(database):
        with pytest.raises(pipeline.PipelineError, match="実行中"):
            with pipeline.ingestion_lock(database):
                pass


def test_native_documents_register_and_program_files_are_excluded(database, settings):
    from docx import Document
    from openpyxl import Workbook
    from reportlab.pdfgen import canvas

    root = settings.sources[0].root
    document = Document()
    document.add_heading("操作", level=1)
    document.add_paragraph("保存前に確認します。")
    document.save(root / "word.docx")
    workbook = Workbook()
    workbook.active.append(["項目", "条件"])
    workbook.active.append(["ユーザーID", "必須"])
    workbook.create_sheet("別シート").append(["code", "E001"])
    workbook.save(root / "sheet.xlsx")
    pdf = canvas.Canvas(str(root / "manual.pdf"))
    pdf.drawString(72, 720, "Save after confirmation")
    pdf.save()
    (root / "script.py").write_text("def register(name):\n    return name.strip()\n", encoding="utf-8")
    extended = replace(settings, pdf_backend="native",
                       include_extensions=(".md", ".docx", ".xlsx", ".pdf"))
    result = pipeline.ingest(extended, database)
    assert result["summary"]["registered"] == 4
    assert result["summary"]["failed"] == 0
    assert count(database, "document_versions") == 4
    assert count(database, "chunk_blocks") >= 4
    assert any(entry["relative_path"] == "script.py" and entry["reason"] == "unsupported_extension"
               for entry in result["exclusions"])


def test_cli_scan_init_ingest_status(tmp_path, capsys):
    source = tmp_path / "input"
    source.mkdir()
    (source / "note.txt").write_text("テスト用の文書です。", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"sources": [{"source_id": "demo", "root": "input"}],
                                     "database_url": "sqlite:///local.sqlite3"}), encoding="utf-8")
    for command in ("scan", "init-db", "ingest", "status"):
        assert main([command, "--config", str(config)]) == 0
    assert '"search_enabled": false' in capsys.readouterr().out


def test_cli_does_not_print_database_credentials(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"sources": [{"source_id": "demo", "root": "input"}],
        "database_url": "invalid://username:secret-password@localhost/database"}), encoding="utf-8")
    assert main(["init-db", "--config", str(config)]) == 1
    assert "secret-password" not in capsys.readouterr().err
