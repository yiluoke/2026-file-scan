"""明示的にダウンロード済みの公開原本を使う受入テスト。ネットへは接続しない。"""

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from file_scan.collector import collect
from file_scan.config import Settings, Source
from file_scan.db import tables
from file_scan.parsers import parse_file
from file_scan.pipeline import ingest

ROOT = Path(__file__).resolve().parents[1]
SHARE = ROOT / "data/file-server/department"
SAMPLES = json.loads((ROOT / "examples/public-samples.json").read_text(encoding="utf-8"))
MODELS = ROOT / "data/models/docling"


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda sample: sample["path"])
def test_downloaded_public_document(sample):
    path = SHARE / sample["path"]
    if not path.exists():
        pytest.skip("先にpython scripts/download_samples.pyを実行")
    if path.suffix == ".pdf" and not (MODELS / "model-manifest.json").is_file():
        pytest.skip("先にpython scripts/download_docling_models.pyを実行")
    if sample.get("sha256"):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sample["sha256"]
    result = parse_file(path, path.suffix)
    assert result.processing_status in {"succeeded", "partial"}
    assert any(block.content_text and block.content_text.strip() for block in result.blocks)
    assert all(block.source_locations for block in result.blocks)
    if path.suffix == ".pdf":
        assert result.metrics["pdf_backend"] == "docling"
        assert result.processing_status == "succeeded"
        assert any("bbox" in location for block in result.blocks for location in block.source_locations)
        if path.name == "table.pdf":
            assert result.metrics["tables"] == 1


def test_file_server_ingest_and_replay(postgres_database, tmp_path):
    if not all((SHARE / sample["path"]).exists() for sample in SAMPLES):
        pytest.skip("公開サンプル未取得")
    if not (MODELS / "model-manifest.json").is_file():
        pytest.skip("Doclingモデル未取得")
    settings = Settings(sources=[Source("mock-file-server", SHARE,
                                       exclude_directories=("旧版/",), exclude_filenames=("~$*",))],
                        storage_dir=tmp_path / "storage", report_dir=tmp_path / "reports",
                        include_extensions=(".txt", ".docx", ".xlsx", ".pdf"))
    scan = collect(settings)
    assert len(scan.items) == 8 and not scan.errors
    # Finder等が作る隠しメタデータも除外されるため、件数や理由集合を固定しない。
    assert {"excluded_directory", "excluded_filename"}.issubset(
        {entry["reason"] for entry in scan.exclusions})
    assert {item.relative_path for item in scan.items} == {sample["path"] for sample in SAMPLES}
    hashes = {item.path: hashlib.sha256(item.path.read_bytes()).hexdigest() for item in scan.items}
    first = ingest(settings, postgres_database)
    assert first["summary"]["registered"] == 8
    assert first["summary"]["failed"] == 0
    assert first["summary"]["partial"] == 0
    assert first["parser_workers"]["started"] == 1
    assert first["parser_workers"]["reused"] == 7
    second = ingest(settings, postgres_database)
    assert second["summary"]["unchanged"] == 8
    assert second["parser_workers"]["started"] == 0
    assert all(entry["temporary_bytes_written"] == 0 for entry in second["files"])
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == value for path, value in hashes.items())
    with postgres_database.connect() as connection:
        assert connection.execute(select(func.count()).select_from(tables["documents"])).scalar_one() == 8
        assert connection.execute(select(func.count()).select_from(tables["chunks"])).scalar_one() >= 8
        assert all(uri.startswith(SHARE.as_uri()) for uri in connection.execute(
            select(tables["documents"].c.source_uri)).scalars())
