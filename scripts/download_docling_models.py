"""DoclingのCPUレイアウト・表構造モデルを事前取得し、版とSHA-256を記録する。"""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path

from file_scan.config import load_config
from file_scan.models import LOCK_PATH, verify_models

ROOT = Path(__file__).resolve().parents[1]


def download(config: Path, *, update_lock: bool = False, verify_only: bool = False) -> None:
    settings = load_config(config)
    destination = settings.docling_artifacts_path.resolve()
    if verify_only:
        verify_models(destination)
        print("固定版・モデル実体の整合性を確認しました", flush=True)
        return
    os.environ.setdefault("HF_HOME", str(ROOT / "data/models/huggingface-cache"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from docling.datamodel.pipeline_options import LayoutObjectDetectionOptions
    from huggingface_hub import HfApi, snapshot_download

    lock = json.loads(LOCK_PATH.read_text())
    if update_lock:
        layout = LayoutObjectDetectionOptions().model_spec
        specs = [(layout.repo_id, layout.revision,
                  ["*.json", "*.safetensors", "*.bin", "README.md", "LICENSE*"]),
                 ("docling-project/docling-models", "v2.3.0",
                  ["model_artifacts/tableformer/accurate/*", "README.md", "LICENSE*"])]
        lock = {"version": 1, "docling": importlib.metadata.version("docling"), "models": [
            {"repo_id": repo, "revision": HfApi().model_info(repo, revision=revision).sha,
             "allow_patterns": patterns} for repo, revision, patterns in specs]}
    if lock["docling"] != importlib.metadata.version("docling"):
        raise RuntimeError("Doclingの版がモデルlockと異なります。依存版を確認してください")
    records = []
    for model in lock["models"]:
        repo, revision, patterns = model["repo_id"], model["revision"], model["allow_patterns"]
        print(f"Downloading {repo} @ {revision}", flush=True)
        directory = destination / repo.replace("/", "--")
        snapshot_download(repo_id=repo, revision=revision, local_dir=directory,
                          allow_patterns=patterns, max_workers=4)
        files = []
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(directory)
            if not path.is_file() or ".cache" in relative.parts:
                continue
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            files.append({"path": path.relative_to(destination).as_posix(),
                          "bytes": path.stat().st_size, "sha256": hasher.hexdigest()})
        if not files:
            raise RuntimeError("モデルファイルを取得できませんでした")
        if not update_lock and files != model["files"]:
            raise RuntimeError("取得したファイルとモデルlockが一致しません")
        model["files"] = files
        records.append({"repo_id": repo, "revision": revision, "files": files})
    manifest = {"docling": importlib.metadata.version("docling"), "models": records}
    destination.mkdir(parents=True, exist_ok=True)
    from file_scan.pipeline import write_json
    if update_lock:
        write_json(LOCK_PATH, lock)
    write_json(destination / "model-manifest.json", manifest)
    verify_models(destination)
    print(f"Model manifest: {destination / 'model-manifest.json'}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.local.yaml")
    parser.add_argument("--update-lock", action="store_true", help="明示的にモデル版とlockを更新")
    parser.add_argument("--verify-only", action="store_true", help="取得せず実体と固定版を検査")
    arguments = parser.parse_args()
    if arguments.update_lock and arguments.verify_only:
        parser.error("--update-lockと--verify-onlyは同時指定できません")
    download(arguments.config, update_lock=arguments.update_lock, verify_only=arguments.verify_only)
