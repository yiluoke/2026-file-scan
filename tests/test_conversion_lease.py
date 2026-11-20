"""長時間の正当な変換を保存し、期限を超えた試行はロールバックする。"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from file_scan import pipeline
from file_scan.config import ImageAnalysisSettings, PdfOcrSettings, Settings, Source
from file_scan.db import migrate, open_database, tables
from file_scan.domain import Block, ExtractionResult


@pytest.mark.parametrize("file_format,backend,ocr,vision,elapsed,expected", [
    pytest.param("pdf", "docling", True, False, 1700, "succeeded", id="long-ocr"),
    pytest.param("pdf", "docling", True, True, 3190, "succeeded", id="ocr-and-serial-images"),
    pytest.param("docx", "docling", True, True, 1650, "succeeded", id="office-render-and-images"),
    pytest.param("pdf", "native", True, False, 421, "failed", id="native-pdf-expired"),
    pytest.param("pdf", "docling", False, False, 421, "failed", id="ocr-disabled-expired"),
])
def test_conversion_lease_covers_enabled_steps(tmp_path, monkeypatch, file_format, backend,
                                              ocr, vision, elapsed, expected):
    source = tmp_path / "input"
    source.mkdir()
    (source / f"manual.{file_format}").write_bytes(b"source")
    settings = Settings(
        sources=[Source("manuals", source)], storage_dir=tmp_path / "store",
        report_dir=tmp_path / "reports", pdf_backend=backend, pdf_ocr=PdfOcrSettings(enabled=ocr),
        image_analysis=ImageAnalysisSettings(enabled=vision, api_key="test-only"),
    )
    # 実モデルや外部APIの代わりに変換結果と所要時間を与え、DBの確定処理は実行する。
    now = pipeline.utcnow()
    monkeypatch.setattr(pipeline, "utcnow", lambda: now)
    monkeypatch.setattr(pipeline, "conversion_profile", lambda *_: (
        "test-revision", {"extraction_revision": "test-extraction"}))

    def extract(*_):
        nonlocal now
        now += timedelta(seconds=elapsed)
        return ExtractionResult(blocks=[Block("p1", "paragraph", "保存する本文", [{"format": file_format}])])

    monkeypatch.setattr(pipeline, "extract", extract)
    engine = open_database("sqlite:///" + str(tmp_path / "database.sqlite3"))
    try:
        migrate(engine)
        report = pipeline.ingest(settings, engine)
        assert report["files"][0]["processing_status"] == expected, report["files"][0]
        jobs = tables["index_jobs"]
        with engine.connect() as conn:
            assert conn.scalar(select(jobs.c.status).where(jobs.c.stage == "convert")) == expected
            saved = conn.execute(select(tables["chunks"].c.content_text)).scalars().all()
            assert saved == (["保存する本文"] if expected == "succeeded" else [])
    finally:
        engine.dispose()
