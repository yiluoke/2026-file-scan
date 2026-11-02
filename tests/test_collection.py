"""収集範囲、障害時の保留、安定コピーと保管物の改竄検知を確認する。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from file_scan.collector import collect
from file_scan.config import ConfigError, Settings, Source, load_config, validate_settings
from file_scan.storage import (
    AssetIntegrityError, FileTooLargeError, SourceChangedError, Storage, StoredAssetMissingError,
    UnsafePathError,
)


def configured(tmp_path: Path, **kwargs) -> Settings:
    root = tmp_path / "source"
    root.mkdir(exist_ok=True)
    return Settings(
        sources=[Source("design", root)], storage_dir=tmp_path / "storage",
        report_dir=tmp_path / "reports", **kwargs,
    )


def write_config(tmp_path: Path, **kwargs) -> Path:
    values = {"sources": [{"source_id": "design", "root": "source"}], **kwargs}
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(values, allow_unicode=True), encoding="utf-8")
    return path


def test_config_resolves_paths_relative_to_configuration_and_applies_defaults(tmp_path):
    settings = load_config(write_config(tmp_path))
    assert settings.sources[0].root == tmp_path / "source"
    assert settings.storage_dir == tmp_path / "var/storage"
    assert settings.report_dir == tmp_path / "var/reports"
    assert settings.database_url == "postgresql+psycopg://file_scan@127.0.0.1:5432/file_scan"
    assert settings.max_file_bytes == 100 * 1024 * 1024
    assert settings.copy_retries == 2
    assert settings.pdf_backend == "docling"
    assert settings.pdf_ocr.enabled is True and settings.pdf_ocr.mode == "auto"
    assert settings.pdf_ocr.timeout_seconds == 1800 and settings.pdf_ocr.scale == 3.0
    assert settings.docling_artifacts_path == tmp_path / "data/models/docling"
    assert settings.knowledge_id == "default"
    assert set(settings.include_extensions) == {".docx", ".xlsx", ".xlsm", ".pdf", ".txt", ".md"}


@pytest.mark.parametrize("extension", [".py", ".sql", ".json", ".yaml", ".yml", ".csv", ".log"])
def test_program_and_data_formats_cannot_be_enabled_in_configuration(tmp_path, extension):
    with pytest.raises(ConfigError, match="文書形式"):
        load_config(write_config(tmp_path, include_extensions=[".txt", extension]))
    with pytest.raises(ConfigError, match="文書形式"):
        validate_settings(configured(tmp_path, include_extensions=(".txt", extension.upper())))


def test_collection_only_includes_documents_and_reports_other_formats_as_excluded(tmp_path):
    settings = configured(tmp_path)
    root = settings.sources[0].root
    removed = ["script.py", "query.sql", "data.json", "settings.yaml", "settings.yml", "table.csv", "server.log"]
    documents = ["manual.TXT", "notes.md", "design.docx", "table.xlsx", "macro.xlsm", "manual.pdf"]
    for filename in documents + removed:
        (root / filename).write_text("sample", encoding="utf-8")
    result = collect(settings)
    assert {item.relative_path for item in result.items} == set(documents)
    assert {entry["relative_path"] for entry in result.exclusions} == set(removed)
    assert all(entry["reason"] == "unsupported_extension" for entry in result.exclusions)


def test_markdown_long_extension_remains_an_optional_document_format(tmp_path):
    settings = configured(tmp_path, include_extensions=(".markdown",))
    (settings.sources[0].root / "manual.markdown").write_text("# 操作\n手順", encoding="utf-8")
    assert [item.format for item in collect(settings).items] == ["markdown"]


def test_database_environment_override_and_secrets_not_in_repr(tmp_path, monkeypatch):
    monkeypatch.setenv("CUSTOM_DB", "sqlite:///:memory:")
    monkeypatch.setenv("FILE_SCAN_DATABASE_URL", "postgresql://user:very-secret@db/example")
    settings = load_config(write_config(tmp_path, database_url_env="CUSTOM_DB"))
    assert settings.database_url == "postgresql://user:very-secret@db/example"
    assert "very-secret" not in repr(settings)


@pytest.mark.parametrize("sources", [
    [{"source_id": "a", "root": "source"}, {"source_id": "b", "root": "source"}],
    [{"source_id": "a", "root": "source"}, {"source_id": "b", "root": "source/child"}],
    [{"source_id": "a", "root": "source"}, {"source_id": "a", "root": "another"}],
])
def test_duplicate_and_parent_child_roots_rejected(tmp_path, sources):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, sources=sources))


@pytest.mark.parametrize("output", ["storage_dir", "report_dir"])
def test_output_inside_collection_root_is_rejected(tmp_path, output):
    with pytest.raises(ConfigError, match="収集ルートの外"):
        load_config(write_config(tmp_path, **{output: "source/generated"}))


def test_invalid_yaml_error_does_not_repeat_credentials(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("database_url: [postgresql://secret-user:secret-password@db\n", encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "secret-password" not in str(error.value)


def test_config_chunk_limit_matches_chunking_minimum(tmp_path):
    with pytest.raises(ConfigError, match="100以上"):
        load_config(write_config(tmp_path, chunk_max_characters=99))
    assert load_config(write_config(tmp_path, chunk_max_characters=100)).chunk_max_characters == 100


def test_exclusions_are_case_insensitive_and_directory_prefix_has_boundaries(tmp_path):
    settings = configured(tmp_path)
    root = settings.sources[0].root
    for folder in ("old/nested", "older", "active"):
        (root / folder).mkdir(parents=True)
    for filename in ("old/nested/a.txt", "older/keep.TXT", "active/KEEP.txt", "active/~$WORK.TXT", "active/file.TMP", ".hidden.txt"):
        (root / filename).write_text("text", encoding="utf-8")
    settings.sources = [replace(settings.sources[0], exclude_directories=("OLD",), exclude_filenames=("~$*", "*.tmp"))]
    result = collect(settings)
    assert [item.relative_path for item in result.items] == ["active/KEEP.txt", "older/keep.TXT"]
    assert result.successful_sources == ["design"]
    assert {entry["reason"] for entry in result.exclusions} >= {"excluded_directory", "excluded_filename", "hidden"}


def test_non_recursive_and_hidden_settings(tmp_path):
    settings = configured(tmp_path, include_hidden=True)
    root = settings.sources[0].root
    (root / ".hidden.txt").write_text("visible by setting", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested/child.txt").write_text("not visited", encoding="utf-8")
    settings.sources = [replace(settings.sources[0], recursive=False)]
    assert [item.source_key for item in collect(settings).items] == [".hidden.txt"]


def test_missing_root_is_not_a_successful_source_or_a_deletion_signal(tmp_path):
    settings = configured(tmp_path)
    (settings.sources[0].root / "valid.txt").write_text("valid", encoding="utf-8")
    settings.sources.append(Source("unavailable", tmp_path / "not-mounted"))
    result = collect(settings)
    assert result.successful_sources == ["design"]
    assert {error["source_id"] for error in result.errors} == {"unavailable"}
    assert len(result.items) == 1


def test_enumeration_failure_is_not_success_and_does_not_leak_exception(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    def fail_scandir(*args, **kwargs):
        raise PermissionError("sensitive diagnostic secret-password")
    monkeypatch.setattr("file_scan.collector.os.scandir", fail_scandir)
    result = collect(settings)
    assert not result.successful_sources
    assert result.errors
    assert "secret-password" not in str(result.errors)


def test_symlink_files_and_directories_are_not_followed(tmp_path):
    settings = configured(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not for collection", encoding="utf-8")
    root = settings.sources[0].root
    (root / "directory-link").symlink_to(outside, target_is_directory=True)
    (root / "file-link.txt").symlink_to(outside / "secret.txt")
    result = collect(settings)
    assert result.items == []
    assert len(result.exclusions) == 2
    assert all(entry["reason"] == "symbolic_link" for entry in result.exclusions)


def test_directory_replaced_after_initial_check_is_not_followed(tmp_path, monkeypatch):
    import file_scan.collector as collector
    settings = configured(tmp_path)
    child = settings.sources[0].root / "child"
    child.mkdir()
    (child / "allowed.txt").write_text("allowed", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    original = collector._directory_is_safe
    def replace_after_check(directory, root):
        safe = original(directory, root)
        if directory == child:
            child.rename(tmp_path / "previous-child")
            child.symlink_to(outside, target_is_directory=True)
        return safe
    monkeypatch.setattr(collector, "_directory_is_safe", replace_after_check)
    result = collect(settings)
    assert not result.items
    assert result.errors
    assert not result.successful_sources


def test_same_bytes_have_different_document_ids_and_shared_stored_asset(tmp_path):
    settings = configured(tmp_path)
    root = settings.sources[0].root
    (root / "a.txt").write_bytes(b"same bytes")
    (root / "b.txt").write_bytes(b"same bytes")
    first, second = collect(settings).items
    assert first.document_id != second.document_id
    assert first.document_id == collect(settings).items[0].document_id
    storage = Storage(settings.storage_dir)
    a = storage.snapshot(first, settings.max_file_bytes, settings.copy_retries)
    b = storage.snapshot(second, settings.max_file_bytes, settings.copy_retries)
    assert a.asset_id == b.asset_id == "sha256:" + hashlib.sha256(b"same bytes").hexdigest()
    assert a.path == b.path
    assert a.asset_id != first.document_id
    assert a.source_modified_at.tzinfo is not None


def test_changed_source_is_retried_and_only_stable_bytes_are_stored(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    path = settings.sources[0].root / "changing.txt"
    path.write_bytes(b"old")
    item = collect(settings).items[0]
    storage = Storage(settings.storage_dir)
    original = storage._copy_stream
    calls = []
    def mutate_once(source, target, limit):
        result = original(source, target, limit)
        calls.append(1)
        if len(calls) == 1:
            path.write_bytes(b"new and stable")
        return result
    monkeypatch.setattr(storage, "_copy_stream", mutate_once)
    asset = storage.snapshot(item, settings.max_file_bytes, retries=2)
    assert len(calls) == 2
    assert asset.path.read_bytes() == b"new and stable"
    assert list((settings.storage_dir / ".tmp").iterdir()) == []


def test_continuously_changing_source_is_deferred_without_partial_objects(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    path = settings.sources[0].root / "changing.txt"
    path.write_bytes(b"old")
    storage = Storage(settings.storage_dir)
    original = storage._copy_stream
    calls = []
    def mutate(source, target, limit):
        result = original(source, target, limit)
        calls.append(1)
        path.write_bytes(path.read_bytes() + b"x")
        return result
    monkeypatch.setattr(storage, "_copy_stream", mutate)
    with pytest.raises(SourceChangedError):
        storage.snapshot(collect(settings).items[0], settings.max_file_bytes, retries=2)
    assert len(calls) == 3
    assert not (settings.storage_dir / "objects").exists()
    assert list((settings.storage_dir / ".tmp").iterdir()) == []


def test_source_changed_to_symlink_after_enumeration_is_rejected(tmp_path):
    settings = configured(tmp_path)
    path = settings.sources[0].root / "manual.txt"
    path.write_bytes(b"original")
    item = collect(settings).items[0]
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(UnsafePathError):
        Storage(settings.storage_dir).snapshot(item, settings.max_file_bytes, retries=2)


def test_size_limit_rejects_snapshot(tmp_path):
    settings = configured(tmp_path)
    (settings.sources[0].root / "large.txt").write_bytes(b"too large")
    with pytest.raises(FileTooLargeError):
        Storage(settings.storage_dir).snapshot(collect(settings).items[0], max_file_bytes=3, retries=2)


def test_cached_object_is_verified_by_hash_not_only_size(tmp_path):
    storage = Storage(tmp_path / "storage")
    asset = storage.put_bytes(b"original", "text/plain")
    asset.path.write_bytes(b"tampered")  # 同じバイト数でも改竄を検出する。
    with pytest.raises(AssetIntegrityError):
        storage.put_bytes(b"original", "text/plain")


def test_cached_object_replaced_with_symlink_is_rejected(tmp_path):
    storage = Storage(tmp_path / "storage")
    asset = storage.put_bytes(b"original", "text/plain")
    outside = tmp_path / "other.txt"
    outside.write_bytes(b"original")
    asset.path.unlink()
    asset.path.symlink_to(outside)
    with pytest.raises(UnsafePathError):
        storage.put_bytes(b"original", "text/plain")


def test_verify_checks_existing_object_without_creating_files(tmp_path):
    storage = Storage(tmp_path / "storage")
    asset = storage.put_bytes(b"original", "text/plain")
    before = set(storage.root.rglob("*"))
    verified = storage.verify(asset.asset_id, asset.storage_key, asset.byte_size)
    assert verified.asset_id == asset.asset_id
    assert verified.path == asset.path
    assert verified.byte_size == asset.byte_size
    assert set(storage.root.rglob("*")) == before


def test_verify_missing_object_has_distinct_error_and_does_not_restore_it(tmp_path):
    storage = Storage(tmp_path / "storage")
    asset = storage.put_bytes(b"original", "text/plain")
    asset.path.unlink()
    with pytest.raises(StoredAssetMissingError) as error:
        storage.verify(asset.asset_id, asset.storage_key, asset.byte_size)
    assert error.value.code == "stored_asset_missing"
    assert not asset.path.exists()


def test_verify_rejects_same_size_corruption_and_symlinks(tmp_path):
    storage = Storage(tmp_path / "storage")
    asset = storage.put_bytes(b"original", "text/plain")
    asset.path.write_bytes(b"tampered")
    with pytest.raises(AssetIntegrityError):
        storage.verify(asset.asset_id, asset.storage_key, asset.byte_size)
    asset.path.unlink()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"original")
    asset.path.symlink_to(outside)
    with pytest.raises(UnsafePathError):
        storage.verify(asset.asset_id, asset.storage_key, asset.byte_size)


def test_verify_rejects_size_mismatch_and_path_traversal(tmp_path):
    storage = Storage(tmp_path / "storage")
    asset = storage.put_bytes(b"original", "text/plain")
    with pytest.raises(AssetIntegrityError):
        storage.verify(asset.asset_id, asset.storage_key, asset.byte_size + 1)
    with pytest.raises(AssetIntegrityError):
        storage.verify(asset.asset_id, "../../outside.txt", asset.byte_size)
