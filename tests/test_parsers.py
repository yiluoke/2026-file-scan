"""実物に近い小さな原本を生成し、欠落・誤公開につながる境界を検証する。"""

from __future__ import annotations

import base64
import builtins
import io
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import pytest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from openpyxl import Workbook
from openpyxl.drawing.image import Image
from openpyxl.styles import Font
from pypdf import PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from file_scan import parsers
from file_scan.parsers import ParserDependencyError, parse_file


# ファイルを配布せず、PNGの原本バイト列が保存されることも比較できる。
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1ioAAAAASUVORK5CYII="
)


def texts(result) -> list[str]:
    return [block.content_text for block in result.blocks if block.content_text]


def test_text_cp932_and_markdown_keep_original_line_positions(tmp_path: Path):
    source = tmp_path / "notes.txt"
    source.write_bytes("障害対応\n再起動する\n\n再確認する\n".encode("cp932"))
    result = parse_file(source, "txt")
    assert result.metrics["encoding"] == "cp932"
    assert texts(result) == ["障害対応\n再起動する\n", "再確認する\n"]
    assert result.blocks[0].source_locations[0] == {"format": "txt", "start_line": 1, "end_line": 2}
    assert result.blocks[1].source_locations[0]["start_line"] == 4
    assert result.processing_status == "succeeded"

    source = tmp_path / "notes.md"
    source.write_text("# 機能\n説明\n\n## 入力\n必須項目\n", encoding="utf-8")
    result = parse_file(source, "md")
    assert result.blocks[-1].section_path == ["機能", "入力"]
    assert result.blocks[-1].source_locations[0]["start_line"] == 5


def test_empty_or_binary_text_is_not_reported_as_success(tmp_path: Path):
    source = tmp_path / "empty.txt"
    source.write_text("\n \n", encoding="utf-8")
    assert parse_file(source, "txt").processing_status == "partial"
    source.write_bytes(b"abc\x00def")
    with pytest.raises(ValueError, match="NUL"):
        parse_file(source, "txt")
    with pytest.raises(ValueError, match="未対応"):
        parse_file(source, "xls")


@pytest.mark.parametrize("file_format", ["py", "python", "sql", "json", "yaml", "yml", "csv", "log"])
def test_program_and_data_formats_are_rejected_without_reading_content(tmp_path, file_format, monkeypatch):
    source = tmp_path / f"source.{file_format}"
    source.write_text("raise RuntimeError('must not be executed')", encoding="utf-8")
    def unexpected_read(*args, **kwargs):
        raise AssertionError("対象外の原本内容を読んではいけません")
    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    with pytest.raises(ValueError, match="未対応"):
        parse_file(source, f".{file_format.upper()}")


def test_excel_reads_all_sheets_and_separates_formula_from_cache(tmp_path: Path):
    source = tmp_path / "design.xlsx"
    book = Workbook()
    main = book.active
    main.title = "項目定義"
    main.merge_cells("A1:B1")
    main["A1"] = "入力条件"
    main.append(["項目", "最大桁数"])
    main.append(["利用者ID", 20])
    main["B4"] = "=B3*2"
    second = book.create_sheet("エラー一覧")
    second.append(["エラーコード", "意味"])
    second.append(["E001", "必須項目不足"])
    hidden = book.create_sheet("非表示")
    hidden["A1"] = "取り込み対象外の内容"
    hidden.sheet_state = "hidden"
    book.save(source)

    result = parse_file(source, "xlsx")
    assert result.metrics["sheets_total"] == 3
    assert result.metrics["sheets_processed"] == 2
    assert result.metrics["sheets_excluded"] == 1
    assert "E001" in "\n".join(texts(result))
    assert "取り込み対象外の内容" not in "\n".join(texts(result))
    sheet = next(b for b in result.blocks if b.element_type == "sheet" and b.section_path == ["項目定義"])
    assert sheet.content_data["merged_ranges"] == ["A1:B1"]
    row = next(b for b in result.blocks if b.source_locations[0].get("cell_range") == "A3:B3")
    assert "A2: 項目" in row.content_data["headers"]
    assert row.source_locations[0]["sheet_name"] == "項目定義"
    formula = next(c for b in result.blocks for c in b.content_data.get("cells", []) if c.get("formula"))
    assert formula["formula"] == "=B3*2"
    assert formula["cached_value"] is None
    assert "formula_cached_value_missing" in result.quality_flags
    included = parse_file(source, "xlsx", include_hidden=True)
    assert "取り込み対象外の内容" in "\n".join(texts(included))


def test_excel_hidden_rows_and_columns_are_inventoried_but_not_in_text(tmp_path: Path):
    source = tmp_path / "hidden.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.append(["公開項目", "非公開列"])
    sheet.append(["公開値", "非公開列の値"])
    sheet.append(["非公開行の値", "別の値"])
    sheet.column_dimensions["B"].hidden = True
    sheet.row_dimensions[3].hidden = True
    book.save(source)
    result = parse_file(source, "xlsx")
    assert "公開値" in "\n".join(texts(result))
    assert "非公開" not in "\n".join(texts(result))
    assert result.metrics["hidden_cells_excluded"] == 4


def test_excel_preserves_cached_value_when_it_exists(tmp_path: Path):
    source = tmp_path / "cached.xlsx"
    book = Workbook()
    book.active["A1"] = "=1+2"
    book.save(source)
    with ZipFile(source) as package:
        parts = {name: package.read(name) for name in package.namelist()}
    namespace = {"s": parsers.S}
    root = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
    root.find("s:sheetData/s:row/s:c/s:v", namespace).text = "3"
    parts["xl/worksheets/sheet1.xml"] = ET.tostring(root)
    with ZipFile(source, "w") as package:
        for name, data in parts.items():
            package.writestr(name, data)
    result = parse_file(source, "xlsx")
    cell = next(c for b in result.blocks for c in b.content_data.get("cells", []))
    assert cell["formula"] == "=1+2"
    assert cell["cached_value"] == 3
    assert "formula_cached_value_missing" not in result.quality_flags


def test_excel_sparse_far_cells_do_not_expand_a_million_row_rectangle(tmp_path: Path):
    source = tmp_path / "sparse.xlsx"
    book = Workbook()
    book.active["A1"] = "先頭"
    book.active["Z1000000"] = "遠いセル"
    book.active["XFD1048576"].font = Font(bold=True)
    book.save(source)
    result = parse_file(source, "xlsx")
    rows = [b for b in result.blocks if b.element_type == "table"]
    assert len(rows) == 2
    assert rows[-1].source_locations[0]["cell_range"] == "Z1000000:Z1000000"


def test_excel_cell_limit_fails_explicitly_instead_of_truncating(tmp_path: Path, monkeypatch):
    source = tmp_path / "many.xlsx"
    book = Workbook()
    book.active.append(["one", "two"])
    book.save(source)
    monkeypatch.setattr(parsers, "MAX_SHEET_CELLS", 1)
    with pytest.raises(ValueError, match="実在セル数"):
        parse_file(source, "xlsx")


def test_excel_keeps_image_bytes_and_anchor_for_later_analysis(tmp_path: Path):
    source = tmp_path / "screen.xlsx"
    book = Workbook()
    book.active["A1"] = "画面説明"
    book.active.add_image(Image(io.BytesIO(PNG)), "C4")
    book.save(source)
    result = parse_file(source, "xlsx")
    assert result.pending_images == 1
    assert result.assets[0].content == PNG
    assert result.assets[0].mime_type == "image/png"
    assert result.assets[0].source_locations[0]["anchor_row"] == 4
    assert result.assets[0].source_locations[0]["anchor_column"] == 3
    assert result.processing_status == "partial"


def test_word_keeps_heading_paragraph_table_and_image_order(tmp_path: Path):
    source = tmp_path / "manual.docx"
    document = Document()
    document.add_heading("1. 操作", level=1)
    document.add_paragraph("前の説明")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "項目"
    table.cell(0, 1).text = "条件"
    table.cell(1, 0).text = "利用者ID"
    table.cell(1, 1).text = "最大20文字"
    document.add_paragraph("後の説明")
    document.add_picture(io.BytesIO(PNG))
    document.save(source)
    result = parse_file(source, "docx")
    assert [b.element_type for b in result.blocks[:4]] == ["heading", "paragraph", "table", "paragraph"]
    assert texts(result)[:4] == ["1. 操作", "前の説明", "項目\t条件\n利用者ID\t最大20文字", "後の説明"]
    assert result.blocks[2].section_path == ["1. 操作"]
    assert result.blocks[2].source_locations[0]["table_index"] == 1
    assert result.assets[0].content == PNG
    assert result.assets[0].source_locations[0]["paragraph_index"] == 4
    assert result.pending_images == 1
    assert "word_non_picture_graphic_unprocessed" not in result.quality_flags


@pytest.mark.parametrize(
    ("graphic_uri", "reference_tag"),
    [
        (
            "http://schemas.openxmlformats.org/drawingml/2006/chart",
            "{http://schemas.openxmlformats.org/drawingml/2006/chart}chart",
        ),
        (
            "http://schemas.openxmlformats.org/drawingml/2006/diagram",
            "{http://schemas.openxmlformats.org/drawingml/2006/diagram}relIds",
        ),
        ("urn:example:unsupported-graphic", None),
        # 種別宣言に頼り切らず、グラフ／SmartArtの参照自体も検知する。
        (
            "http://schemas.openxmlformats.org/drawingml/2006/picture",
            "{http://schemas.openxmlformats.org/drawingml/2006/chart}chart",
        ),
        (
            "http://schemas.openxmlformats.org/drawingml/2006/picture",
            "{http://schemas.openxmlformats.org/drawingml/2006/diagram}relIds",
        ),
    ],
)
def test_word_inline_non_picture_graphics_are_not_silently_ignored(
    tmp_path: Path, graphic_uri: str, reference_tag: str | None,
):
    source = tmp_path / "inline-graphic.docx"
    document = Document()
    document.add_paragraph("グラフ・構成図の説明")
    document.save(source)
    with ZipFile(source) as package:
        parts = {name: package.read(name) for name in package.namelist()}
    root = ET.fromstring(parts["word/document.xml"])
    paragraph = root.find("w:body/w:p", parsers.NS)
    run = ET.SubElement(paragraph, f"{{{parsers.W}}}r")
    drawing = ET.SubElement(run, f"{{{parsers.W}}}drawing")
    inline = ET.SubElement(
        drawing,
        "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}inline",
    )
    graphic = ET.SubElement(inline, f"{{{parsers.A}}}graphic")
    graphic_data = ET.SubElement(graphic, f"{{{parsers.A}}}graphicData", {"uri": graphic_uri})
    if reference_tag is not None:
        ET.SubElement(graphic_data, reference_tag, {f"{{{parsers.R}}}id": "rIdGraphic"})
    parts["word/document.xml"] = ET.tostring(root)
    with ZipFile(source, "w") as package:
        for name, data in parts.items():
            package.writestr(name, data)

    result = parse_file(source, "docx")
    assert texts(result) == ["グラフ・構成図の説明"]
    assert "word_non_picture_graphic_unprocessed" in result.quality_flags
    assert result.processing_status == "partial"
    # 保存できる原画像がない図を、画像取得済みや画像API待ちとは扱わない。
    assert result.assets == []
    assert result.pending_images == 0
    assert not any(block.element_type == "figure" for block in result.blocks)


def test_word_toc_resolves_bookmark_and_detects_stale_title(tmp_path: Path):
    source = tmp_path / "toc.docx"
    document = Document()
    document.styles.add_style("TOC 1", WD_STYLE_TYPE.PARAGRAPH)
    entry = document.add_paragraph(style="TOC 1")
    link = OxmlElement("w:hyperlink")
    link.set(qn("w:anchor"), "_Toc001")
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "1. 古いタイトル\t1"
    run.append(text)
    link.append(run)
    entry._p.append(link)
    heading = document.add_heading("1. 現在のタイトル", level=1)
    bookmark = OxmlElement("w:bookmarkStart")
    bookmark.set(qn("w:id"), "1")
    bookmark.set(qn("w:name"), "_Toc001")
    heading._p.insert(0, bookmark)
    document.add_paragraph("現在の本文")
    document.save(source)
    result = parse_file(source, "docx")
    assert result.blocks[0].element_type == "navigation"
    assert len(result.relations) == 1
    assert result.relations[0].relation_type == "toc_entry"
    assert result.relations[0].to_block_id == result.blocks[1].block_id
    assert "word_toc_text_mismatch" in result.quality_flags
    assert "現在の本文" in texts(result)


def test_word_tracks_revisions_and_does_not_mix_deleted_text_into_body(tmp_path: Path):
    source = tmp_path / "revisions.docx"
    document = Document()
    paragraph = document.add_paragraph("現在の本文")
    deleted = OxmlElement("w:del")
    deleted_run = OxmlElement("w:r")
    deleted_text = OxmlElement("w:delText")
    deleted_text.text = "削除された仕様"
    deleted_run.append(deleted_text)
    deleted.append(deleted_run)
    paragraph._p.append(deleted)
    inserted = OxmlElement("w:ins")
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "追加された仕様"
    run.append(text)
    inserted.append(run)
    paragraph._p.append(inserted)
    document.save(source)
    result = parse_file(source, "docx")
    assert "削除された仕様" not in "\n".join(texts(result))
    assert "追加された仕様" in "\n".join(texts(result))
    assert "word_tracked_changes_require_review" in result.quality_flags
    assert result.processing_status == "partial"


def test_word_automatic_list_numbering_is_not_silently_discarded(tmp_path: Path):
    source = tmp_path / "numbered.docx"
    document = Document()
    document.add_paragraph("停止する", style="List Number")
    document.add_paragraph("再起動する", style="List Number")
    document.save(source)
    result = parse_file(source, "docx")
    assert "word_auto_numbering_not_rendered" in result.quality_flags
    assert result.blocks[0].content_data["numbering"]["definition"]["numFmt"] == "decimal"
    assert result.blocks[0].content_data["numbering"]["rendered"] is False
    assert texts(result) == ["停止する", "再起動する"]


def test_word_body_cross_reference_is_not_mistaken_for_a_toc_entry(tmp_path: Path):
    source = tmp_path / "reference.docx"
    document = Document()
    paragraph = document.add_paragraph("詳細は")
    link = OxmlElement("w:hyperlink")
    link.set(qn("w:anchor"), "_Toc123")
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "入力仕様を参照"
    run.append(text)
    link.append(run)
    paragraph._p.append(link)
    document.save(source)
    result = parse_file(source, "docx")
    assert result.blocks[0].element_type == "paragraph"
    assert "word_toc_unresolved_target" not in result.quality_flags


def test_pdf_native_reads_text_and_scan_is_pending_instead_of_success(tmp_path: Path):
    source = tmp_path / "mixed.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.drawString(20, 750, "Maintenance manual")
    pdf.showPage()
    pdf.drawImage(ImageReader(io.BytesIO(PNG)), 20, 500, width=100, height=100)
    pdf.save()
    result = parse_file(source, "pdf", pdf_backend="native")
    assert "Maintenance manual" in "\n".join(texts(result))
    assert result.metrics["pages_total"] == 2
    assert result.metrics["pages_processed"] == 2
    assert "pdf_page_without_text_ocr_required" in result.quality_flags
    assert len(result.assets) == 1
    assert result.assets[0].source_locations[0]["page_number"] == 2
    assert result.pending_images == 1
    assert result.processing_status == "partial"


def test_blank_pdf_does_not_look_like_a_successful_scan(tmp_path: Path):
    source = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(source)
    result = parse_file(source, "pdf", pdf_backend="native")
    assert result.processing_status == "partial"
    assert "no_extractable_text" in result.quality_flags


def test_pdf_vector_drawing_is_not_silently_ignored(tmp_path: Path):
    source = tmp_path / "diagram.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.drawString(20, 750, "Diagram")
    pdf.rect(20, 500, 100, 100)
    pdf.save()
    result = parse_file(source, "pdf", pdf_backend="native")
    assert "pdf_vector_graphics_unprocessed" in result.quality_flags
    assert result.processing_status == "partial"


def test_docling_missing_dependency_has_a_clear_error_and_never_falls_back(tmp_path: Path, monkeypatch):
    source = tmp_path / "input.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(source)
    actual_import = builtins.__import__

    def without_docling(name, *args, **kwargs):
        if name.startswith("docling"):
            raise ModuleNotFoundError("test: optional dependency absent")
        return actual_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_docling)
    with pytest.raises(ParserDependencyError, match="docling"):
        parse_file(source, "pdf", pdf_backend="docling")


def test_package_limit_and_corrupt_office_are_explicit_failures(tmp_path: Path, monkeypatch):
    source = tmp_path / "input.docx"
    document = Document()
    document.add_paragraph("本文")
    document.save(source)
    monkeypatch.setattr(parsers, "MAX_PACKAGE_UNCOMPRESSED_BYTES", 1)
    with pytest.raises(ValueError, match="展開後サイズ"):
        parse_file(source, "docx")
    source.write_bytes(b"not an Office package")
    with pytest.raises(BadZipFile):
        parse_file(source, "docx")
