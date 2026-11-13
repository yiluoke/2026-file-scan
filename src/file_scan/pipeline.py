"""収集・抽出・DB登録をつなぐ、第1版のバッチ処理。

特に守ること:
* 原本バイト列の保存とDBの本文登録を分離する。
* 時間のかかるコピー・文書解析中にはDBトランザクションを開かない。
* 1文書のブロック／チャンクは一つのトランザクションで登録する。
* 全てdraft。画像の外部送信は設定で有効にした対象だけに限る。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import and_, func, inspect, select, text
from sqlalchemy.engine import Connection, Engine

from file_scan.cache import load_extraction, save_extraction
from file_scan.chunking import build_chunks
from file_scan.collector import ScanItem, ScanResult, collect
from file_scan.config import Settings, validate_settings
from file_scan.db import SCHEMA_VERSION, tables
from file_scan.domain import Chunk, ExtractionResult
from file_scan.storage import Storage, StorageError, StoredAsset
from file_scan.profiles import conversion_profile
from file_scan.workers import ACTIVE_POOL, ParsePool


class PipelineError(RuntimeError):
    """接続情報や原本文面を含めず、CLIへ通知できるバッチエラー。"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def digest(value: object) -> str:
    """辞書の順序に左右されないID。秘密情報入りのSettings全体は渡さない。"""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _insert_missing(conn: Connection, table, identity: dict, values: dict) -> None:
    """不変な版・実体を再実行で上書きしない。同時実行はバッチロックで防ぐ。"""
    condition = and_(*(table.c[key] == value for key, value in identity.items()))
    if conn.execute(select(table).where(condition)).first() is None:
        conn.execute(table.insert().values(**identity, **values))


@contextmanager
def ingestion_lock(engine: Engine):
    """同じDBへ登録するv1バッチを一つに限定する。クラッシュ時はOS/DBが解放。

    PostgreSQLはセッションロックなので解析中に長時間の行ロックを持たない。
    SQLiteはローカル検証用。ネットワーク共有上のSQLiteは利用しない。
    """
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            key = 5064661230126350670
            acquired = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
            conn.commit()
            if not acquired:
                raise PipelineError("別の登録処理が実行中です")
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                conn.commit()
        return
    filename = engine.url.database
    if not filename or filename == ":memory:":
        raise PipelineError("ingestにはファイル型SQLiteまたはPostgreSQLが必要です")
    lock_path = Path(filename).with_suffix(Path(filename).suffix + ".ingest.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise PipelineError("別の登録処理が実行中です") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def require_schema(engine: Engine) -> None:
    """ingestが既存DBへ勝手にDDLを実行しないよう、初期化を別コマンドにする。"""
    with engine.connect() as conn:
        from file_scan.namespace import database_schema
        existing = set(inspect(conn).get_table_names(schema=database_schema(conn)))
        if "schema_migrations" not in existing:
            raise PipelineError("先に init-db --config 設定ファイル を実行してください")
        versions = list(conn.execute(select(tables["schema_migrations"].c.version)).scalars())
        if versions != [SCHEMA_VERSION]:
            raise PipelineError("未対応のDBスキーマ版です")
        if set(tables) - existing:
            raise PipelineError("移行記録とDBテーブルが一致しません")


def _start_job(conn: Connection, knowledge_id: str, stage: str, job_key: str,
               document_id: str | None = None, generation_id: str | None = None,
               timeout: int = 300) -> str:
    jobs = tables["index_jobs"]
    attempt = conn.execute(select(func.max(jobs.c.attempt_no)).where(
        jobs.c.job_key == job_key)).scalar() or 0
    run_id = str(uuid4())
    now = utcnow()
    conn.execute(jobs.insert().values(
        run_id=run_id, job_key=job_key, attempt_no=attempt + 1, stage=stage,
        knowledge_id=knowledge_id, document_id=document_id, generation_id=generation_id,
        status="running", started_at=now, lease_until=now + timedelta(seconds=timeout),
        lease_version=1, target_parameters={"generation_id": generation_id},
    ))
    return run_id


def _finish_job(conn: Connection, run_id: str, status: str, error_code: str | None = None) -> None:
    jobs = tables["index_jobs"]
    conditions = [jobs.c.run_id == run_id, jobs.c.status == "running", jobs.c.lease_version == 1]
    # 期限切れの成功は拒否する。一方、自分が占有したまま期限切れになった試行は
    # failedで終了できるようにし、後続ファイルと最終レポートの処理を継続する。
    if status != "failed":
        conditions.append(jobs.c.lease_until > utcnow())
    updated = conn.execute(jobs.update().where(*conditions).values(
             status=status, finished_at=utcnow(), lease_until=None,
             error_code=error_code, error_summary=error_code))
    if updated.rowcount != 1:
        raise PipelineError("処理ジョブの占有が失効しています")


def _recover_jobs(conn: Connection) -> None:
    # DB全体の排他ロック取得後なので、残存runningは前回中断したv1の記録。
    # 前回の行を成功で上書きせず、新しい試行行を作成できる状態にする。
    jobs = tables["index_jobs"]
    conn.execute(jobs.update().where(jobs.c.status.in_(("queued", "running"))).values(
        status="failed", finished_at=utcnow(), lease_until=None,
        lease_version=jobs.c.lease_version + 1, error_code="interrupted_previous_run",
        error_summary="前回の登録処理が完了する前に中断されました",
    ))


def _register_discovery(conn: Connection, settings: Settings, scan: ScanResult,
                        scan_id: str, unchanged_ids: set[str]) -> None:
    documents, memberships = tables["documents"], tables["knowledge_documents"]
    for item in scan.items:
        if item.document_id in unchanged_ids:
            conn.execute(memberships.update().where(
                memberships.c.knowledge_id == settings.knowledge_id,
                memberships.c.document_id == item.document_id,
            ).values(last_seen_scan_id=scan_id))
            continue
        _insert_missing(conn, documents, {"document_id": item.document_id}, {
            "source_system": item.source_id, "source_key": item.source_key,
            "source_uri": item.source_uri, "title": item.relative_path, "format": item.format,
        })
        conn.execute(documents.update().where(documents.c.document_id == item.document_id).values(
            source_uri=item.source_uri, title=item.relative_path, format=item.format,
            last_observed_at=utcnow(),
        ))
        identity = {"knowledge_id": settings.knowledge_id, "document_id": item.document_id}
        _insert_missing(conn, memberships, identity, {})
        conn.execute(memberships.update().where(and_(
            *(memberships.c[key] == value for key, value in identity.items())
        )).values(included=True, collection_status="pending", last_seen_scan_id=scan_id,
                  last_error_code=None))
        selections = tables["selected_generations"]
        conn.execute(selections.update().where(
            selections.c.knowledge_id == settings.knowledge_id,
            selections.c.document_id == item.document_id).values(generation_id=None))


def _reconcile_missing(conn: Connection, settings: Settings, scan: ScanResult) -> int:
    """消えた原本だけを論理削除。除外・列挙障害・収集元削除とは区別する。"""
    docs, members = tables["documents"], tables["knowledge_documents"]
    seen = {item.document_id for item in scan.items}
    configured = {source.source_id for source in settings.sources}
    rows = conn.execute(select(docs).join(members).where(
        members.c.knowledge_id == settings.knowledge_id, members.c.included.is_(True)
    )).mappings().all()
    deleted = 0
    for row in rows:
        if row["document_id"] in seen:
            continue
        membership = and_(members.c.knowledge_id == settings.knowledge_id,
                          members.c.document_id == row["document_id"])
        excluded = row["source_system"] not in configured or any(
            e["source_id"] == row["source_system"] and (
                e["relative_path"] == row["source_key"]
                or row["source_key"].startswith(e["relative_path"].rstrip("/") + "/")
            ) for e in scan.exclusions
        )
        if excluded:
            conn.execute(members.update().where(membership).values(
                included=False, collection_status="excluded", last_error_code=None))
        elif row["source_system"] not in scan.successful_sources:
            conn.execute(members.update().where(membership).values(
                collection_status="failed", last_error_code="scan_incomplete"))
            conn.execute(docs.update().where(docs.c.document_id == row["document_id"]).values(
                source_status="unavailable"))
        else:
            conn.execute(docs.update().where(docs.c.document_id == row["document_id"]).values(
                source_status="deleted"))
            conn.execute(members.update().where(membership).values(
                collection_status="failed", last_error_code="source_deleted"))
            deleted += row["source_status"] != "deleted"
    return deleted


def _put_asset(conn: Connection, asset: StoredAsset) -> None:
    _insert_missing(conn, tables["artifact_assets"], {"asset_id": asset.asset_id}, {
        "storage_key": asset.storage_key, "mime_type": asset.mime_type,
        "byte_size": asset.byte_size, "storage_status": "available",
    })


def extract(asset: StoredAsset, item: ScanItem, settings: Settings) -> ExtractionResult:
    """解析ワーカーを再利用する。直接呼び出す場合も終了時に確実に回収する。"""
    pool = ACTIVE_POOL.get()
    if pool is not None:
        return pool.extract(asset, item)
    with ParsePool(settings) as pool:
        return pool.extract(asset, item)


def _validate(result: ExtractionResult, chunks: list[Chunk]) -> list:
    """FKだけでは判定できない親循環・文字位置の整合を、保存前に検査する。"""
    by_id = {block.block_id: block for block in result.blocks}
    if len(by_id) != len(result.blocks):
        raise PipelineError("duplicate_block_id")
    ordered, visiting, visited = [], set(), set()

    def visit(block):
        if block.block_id in visiting:
            raise PipelineError("cyclic_block_parent")
        if block.block_id in visited:
            return
        if not block.source_locations:
            raise PipelineError("missing_source_location")
        visiting.add(block.block_id)
        if block.parent_id:
            if block.parent_id not in by_id:
                raise PipelineError("missing_parent_block")
            visit(by_id[block.parent_id])
        visiting.remove(block.block_id)
        visited.add(block.block_id)
        ordered.append(block)

    for block in result.blocks:
        visit(block)
    for chunk in chunks:
        if not chunk.blocks:
            raise PipelineError("missing_chunk_provenance")
        for mapping in chunk.blocks:
            block = by_id.get(mapping.block_id)
            if block is None or block.artifact_type != chunk.artifact_type:
                raise PipelineError("inconsistent_chunk_provenance")
            if not (0 <= mapping.chunk_char_start < mapping.chunk_char_end <= len(chunk.content_text)):
                raise PipelineError("invalid_chunk_range")
            if mapping.block_char_start is not None:
                original = (block.content_text or "")[mapping.block_char_start:mapping.block_char_end]
                quote = chunk.content_text[mapping.chunk_char_start:mapping.chunk_char_end]
                if original != quote:
                    raise PipelineError("chunk_source_text_mismatch")
    if any(asset.block_id not in by_id for asset in result.assets):
        raise PipelineError("missing_asset_block")
    return ordered


def _save_payload(conn: Connection, generation_id: str, result: ExtractionResult,
                  chunks: list[Chunk], stored_images: list[StoredAsset]) -> dict:
    ordered = _validate(result, chunks)
    # --retry-partial時も、以前の公開物を書き換えない。v1はdraftだけを扱う。
    generations = tables["extraction_generations"]
    row = conn.execute(select(generations).where(
        generations.c.generation_id == generation_id).with_for_update()).mappings().one()
    if row["publication_state"] != "draft":
        raise PipelineError("published_generation_is_immutable")
    # 全ての対応を外してから本文を置換する。途中で失敗すれば全体をrollback。
    conn.execute(tables["artifact_relations"].delete().where(
        tables["artifact_relations"].c.from_generation_id == generation_id))
    for name in ("asset_occurrences", "chunk_blocks", "chunks"):
        conn.execute(tables[name].delete().where(tables[name].c.generation_id == generation_id))
    conn.execute(tables["blocks"].update().where(tables["blocks"].c.generation_id == generation_id)
                 .values(parent_id=None))
    conn.execute(tables["blocks"].delete().where(tables["blocks"].c.generation_id == generation_id))
    for order, block in enumerate(ordered):
        conn.execute(tables["blocks"].insert().values(
            generation_id=generation_id, reading_order=order, generation_details={}, **asdict(block)))
    for chunk in chunks:
        values = asdict(chunk)
        mappings = values.pop("blocks")
        conn.execute(tables["chunks"].insert().values(generation_id=generation_id, **values))
        for order, mapping in enumerate(mappings):
            conn.execute(tables["chunk_blocks"].insert().values(
                generation_id=generation_id, chunk_id=chunk.chunk_id, sequence_no=order,
                artifact_type=chunk.artifact_type, **mapping))
    occurrences = Counter()
    for asset, stored in zip(result.assets, stored_images, strict=True):
        _put_asset(conn, stored)
        order = occurrences[asset.block_id]
        occurrences[asset.block_id] += 1
        conn.execute(tables["asset_occurrences"].insert().values(
            occurrence_id=digest([generation_id, asset.block_id, order]),
            generation_id=generation_id, block_id=asset.block_id, asset_id=stored.asset_id,
            occurrence_order=order, asset_role=asset.asset_role,
            source_locations=asset.source_locations, rendering_details=asset.rendering_details,
        ))
    for order, relation in enumerate(result.relations):
        values = asdict(relation)
        conn.execute(tables["artifact_relations"].insert().values(
            relation_id=digest([generation_id, order, values]),
            from_generation_id=generation_id, to_generation_id=generation_id, **values))
    summary = {"quality_flags": sorted(set(result.quality_flags)),
               "pending_images": result.pending_images, "blocks": len(result.blocks),
               "chunks": len(chunks), "metrics": result.metrics}
    conn.execute(generations.update().where(generations.c.generation_id == generation_id).values(
        processing_status=result.processing_status, quality_summary=summary))
    return summary


def write_text(path: Path, payload: str) -> None:
    """書き込みの途中で停止しても、完成済みファイルを壊さない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def _export(engine: Engine, settings: Settings, generation_id: str, manifest: dict) -> None:
    """DBの確定済み本文から確認資料を作る。再実行でも再生成して欠落を修復。

    生成ファイルは正本ではない。ファイルの書き換えをDBへ逆輸入しない。
    データを取得して接続を閉じた後に書き出し、DB更新のロックを長引かせない。
    """
    with engine.connect() as conn:
        blocks = [dict(row) for row in conn.execute(select(tables["blocks"]).where(
            tables["blocks"].c.generation_id == generation_id
        ).order_by(tables["blocks"].c.reading_order)).mappings()]
        relations = [dict(row) for row in conn.execute(select(tables["artifact_relations"]).where(
            tables["artifact_relations"].c.from_generation_id == generation_id)).mappings()]
        chunks = [dict(row) for row in conn.execute(select(tables["chunks"]).where(
            tables["chunks"].c.generation_id == generation_id
        ).order_by(tables["chunks"].c.chunk_id)).mappings()]
        mappings = [dict(row) for row in conn.execute(select(tables["chunk_blocks"]).where(
            tables["chunk_blocks"].c.generation_id == generation_id
        ).order_by(tables["chunk_blocks"].c.sequence_no)).mappings()]
        assets = [dict(row) for row in conn.execute(select(tables["asset_occurrences"]).where(
            tables["asset_occurrences"].c.generation_id == generation_id)).mappings()]
    folder = settings.report_dir / "generations" / generation_id.split(":")[-1]
    write_json(folder / "manifest.json", manifest)
    write_json(folder / "document.json", {"blocks": blocks, "relations": relations, "assets": assets})
    # JSONLはJSON文字列の配列と混同しないよう、1行に1チャンクを書き込む。
    lines = []
    for chunk in chunks:
        chunk["blocks"] = [m for m in mappings if m["chunk_id"] == chunk["chunk_id"]]
        lines.append(json.dumps(chunk, ensure_ascii=False, default=str))
    write_text(folder / "chunks.jsonl", "\n".join(lines) + ("\n" if lines else ""))
    review = ["# 下書き抽出結果\n"]
    for block in blocks:
        review.extend([f"## {block['block_id']} ({block['element_type']})\n",
                       json.dumps(block["source_locations"], ensure_ascii=False) + "\n",
                       (block["content_text"] or "") + "\n"])
        if block["element_type"] == "image_description" and block["content_data"].get("unreadable"):
            review.append("判読不能箇所 " + block["content_data"]["unreadable"] + "\n")
    write_text(folder / "review.md", "\n".join(review))


def _cached_assets_available(engine: Engine, generation_id: str, storage: Storage) -> bool:
    occurrences, assets = tables["asset_occurrences"], tables["artifact_assets"]
    with engine.connect() as conn:
        rows = conn.execute(select(assets).join(occurrences).where(
            occurrences.c.generation_id == generation_id)).mappings().all()
    for row in rows:
        try:
            storage.verify(row["asset_id"], row["storage_key"], row["byte_size"])
        except Exception as exc:
            if getattr(exc, "code", None) == "stored_asset_missing":
                return False  # 元コピーから再抽出して欠落した画像を補完する。
            raise  # 内容ハッシュの不一致は、欠落とは区別して失敗記録へ残す。
    return True


def _unchanged_documents(conn: Connection, settings: Settings, scan: ScanResult,
                         hashes: dict[str, str], storage: Storage) -> dict:
    """生成済みMarkdownと現在の正常な変換世代が一致する場合だけ再利用する。"""
    docs, members = tables["documents"], tables["knowledge_documents"]
    selected, generations = tables["selected_generations"], tables["extraction_generations"]
    assets = tables["artifact_assets"]
    revision, _ = conversion_profile(settings, "md")
    rows = conn.execute(select(
        docs.c.document_id, docs.c.latest_source_version, docs.c.source_uri,
        selected.c.generation_id, generations.c.quality_summary,
        assets.c.storage_key, assets.c.byte_size,
    ).select_from(members.join(docs).join(selected, and_(
        selected.c.document_id == members.c.document_id,
        selected.c.knowledge_id == members.c.knowledge_id,
    )).join(generations, generations.c.generation_id == selected.c.generation_id)
        .join(assets, assets.c.asset_id == docs.c.latest_source_version)).where(
        members.c.knowledge_id == settings.knowledge_id, members.c.included.is_(True),
        members.c.collection_status == "available", docs.c.source_status == "available",
        docs.c.format == "md", selected.c.conversion_revision == revision,
        generations.c.conversion_revision == revision,
        generations.c.processing_status == "succeeded",
        generations.c.source_version == docs.c.latest_source_version,
        assets.c.storage_status == "available",
    )).mappings()
    by_id = {row["document_id"]: row for row in rows}
    result = {}
    for item in scan.items:
        row = by_id.get(item.document_id)
        if (item.format != "md" or not row or row["source_uri"] != item.source_uri
                or row["byte_size"] > settings.max_file_bytes
                or row["latest_source_version"] != "sha256:" + hashes[item.relative_path]):
            continue
        try:
            storage.verify(row["latest_source_version"], row["storage_key"], row["byte_size"])
        except StorageError:
            continue  # 既存の原本修復・破損検出経路へ戻す。
        result[item.document_id] = {
            **row["quality_summary"], "document_id": item.document_id, "source_id": item.source_id,
            "relative_path": item.relative_path, "status": "unchanged", "processing_status": "succeeded",
            "source_version": row["latest_source_version"], "generation_id": row["generation_id"],
            "conversion_revision": revision, "source_bytes_read": 0, "temporary_bytes_written": 0,
            "original_reused": True,
        }
    return result


def ingest(settings: Settings, engine: Engine, *, retry_partial: bool = False,
           verified_markdown: dict[str, str] | None = None,
           allow_removals: bool = False) -> dict:
    """全体の失敗で既存の版を消さず、文書単位で結果を確定してレポートを返す。"""
    validate_settings(settings)
    vision = settings.image_analysis
    if vision.enabled and any(vision.applies_to(ext.lstrip(".")) for ext in settings.include_extensions):
        if not vision.api_key.strip() or any(marker in vision.api_key.lower()
                                               for marker in ("placeholder", "replace-me", "dummy", "your-key")):
            raise PipelineError("画像認識用のAPIキーを環境変数に設定してください")
    require_schema(engine)
    report = {"run_id": str(uuid4()), "started_at": utcnow().isoformat(),
              "knowledge_id": settings.knowledge_id, "status": "succeeded", "files": []}
    storage = Storage(settings.storage_dir)
    profiles = {}
    kb, docs, members = tables["knowledge_bases"], tables["documents"], tables["knowledge_documents"]
    generations = tables["extraction_generations"]
    with ingestion_lock(engine), ParsePool(settings) as pool:
        scan = None
        unchanged = {}
        if verified_markdown is not None:
            if len(settings.sources) != 1 or set(settings.include_extensions) != {".md"}:
                raise PipelineError("verified_markdown_requires_dedicated_source")
            scan = collect(settings)
            if scan.errors or {item.relative_path for item in scan.items} != set(verified_markdown):
                raise PipelineError("ingestion_document_set_mismatch")
            with engine.connect() as conn:
                if not allow_removals:
                    seen = {item.document_id for item in scan.items}
                    existing = conn.execute(select(docs.c.document_id).join(members).where(
                        members.c.knowledge_id == settings.knowledge_id,
                        members.c.included.is_(True), docs.c.source_status != "deleted",
                    )).scalars()
                    if any(document_id not in seen for document_id in existing):
                        raise PipelineError("handoff_removals_require_explicit_option")
                unchanged = _unchanged_documents(conn, settings, scan, verified_markdown, storage)
        with engine.begin() as conn:
            binding = tables["ingestion_settings"]
            _insert_missing(conn, binding, {"singleton_id": 1}, {
                "storage_root": str(settings.storage_dir.resolve())})
            bound_root = conn.execute(select(binding.c.storage_root).where(
                binding.c.singleton_id == 1)).scalar_one()
            if bound_root != str(settings.storage_dir.resolve()):
                raise PipelineError("このDBのstorage_dirは初回登録時と異なります。保管先の移行が必要です")
            _insert_missing(conn, kb, {"knowledge_id": settings.knowledge_id}, {
                "name": settings.knowledge_id, "system_id": settings.system_id,
                "environment": settings.environment,
            })
            catalog = conn.execute(select(kb).where(kb.c.knowledge_id == settings.knowledge_id)).mappings().one()
            if (catalog["system_id"], catalog["environment"]) != (settings.system_id, settings.environment):
                raise PipelineError("同じknowledge_idのシステム・環境を変更できません")
            _recover_jobs(conn)
            if scan is None:
                conn.execute(kb.update().where(kb.c.knowledge_id == settings.knowledge_id)
                             .values(scan_status="running"))
            scan_id = _start_job(conn, settings.knowledge_id, "scan",
                                 digest([settings.knowledge_id, "scan"]))
        if scan is None:
            scan = collect(settings)
        report["scan_errors"], report["exclusions"] = scan.errors, scan.exclusions
        with engine.begin() as conn:
            _register_discovery(conn, settings, scan, scan_id, set(unchanged))
            deleted = _reconcile_missing(conn, settings, scan)
            scan_status = "partial" if scan.errors else "succeeded"
            # バッチ排他を持ち続けて完了した列挙なので、回収されていない自分の
            # ジョブだけを更新する。300秒を超える棚卸しでも失敗記録を失わない。
            jobs = tables["index_jobs"]
            conn.execute(jobs.update().where(
                jobs.c.run_id == scan_id, jobs.c.lease_version == 1, jobs.c.status == "running"
            ).values(lease_until=utcnow() + timedelta(seconds=300)))
            _finish_job(conn, scan_id, scan_status)
            values = {"scan_status": scan_status}
            if (verified_markdown is None or len(unchanged) != len(scan.items) or deleted
                    or catalog["scan_status"] != scan_status):
                values["catalog_revision"] = kb.c.catalog_revision + 1
            if not scan.errors:
                values["last_successful_scan_at"] = utcnow()
            conn.execute(kb.update().where(kb.c.knowledge_id == settings.knowledge_id).values(**values))

        for item in scan.items:
            if item.document_id in unchanged:
                report["files"].append(unchanged[item.document_id])
                continue
            entry = {"document_id": item.document_id, "source_id": item.source_id,
                     "relative_path": item.relative_path, "status": "failed"}
            generation_id, job_id, stage = None, None, "collect"
            try:
                with engine.begin() as conn:
                    job_id = _start_job(conn, settings.knowledge_id, "collect",
                                        digest([item.document_id, "collect"]), item.document_id)
                with engine.connect() as conn:
                    assets = tables["artifact_assets"]
                    versions = tables["document_versions"]
                    known = conn.execute(select(assets).join(versions, versions.c.asset_id == assets.c.asset_id)
                        .join(docs, and_(docs.c.document_id == versions.c.document_id,
                                        docs.c.latest_source_version == versions.c.source_version))
                        .where(docs.c.document_id == item.document_id)).mappings().first()
                original = storage.snapshot(item, settings.max_file_bytes, settings.copy_retries, known)
                if (verified_markdown is not None
                        and original.asset_id != "sha256:" + verified_markdown[item.relative_path]):
                    raise PipelineError("ingestion_markdown_hash_mismatch")
                entry.update(source_version=original.asset_id, source_bytes_read=original.source_bytes_read,
                             temporary_bytes_written=original.temporary_bytes_written,
                             original_reused=original.reused)
                with engine.begin() as conn:
                    _put_asset(conn, original)
                    _insert_missing(conn, tables["document_versions"], {
                        "document_id": item.document_id, "source_version": original.asset_id,
                    }, {"asset_id": original.asset_id, "source_uri": item.source_uri,
                        "title": item.relative_path, "format": item.format,
                        "source_modified_at": original.source_modified_at})
                    conn.execute(docs.update().where(docs.c.document_id == item.document_id).values(
                        latest_source_version=original.asset_id, source_status="available",
                        source_modified_at=original.source_modified_at))
                    conn.execute(members.update().where(
                        members.c.knowledge_id == settings.knowledge_id,
                        members.c.document_id == item.document_id,
                    ).values(collection_status="available", last_error_code=None))
                    _finish_job(conn, job_id, "succeeded")
                    job_id = None
                stage = "convert"
                if item.format not in profiles:
                    profiles[item.format] = conversion_profile(settings, item.format)
                revision, profile = profiles[item.format]
                generation_id = digest([item.document_id, original.asset_id, revision])
                cache_key = digest([original.asset_id, profile["extraction_revision"]])
                entry.update(generation_id=generation_id, conversion_revision=revision)
                with engine.begin() as conn:
                    _insert_missing(conn, tables["conversion_profiles"], {"conversion_revision": revision}, {
                        "schema_version": "file-scan-v3", "configuration": profile})
                    existing = conn.execute(select(generations).where(
                        generations.c.generation_id == generation_id)).mappings().first()
                    _insert_missing(conn, generations, {"generation_id": generation_id}, {
                        "document_id": item.document_id, "source_version": original.asset_id,
                        "conversion_revision": revision, "processing_status": "staging"})
                    selection = tables["selected_generations"]
                    identity = {"knowledge_id": settings.knowledge_id, "document_id": item.document_id}
                    _insert_missing(conn, selection, identity, {
                        "generation_id": generation_id, "conversion_revision": revision})
                    conn.execute(selection.update().where(selection.c.knowledge_id == settings.knowledge_id,
                        selection.c.document_id == item.document_id).values(
                            generation_id=generation_id, conversion_revision=revision))
                if existing and (existing["processing_status"] == "succeeded" or (
                    existing["processing_status"] == "partial" and not retry_partial
                )):
                    if _cached_assets_available(engine, generation_id, storage):
                        entry.update(status="unchanged", processing_status=existing["processing_status"],
                                     **existing["quality_summary"])
                        try:
                            _export(engine, settings, generation_id, entry)
                        except OSError:
                            entry["export_error"] = "export_write_failed"
                        report["files"].append(entry)
                        continue
                with engine.begin() as conn:
                    _insert_missing(conn, generations, {"generation_id": generation_id}, {
                        "document_id": item.document_id, "source_version": original.asset_id,
                        "conversion_revision": revision, "processing_status": "staging",
                    })
                    job_id = _start_job(conn, settings.knowledge_id, "convert",
                                        digest([generation_id, "convert"]), item.document_id,
                                        generation_id, settings.parse_timeout_seconds + 300)
                result = None if retry_partial else load_extraction(engine, storage, cache_key)
                entry["extraction_cached"] = result is not None
                if result is None:
                    result = extract(original, item, settings)
                    if vision.applies_to(item.format):
                        from file_scan.image_analysis import analyze_images

                        analyze_images(result, original.path, item.format, vision)
                chunks = build_chunks(result.blocks, item.relative_path, settings.chunk_max_characters,
                                      settings.chunk_max_tokens, settings.chunk_context_max_tokens)
                stored = [storage.put_bytes(asset.content, asset.mime_type) for asset in result.assets]
                with engine.begin() as conn:
                    summary = _save_payload(conn, generation_id, result, chunks, stored)
                    save_extraction(conn, cache_key, original.asset_id, profile["extraction_revision"], result, stored)
                    _finish_job(conn, job_id, result.processing_status)
                    conn.execute(kb.update().where(kb.c.knowledge_id == settings.knowledge_id)
                                 .values(catalog_revision=kb.c.catalog_revision + 1))
                job_id = None
                entry.update(status="registered", processing_status=result.processing_status, **summary)
                try:
                    _export(engine, settings, generation_id, entry)
                except OSError:
                    # DB登録は成功済み。書き出しの失敗を文書変換の失敗で上書きしない。
                    entry["export_error"] = "export_write_failed"
            except Exception as exc:
                code = getattr(exc, "code", type(exc).__name__)
                # 自作PipelineErrorの内容だけは区分として使える（原本由来の例外文は使わない）。
                if isinstance(exc, PipelineError):
                    code = str(exc)
                entry.update(error_code=code, processing_status="failed")
                with engine.begin() as conn:
                    if stage == "collect":
                        conn.execute(docs.update().where(docs.c.document_id == item.document_id)
                                     .values(source_status="unavailable"))
                        conn.execute(members.update().where(
                            members.c.knowledge_id == settings.knowledge_id,
                            members.c.document_id == item.document_id,
                        ).values(collection_status="failed", last_error_code=code))
                    elif generation_id:
                        conn.execute(generations.update().where(
                            generations.c.generation_id == generation_id
                        ).values(processing_status="failed", quality_summary={"error_code": code}))
                    if stage == "convert":
                        conn.execute(members.update().where(
                            members.c.knowledge_id == settings.knowledge_id,
                            members.c.document_id == item.document_id).values(last_error_code=code))
                    if job_id:
                        _finish_job(conn, job_id, "failed", code)
            report["files"].append(entry)
        report["parser_workers"] = pool.metrics
        report["summary"] = {
            "discovered": len(scan.items), "excluded": len(scan.exclusions), "deleted": deleted,
            "registered": sum(e["status"] == "registered" for e in report["files"]),
            "unchanged": sum(e["status"] == "unchanged" for e in report["files"]),
            "failed": sum(e["status"] == "failed" for e in report["files"]),
            "partial": sum(e["processing_status"] == "partial" for e in report["files"]),
            "scan_errors": len(scan.errors),
        }
    if scan.errors or any(e["processing_status"] != "succeeded" or e.get("export_error")
                          for e in report["files"]):
        report["status"] = "partial"
    report["finished_at"] = utcnow().isoformat()
    write_json(settings.report_dir / f"ingest-{report['run_id']}.json", report)
    return report
