"""収集・構造解析・生成を版単位で管理する。未完成の版を選択しない。"""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from . import documents, qt
from .codewiki import CodeWiki
from .collector import collect
from .config import Settings, validate_settings
from .snapshot import create_snapshot, load_snapshot
from .store import (ScanError, bytes_hash, digest, file_manifest, now, project_lock, read_bytes,
                    read_json, verify_files, version_path, write_json)


def implementation_profile() -> dict:
    names = ("config.py", "collector.py", "snapshot.py", "text.py", "qt.py", "pipeline.py",
             "documents.py", "markdown.py", "codewiki.py", "codewiki_bridge.py")
    return {name: bytes_hash(Path(__file__).with_name(name).read_bytes()) for name in names}


def state(settings):
    path = settings.artifact_root / "state" / "state.json"
    return read_json(path) if path.exists() else {
        "project_id": settings.project_id, "selected": {}, "cache": {}, "latest": {}}


def save_state(settings, value):
    write_json(settings.artifact_root / "state" / "state.json", value)


@contextmanager
def stage(settings, name):
    """呼出側がプロジェクト排他を保持。失敗・中断を履歴と最新状態へ残す。"""
    value = state(settings)
    if value["project_id"] != settings.project_id:
        raise ScanError("artifact_project_mismatch")
    for previous in value["latest"].values():
        if previous["status"] == "running":
            previous.update(status="cancelled", error="previous_process_interrupted", finished_at=now())
            write_json(settings.artifact_root / "state" / "runs" / (previous["run_id"] + ".json"), previous)
    record = {"run_id": uuid4().hex, "stage": name, "status": "running", "started_at": now()}
    value["latest"][name] = record
    save_state(settings, value)
    try:
        yield record
    except BaseException as exc:
        record.update(status="cancelled" if isinstance(exc, KeyboardInterrupt) else "failed",
                      error=str(exc) if isinstance(exc, ScanError) else type(exc).__name__)
        raise
    finally:
        record["finished_at"] = now()
        current = state(settings)
        current["latest"][name] = record
        save_state(settings, current)
        write_json(settings.artifact_root / "state" / "runs" / (record["run_id"] + ".json"), record)


def _select(settings, name, identifier, key=None):
    value = state(settings)
    value["selected"][name] = identifier
    if key:
        value["cache"][key] = identifier
    save_state(settings, value)


def scan(settings: Settings):
    validate_settings(settings)
    with project_lock(settings.artifact_root), stage(settings, "scan") as record:
        result = collect(settings)
        report = {"project_id": settings.project_id, "run_id": record["run_id"],
                  "status": "partial" if result.errors else "succeeded",
                  "files": [{"path": i.relative_path, "kind": i.kind} for i in result.items],
                  "errors": result.errors, "exclusions": result.exclusions,
                  "detected_ui_modes": sorted({i.kind for i in result.items
                                               if i.kind in {"widgets", "qml"}})}
        path = settings.artifact_root / "state" / "scans" / (record["run_id"] + ".json")
        write_json(path, report)
        record.update(status=report["status"], report=str(path), files=len(result.items),
                      errors=len(result.errors))
        return {**record, "detected_ui_modes": report["detected_ui_modes"]}


def _working_copy(settings, snapshot, root):
    source = version_path(settings.artifact_root, "snapshots", snapshot["snapshot_id"]) / "utf8"
    root.mkdir(parents=True, exist_ok=True)
    # snapshot manifest検証後の正規ファイルだけをコピーする。リンクや余分なファイルを持ち込まない。
    for entry in snapshot["files"]:
        if entry["text_status"] == "ready":
            destination = root / entry["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            from .store import write_bytes
            write_bytes(destination, read_bytes(source / entry["path"]))


def normalize_components(snapshot, raw):
    files = {entry["path"]: entry for entry in snapshot["files"]}
    result, errors, ids = [], [], {}
    for node in raw.get("components", []):
        entry = files.get(node.get("relative_path"))
        first, last = node.get("start_line", 0), node.get("end_line", 0)
        if not entry or not 1 <= first <= last <= entry.get("line_count", 0):
            errors.append({"path": node.get("relative_path", "."), "code": "codewiki_invalid_location"})
            continue
        name = node.get("qualified_name") or node.get("display_name") or node["name"]
        identifier = digest([entry["path"], node["component_type"], name,
                             node.get("parameters"), node["id"]])
        ids[node["id"]] = identifier
        result.append({"id": identifier, "name": name, "kind": node["component_type"],
                       "codewiki_id": node["id"], "depends_on": node.get("depends_on", []),
                       "certainty": "source_fact", "review": "unreviewed",
                       "evidence": {"path": entry["path"], "sha256": entry["sha256"],
                                    "encoding": entry["encoding"], "start_line": first,
                                    "end_line": last}, "method": "codewiki-tree-sitter"})
    covered = {item["evidence"]["path"] for item in result}
    for entry in snapshot["files"]:
        if entry["kind"] in {"cpp", "javascript"} and entry["text_status"] == "ready" and entry["path"] not in covered:
            # 空ファイルは解析対象として有効だが、宣言・根拠行がない。
            if entry["line_count"]:
                errors.append({"path": entry["path"], "code": "codewiki_file_not_indexed"})
    structural_files = {item["evidence"]["path"] for item in result if item["kind"] != "file"}
    for diagnostic in raw.get("syntax_diagnostics", []):
        if diagnostic["path"] not in structural_files:
            errors.append({"path": diagnostic["path"], "code": "codewiki_syntax_unparsed"})
    relations = []
    for component in result:
        for target in component.pop("depends_on"):
            relations.append({"from": component["id"], "to": ids.get(target), "target": target,
                              "certainty": "candidate" if target in ids else "unresolved"})
    return result, relations, errors


def load_analysis(settings, identifier):
    folder = version_path(settings.artifact_root, "analyses", identifier)
    manifest = read_json(folder / "manifest.json")
    if manifest.get("project_id") != settings.project_id or manifest.get("analysis_id") != identifier:
        raise ScanError("analysis_identity_mismatch")
    verify_files(folder, manifest["artifacts"], exact=False)
    result = read_json(folder / "analysis.json")
    return manifest, result


def analyze(settings: Settings, *, snapshot_id=None, force=False, adapter=None):
    validate_settings(settings)
    adapter = adapter or CodeWiki(settings)
    with project_lock(settings.artifact_root), stage(settings, "analyze") as record:
        snapshot = load_snapshot(settings, snapshot_id) if snapshot_id else create_snapshot(settings)
        record["snapshot_id"] = snapshot["snapshot_id"]
        _select(settings, "snapshot", snapshot["snapshot_id"])
        profile = {"implementation": implementation_profile(), "codewiki": adapter.profile()}
        key = "analyze:" + digest([snapshot["snapshot_id"], profile])
        cached = state(settings)["cache"].get(key)
        if cached and not force:
            manifest, _ = load_analysis(settings, cached)
            _select(settings, "analysis", cached)
            record.update(status=manifest["status"], analysis_id=cached, reused=True)
            return {**manifest, "reused": True}
        identifier = "analysis-" + uuid4().hex
        folder = version_path(settings.artifact_root, "analyses", identifier)
        folder.mkdir(parents=True)
        record["analysis_id"] = identifier
        work = folder / "work"
        try:
            _working_copy(settings, snapshot, work)
            before = file_manifest(work)
            raw = adapter.analyze(work, folder / "codewiki")
            verify_files(work, before, exact=False)
            components, relations, errors = normalize_components(snapshot, raw)
            source_root = version_path(settings.artifact_root, "snapshots", snapshot["snapshot_id"])
            for component in components:
                e = component["evidence"]
                mapping = read_json(source_root / "maps" / (e["path"] + ".json"))["lines"]
                e.update(byte_start=mapping[e["start_line"] - 1]["source_start"],
                         byte_end=mapping[e["end_line"] - 1]["source_end"])
            supplemental = qt.extract(settings, snapshot)
            errors += documents.validate_evidence(snapshot, supplemental, components)
            if not any(e["kind"] == "cpp" for e in snapshot["files"]):
                errors.append({"path": ".", "code": "cpp_source_not_found"})
            result = {"project_id": settings.project_id, "snapshot_id": snapshot["snapshot_id"],
                      "analysis_id": identifier, "components": components, "relations": relations,
                      "syntax_diagnostics": raw.get("syntax_diagnostics", []),
                      "qt": supplemental, "errors": errors,
                      "status": "partial" if errors or supplemental["errors"] or snapshot["errors"]
                      else "succeeded"}
            write_json(folder / "analysis.json", result)
            write_json(folder / "codewiki-raw.json", raw)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        manifest = {"project_id": settings.project_id, "analysis_id": identifier,
                    "snapshot_id": snapshot["snapshot_id"], "status": result["status"],
                    "profile": profile, "created_at": now(), "artifacts": file_manifest(folder)}
        write_json(folder / "manifest.json", manifest)
        _select(settings, "analysis", identifier, key if result["status"] == "succeeded" else None)
        record.update(status=result["status"], reused=False)
        return {**manifest, "reused": False}


def load_generation(settings, identifier, *, with_snapshot=False):
    folder = version_path(settings.artifact_root, "generations", identifier)
    manifest = read_json(folder / "manifest.json")
    if manifest.get("project_id") != settings.project_id or manifest.get("generation_id") != identifier:
        raise ScanError("generation_identity_mismatch")
    snapshot = load_snapshot(settings, manifest["snapshot_id"])
    documents.validate_generation(folder, manifest, snapshot)
    return (manifest, snapshot) if with_snapshot else manifest


def generate(settings: Settings, analysis_id: str, *, force=False, adapter=None):
    validate_settings(settings)
    adapter = adapter or CodeWiki(settings)
    with project_lock(settings.artifact_root), stage(settings, "generate") as record:
        analysis_manifest, analysis = load_analysis(settings, analysis_id)
        snapshot = load_snapshot(settings, analysis["snapshot_id"])
        model = os.environ.get(settings.documentation.model_env, "")
        endpoint = os.environ.get(settings.documentation.base_url_env, "")
        # APIキーや接続URLそのものは永続化せず、生成条件の変更だけを検出する。
        profile = {"implementation": implementation_profile(), "codewiki": adapter.profile(),
                   "documentation": asdict(settings.documentation), "model": model,
                   "endpoint_hash": digest(endpoint), "limits": {
                       name: getattr(settings.codewiki, name) for name in
                       ("max_tokens", "request_limit", "max_depth")}}
        key = "generate:" + digest([analysis_id, profile])
        cached = state(settings)["cache"].get(key)
        if cached and not force:
            manifest = load_generation(settings, cached)
            _select(settings, "generation", cached)
            record.update(status=manifest["status"], generation_id=cached, reused=True)
            return {**manifest, "reused": True}
        identifier = "gen-" + uuid4().hex
        folder = version_path(settings.artifact_root, "generations", identifier)
        folder.mkdir(parents=True)
        record.update(generation_id=identifier, analysis_id=analysis_id)
        work = folder / "work"
        try:
            _working_copy(settings, snapshot, work)
            before = file_manifest(work)
            output = folder / "codewiki"
            usage = adapter.generate(work, output)
            verify_files(work, before, exact=False)
            result = documents.format_documents(settings, snapshot, analysis, output,
                                                 folder / "documents")
            errors = documents.validate_evidence(snapshot, analysis["qt"], analysis["components"])
            errors += documents.validate_markdown(folder / "documents", snapshot)
            quality = {"status": "failed" if errors else analysis["status"], "errors": errors,
                       "source_files": len(snapshot["files"]), "components": len(analysis["components"]),
                       "qt_records": len(analysis["qt"]["records"]),
                       "qt_coverage": analysis["qt"]["coverage"], "source_reference_validation": "markdown_locations",
                       "cpp_syntax_diagnostics": analysis.get("syntax_diagnostics", []),
                       "semantic_claim_validation": "unreviewed", "mermaid_validation": "disabled"}
            write_json(folder / "quality-report.json", quality)
            write_json(folder / "usage.json", usage)
            write_json(folder / "analysis.json", analysis)
            from .store import write_bytes
            write_bytes(folder / "review.md", ("# 生成仕様書の確認\n\n"
                        f"解析状態: {analysis_manifest['status']}。未レビュー。\n\n"
                        "documents/overview.mdとdocuments/unknowns.mdを確認してください。\n").encode())
            manifest = {"project_id": settings.project_id, "generation_id": identifier,
                        "analysis_id": analysis_id, "snapshot_id": snapshot["snapshot_id"],
                        "status": quality["status"], "created_at": now(), "profile": profile,
                        "scope_profile": snapshot["profile"], **result}
            write_json(folder / "manifest.json", manifest)
            if errors:
                raise ScanError("generated_documents_invalid")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        _select(settings, "generation", identifier, key if manifest["status"] == "succeeded" else None)
        record.update(status=manifest["status"], reused=False)
        return {**manifest, "reused": False}


def status(settings):
    validate_settings(settings)
    # DBへ接続せず、最後の各段階・失敗・選択世代・受渡しを返す。
    value = state(settings)
    if value["project_id"] != settings.project_id:
        raise ScanError("artifact_project_mismatch")
    return value
