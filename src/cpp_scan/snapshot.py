"""安定コピーと二度目の内容照合で、解析対象の固定版を作成する。"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from file_scan.collector import ScanItem
from file_scan.storage import Storage, StorageError

from .collector import SourceFile, collect
from .config import Settings
from .store import (ScanError, bytes_hash, digest, file_manifest, now, read_bytes,
                    read_json, verify_files, version_path, write_bytes, write_json)
from .text import decode


def _item(settings: Settings, item: SourceFile) -> ScanItem:
    return ScanItem(settings.project_id, item.relative_path, item.path, item.path.as_uri(),
                    digest([settings.project_id, item.relative_path]), item.kind)


def create_snapshot(settings: Settings) -> dict:
    inventory = collect(settings)
    storage = Storage(settings.artifact_root / "objects")
    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=settings.artifact_root) as temporary:
        staging = Path(temporary)
        entries, errors = [], list(inventory.errors)
        for item in inventory.items:
            try:
                asset = storage.snapshot(_item(settings, item), settings.max_file_bytes,
                                         settings.copy_retries)
                data = read_bytes(asset.path, settings.max_file_bytes)
                entry = {"path": item.relative_path, "kind": item.kind,
                         "sha256": bytes_hash(data), "size": len(data), "text_status": "ready"}
                write_bytes(staging / "original" / item.relative_path, data)
                try:
                    text, mapping = decode(data, item.relative_path, settings)
                    write_bytes(staging / "utf8" / item.relative_path, text.encode("utf-8"))
                    write_json(staging / "maps" / (item.relative_path + ".json"), mapping)
                    entry.update({k: v for k, v in mapping.items() if k != "lines"})
                except ScanError as exc:
                    entry.update(text_status="failed", encoding=None)
                    errors.append({"path": item.relative_path, "code": str(exc)})
                entries.append(entry)
            except (StorageError, OSError) as exc:
                errors.append({"path": item.relative_path,
                               "code": getattr(exc, "code", "snapshot_copy_failed")})
        second = collect(settings)
        if [i.relative_path for i in inventory.items] != [i.relative_path for i in second.items]:
            errors.append({"path": ".", "code": "source_file_set_changed"})
        errors.extend(second.errors)
        by_path = {i.relative_path: i for i in second.items}
        for entry in entries:
            item = by_path.get(entry["path"])
            if item is None:
                continue
            try:
                asset = storage.snapshot(_item(settings, item), settings.max_file_bytes,
                                         settings.copy_retries)
                if asset.asset_id != "sha256:" + entry["sha256"]:
                    errors.append({"path": entry["path"], "code": "source_content_changed"})
            except (StorageError, OSError) as exc:
                errors.append({"path": entry["path"],
                               "code": getattr(exc, "code", "source_recheck_failed")})
        profile = settings.scan_profile()
        implementation = {name: bytes_hash(Path(__file__).with_name(name).read_bytes())
                          for name in ("snapshot.py", "collector.py", "text.py")}
        identity = {"profile": profile, "implementation": implementation, "files": entries, "errors": errors,
                    "exclusions": inventory.exclusions}
        identifier = "snap-" + digest(identity)
        destination = version_path(settings.artifact_root, "snapshots", identifier)
        if destination.exists():
            manifest = load_snapshot(settings, identifier)
            return {**manifest, "reused": True}
        manifest = {"schema_version": "cpp-snapshot-v1", "snapshot_id": identifier,
                    "project_id": settings.project_id, "created_at": now(), "profile": profile,
                    "implementation": implementation,
                    "status": "partial" if errors else "succeeded", "files": entries,
                    "errors": errors, "exclusions": inventory.exclusions,
                    "consistency": "two_pass_content_check_not_atomic_across_files",
                    "artifacts": file_manifest(staging)}
        write_json(staging / "manifest.json", manifest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(destination))
        # TemporaryDirectoryの終了処理では移動済みの保存先を削除しない。
        return {**manifest, "reused": False}


def load_snapshot(settings: Settings, identifier: str) -> dict:
    root = version_path(settings.artifact_root, "snapshots", identifier)
    manifest = read_json(root / "manifest.json")
    if manifest.get("project_id") != settings.project_id or manifest.get("snapshot_id") != identifier:
        raise ScanError("snapshot_identity_mismatch")
    verify_files(root, manifest["artifacts"], exact=False)
    return manifest
