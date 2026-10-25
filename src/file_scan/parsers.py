"""原本を実行・更新せず、位置を保持した共通ブロックへ変換する。

第1版では抽出できた内容と未対応部分を明確に分ける。画像は保存するが、
OCRや画像説明のAPIを呼ばず、後続処理が必要な画像として記録する。
PDFのnativeルートは軽量な文字抽出であり、レイアウト解析器ではない。
"""

from __future__ import annotations

import ast
import io
import mimetypes
import posixpath
import re
import tokenize
import xml.etree.ElementTree as ET
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from .domain import Block, ExtractedAsset, ExtractionResult, Relation


# XMLの巨大な宣言範囲や、書式だけが設定された最終セルを全面走査しない。
# 実在セル数にも上限を設け、上限超過を無言の切り捨てにしない。
MAX_PACKAGE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_XML_BYTES = 128 * 1024 * 1024
MAX_SHEET_CELLS = 250_000
MAX_PACKAGE_PARTS = 20_000

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
PKG_R = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W, "r": R, "a": A, "s": S, "xdr": XDR}


class ParserDependencyError(RuntimeError):
    """選択された抽出方式の依存パッケージが利用できない。"""


def parse_file(
    path: Path,
    file_format: str,
    *,
    pdf_backend: str = "docling",
    include_hidden: bool = False,
    docling_artifacts_path: Path = Path("data/models/docling"),
) -> ExtractionResult:
    """形式に応じて抽出する。未対応形式や読めない原本は例外にする。

    品質警告や画像の後続処理待ちはExtractionResultに残し、成功と区別する。
    ファイル名から実行モジュールを読み込むことはなく、PythonもAST解析だけを行う。
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    fmt = file_format.lower().lstrip(".")
    if pdf_backend not in {"native", "docling"}:
        raise ValueError(f"未対応のPDF抽出方式です: {pdf_backend}")
    if fmt in {"txt", "md", "markdown", "sql", "json", "yaml", "yml", "csv", "log"}:
        # 設定・ログ・SQL等は構文を評価せず、まず原文と行位置を取得する。
        result = _parse_text(path, markdown=fmt in {"md", "markdown"}, source_format=fmt)
    elif fmt in {"py", "python"}:
        result = _parse_python(path)
    elif fmt in {"xlsx", "xlsm"}:
        result = _parse_excel(path, include_hidden=include_hidden)
    elif fmt == "docx":
        result = _parse_word(path)
    elif fmt == "pdf":
        result = (_parse_pdf_docling(path, docling_artifacts_path)
                  if pdf_backend == "docling" else _parse_pdf_native(path))
    else:
        raise ValueError(f"第1版では未対応の形式です: {file_format}")
    if not any(b.content_text and b.content_text.strip() for b in result.blocks):
        _flag(result, "no_extractable_text")
    result.metrics["blocks"] = len(result.blocks)
    result.metrics["images"] = len(result.assets)
    return result


def _flag(result: ExtractionResult, flag: str) -> None:
    """同じ警告がページ数だけ重複しないように集約する。"""
    if flag not in result.quality_flags:
        result.quality_flags.append(flag)


def _scalar(value: Any) -> Any:
    """DBへ渡す値をJSON化できる形にする。日付の意味は文字列として保持する。"""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if type(value).__module__ == "openpyxl.worksheet.formula":
        # 配列数式等のオブジェクトreprにはメモリアドレスが含まれるため使わない。
        return getattr(value, "text", None) or f"[{type(value).__name__}]"
    return str(value)


def _decode_text(path: Path, *, python: bool = False) -> tuple[str, str]:
    """置換文字で誤魔化さずに読み、失敗したら明示的に停止する。"""
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ValueError("NULを含むため、対応するテキスト形式として読み取れません")
    if python:
        # coding: cp932等の宣言も、コードを実行せず標準ライブラリで読み取れる。
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
            return raw.decode(encoding), encoding
        except (SyntaxError, UnicodeError):
            pass
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeError("UTF-8またはCP932としてテキストを読み取れません")


def _parse_text(path: Path, *, markdown: bool, source_format: str) -> ExtractionResult:
    """行位置を保ち、段落とMarkdown見出しの境界で分ける。"""
    text, encoding = _decode_text(path)
    lines = text.splitlines(keepends=True)
    result = ExtractionResult(metrics={"encoding": encoding, "lines": len(lines)})
    headings: list[tuple[int, str]] = []
    start: int | None = None

    def append_paragraph(end: int) -> None:
        nonlocal start
        if start is not None:
            result.blocks.append(Block(
                block_id=f"text-{start + 1}", element_type="paragraph",
                content_text="".join(lines[start:end]),
                source_locations=[{"format": source_format, "start_line": start + 1, "end_line": end}],
                section_path=[label for _, label in headings],
                extraction_method="text_decode",
            ))
        start = None

    in_fence = False
    for index, line in enumerate(lines):
        if markdown and re.match(r"^\s{0,3}(```|~~~)", line):
            in_fence = not in_fence
        match = re.match(r"^(#{1,6})[ \t]+(.+?)\s*#*\s*$", line) if markdown and not in_fence else None
        if match:
            append_paragraph(index)
            level, label = len(match.group(1)), match.group(2)
            headings[:] = [(n, t) for n, t in headings if n < level]
            headings.append((level, label))
            result.blocks.append(Block(
                block_id=f"text-heading-{index + 1}", element_type="heading", content_text=line.rstrip("\r\n"),
                source_locations=[{"format": source_format, "start_line": index + 1, "end_line": index + 1}],
                section_path=[t for _, t in headings], content_data={"level": level}, extraction_method="markdown",
            ))
        elif not line.strip() and not in_fence:
            append_paragraph(index)
        elif start is None:
            start = index
    append_paragraph(len(lines))
    return result


def _parse_python(path: Path) -> ExtractionResult:
    """トップレベルの定義とその前後を、実行せず行位置付きで保持する。"""
    text, encoding = _decode_text(path, python=True)
    lines = text.splitlines(keepends=True)
    result = ExtractionResult(metrics={"encoding": encoding, "lines": len(lines)})
    try:
        tree = ast.parse(text, filename=path.name)
    except SyntaxError as exc:
        _flag(result, "python_syntax_error")
        result.metrics["syntax_error_line"] = exc.lineno
        if text.strip():
            result.blocks.append(Block(
                block_id="python-file", element_type="code", content_text=text,
                source_locations=[{"format": "py", "path": str(path), "start_line": 1, "end_line": len(lines)}],
                quality_flags=["python_syntax_error"], extraction_method="text_decode",
            ))
        return result
    next_line = 1

    def append_code(start: int, end: int, node: ast.AST | None = None) -> None:
        raw = "".join(lines[start - 1:end])
        if not raw.strip():
            return
        data: dict[str, Any] = {}
        if node is not None:
            data = {
                "symbol_name": getattr(node, "name", None),
                "symbol_type": type(node).__name__,
                "identifiers": sorted({n.id for n in ast.walk(node) if isinstance(n, ast.Name)}),
            }
        result.blocks.append(Block(
            block_id=f"python-{start}-{end}", element_type="code", content_text=raw,
            source_locations=[{"format": "py", "path": str(path), "start_line": start, "end_line": end}],
            section_path=[data["symbol_name"]] if data else [], content_data=data, extraction_method="python_ast",
        ))

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        decorators = getattr(node, "decorator_list", [])
        start = min([node.lineno, *[d.lineno for d in decorators]])
        end = node.end_lineno or node.lineno
        append_code(next_line, start - 1)
        append_code(start, end, node)
        next_line = end + 1
    append_code(next_line, len(lines))
    return result


def _check_package(package: ZipFile) -> None:
    """ZIPを展開せず、処理前にサイズ上限を検査する。"""
    parts = package.infolist()
    if len(parts) > MAX_PACKAGE_PARTS or sum(p.file_size for p in parts) > MAX_PACKAGE_UNCOMPRESSED_BYTES:
        raise ValueError("Officeパッケージの展開後サイズまたは部品数が処理上限を超えています")
    if any(p.filename.endswith(".xml") and p.file_size > MAX_XML_BYTES for p in parts):
        raise ValueError("Office内のXMLが処理上限を超えています")


def _part_target(source_part: str, target: str) -> str:
    """Office内部の参照を解決する。ネットワークのリンクを取得しない。"""
    return posixpath.normpath(target.lstrip("/")) if target.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


def _relationships(package: ZipFile, source_part: str) -> dict[str, dict[str, str]]:
    rel_part = posixpath.join(posixpath.dirname(source_part), "_rels", posixpath.basename(source_part) + ".rels")
    if rel_part not in package.namelist():
        return {}
    return {r.attrib["Id"]: dict(r.attrib) for r in ET.fromstring(package.read(rel_part))}


def _mime_type(package: ZipFile, part: str) -> str:
    """OOXMLのContent Typesを優先し、拡張子だけへの依存を避ける。"""
    if "[Content_Types].xml" in package.namelist():
        for item in ET.fromstring(package.read("[Content_Types].xml")):
            if item.tag.endswith("Override") and item.attrib.get("PartName", "").lstrip("/") == part:
                return item.attrib["ContentType"]
            if item.tag.endswith("Default") and item.attrib.get("Extension", "").lower() == Path(part).suffix.lstrip(".").lower():
                return item.attrib["ContentType"]
    return mimetypes.guess_type(part)[0] or "application/octet-stream"


def _add_image(
    result: ExtractionResult, package: ZipFile, part: str | None, block_id: str,
    locations: list[dict[str, Any]], *, parent_id: str | None, section_path: list[str],
) -> None:
    """画像の実体と出現位置を別に記録し、画像解析待ちを明示する。"""
    flags = ["image_analysis_pending"]
    if part is None or part not in package.namelist():
        flags.append("image_reference_unresolved")
        _flag(result, "image_reference_unresolved")
    else:
        result.assets.append(ExtractedAsset(
            block_id=block_id, content=package.read(part), mime_type=_mime_type(package, part),
            source_locations=locations,
        ))
    result.blocks.append(Block(
        block_id=block_id, element_type="figure", content_text=None, source_locations=locations,
        parent_id=parent_id, section_path=list(section_path), quality_flags=flags,
        content_data={"package_part": part}, extraction_method="ooxml_image",
    ))
    result.pending_images += 1


def _sheet_inventory(package: ZipFile, part: str) -> tuple[list[str], list[str]]:
    """実在する値セルと結合だけを列挙し、巨大な矩形全体を作らない。"""
    coordinates: list[str] = []
    merged: list[str] = []
    seen = 0
    with package.open(part) as stream:
        for _, element in ET.iterparse(stream, events=("end",)):
            if element.tag == f"{{{S}}}c":
                seen += 1
                if seen > MAX_SHEET_CELLS:
                    raise ValueError(f"シート内の実在セル数が処理上限を超えています: {part}")
                if element.find(f"{{{S}}}f") is not None or element.find(f"{{{S}}}v") is not None or element.find(f"{{{S}}}is") is not None:
                    coordinate = element.attrib.get("r")
                    if not coordinate:
                        raise ValueError("座標のないExcelセルは位置を保証できません")
                    coordinates.append(coordinate)
                element.clear()
            elif element.tag == f"{{{S}}}mergeCell":
                merged.append(element.attrib["ref"])
                element.clear()
    return coordinates, merged


def _parse_excel(path: Path, *, include_hidden: bool) -> ExtractionResult:
    """全シートを棚卸しし、行ごとの内容・数式・原本位置を抽出する。"""
    try:
        import openpyxl
        from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter, range_boundaries
    except ImportError as exc:
        raise ParserDependencyError("Excel抽出にはopenpyxlが必要です") from exc
    result = ExtractionResult(metrics={"sheets_total": 0, "sheets_processed": 0, "sheets_excluded": 0, "hidden_cells_excluded": 0})
    with ZipFile(path) as package:
        _check_package(package)
        if any(name.lower().endswith("vbaproject.bin") for name in package.namelist()):
            _flag(result, "xlsx_vba_unprocessed")
        workbook_part = "xl/workbook.xml"
        relationships = _relationships(package, workbook_part)
        inventory: dict[str, tuple[str, list[str], list[str]]] = {}
        workbook_xml = ET.fromstring(package.read(workbook_part))
        for element in workbook_xml.findall("s:sheets/s:sheet", NS):
            relation = relationships.get(element.attrib.get(f"{{{R}}}id", ""), {})
            if relation.get("TargetMode") == "External":
                raise ValueError("シートへの外部参照は処理できません")
            part = _part_target(workbook_part, relation.get("Target", ""))
            if part not in package.namelist():
                raise ValueError("Excelシートの実体が見つかりません")
            coords, merged = _sheet_inventory(package, part)
            inventory[element.attrib["name"]] = (part, coords, merged)
        # data_onlyの有無を分けて読み、数式を計算結果として誤って保存しない。
        # 保存先の名前は内容ハッシュであり、.xlsx拡張子を持つとは限らない。
        # ファイル名ではなくバイト列を渡し、原本の保存名に依存させない。
        raw_workbook = path.read_bytes()
        formulas = openpyxl.load_workbook(io.BytesIO(raw_workbook), data_only=False, read_only=False, keep_links=False)
        cached = openpyxl.load_workbook(io.BytesIO(raw_workbook), data_only=True, read_only=False, keep_links=False)
        try:
            result.metrics["sheets_total"] = len(inventory)
            if formulas.chartsheets:
                result.metrics["chartsheets_unprocessed"] = len(formulas.chartsheets)
                _flag(result, "xlsx_chartsheets_unprocessed")
            for sheet_index, sheet in enumerate(formulas.worksheets, 1):
                part, coordinates, merged = inventory[sheet.title]
                sheet_id = f"xlsx-sheet-{sheet_index}"
                excluded = sheet.sheet_state != "visible" and not include_hidden
                sheet_block = Block(
                    block_id=sheet_id, element_type="sheet", content_text=None,
                    source_locations=[{"format": "xlsx", "sheet_name": sheet.title}],
                    section_path=[sheet.title], content_data={"title": sheet.title, "sheet_state": sheet.sheet_state, "merged_ranges": merged, "excluded": excluded},
                    quality_flags=["hidden_excluded"] if excluded else [], extraction_method="openpyxl",
                )
                result.blocks.append(sheet_block)
                if excluded:
                    result.metrics["sheets_excluded"] += 1
                    continue
                result.metrics["sheets_processed"] += 1
                visible_cells: dict[int, list[Any]] = {}
                hidden_columns = {
                    column
                    for dimension in sheet.column_dimensions.values() if dimension.hidden
                    for column in range(max(1, dimension.min or 1), min(16_384, dimension.max or dimension.min or 1) + 1)
                }
                for coordinate in coordinates:
                    row, column = coordinate_to_tuple(coordinate)
                    row_dimension = sheet.row_dimensions.get(row)
                    if not include_hidden and ((row_dimension is not None and row_dimension.hidden) or column in hidden_columns):
                        result.metrics["hidden_cells_excluded"] += 1
                        continue
                    cell = sheet.cell(row, column)
                    if cell.value is not None:
                        visible_cells.setdefault(row, []).append(cell)
                # 明示的なテーブルはヘッダーの根拠にできる。自動推定は候補として残す。
                tables = list(sheet.tables.values())
                first_row = min(visible_cells, default=0)
                candidate_rows = {first_row} if first_row else set()
                for cell_range in merged:
                    min_col, min_row, max_col, max_row = range_boundaries(cell_range)
                    if min_row == first_row and max_row > first_row:
                        candidate_rows.add(max_row + 1)
                    elif min_row == first_row and max_col > min_col:
                        candidate_rows.add(first_row + 1)
                candidate_by_column: dict[int, list[Any]] = {}
                for header_row in sorted(candidate_rows):
                    for cell in visible_cells.get(header_row, []):
                        candidate_by_column.setdefault(cell.column, []).append(cell)
                for row, cells in sorted(visible_cells.items()):
                    cells.sort(key=lambda c: c.column)
                    header_cells: list[Any] = []
                    header_method = "candidate_first_rows"
                    for table in tables:
                        min_col, min_row, max_col, max_row = range_boundaries(table.ref)
                        if min_row <= row <= max_row and any(min_col <= c.column <= max_col for c in cells):
                            header_cells = [c for c in visible_cells.get(min_row, []) if min_col <= c.column <= max_col]
                            header_method = "explicit_table"
                            break
                    if not header_cells:
                        # 一行のデータと無関係な全列見出しを複製しない。
                        header_cells = [header for cell in cells for header in candidate_by_column.get(cell.column, [])]
                    headers = [f"{c.coordinate}: {_scalar(c.value)}" for c in header_cells]
                    records: list[dict[str, Any]] = []
                    row_flags: list[str] = []
                    for cell in cells:
                        value = _scalar(cell.value)
                        record = {"coordinate": cell.coordinate, "value": value, "data_type": cell.data_type, "number_format": cell.number_format}
                        if cell.comment:
                            record["comment"] = cell.comment.text
                        if cell.hyperlink:
                            record["hyperlink"] = cell.hyperlink.target or cell.hyperlink.location
                        if cell.data_type == "f":
                            cached_value = _scalar(cached[sheet.title][cell.coordinate].value)
                            record["formula"] = value
                            record["cached_value"] = cached_value
                            if not isinstance(cell.value, str):
                                record["formula_metadata"] = {
                                    key: _scalar(value) for key, value in vars(cell.value).items()
                                }
                                row_flags.append("xlsx_complex_formula_requires_review")
                                _flag(result, "xlsx_complex_formula_requires_review")
                            if cached_value is None:
                                row_flags.append("formula_cached_value_missing")
                                _flag(result, "formula_cached_value_missing")
                        records.append(record)
                    last_column = get_column_letter(cells[-1].column)
                    first_column = get_column_letter(cells[0].column)
                    result.blocks.append(Block(
                        block_id=f"{sheet_id}-row-{row}", element_type="table",
                        # 表示形式による単位や先頭ゼロを、単なる数値と混同しない。
                        content_text="\t".join(
                            f"{r['coordinate']}: {r['value']}"
                            + (f" (表示形式: {r['number_format']})" if r["number_format"] not in {"General", "@"} else "")
                            for r in records
                        ),
                        source_locations=[{"format": "xlsx", "sheet_name": sheet.title, "cell_range": f"{first_column}{row}:{last_column}{row}"}],
                        parent_id=sheet_id, section_path=[sheet.title],
                        # 結合情報は親sheetに集約し、行数×結合数の出力膨張を防ぐ。
                        content_data={"cells": records, "headers": headers, "header_detection": header_method},
                        quality_flags=sorted(set(row_flags)), extraction_method="openpyxl",
                    ))
                _excel_images(result, package, part, sheet.title, sheet_id)
        finally:
            formulas.close()
            cached.close()
    return result


def _excel_images(result: ExtractionResult, package: ZipFile, part: str, sheet_name: str, parent_id: str) -> None:
    """openpyxlの非公開属性に依存せず、OOXMLから画像の位置と実体を取る。"""
    root = ET.fromstring(package.read(part))
    sheet_rels = _relationships(package, part)
    for index, drawing in enumerate(root.findall("s:drawing", NS), 1):
        rel = sheet_rels.get(drawing.attrib.get(f"{{{R}}}id", ""), {})
        drawing_part = _part_target(part, rel.get("Target", ""))
        if not rel or rel.get("TargetMode") == "External" or drawing_part not in package.namelist():
            _flag(result, "xlsx_drawing_unresolved")
            continue
        drawing_xml = ET.fromstring(package.read(drawing_part))
        drawing_rels = _relationships(package, drawing_part)
        for anchor_index, anchor in enumerate(drawing_xml, 1):
            location: dict[str, Any] = {"format": "xlsx", "sheet_name": sheet_name, "drawing_index": index, "anchor_index": anchor_index}
            start = anchor.find("xdr:from", NS)
            if start is not None:
                location["anchor_row"] = int(start.findtext("xdr:row", "0", NS)) + 1
                location["anchor_column"] = int(start.findtext("xdr:col", "0", NS)) + 1
            for image_index, blip in enumerate(anchor.findall(".//a:blip", NS), 1):
                image_rel = drawing_rels.get(blip.attrib.get(f"{{{R}}}embed", ""), {})
                image_part = _part_target(drawing_part, image_rel["Target"]) if image_rel and image_rel.get("TargetMode") != "External" else None
                _add_image(result, package, image_part, f"{parent_id}-drawing-{index}-{anchor_index}-{image_index}", [location], parent_id=parent_id, section_path=[sheet_name])
            if any(e.tag.rsplit("}", 1)[-1] in {"sp", "cxnSp", "graphicFrame", "grpSp"} for e in anchor.iter()):
                _flag(result, "xlsx_shapes_or_charts_unprocessed")
    if root.find("s:legacyDrawing", NS) is not None:
        _flag(result, "xlsx_legacy_drawing_unprocessed")


def _word_text(element: ET.Element) -> str:
    """フィールド命令・削除済み文字・図形内文字を本文に混ぜず読む。"""
    fragments: list[str] = []

    def visit(node: ET.Element) -> None:
        if node.tag in {f"{{{W}}}del", f"{{{W}}}moveFrom", f"{{{W}}}txbxContent"}:
            return
        if node.tag == f"{{{W}}}t":
            fragments.append(node.text or "")
        elif node.tag == f"{{{W}}}tab":
            fragments.append("\t")
        elif node.tag in {f"{{{W}}}br", f"{{{W}}}cr"}:
            fragments.append("\n")
        else:
            for child in node:
                visit(child)
    visit(element)
    return "".join(fragments)


def _word_style_levels(package: ZipFile) -> dict[str, int]:
    """独自スタイルの継承とアウトラインレベルも見出し判定に使う。"""
    if "word/styles.xml" not in package.namelist():
        return {}
    styles = {s.attrib.get(f"{{{W}}}styleId", ""): s for s in ET.fromstring(package.read("word/styles.xml")).findall("w:style", NS)}
    levels: dict[str, int] = {}

    def resolve(style_id: str, seen: set[str]) -> int | None:
        if style_id in seen or style_id not in styles:
            return None
        seen = seen | {style_id}
        style = styles[style_id]
        outline = style.find("w:pPr/w:outlineLvl", NS)
        if outline is not None:
            value = int(outline.attrib.get(f"{{{W}}}val", "9"))
            return value + 1 if value < 9 else None
        name = style.find("w:name", NS)
        match = re.search(r"(?:heading|見出し)\s*([1-9])$", name.attrib.get(f"{{{W}}}val", "") if name is not None else style_id, re.I)
        if match:
            return int(match.group(1))
        based = style.find("w:basedOn", NS)
        return resolve(based.attrib.get(f"{{{W}}}val", ""), seen) if based is not None else None
    for style_id in styles:
        level = resolve(style_id, set())
        if level is not None:
            levels[style_id] = level
    return levels


def _word_numbering(
    paragraph: ET.Element, style_id: str, styles: dict[str, ET.Element],
    definitions: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """本文に文字として入らないWord自動番号の定義を保存する。

    第1版は再開始・継承を含む表示番号の完全再現を行わない。番号を本文から
    黙って落とす代わりに、定義と未レンダリング状態を確認者へ渡す。
    """
    properties = paragraph.find("w:pPr/w:numPr", NS)
    if properties is None:
        visited: set[str] = set()
        while style_id in styles and style_id not in visited:
            visited.add(style_id)
            style = styles[style_id]
            properties = style.find("w:pPr/w:numPr", NS)
            if properties is not None:
                break
            based = style.find("w:basedOn", NS)
            style_id = based.attrib.get(f"{{{W}}}val", "") if based is not None else ""
    if properties is None:
        return None
    num_id = properties.find("w:numId", NS)
    level = properties.find("w:ilvl", NS)
    result: dict[str, Any] = {
        "num_id": num_id.attrib.get(f"{{{W}}}val") if num_id is not None else None,
        "level": level.attrib.get(f"{{{W}}}val", "0") if level is not None else "0",
        "rendered": False,
    }
    definition = definitions.get(result["num_id"], {})
    if definition:
        result["definition"] = definition.get(result["level"], {})
    # numId=0は番号付けを打ち消す指定なので、番号の欠落には数えない。
    return None if result["num_id"] == "0" else result


def _word_numbering_definitions(package: ZipFile) -> dict[str, dict[str, Any]]:
    """自動番号の書式・開始値を一度だけ読み、各段落に必要な定義を渡す。"""
    if "word/numbering.xml" not in package.namelist():
        return {}
    root = ET.fromstring(package.read("word/numbering.xml"))
    abstracts: dict[str, dict[str, Any]] = {}
    for abstract in root.findall("w:abstractNum", NS):
        levels = {}
        for level in abstract.findall("w:lvl", NS):
            details = {}
            for tag in ("start", "numFmt", "lvlText", "lvlRestart", "suff"):
                item = level.find(f"w:{tag}", NS)
                if item is not None:
                    details[tag] = item.attrib.get(f"{{{W}}}val")
            levels[level.attrib.get(f"{{{W}}}ilvl", "0")] = details
        abstracts[abstract.attrib.get(f"{{{W}}}abstractNumId", "")] = levels
    result = {}
    for number in root.findall("w:num", NS):
        abstract = number.find("w:abstractNumId", NS)
        abstract_id = abstract.attrib.get(f"{{{W}}}val", "") if abstract is not None else ""
        result[number.attrib.get(f"{{{W}}}numId", "")] = abstracts.get(abstract_id, {})
    return result


def _parse_word(path: Path) -> ExtractionResult:
    """本文順に段落・表・画像を取り、目次は本文への参照として残す。"""
    result = ExtractionResult(metrics={"paragraphs": 0, "tables": 0, "toc_entries": 0})
    with ZipFile(path) as package:
        _check_package(package)
        part = "word/document.xml"
        root = ET.fromstring(package.read(part))
        body = root.find("w:body", NS)
        if body is None:
            raise ValueError("Word本文がありません")
        levels = _word_style_levels(package)
        styles = {
            s.attrib.get(f"{{{W}}}styleId", ""): s
            for s in ET.fromstring(package.read("word/styles.xml")).findall("w:style", NS)
        } if "word/styles.xml" in package.namelist() else {}
        numbering_definitions = _word_numbering_definitions(package)
        relationships = _relationships(package, part)
        headings: list[tuple[int, str, str]] = []
        bookmarks: dict[str, str] = {}
        toc_links: list[tuple[str, str]] = []
        paragraph_index = 0
        table_index = 0
        # 図形・変更履歴等が残る原本は、未対応を隠さず部分抽出にする。
        if any(e.tag in {f"{{{W}}}ins", f"{{{W}}}del", f"{{{W}}}moveFrom", f"{{{W}}}moveTo", f"{{{W}}}pPrChange", f"{{{W}}}rPrChange"} for e in root.iter()):
            _flag(result, "word_tracked_changes_require_review")
        if any(e.tag.rsplit("}", 1)[-1] in {"anchor", "txbxContent", "wsp", "wgp", "shape", "group", "object", "altChunk"} for e in root.iter()):
            _flag(result, "word_unsupported_or_floating_content")
        # インラインのグラフ／SmartArtは画像blipを持たない場合がある。
        # 通常の画像だけを探すと「本文だけ抽出して成功」になるため、
        # graphicDataの種別と既知の参照要素を別途調べる。未取得の画像は作らない。
        picture_uri = "http://schemas.openxmlformats.org/drawingml/2006/picture"
        non_picture_reference_tags = {
            "{http://schemas.openxmlformats.org/drawingml/2006/chart}chart",
            "{http://schemas.openxmlformats.org/drawingml/2006/diagram}relIds",
        }
        if any(
            (element.tag == f"{{{A}}}graphicData" and element.attrib.get("uri") != picture_uri)
            or element.tag in non_picture_reference_tags
            for element in root.iter()
        ):
            _flag(result, "word_non_picture_graphic_unprocessed")
        for name in package.namelist():
            if re.match(r"word/(header\d*|footer\d*|footnotes|endnotes|comments)\.xml$", name):
                auxiliary = ET.fromstring(package.read(name))
                if any(e.text and e.text.strip() for e in auxiliary.iter(f"{{{W}}}t")):
                    _flag(result, "word_auxiliary_content_unprocessed")

        def body_elements(element: ET.Element):
            # コンテンツコントロールの中身も、外側の順序を崩さず読む。
            for child in element:
                if child.tag in {f"{{{W}}}p", f"{{{W}}}tbl"}:
                    yield child
                elif child.tag in {f"{{{W}}}sdt", f"{{{W}}}sdtContent", f"{{{W}}}ins", f"{{{W}}}moveTo", f"{{{W}}}customXml"}:
                    yield from body_elements(child)

        for body_index, element in enumerate(body_elements(body), 1):
            section = [label for _, label, _ in headings]
            parent_id = headings[-1][2] if headings else None
            location: dict[str, Any] = {"format": "docx", "body_index": body_index}
            if element.tag == f"{{{W}}}p":
                paragraph_index += 1
                location["paragraph_index"] = paragraph_index
                text = _word_text(element)
                style = element.find("w:pPr/w:pStyle", NS)
                style_id = style.attrib.get(f"{{{W}}}val", "") if style is not None else ""
                numbering = _word_numbering(element, style_id, styles, numbering_definitions)
                if numbering is not None:
                    _flag(result, "word_auto_numbering_not_rendered")
                outline = element.find("w:pPr/w:outlineLvl", NS)
                level = levels.get(style_id)
                if outline is not None:
                    value = int(outline.attrib.get(f"{{{W}}}val", "9"))
                    level = value + 1 if value < 9 else None
                instructions = " ".join(e.text or "" for e in element.iter(f"{{{W}}}instrText"))
                instructions += " " + " ".join(e.attrib.get(f"{{{W}}}instr", "") for e in element.iter(f"{{{W}}}fldSimple"))
                links = [h.attrib[f"{{{W}}}anchor"] for h in element.iter(f"{{{W}}}hyperlink") if f"{{{W}}}anchor" in h.attrib]
                # 本文の「3節参照」リンクも_Tocへ向くことがあるため、リンク先
                # だけで目次と断定せず、スタイル・命令・目次らしい表示を使う。
                is_toc = bool(
                    re.match(r"^(?:TOC|目次)\s*\d*$", style_id, re.I)
                    or re.search(r"\bTOC\b", instructions)
                    or (any(a.startswith("_Toc") for a in links) and re.search(r"(?:\t|\.{2,}|…|・{2,})\s*\d+\s*$", text))
                )
                block_id = f"word-paragraph-{paragraph_index}"
                kind = "navigation" if is_toc else "heading" if level else "paragraph"
                if kind == "heading" and text.strip():
                    headings[:] = [(n, t, i) for n, t, i in headings if n < level]
                    parent_id = headings[-1][2] if headings else None
                    headings.append((level, text, block_id))
                    section = [label for _, label, _ in headings]
                result.blocks.append(Block(
                    block_id=block_id, element_type=kind, content_text=text or None,
                    source_locations=[dict(location)], parent_id=parent_id, section_path=section,
                    content_data={"style_id": style_id, "level": level, "numbering": numbering, "bookmark_targets": links, "field_instructions": instructions.strip()},
                    extraction_method="ooxml",
                ))
                if is_toc:
                    result.metrics["toc_entries"] += 1
                    toc_links.extend((block_id, anchor) for anchor in links)
            else:
                table_index += 1
                location["table_index"] = table_index
                block_id = f"word-table-{table_index}"
                rows = [["\n".join(_word_text(p) for p in cell.findall("w:p", NS)) for cell in row.findall("w:tc", NS)] for row in element.findall("w:tr", NS)]
                if element.findall(".//w:tbl", NS):
                    _flag(result, "word_nested_table_unprocessed")
                merges = []
                for row_index, row in enumerate(element.findall("w:tr", NS), 1):
                    for col_index, cell in enumerate(row.findall("w:tc", NS), 1):
                        span = cell.find("w:tcPr/w:gridSpan", NS)
                        vertical = cell.find("w:tcPr/w:vMerge", NS)
                        if span is not None or vertical is not None:
                            merges.append({"row": row_index, "column": col_index, "grid_span": span.attrib.get(f"{{{W}}}val") if span is not None else None, "vertical_merge": vertical.attrib.get(f"{{{W}}}val", "continue") if vertical is not None else None})
                result.blocks.append(Block(
                    block_id=block_id, element_type="table", content_text="\n".join("\t".join(row) for row in rows) or None,
                    source_locations=[dict(location)], parent_id=parent_id, section_path=section,
                    content_data={"rows": rows, "merges": merges}, extraction_method="ooxml",
                ))
            for bookmark in element.iter(f"{{{W}}}bookmarkStart"):
                name = bookmark.attrib.get(f"{{{W}}}name")
                if name:
                    bookmarks[name] = block_id
            image_refs = [blip.attrib.get(f"{{{R}}}embed") or blip.attrib.get(f"{{{R}}}link") for blip in element.iter(f"{{{A}}}blip")]
            image_refs += [image.attrib.get(f"{{{R}}}id") for image in element.iter("{urn:schemas-microsoft-com:vml}imagedata")]
            for image_index, image_ref in enumerate(image_refs, 1):
                rel = relationships.get(image_ref or "", {})
                image_part = _part_target(part, rel["Target"]) if rel and rel.get("TargetMode") != "External" else None
                _add_image(result, package, image_part, f"{block_id}-image-{image_index}", [dict(location, image_index=image_index)], parent_id=block_id, section_path=section)
            for textbox_index, textbox in enumerate(element.iter(f"{{{W}}}txbxContent"), 1):
                text = "\n".join(_word_text(p) for p in textbox.findall("w:p", NS))
                if text.strip():
                    result.blocks.append(Block(
                        block_id=f"{block_id}-textbox-{textbox_index}", element_type="paragraph", content_text=text,
                        source_locations=[dict(location, textbox_index=textbox_index)], parent_id=block_id, section_path=section,
                        quality_flags=["word_textbox_reading_order_unverified"], extraction_method="ooxml",
                    ))
        block_map = {block.block_id: block for block in result.blocks}
        for from_id, bookmark in toc_links:
            if bookmark in bookmarks:
                result.relations.append(Relation(from_id, bookmarks[bookmark], "toc_entry", evidence={"bookmark": bookmark}))
                entry_text = block_map[from_id].content_text or ""
                target_text = block_map[bookmarks[bookmark]].content_text or ""
                entry_label = re.sub(r"(?:\t|\.{2,}|…|・{2,})\s*\d+\s*$", "", entry_text)
                if entry_label.strip() and re.sub(r"\s+", "", entry_label) != re.sub(r"\s+", "", target_text):
                    _flag(result, "word_toc_text_mismatch")
            else:
                _flag(result, "word_toc_unresolved_target")
        result.metrics["paragraphs"] = paragraph_index
        result.metrics["tables"] = table_index
    return result


def _pdf_original_images(page, page_index: int, result: ExtractionResult) -> None:
    """PDF内の原画像を保持する補助。本文・表の抽出は行わない。"""
    location = {"format": "pdf", "page_number": page_index,
                "coordinate_system": "pdf_bottom_left", "rotation": int(page.get("/Rotate", 0))}
    page_id = f"pdf-page-{page_index}"
    try:
        for image_index, image in enumerate(page.images, 1):
            image_location = dict(location, image_index=image_index, image_name=image.name, position_status="page_only")
            image_id = f"{page_id}-image-{image_index}"
            result.assets.append(ExtractedAsset(
                block_id=image_id, content=image.data, mime_type=mimetypes.guess_type(image.name)[0] or "application/octet-stream",
                source_locations=[image_location],
            ))
            result.blocks.append(Block(
                block_id=image_id, element_type="figure", content_text=None, source_locations=[image_location], parent_id=page_id,
                quality_flags=["image_analysis_pending", "pdf_image_position_unavailable"], extraction_method="pypdf_original_image",
            ))
            result.pending_images += 1
    except Exception:
        _flag(result, "pdf_image_extraction_failed")


def _parse_pdf_native(path: Path) -> ExtractionResult:
    """文字レイヤーを読む軽量ルート。OCRや表・図の意味解析は行わない。"""
    try:
        from pypdf import PdfReader
        from pypdf.generic import ContentStream
    except ImportError as exc:
        raise ParserDependencyError("PDF抽出にはpypdfが必要です") from exc
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("暗号化されたPDFは第1版では処理できません")
    result = ExtractionResult(metrics={"pdf_backend": "native", "ocr_enabled": False, "pages_total": len(reader.pages), "pages_processed": 0})
    if not reader.pages:
        _flag(result, "pdf_has_no_pages")
    for page_index, page in enumerate(reader.pages, 1):
        location = {"format": "pdf", "page_number": page_index, "coordinate_system": "pdf_bottom_left", "rotation": int(page.get("/Rotate", 0))}
        page_id = f"pdf-page-{page_index}"
        flags: list[str] = []
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
            flags.append("pdf_text_extraction_failed")
            _flag(result, "pdf_text_extraction_failed")
        if not text.strip():
            flags.append("pdf_page_without_text_ocr_required")
            _flag(result, "pdf_page_without_text_ocr_required")
        elif "\ufffd" in text or "\x00" in text:
            flags.append("pdf_garbled_text")
            _flag(result, "pdf_garbled_text")
        result.blocks.append(Block(
            block_id=page_id, element_type="paragraph" if text.strip() else "page", content_text=text or None,
            source_locations=[location], quality_flags=flags, extraction_method="pypdf_native",
            content_data={"title": f"ページ{page_index}", "layout_analysis": False},
        ))
        _pdf_original_images(page, page_index, result)
        try:
            contents = page.get_contents()
            if contents is not None and any(op in {b"m", b"l", b"c", b"re", b"S", b"s", b"f", b"f*", b"B", b"B*"} for _, op in ContentStream(contents, reader).operations):
                _flag(result, "pdf_vector_graphics_unprocessed")
        except Exception:
            _flag(result, "pdf_graphics_inventory_failed")
        result.metrics["pages_processed"] += 1
    return result


@lru_cache(maxsize=2)
def _docling_converter(artifacts_path: Path, model_fingerprint: str, converter_type):
    try:
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import PdfFormatOption
    except ImportError as exc:
        raise ParserDependencyError("pdf_backend='docling'にはDoclingが必要です。プロジェクトの依存を導入してください") from exc
    options = PdfPipelineOptions(artifacts_path=artifacts_path)
    options.do_ocr = False
    options.do_table_structure = True
    options.generate_picture_images = True
    options.do_picture_description = False
    options.do_picture_classification = False
    options.enable_remote_services = False
    options.allow_external_plugins = False
    options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=2)
    return converter_type(allowed_formats=[InputFormat.PDF], format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def _parse_pdf_docling(path: Path, artifacts_path: Path) -> ExtractionResult:
    """検査済みの固定モデルとプロセス内のConverterを再利用する。"""
    from file_scan.models import verify_models
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as error:
        raise ParserDependencyError("pdf_backend='docling'にはDoclingが必要です") from error

    artifacts_path = Path(artifacts_path).resolve()
    if not (artifacts_path / "model-manifest.json").is_file():
        raise ParserDependencyError(
            "Doclingモデルが未配置です。python scripts/download_docling_models.pyを実行してください")
    fingerprint = verify_models(artifacts_path)
    converter = _docling_converter(artifacts_path, fingerprint, DocumentConverter)
    converted = converter.convert(path)
    status = str(getattr(converted.status, "value", converted.status)).lower()
    if status not in {"success", "partial_success"}:
        raise ValueError("DoclingによるPDF変換が完了しませんでした")
    document = converted.document
    page_count = converted.input.page_count
    result = ExtractionResult(metrics={
        "pdf_backend": "docling", "ocr_enabled": False, "device": "cpu",
        "pages_total": page_count, "pages_processed": len(converted.pages),
        "tables": 0, "pictures": 0, "layout_analysis": True,
    })
    if status == "partial_success" or converted.errors:
        _flag(result, "docling_partial_conversion")
    if len(converted.pages) != page_count:
        _flag(result, "docling_incomplete_pages")
    for page_no in range(1, page_count + 1):
        page = document.pages.get(page_no)
        result.blocks.append(Block(
            block_id=f"pdf-page-{page_no}", element_type="page", content_text=None,
            source_locations=[{"format": "pdf", "page_number": page_no}],
            content_data={"size": page.size.model_dump(mode="json") if page else None},
            extraction_method="docling_cpu_ocr_disabled",
        ))
    section: list[str] = []
    text_pages: set[int] = set()
    for index, (item, _) in enumerate(document.iterate_items(), 1):
        label = str(getattr(getattr(item, "label", None), "value", getattr(item, "label", "")))
        locations: list[dict[str, Any]] = []
        for provenance in getattr(item, "prov", []):
            loc: dict[str, Any] = {"format": "pdf", "page_number": provenance.page_no}
            if getattr(provenance, "bbox", None) is not None:
                loc["bbox"] = provenance.bbox.model_dump(mode="json")
                loc["coordinate_system"] = {"BOTTOMLEFT": "pdf_bottom_left", "TOPLEFT": "pdf_top_left"}[
                    provenance.bbox.coord_origin.value]
            locations.append(loc)
        if not locations:
            _flag(result, "docling_source_location_missing")
            continue
        block_id = f"docling-item-{index}"
        parent_id = f"pdf-page-{locations[0]['page_number']}"
        data: dict[str, Any] = {"docling_label": label}
        if label == "picture":
            result.blocks.append(Block(
                block_id=block_id, element_type="figure", content_text=None,
                source_locations=locations, parent_id=parent_id, section_path=list(section),
                quality_flags=["image_analysis_pending"], content_data=data,
                extraction_method="docling_cpu_ocr_disabled",
            ))
            result.pending_images += 1
            result.metrics["pictures"] += 1
            try:
                image = item.get_image(document)
                if image is None:
                    raise ValueError("picture unavailable")
                output = io.BytesIO()
                image.save(output, format="PNG")
                result.assets.append(ExtractedAsset(
                    block_id=block_id, content=output.getvalue(), mime_type="image/png",
                    source_locations=locations, asset_role="region_image",
                    rendering_details={"renderer": "docling", "region": "picture"},
                ))
            except Exception:
                _flag(result, "docling_picture_image_unavailable")
            continue
        text = getattr(item, "text", None)
        if label == "table":
            text = item.export_to_markdown(doc=document)
            data["table"] = item.data.model_dump(mode="json")
            result.metrics["tables"] += 1
        if not text or not text.strip():
            if label == "table":
                _flag(result, "docling_empty_table")
            continue
        if "\ufffd" in text or "\x00" in text:
            _flag(result, "docling_garbled_text")
        text_pages.update(location["page_number"] for location in locations)
        kind = "table" if label == "table" else "heading" if label in {"section_header", "title"} else "paragraph"
        if kind == "heading":
            section = [text]
        result.blocks.append(Block(
            block_id=block_id, element_type=kind, content_text=text,
            source_locations=locations, parent_id=parent_id, section_path=list(section),
            content_data=data, extraction_method="docling_cpu_ocr_disabled",
        ))
    # 原画像はDoclingが図として検出しなかった場合も保存する。本文の代替には使わない。
    from pypdf import PdfReader
    for page_no, page in enumerate(PdfReader(path).pages, 1):
        _pdf_original_images(page, page_no, result)
    for block in result.blocks:
        if block.element_type == "page" and block.source_locations[0]["page_number"] not in text_pages:
            block.quality_flags.append("pdf_page_without_text_ocr_required")
            _flag(result, "pdf_page_without_text_ocr_required")
    return result
