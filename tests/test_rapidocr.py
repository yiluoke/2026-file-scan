"""文字層を含まない日本語画像で、実RapidOCR・DB保存・更新を検証する。"""

import json
import shutil
import socket
from dataclasses import replace
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas
from sqlalchemy import select

from file_scan.chunking import build_chunks
from file_scan.config import ConfigError, PdfOcrSettings, Settings, Source, load_config
from file_scan.db import tables
from file_scan.models import file_hash
from file_scan.ocr_models import OCR_LOCK_PATH, OcrModelIntegrityError, verify_ocr_models
from file_scan.parsers import parse_file
from file_scan.pipeline import ingest
from file_scan.profiles import extraction_profile

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "data/models/docling"
IMAGE = ROOT / "tests/fixtures/ocr/japanese-horizontal.png"


def scan_pdf(path, *, native=False, broken_layer=False, blank=False):
    pdf = canvas.Canvas(str(path), pagesize=(540, 720))
    if native:
        pdf.drawString(40, 650, "Native text page")
        pdf.showPage()
    if not blank:
        pdf.drawImage(str(IMAGE), 0, 0, width=540, height=720)
    if broken_layer:
        pdf.setFillColorRGB(1, 1, 1)
        pdf.drawString(40, 650, "BROKENLAYER")
    pdf.showPage()
    pdf.save()


@pytest.fixture(scope="module")
def models():
    if not (MODELS / "ocr-model-manifest.json").exists():
        pytest.skip("先に日本語OCR有効の設定でscripts/download_docling_models.pyを実行")
    verify_ocr_models(MODELS)
    return MODELS


def test_japanese_scan_is_ocr_searchable_offline_with_original_coordinates(tmp_path, models, monkeypatch):
    source = tmp_path / "scan.pdf"
    scan_pdf(source, native=True)

    def offline(*args, **kwargs):
        pytest.fail("配置済みモデルの取込中にネットワークへ接続してはいけません")

    monkeypatch.setattr(socket.socket, "connect", offline)
    result = parse_file(source, "pdf", docling_artifacts_path=models)
    assert result.processing_status == "succeeded"
    assert result.metrics["ocr_pages"] == [2]
    assert result.metrics["ocr_background_pages"] == [2]
    assert result.metrics["ocr_text_cells"] >= 6
    assert result.pending_images == 0 and result.assets
    text = "\n".join(block.content_text or "" for block in result.blocks)
    for expected in ("Native text page", "日本語の文書取込テスト", "スキャンした書籍", "ベクトル化", "１２３４５"):
        assert expected in text
    chunks = build_chunks(result.blocks, "日本語書籍.pdf")
    assert any("スキャンした書籍" in chunk.search_text for chunk in chunks)
    assert all(block.extraction_method == "docling_cpu_rapidocr_ja"
               for block in result.blocks if block.content_text)
    assert all("bbox" in loc for block in result.blocks if block.content_text
               for loc in block.source_locations)


def test_full_page_replaces_bad_text_layer_and_changes_converter(tmp_path, models):
    source = tmp_path / "bad-layer.pdf"
    scan_pdf(source, broken_layer=True)
    result = parse_file(source, "pdf", docling_artifacts_path=models,
                        pdf_ocr=PdfOcrSettings(mode="full_page"))
    text = "\n".join(block.content_text or "" for block in result.blocks)
    assert "日本語の文書取込テスト" in text and "BROKENLAYER" not in text
    assert result.metrics["ocr"]["mode"] == "full_page"


def test_blank_page_stays_partial_after_ocr(tmp_path, models):
    source = tmp_path / "blank.pdf"
    scan_pdf(source, blank=True)
    result = parse_file(source, "pdf", docling_artifacts_path=models)
    assert result.processing_status == "partial"
    assert "pdf_page_without_text_after_ocr" in result.quality_flags
    assert "no_extractable_text" in result.quality_flags


def test_figure_semantics_stays_pending_even_when_pdf_ocr_enabled(tmp_path, models):
    source = tmp_path / "figure.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(540, 720))
    pdf.drawString(40, 680, "A figure remains to be described")
    pdf.drawImage(str(IMAGE), 40, 40, width=180, height=240)
    pdf.save()
    result = parse_file(source, "pdf", docling_artifacts_path=models)
    assert result.processing_status == "partial" and result.pending_images >= 1
    assert result.metrics["ocr_background_pages"] == []


def test_missing_ocr_models_fail_before_converter_can_download(tmp_path, models, monkeypatch):
    from file_scan import models as model_module, parsers
    source = tmp_path / "scan.pdf"
    scan_pdf(source)
    shutil.copyfile(models / "model-manifest.json", tmp_path / "model-manifest.json")
    # OCR不足を分離して検証する。レイアウトモデルの実体は別テストで検査済み。
    monkeypatch.setattr(model_module, "verify_models", lambda *_: "layout-verified")

    def unexpected(*args, **kwargs):
        pytest.fail("OCRモデル不足時にConverter初期化・モデル取得を開始してはいけません")

    monkeypatch.setattr(parsers, "_docling_converter", unexpected)
    with pytest.raises(OcrModelIntegrityError):
        parse_file(source, "pdf", docling_artifacts_path=tmp_path)


def test_ocr_model_verification_detects_missing_and_cached_tampering(tmp_path, models):
    with pytest.raises(OcrModelIntegrityError):
        verify_ocr_models(tmp_path)
    lock = json.loads(OCR_LOCK_PATH.read_text())
    asset = tmp_path / "model.onnx"
    asset.write_bytes(b"model")
    lock["models"][0]["files"] = [{"path": "model.onnx", "bytes": 5, "sha256": file_hash(asset)}]
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock))
    (tmp_path / "ocr-model-manifest.json").write_text(json.dumps(lock))
    verify_ocr_models(tmp_path, lock_path)
    asset.write_bytes(b"other")
    with pytest.raises(OcrModelIntegrityError):
        verify_ocr_models(tmp_path, lock_path)
    asset.unlink()
    with pytest.raises(OcrModelIntegrityError):
        verify_ocr_models(tmp_path, lock_path)


def test_ocr_settings_only_change_pdf_extraction_and_time_limit_does_not(models):
    settings = Settings(docling_artifacts_path=models)
    baseline = extraction_profile(settings, "pdf")
    for ocr in (PdfOcrSettings(enabled=False), PdfOcrSettings(mode="full_page"), PdfOcrSettings(scale=4)):
        changed = replace(settings, pdf_ocr=ocr)
        assert baseline[0] != extraction_profile(changed, "pdf")[0]
        assert extraction_profile(settings, "txt") == extraction_profile(changed, "txt")
    assert baseline == extraction_profile(replace(settings, pdf_ocr=PdfOcrSettings(timeout_seconds=3600)), "pdf")


@pytest.mark.parametrize("value", [{"enabled": "yes"}, {"mode": "gpu"}, {"scale": 0},
                                  {"scale": float("nan")}, {"timeout_seconds": 0}, {"language": "en"}])
def test_invalid_ocr_settings_are_rejected(tmp_path, value):
    import yaml
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"sources": [{"source_id": "books", "root": "source"}],
                                     "pdf_ocr": value}))
    with pytest.raises(ConfigError):
        load_config(config)


def test_ocr_change_reextracts_and_replay_reuses_in_postgres(postgres_database, tmp_path, models):
    source = tmp_path / "source"
    source.mkdir()
    scan_pdf(source / "book.pdf")
    settings = Settings(sources=[Source("books", source)], storage_dir=tmp_path / "storage",
                        report_dir=tmp_path / "reports", docling_artifacts_path=models,
                        pdf_ocr=PdfOcrSettings(enabled=False))
    first = ingest(settings, postgres_database)
    assert first["summary"]["partial"] == 1
    # 通常解析の1秒制限ではなく、OCR固有の期限を使用することも実プロセスで確認する。
    settings.pdf_ocr = PdfOcrSettings()
    settings.parse_timeout_seconds = 1
    second = ingest(settings, postgres_database)
    assert second["summary"]["registered"] == 1 and second["summary"]["partial"] == 0
    assert second["summary"]["failed"] == 0
    third = ingest(settings, postgres_database)
    assert third["summary"]["unchanged"] == 1 and third["parser_workers"]["started"] == 0
    with postgres_database.connect() as connection:
        assert any("スキャンした書籍" in text for text in connection.execute(
            select(tables["chunks"].c.search_text)).scalars())
