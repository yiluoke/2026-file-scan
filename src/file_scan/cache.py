"""検査済みの抽出結果を分割設定から独立して再利用する。"""

from dataclasses import asdict

from sqlalchemy import select

from file_scan.db import tables
from file_scan.domain import Block, ExtractedAsset, ExtractionResult, Relation
from file_scan.profiles import digest
from file_scan.storage import Storage


def load_extraction(engine, storage: Storage, cache_key: str) -> ExtractionResult | None:
    table = tables["extraction_cache"]
    with engine.connect() as connection:
        row = connection.execute(select(table).where(table.c.cache_key == cache_key)).mappings().first()
    if row is None:
        return None
    payload = row["payload"]
    if digest(payload) != row["payload_hash"]:
        raise ValueError("extraction_cache_integrity_failed")
    assets = []
    for data in payload["assets"]:
        stored = data["stored"]
        try:
            asset = storage.verify(stored["asset_id"], stored["storage_key"], stored["byte_size"])
        except Exception as error:
            if getattr(error, "code", None) == "stored_asset_missing":
                return None
            raise
        assets.append(ExtractedAsset(content=asset.path.read_bytes(), **data["metadata"]))
    return ExtractionResult(blocks=[Block(**block) for block in payload["blocks"]], assets=assets,
                            relations=[Relation(**relation) for relation in payload["relations"]],
                            quality_flags=payload["quality_flags"], pending_images=payload["pending_images"],
                            metrics=payload["metrics"])


def save_extraction(connection, cache_key, source_version, extraction_revision, result, stored):
    payload = {"blocks": [asdict(block) for block in result.blocks],
               "relations": [asdict(relation) for relation in result.relations],
               "quality_flags": result.quality_flags, "pending_images": result.pending_images,
               "metrics": result.metrics, "assets": []}
    for asset, reference in zip(result.assets, stored, strict=True):
        data = asdict(asset)
        data.pop("content")
        payload["assets"].append({"metadata": data, "stored": {
            "asset_id": reference.asset_id, "storage_key": reference.storage_key,
            "byte_size": reference.byte_size}})
    table = tables["extraction_cache"]
    # 同じ入力の部分抽出を明示的に再試行した場合は、検査・登録成功と同時に更新する。
    connection.execute(table.delete().where(table.c.cache_key == cache_key))
    connection.execute(table.insert().values(cache_key=cache_key, source_version=source_version,
                       extraction_revision=extraction_revision, payload=payload, payload_hash=digest(payload)))
