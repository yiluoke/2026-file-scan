"""RapidOCR日本語モデルの固定・事前取得・オフライン検査。"""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path

from file_scan.config import PdfOcrSettings
from file_scan.models import ModelIntegrityError, docling_version, file_hash, verify_models

OCR_LOCK_PATH = Path(__file__).with_name("rapidocr-models.lock.json")
OCR_MANIFEST = "ocr-model-manifest.json"


class OcrModelIntegrityError(ModelIntegrityError):
    code = "rapidocr_model_integrity_failed"


def ocr_profile(options: PdfOcrSettings) -> dict:
    return {"engine": "rapidocr", "backend": "onnxruntime", "device": "cpu",
            "language": "iso:ja", "model_size": "small", "mode": options.mode,
            "scale": options.scale, "text_score": 0.5, "threads": 2}


def verify_ocr_models(root: Path, lock_path: Path = OCR_LOCK_PATH) -> str:
    """読込時の自動取得は行わない。モデル・辞書・依存版を一括検査する。"""
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        manifest = json.loads((root / OCR_MANIFEST).read_text(encoding="utf-8"))
        for key in ("docling", "rapidocr", "onnxruntime", "language", "backend", "model_size"):
            if manifest[key] != lock[key]:
                raise ValueError("OCR構成が固定版と異なります")
        versions = {"docling": docling_version(),
                    **{name: importlib.metadata.version(name) for name in ("rapidocr", "onnxruntime")}}
        if any(lock[name] != version for name, version in versions.items()):
            raise ValueError("OCR依存版が固定版と異なります")
        if (lock["language"], lock["backend"], lock["model_size"]) != (
                "iso:ja", "onnxruntime", "small"):
            raise ValueError("未対応のOCRモデルです")
        return verify_models(root, lock_path, manifest_name=OCR_MANIFEST)
    except (ModelIntegrityError, OSError, ValueError, KeyError, TypeError,
            importlib.metadata.PackageNotFoundError) as error:
        raise OcrModelIntegrityError(
            "日本語OCRモデルの配置・固定版を検証できません。"
            "python scripts/download_docling_models.py --config <設定ファイル>で準備してください"
        ) from error


def prepare_ocr_models(root: Path, *, update_lock: bool = False) -> None:
    """準備コマンドだけが通信する。通常はlockのURLとSHA-256を使用する。"""
    from urllib.request import urlopen

    if update_lock:
        # モデル更新時のみ固定Doclingのレジストリを参照し、取得した実体を再固定する。
        from docling.models.stages.ocr import rapid_ocr_model as rapid
        resolved = rapid._resolve_rapidocr("iso:ja", "onnxruntime")
        target = root / rapid.RapidOcrModel._model_repo_folder
        artifacts = rapid._rapidocr_artifacts(
            target, rapid._backend_to_engine_type("onnxruntime"),
            resolved.ppocr_version, resolved.rapidocr_code, model_size="small")
        records = [{"path": dest.relative_to(root).as_posix(), "url": url}
                   for artifact in artifacts.values() for dest, url in artifact.files.items()]
        lock = {"version": 1, "docling": docling_version(), "rapidocr": "3.9.1",
                "onnxruntime": "1.24.1", "language": "iso:ja", "backend": "onnxruntime",
                "model_size": "small", "models": [{"repo_id": "RapidAI/RapidOCR",
                "revision": "v3.9.1/PP-OCRv6-small", "files": records}]}
    else:
        lock = json.loads(OCR_LOCK_PATH.read_text(encoding="utf-8"))
    root.mkdir(parents=True, exist_ok=True)
    for model in lock["models"]:
        for record in model["files"]:
            relative = Path(record["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise OcrModelIntegrityError("OCRモデルの保存先が不正です")
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if any(part.is_symlink() for part in (destination, *destination.parents)):
                raise OcrModelIntegrityError("OCRモデルのシンボリックリンクは使用できません")
            if update_lock or not destination.is_file() or file_hash(destination) != record["sha256"]:
                print(f"Downloading Japanese OCR: {relative}", flush=True)
                temporary = destination.with_suffix(destination.suffix + ".part")
                try:
                    with urlopen(record["url"], timeout=120) as response, temporary.open("wb") as handle:
                        while block := response.read(1024 * 1024):
                            handle.write(block)
                    if not update_lock and (file_hash(temporary) != record["sha256"]
                                            or temporary.stat().st_size != record["bytes"]):
                        raise OcrModelIntegrityError("取得したOCRモデルが固定SHA-256と異なります")
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
            if update_lock:
                record.update(bytes=destination.stat().st_size, sha256=file_hash(destination))
    if update_lock:
        OCR_LOCK_PATH.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / OCR_MANIFEST).write_text(
        json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    verify_ocr_models(root)
