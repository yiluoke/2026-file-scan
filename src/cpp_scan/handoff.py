"""完成したMarkdownを専用収集先へ配置し、既存file-scanへ下書き登録する。"""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import select

from file_scan.config import load_config
from file_scan.db import open_database, tables
from file_scan.pipeline import ingest as ingest_documents
from file_scan.pipeline import PipelineError, require_schema

from .pipeline import load_analysis, load_generation, stage, state, save_state
from .store import (ScanError, bytes_hash, file_manifest, now, project_lock, read_bytes, read_json,
                    verify_files, version_path, write_bytes, write_json)


def _scope(profile):
    return {k: profile[k] for k in ("include_tests", "include_third_party", "include_files", "exclude_patterns")}


def _configuration(settings):
    if settings.file_scan_config is None:
        raise ScanError("file_scan_config_required")
    config = load_config(settings.file_scan_config)
    handoff = (settings.artifact_root / "handoff").resolve()
    current = handoff / "current"
    if (len(config.sources) != 1 or config.sources[0].root != current
            or set(config.include_extensions) != {".md"} or not config.sources[0].recursive
            or config.sources[0].exclude_directories or config.sources[0].exclude_filenames
            or config.sources[0].exclude_directory_names
            or config.knowledge_id == "default"):
        raise ScanError("dedicated_markdown_ingestion_config_required")
    # 保管・レポートを原本や受渡しの中へ作成しない。
    for output in (config.storage_dir, config.report_dir):
        output = output.resolve()
        if output.is_relative_to(settings.source_root):
            raise ScanError("ingestion_output_overlaps_source")
        if handoff.is_relative_to(output) or output.is_relative_to(handoff):
            raise ScanError("ingestion_output_overlaps_handoff")
    return config


def _recover(settings):
    journal = settings.artifact_root / "state" / "handoff-journal.json"
    base = settings.artifact_root / "handoff"
    current, backup, pending = base / "current", base / "previous", base / "pending"
    if not journal.exists():
        return
    record = read_json(journal)
    if record["phase"] == "preparing":
        if backup.exists():
            if current.exists():
                shutil.rmtree(current)
            backup.rename(current)
        elif record.get("had_current") is False and current.exists():
            shutil.rmtree(current)
        if pending.exists():
            shutil.rmtree(pending)
        if "previous_binding" in record:
            value = state(settings)
            if record["previous_binding"] is None:
                value.pop("handoff_binding", None)
            else:
                value["handoff_binding"] = record["previous_binding"]
            save_state(settings, value)
        journal.unlink()
    # DB処理開始後はファイルだけ旧版へ戻さない。次の再取込で状態を照合する。
    elif record["phase"] == "ingesting" and not current.exists():
        raise ScanError("interrupted_handoff_missing_current")


def ingest(settings, generation_id, *, retry_partial=False, accept_scope_change=False, engine=None):
    owned_engine = engine is None
    with project_lock(settings.artifact_root), stage(settings, "ingest") as record:
        config = _configuration(settings)
        manifest, snapshot = load_generation(settings, generation_id, with_snapshot=True)
        record["generation_id"] = generation_id
        _, analysis = load_analysis(settings, manifest["analysis_id"])
        if analysis["snapshot_id"] != snapshot["snapshot_id"]:
            raise ScanError("generation_snapshot_mismatch")
        if manifest["status"] == "partial" and not settings.allow_partial_ingest:
            raise ScanError("partial_generation_ingestion_disabled")
        record["knowledge_id"] = config.knowledge_id
        current = settings.artifact_root / "handoff" / "current"
        source_root = snapshot.get("source_root")
        if source_root is not None and source_root != str(settings.source_root.resolve()):
            raise ScanError("handoff_snapshot_source_root_mismatch")
        binding = {"source_id": config.sources[0].source_id, "knowledge_id": config.knowledge_id,
                   "root": str(current), "scope": _scope(snapshot["profile"]),
                   "source_root": source_root}
        if manifest["status"] == "partial" and accept_scope_change:
            raise ScanError("partial_generation_cannot_change_scope")
        if engine is None:
            engine = open_database(config.database_url)
        try:
            # 既存スキーマと保管先を配置前に確認。自動DB初期化・削除・公開は行わない。
            require_schema(engine)
            with engine.connect() as conn:
                if conn.dialect.name == "sqlite":
                    for _, _, filename in conn.exec_driver_sql("PRAGMA database_list"):
                        if filename and Path(filename).resolve().is_relative_to(current.parent.resolve()):
                            raise ScanError("ingestion_output_overlaps_handoff")
                stored_root = conn.execute(select(tables["ingestion_settings"].c.storage_root)).scalar()
                if stored_root and stored_root != str(config.storage_dir.resolve()):
                    raise ScanError("existing_database_storage_root_mismatch")
                docs, members = tables["documents"], tables["knowledge_documents"]
                rows = conn.execute(select(
                    docs.c.source_system, docs.c.source_uri, docs.c.source_key,
                    docs.c.source_status, members.c.included,
                ).join(members).where(members.c.knowledge_id == config.knowledge_id)).all()
                if any(row.source_system != binding["source_id"]
                       or not row.source_uri.startswith(current.as_uri() + "/") for row in rows):
                    raise ScanError("knowledge_contains_other_sources")
                other = conn.execute(select(members.c.knowledge_id).join(docs).where(
                    docs.c.source_system == binding["source_id"],
                    members.c.knowledge_id != config.knowledge_id)).first()
                if other:
                    raise ScanError("source_id_used_by_other_knowledge")
            # 復旧も受渡し内を削除するため、DBに結び付いた保管先の確認後に行う。
            _recover(settings)
            value = state(settings)
            previous = value.get("handoff_binding")
            scope_changed = False
            if previous:
                if any(binding[k] != previous[k] for k in ("source_id", "knowledge_id", "root")):
                    raise ScanError("handoff_identity_change_rejected")
                scope_changed = (binding["scope"] != previous["scope"]
                                 or source_root != previous.get("source_root"))
                if scope_changed:
                    record["scope_change"] = {"previous_source_root": previous.get("source_root"),
                                              "source_root": source_root}
            incoming = {item["path"]: item["sha256"] for item in manifest["document_files"]}
            retained = []
            if manifest["status"] == "partial" and current.exists():
                retained = [item for item in file_manifest(current) if item["path"] not in incoming]
                if any(not item["path"].endswith(".md") for item in retained):
                    raise ScanError("unexpected_handoff_file")
            hashes = incoming | {item["path"]: item["sha256"] for item in retained}
            removals = sorted(row.source_key for row in rows if row.included
                              and row.source_status != "deleted" and row.source_key not in hashes)
            record["planned_removals"] = removals
            if scope_changed and not accept_scope_change:
                raise ScanError("handoff_scope_change_requires_explicit_option")
            if removals and not accept_scope_change:
                raise ScanError("handoff_removals_require_explicit_option")
            base = current.parent
            base.mkdir(parents=True, exist_ok=True)
            pending, backup = base / "pending", base / "previous"
            journal = settings.artifact_root / "state" / "handoff-journal.json"
            # 前回DB処理が失敗した場合も、現配置を履歴に残してから新しい一式を準備する。
            if pending.exists():
                shutil.rmtree(pending)
            pending.mkdir()
            source = version_path(settings.artifact_root, "generations", generation_id) / "documents"
            for path, sha256 in hashes.items():
                data = read_bytes((source if path in incoming else current) / path)
                if bytes_hash(data) != sha256:
                    raise ScanError("handoff_markdown_hash_mismatch")
                write_bytes(pending / path, data)
            expected = file_manifest(pending)
            verify_files(pending, expected)
            if backup.exists():
                shutil.rmtree(backup)
            write_json(journal, {"phase": "preparing", "generation_id": generation_id,
                                 "had_current": current.exists(), "previous_binding": previous})
            if current.exists():
                current.rename(backup)
            pending.rename(current)
            # 次回の開始時点からIDと範囲を固定する。DB失敗でも別ナレッジへ転用させない。
            value["handoff_binding"] = binding
            save_state(settings, value)
            write_json(journal, {"phase": "ingesting", "generation_id": generation_id})
            report = ingest_documents(config, engine, retry_partial=retry_partial,
                                      verified_markdown=hashes, allow_removals=accept_scope_change)
            verify_files(current, expected)
            expected_paths = {i["path"] for i in expected}
            actual_paths = {i["relative_path"] for i in report["files"]}
            if actual_paths != expected_paths:
                raise ScanError("ingestion_document_set_mismatch")
            selections = []
            with engine.connect() as conn:
                selected = tables["selected_generations"]
                for item in report["files"]:
                    extraction = conn.execute(select(selected.c.generation_id).where(
                        selected.c.knowledge_id == config.knowledge_id,
                        selected.c.document_id == item["document_id"])).scalar()
                    selections.append({"path": item["relative_path"], "document_id": item["document_id"],
                                       "extraction_generation_id": extraction,
                                       "markdown_sha256": bytes_hash(read_bytes(current / item["relative_path"]))})
                    if item["processing_status"] == "succeeded" and not extraction:
                        raise ScanError("ingestion_selection_missing")
            result = {"generation_id": generation_id, "snapshot_id": manifest["snapshot_id"],
                      "status": report["status"], "analysis_status": manifest["status"],
                      "ingestion_run_id": report["run_id"], "knowledge_id": config.knowledge_id,
                      "summary": report["summary"], "retained_paths": [item["path"] for item in retained],
                      "documents": selections, "finished_at": now(),
                      "publication": "draft", "embedding": "not_requested"}
            write_json(settings.artifact_root / "state" / "handoffs" / (record["run_id"] + ".json"), result)
            value = state(settings)
            value["last_handoff"] = result
            if report["status"] == "succeeded":
                value["selected"]["ingested_generation"] = generation_id
                if backup.exists():
                    shutil.rmtree(backup)
                journal.unlink(missing_ok=True)
            save_state(settings, value)
            record.update(status=result["status"], summary=result["summary"])
            return result
        except PipelineError as exc:
            raise ScanError(str(exc)) from None
        finally:
            if owned_engine:
                engine.dispose()
