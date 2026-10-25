"""抽出と分割の更新範囲を分け、形式に必要な依存・モデルだけを識別する。"""

import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path

from file_scan.config import Settings
from file_scan.models import verify_models


def digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def extraction_profile(settings: Settings, file_format: str) -> tuple[str, dict]:
    dependencies = {
        "docx": ("python-docx", "lxml", "Pillow"),
        "xlsx": ("openpyxl", "Pillow"), "xlsm": ("openpyxl", "Pillow"),
        "pdf": ("pypdf", "Pillow"),
    }.get(file_format, ("charset-normalizer",))
    profile = {"schema_version": "extraction-v1", "format": file_format,
               "python": list(sys.version_info[:3]), "dependencies": {},
               "implementation": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                  for name in ("parsers.py", "domain.py")}}
    if file_format in {"docx", "xlsx", "xlsm"}:
        profile["include_hidden"] = settings.include_hidden
    if file_format == "pdf":
        profile["pdf_backend"] = settings.pdf_backend
        if settings.pdf_backend == "docling":
            dependencies += (
                "docling", "docling-slim", "docling-core", "docling-parse", "docling-ibm-models",
                "torch", "torchvision", "transformers", "huggingface-hub",
                "opencv-python-headless", "numpy", "scipy",
            )
            profile["models"] = verify_models(settings.docling_artifacts_path)
            profile["options"] = {"device": "cpu", "ocr": False, "table_structure": True,
                                  "picture_images": True, "remote_services": False, "threads": 2}
    for dependency in dependencies:
        try:
            profile["dependencies"][dependency] = importlib.metadata.version(dependency)
        except importlib.metadata.PackageNotFoundError:
            profile["dependencies"][dependency] = None
    return digest(profile), profile


def conversion_profile(settings: Settings, file_format: str = "txt") -> tuple[str, dict]:
    extraction_revision, extraction = extraction_profile(settings, file_format)
    chunking = {"version": "structure-tokens-v1", "max_characters": settings.chunk_max_characters,
                "max_tokens": settings.chunk_max_tokens,
                "context_max_tokens": settings.chunk_context_max_tokens,
                "encoding": "cl100k_base", "tiktoken": importlib.metadata.version("tiktoken"),
                "implementation": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                   for name in ("chunking.py", "tokens.py")}}
    profile = {"schema_version": "file-scan-v3", "extraction_revision": extraction_revision,
               "extraction": extraction, "chunking_revision": digest(chunking), "chunking": chunking}
    return digest(profile), profile
