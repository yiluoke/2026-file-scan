"""事前取得したDoclingの実モデルを使い、本文・表・座標・画像保持を確認する。"""

import io
from pathlib import Path

import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from file_scan import parsers
from file_scan.chunking import build_chunks
from file_scan.parsers import ParserDependencyError, parse_file
from test_parsers import PNG

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "data/models/docling"
PDFS = ROOT / "data/file-server/department/pdf"


@pytest.fixture(scope="module")
def converted():
    if not (MODELS / "model-manifest.json").is_file():
        pytest.skip("先にpython scripts/download_docling_models.pyを実行")
    if not all((PDFS / name).is_file() for name in ("dummy.pdf", "table.pdf")):
        pytest.skip("先にpython scripts/download_samples.pyを実行")
    return {name: parse_file(PDFS / name, "pdf", docling_artifacts_path=MODELS)
            for name in ("dummy.pdf", "table.pdf")}


@pytest.mark.parametrize("name", ["dummy.pdf", "table.pdf"])
def test_real_docling_model_extracts_pdf_with_coordinates(converted, name):
    result = converted[name]
    assert result.processing_status == "succeeded"
    assert result.metrics["pdf_backend"] == "docling"
    assert result.metrics["device"] == "cpu" and result.metrics["ocr_enabled"] is False
    assert result.metrics["pages_total"] == result.metrics["pages_processed"] == 1
    text = [block for block in result.blocks if block.content_text]
    assert text and all(block.extraction_method == "docling_cpu_ocr_disabled" for block in text)
    for block in text:
        assert block.source_locations[0]["page_number"] == 1
        assert "bbox" in block.source_locations[0]
        assert block.source_locations[0]["coordinate_system"] == "pdf_bottom_left"
    assert "pdf_vector_graphics_unprocessed" not in result.quality_flags


def test_real_docling_recognizes_table_cells(converted):
    tables = [block for block in converted["table.pdf"].blocks if block.element_type == "table"]
    assert len(tables) == 1
    table = tables[0]
    assert table.content_data["table"]["num_cols"] == 6
    for word in ("Participants", "Blind", "Low Vision", "Dexterity", "Mobility"):
        assert word in table.content_text
    assert build_chunks(converted["table.pdf"].blocks, "table.pdf")


def test_real_docling_reuses_converter_between_pdfs(converted):
    # 2原本の実変換後も、同じモデルのConverterは一つだけ初期化する。
    info = parsers._docling_converter.cache_info()
    assert info.misses == 1 and info.hits >= 1


def test_real_docling_title_only_pdf_still_has_searchable_chunk(converted):
    chunks = build_chunks(converted["dummy.pdf"].blocks, "dummy.pdf")
    assert len(chunks) == 1
    assert chunks[0].content_text == "Dummy PDF file"
    mapping = chunks[0].blocks[0]
    block = next(block for block in converted["dummy.pdf"].blocks if block.block_id == mapping.block_id)
    assert block.content_text[mapping.block_char_start:mapping.block_char_end] == chunks[0].content_text


def test_real_docling_preserves_scan_image_and_reports_ocr_pending(tmp_path, converted):
    source = tmp_path / "scan.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.drawString(20, 750, "Maintenance manual")
    pdf.showPage()
    pdf.drawImage(ImageReader(io.BytesIO(PNG)), 20, 500, width=100, height=100)
    pdf.save()
    result = parse_file(source, "pdf", docling_artifacts_path=MODELS)
    assert result.metrics["pages_processed"] == 2
    assert result.processing_status == "partial"
    assert "pdf_page_without_text_ocr_required" in result.quality_flags
    assert result.pending_images >= 1 and result.assets
    assert any(asset.asset_role == "original_image" and
               asset.source_locations[0]["page_number"] == 2 for asset in result.assets)


def test_docling_missing_models_never_falls_back(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Doclingの失敗をnative成功で置き換えてはいけません")
    monkeypatch.setattr(parsers, "_parse_pdf_native", unexpected)
    source = tmp_path / "input.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.drawString(20, 750, "Text")
    pdf.save()
    with pytest.raises(ParserDependencyError, match="モデルが未配置"):
        parse_file(source, "pdf", docling_artifacts_path=tmp_path / "missing")
