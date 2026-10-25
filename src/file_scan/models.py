"""Doclingモデルの固定版と配置実体を検査する。ネットワークには接続しない。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from functools import lru_cache
from pathlib import Path

LOCK_PATH = Path(__file__).with_name("docling-models.lock.json")


def docling_version() -> str:
    """公式slim配布でもモデルの取得・検査に同じDocling版を記録する。"""
    for name in ("docling-slim", "docling"):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    raise importlib.metadata.PackageNotFoundError("docling-slim")


class ModelIntegrityError(RuntimeError):
    code = "docling_model_integrity_failed"


def file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


@lru_cache(maxsize=4)
def _verify(root: str, manifest_text: str, lock_text: str, signatures: tuple) -> str:
    manifest, lock = json.loads(manifest_text), json.loads(lock_text)
    expected = {model["repo_id"]: model["revision"] for model in lock["models"]}
    actual = {model["repo_id"]: model["revision"] for model in manifest["models"]}
    if actual != expected or manifest.get("docling") != lock.get("docling"):
        raise ModelIntegrityError("モデルの固定版と配置版が一致しません")
    expected_files = {m["repo_id"]: m["files"] for m in lock["models"]}
    for model in manifest["models"]:
        if not model["files"] or model["files"] != expected_files[model["repo_id"]]:
            raise ModelIntegrityError("モデルのファイル一覧が空です")
        for record in model["files"]:
            if file_hash(Path(root) / record["path"]) != record["sha256"]:
                raise ModelIntegrityError("モデルファイルのハッシュが一致しません")
    return hashlib.sha256((manifest_text + lock_text).encode()).hexdigest()


def verify_models(root: Path, lock_path: Path = LOCK_PATH) -> str:
    """変更のない配置はプロセス内で再検査を省略。サイズ・mtime・ctimeも鍵にする。"""
    try:
        root = root.resolve()
        manifest_text = (root / "model-manifest.json").read_text(encoding="utf-8")
        lock_text = lock_path.read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
        signatures = []
        paths = set()
        for model in manifest["models"]:
            for record in model["files"]:
                relative = Path(record["path"])
                if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                    raise ModelIntegrityError("モデルの保存先参照が不正です")
                path = root / relative
                if any(part.is_symlink() for part in (path, *path.parents) if part != root.parent):
                    raise ModelIntegrityError("モデルのシンボリックリンクは使用できません")
                info = path.stat()
                if not path.is_file() or info.st_size != record["bytes"] or str(relative) in paths:
                    raise ModelIntegrityError("モデルのファイル構成が一致しません")
                paths.add(str(relative))
                signatures.append((str(relative), info.st_dev, info.st_ino, info.st_size,
                                   info.st_mtime_ns, info.st_ctime_ns))
        if not paths:
            raise ModelIntegrityError("モデルのファイル一覧が空です")
        return _verify(str(root), manifest_text, lock_text, tuple(signatures))
    except ModelIntegrityError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise ModelIntegrityError("モデルの配置情報を検証できません") from None
