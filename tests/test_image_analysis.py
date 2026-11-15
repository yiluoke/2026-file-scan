"""実際のOffice画像と模擬Responses APIで、公開検索までの経路を確認する。"""

import io
import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
from docx import Document
from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.drawing.image import Image as SpreadsheetImage
from PIL import Image
from sqlalchemy import select

from file_scan import image_analysis
from file_scan.config import EmbeddingSettings, ImageAnalysisSettings, load_config
from file_scan.db import tables
from file_scan.domain import Block, ExtractedAsset, ExtractionResult
from file_scan.embeddings import embed
from file_scan.parsers import parse_file
from file_scan.pipeline import ingest
from file_scan.profiles import conversion_profile
from search import schema
from search.management import create_release, publish


@pytest.mark.parametrize("file_format", ["docx", "xlsx"])
def test_office_image_analysis_publication(postgres_database, tmp_path, monkeypatch, file_format):
    from fastapi.testclient import TestClient
    from search.api import create_app
    from search.config import SearchSettings, async_database
    from sqlalchemy import text

    root = tmp_path / "input"
    root.mkdir()
    picture = io.BytesIO()
    Image.new("RGB", (160, 80), "white").save(picture, "PNG")
    picture_bytes = picture.getvalue()
    picture.seek(0)
    if file_format == "docx":
        document = Document()
        document.add_paragraph("申請画面の操作手順です。")
        document.add_picture(picture)
        document.save(root / "manual.docx")
    else:
        book = Workbook()
        book.active["A1"] = "申請画面の操作手順です。"
        book.active.add_image(SpreadsheetImage(picture), "B3")
        book.save(root / "manual.xlsx")
    config = tmp_path / "config.yaml"
    config.write_text("sources: [{source_id: docs, root: input}]\n"
                      "storage_dir: store\nreport_dir: report\n", encoding="utf-8")
    settings = load_config(config)
    assert not settings.image_analysis.enabled
    requests = []

    def handle(request):
        data = json.loads(request.content)
        requests.append((request.url.path, data))
        if request.url.path.endswith("/responses"):
            assert data["store"] is False
            assert data["input"][0]["content"][0]["image_url"].startswith("data:image/png;base64,")
            assert data["text"]["format"]["strict"]
            return httpx.Response(200, json={
                "status": "completed", "output": [{"type": "message", "content": [{
                    "type": "output_text", "text": json.dumps({
                        "visible_text": "", "description": "左側に一覧、右側に編集欄があります。",
                        "unreadable": "ボタン名",
                    }, ensure_ascii=False),
                }]}],
            })
        return httpx.Response(200, json={
            "data": [{"index": i, "embedding": [1, 0, 0]} for i in range(len(data["input"]))],
            "model": data["model"], "object": "list",
        })

    original = openai.OpenAI
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: original(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handle))))
    first = ingest(settings, postgres_database)
    assert first["summary"]["partial"] == 1 and not requests

    monkeypatch.setenv("VISION_KEY", "local-test-key")
    config.write_text(config.read_text(encoding="utf-8") +
                      f"image_analysis: {{enabled: true, formats: [{file_format}], api_key_env: VISION_KEY}}\n",
                      encoding="utf-8")
    settings = load_config(config)
    settings.embedding = EmbeddingSettings(enabled=True, api_key="local-test-key", dimensions=3)
    assert conversion_profile(settings, "md") == conversion_profile(replace(
        settings, image_analysis=ImageAnalysisSettings()), "md")
    result = ingest(settings, postgres_database)
    generation = result["files"][0]
    assert generation["generation_id"] != first["files"][0]["generation_id"]
    assert generation["metrics"]["image_analysis"]["unreadable"] == 1
    assert generation["processing_status"] == "partial"
    assert ingest(settings, postgres_database)["summary"]["unchanged"] == 1
    assert len(requests) == 1
    with postgres_database.connect() as conn:
        namespace = conn.execute(text("SELECT current_schema()")).scalar_one()
        derived = conn.execute(select(tables["blocks"]).where(
            tables["blocks"].c.artifact_type == "derived"
        )).mappings().one()
        assert derived["content_data"]["visible_text"] == ""
        assert derived["content_data"]["unreadable"] == "ボタン名"
        location = derived["source_locations"][0]
        if file_format == "docx":
            assert location["paragraph_index"]
        else:
            assert (location["anchor_row"], location["anchor_column"]) == (3, 2)
        relation = conn.execute(select(tables["artifact_relations"]).where(
            tables["artifact_relations"].c.relation_type == "derived_from"
        )).mappings().one()
        assert relation["to_block_id"] == derived["parent_id"]
    assert embed(settings, postgres_database)["embedded"] == 2
    schema.migrate(postgres_database)
    release = create_release(postgres_database, settings, "images", make_default=True)
    args = (postgres_database, settings, release, generation["document_id"],
            generation["generation_id"], "reviewer")
    before = publish(*args, allow_partial=True)
    after = publish(*args, allow_partial=True, include_image_descriptions=True)
    assert before != after
    with postgres_database.connect() as conn:
        rows = conn.execute(select(schema.chunks.c.publication_id, schema.chunks.c.content_text)).all()
        assert all("編集欄" not in content for pid, content in rows if pid == before)

    async def query_embedding(*_):
        return [1, 0, 0]

    search = SearchSettings(settings, "test-search-key")
    engine = async_database(postgres_database.url, schema=namespace)
    headers = {"Authorization": "Bearer test-search-key"}
    condition = {"name": "artifact_type", "comparison_operator": "is", "value": "derived"}
    payload = {
        "knowledge_id": settings.knowledge_id, "query": "編集欄",
        "retrieval_setting": {"top_k": 5, "score_threshold": 0.0},
        "metadata_condition": {"conditions": [condition]},
    }
    original_path = root / f"manual.{file_format}"
    original_bytes = original_path.read_bytes()
    with TestClient(create_app(search, engine=engine, embedder=query_embedding, allow_history=True)) as client:
        response = client.post("/search-with-status", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        hit, = response.json()["records"]
        assert "編集欄" in hit["content"]
        metadata = hit["metadata"]
        assert metadata["artifact_type"] == "derived"
        assert metadata["source_spans"] == []
        asset, = metadata["related_assets"]
        assert asset["asset_id"] == derived["content_data"]["asset_id"]
        source_ref, image_ref = metadata["source_ref"], asset["image_ref"]
        assert source_ref == f"/sources/{after}"
        assert image_ref == f"/sources/{after}/images/{asset['occurrence_id']}"
        assert client.get(source_ref, headers=headers).content == original_bytes
        download = client.get(image_ref, headers=headers)
        assert download.status_code == 200
        assert download.content == picture_bytes
        assert download.headers["Content-Type"] == "image/png"
        assert download.headers["Cache-Control"] == "no-store"
        assert download.headers["Content-Disposition"] == 'attachment; filename="image.png"'
        assert client.get(image_ref).status_code == 401
        assert client.get(f"/sources/{after}/images/{asset['asset_id']}", headers=headers).status_code == 404
        assert client.get(image_ref.replace(after, before), headers=headers).status_code == 404

        image_digest = asset["asset_id"].removeprefix("sha256:")
        image_path = settings.storage_dir / "objects" / "sha256" / image_digest[:2] / image_digest
        image_path.chmod(0o600)
        image_path.write_bytes(b"corrupt")
        assert client.get(image_ref, headers=headers).status_code == 503
        image_path.write_bytes(picture_bytes)

        if file_format == "docx":
            changed = Document(original_path)
            changed.add_paragraph("更新後の手順です。")
        else:
            changed = load_workbook(original_path)
            changed.active["A2"] = "更新後の手順です。"
        changed.save(original_path)
        ingest(settings, postgres_database)
        assert client.get(source_ref, headers=headers).status_code == 404
        assert client.get(image_ref, headers=headers).status_code == 404
        payload["metadata_condition"]["conditions"].append({
            "name": "target_release", "comparison_operator": "is", "value": release,
        })
        history = client.post("/search-with-status", headers=headers, json=payload)
        assert history.status_code == 200, history.text
        hit, = history.json()["records"]
        assert hit["metadata"]["source_ref"] == source_ref + "?mode=history"
        assert hit["metadata"]["related_assets"][0]["image_ref"] == image_ref + "?mode=history"
        assert client.get(source_ref + "?mode=history", headers=headers).content == original_bytes
        assert client.get(image_ref + "?mode=history", headers=headers).content == picture_bytes

    engine = async_database(postgres_database.url, schema=namespace)
    with TestClient(create_app(search, engine=engine, embedder=query_embedding)) as client:
        assert client.get(source_ref + "?mode=history", headers=headers).status_code == 404
        assert client.get(image_ref + "?mode=history", headers=headers).status_code == 404


def test_office_chart_rendered_for_recognition(tmp_path, monkeypatch):
    if not shutil.which("soffice"):
        pytest.skip("LibreOfficeが未導入")
    source = tmp_path / "chart.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.append(["売上"])
    sheet.append([100])
    sheet.append([150])
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=1, min_row=1, max_row=3), titles_from_data=True)
    sheet.add_chart(chart, "C4")
    book.save(source)
    result = parse_file(source, "xlsx")
    assert "xlsx_shapes_or_charts_unprocessed" in result.quality_flags
    assert not result.assets

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def recognize(self, **kwargs):
            assert kwargs["input"][0]["content"][0]["image_url"].startswith("data:image/png;base64,")
            return SimpleNamespace(status="completed", output_text=json.dumps({
                "visible_text": "売上", "description": "棒グラフが右に配置されています。", "unreadable": "",
            }))

        @property
        def responses(self):
            return SimpleNamespace(create=self.recognize)

    monkeypatch.setattr(image_analysis.openai, "OpenAI", lambda **_: Client())
    image_analysis.analyze_images(result, Path(source), "xlsx",
                                  ImageAnalysisSettings(enabled=True, api_key="test-key"))
    assert any(asset.rendering_details["renderer"] == "libreoffice_pdf" for asset in result.assets)
    assert any(block.artifact_type == "derived" and block.source_locations[0]["preview_page_number"] == 1
               for block in result.blocks)


def test_pdf_image_api_failure_keeps_original_and_retryable_state(tmp_path, monkeypatch):
    image = io.BytesIO()
    Image.new("RGB", (80, 60)).save(image, "PNG")
    location = [{"format": "pdf", "page_number": 2}]
    figure = Block("figure", "figure", None, location, quality_flags=["image_analysis_pending"])
    result = ExtractionResult(
        blocks=[figure], assets=[ExtractedAsset("figure", image.getvalue(), "image/png", location)],
        pending_images=1,
    )
    original = openai.OpenAI

    def fail(_):
        return httpx.Response(503, json={"error": {"message": "secret error text"}})

    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: original(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(fail))))
    image_analysis.analyze_images(result, tmp_path / "original.pdf", "pdf",
                                  ImageAnalysisSettings(enabled=True, api_key="test-key"))
    assert result.processing_status == "partial"
    assert result.pending_images == 1 and result.blocks == [figure]
    assert result.quality_flags == ["image_analysis_api_failed"]
    assert len(result.assets) == 1 and not result.relations
