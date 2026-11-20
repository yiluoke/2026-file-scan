"""Office図形の表示をPDF経由で画像化し、表示上の座標を保持する。"""

import io
import os
import shutil
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

import pypdfium2 as pdfium
from defusedxml import ElementTree

from file_scan.config import ImageAnalysisSettings
from file_scan.domain import Block, ExtractedAsset, ExtractionResult

PROFILE = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
  <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
    <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
  </item>
  <item oor:path="/org.openoffice.Office.Calc/Content/Update">
    <prop oor:name="Link" oor:op="fuse"><value>1</value></prop>
  </item>
</oor:items>
"""
GRAPHICS = [
    pdfium.raw.FPDF_PAGEOBJ_PATH, pdfium.raw.FPDF_PAGEOBJ_IMAGE,
    pdfium.raw.FPDF_PAGEOBJ_FORM, pdfium.raw.FPDF_PAGEOBJ_SHADING,
]
MAX_PREVIEW_PAGES = 100


def _verify_render_input(source: Path) -> None:
    # レンダラーに外部ファイルや埋め込みアプリケーションを開かせない。
    with ZipFile(source) as package:
        for name in package.namelist():
            if "/externalLinks/" in name or "/embeddings/" in name or name.lower().endswith("vbaproject.bin"):
                raise ValueError("office_render_external_content")
            if name.endswith(".rels"):
                for link in ElementTree.fromstring(package.read(name)):
                    if (link.get("TargetMode") == "External"
                            and not link.get("Type", "").endswith("/hyperlink")):
                        raise ValueError("office_render_external_content")
            if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                for node in ElementTree.fromstring(package.read(name)).iter():
                    if node.tag.endswith("}f") and node.text and any(
                        token in node.text.upper()
                        for token in ("WEBSERVICE", "RTD(", "DDE(", "IMAGE(", "|")
                    ):
                        raise ValueError("office_render_external_content")


def _copy_render_input(source: Path, local: Path, hidden_drawing_parts: list[str]) -> None:
    if not hidden_drawing_parts:
        shutil.copyfile(source, local)
        return

    from lxml import etree
    from file_scan.parsers import _omit_hidden_drawings

    # OOXMLの名前空間接頭辞を保ち、レンダラーにも除外済みの描画だけを渡す。
    with ZipFile(source) as incoming, ZipFile(local, "w") as outgoing:
        for part in incoming.infolist():
            if part.filename in hidden_drawing_parts:
                xml = etree.fromstring(incoming.read(part), etree.XMLParser(resolve_entities=False))
                _omit_hidden_drawings(xml)
                outgoing.writestr(part, etree.tostring(xml, encoding="utf-8"))
            else:
                with incoming.open(part) as original, outgoing.open(part, "w") as copied:
                    shutil.copyfileobj(original, copied)


def _render_pdf(source: Path, file_format: str, directory: Path, timeout: int,
                hidden_drawing_parts: list[str]) -> Path:
    executable = shutil.which("soffice")
    if not executable:
        raise ValueError("office_render_unavailable")
    _verify_render_input(source)
    profile = directory / "profile"
    (profile / "user").mkdir(parents=True)
    (profile / "user" / "registrymodifications.xcu").write_text(PROFILE, encoding="utf-8")
    local = directory / f"document.{file_format}"
    _copy_render_input(source, local, hidden_drawing_parts)
    environment = {key: os.environ[key] for key in (
        "PATH", "SystemRoot", "WINDIR", "LANG", "LC_ALL",
    ) if key in os.environ}
    # 新規プロファイルと絞った環境変数で起動する。APIキーやDB設定は渡さない。
    environment["TMPDIR"] = str(directory)
    subprocess.run(
        [executable, f"-env:UserInstallation={profile.as_uri()}", "--headless",
         "--convert-to", "pdf", "--outdir", str(directory), str(local)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=timeout, check=True, env=environment,
    )
    output = directory / "document.pdf"
    if not output.is_file() or output.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("office_render_failed")
    return output


def _graphic_region(page) -> tuple | None:
    if page.get_rotation():
        return None
    left, bottom, right, top = page.get_bbox()
    bounds = [obj.get_bounds() for obj in page.get_objects(filter=GRAPHICS, max_depth=1)]
    bounds = [box for box in bounds if box[2] - box[0] >= 8 or box[3] - box[1] >= 8]
    if not bounds:
        return None
    x_min = max(left, min(box[0] for box in bounds) - 12)
    y_min = max(bottom, min(box[1] for box in bounds) - 12)
    x_max = min(right, max(box[2] for box in bounds) + 12)
    y_max = min(top, max(box[3] for box in bounds) + 12)
    if x_max - x_min < 8 or y_max - y_min < 8:
        return None
    return (x_min, y_min, x_max, y_max), (x_min - left, y_min - bottom, right - x_max, top - y_max)


def append_rendered_regions(result: ExtractionResult, source: Path, file_format: str,
                            settings: ImageAnalysisSettings) -> None:
    warning = "office_render_no_graphic_region"
    try:
        with tempfile.TemporaryDirectory(prefix="file-scan-office-") as temporary:
            output = _render_pdf(source, file_format, Path(temporary).resolve(), settings.timeout_seconds,
                                 result.metrics.get("hidden_drawing_parts", []))
            with pdfium.PdfDocument(output) as document:
                limit = min(len(document), MAX_PREVIEW_PAGES)
                rendered = 0
                for index in range(limit):
                    with closing(document[index]) as page:
                        region = _graphic_region(page)
                        if not region:
                            continue
                        bbox, crop = region
                        scale = min(2.5, 2048 / max(bbox[2] - bbox[0], bbox[3] - bbox[1]))
                        with closing(page.render(scale=scale, crop=crop, may_draw_forms=False,
                                                 draw_annots=False)) as bitmap, io.BytesIO() as image:
                            bitmap.to_pil().save(image, "PNG")
                            content = image.getvalue()
                    block_id = f"office-preview-{index + 1}"
                    locations = [{"format": file_format, "preview_page_number": index + 1,
                                  "bbox": list(bbox), "coordinate_system": "office_preview_pdf_bottom_left"}]
                    result.blocks.append(Block(
                        block_id, "figure", None, locations,
                        quality_flags=["image_analysis_pending"], extraction_method="office_pdf_render",
                    ))
                    result.assets.append(ExtractedAsset(
                        block_id, content, "image/png", locations, asset_role="region_image",
                        rendering_details={"renderer": "libreoffice_pdf"},
                    ))
                    result.pending_images += 1
                    rendered += 1
                    warning = ""
                    if rendered >= settings.max_images_per_document:
                        if index + 1 < len(document):
                            warning = "office_render_region_limit"
                        break
                else:
                    if len(document) > limit:
                        warning = "office_render_page_limit"
    except (OSError, subprocess.SubprocessError, ValueError, pdfium.PdfiumError) as error:
        warning = str(error) if type(error) is ValueError and str(error) in {
            "office_render_external_content", "office_render_unavailable",
        } else "office_render_failed"
    if warning:
        result.quality_flags.append(warning)
