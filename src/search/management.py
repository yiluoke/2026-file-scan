"""管理者の明示操作で検索対象版を作り、下書きから不変の公開コピーを作る。"""

from collections import defaultdict
from datetime import timezone
import math

from sqlalchemy import and_, select

from file_scan.db import tables as core
from file_scan.image_analysis import METHOD
from file_scan.pipeline import ingestion_lock
from file_scan.profiles import digest
from file_scan.storage import Storage
from search import schema as s
from search.ranking import identifiers


class PublicationError(RuntimeError):
    """管理コマンド用の本文を含まない区分。"""


def profile_values(settings):
    return {
        "provider": "openai",
        "model": settings.embedding.model,
        "dimensions": settings.embedding.dimensions,
        "input_version": "search_text-v1",
    }


def create_release(engine, settings, name, *, make_default=False):
    if len(settings.knowledge_id) > 128:
        raise PublicationError("knowledge_id_too_long")
    if not name.strip() or len(name) > 128 or name == "current":
        raise PublicationError("invalid_release_name")
    release_id = digest([settings.knowledge_id, name])
    profile = profile_values(settings)
    profile_id = digest(profile)
    with ingestion_lock(engine), engine.begin() as c:
        if not c.execute(
            select(core["knowledge_bases"].c.knowledge_id).where(
                core["knowledge_bases"].c.knowledge_id == settings.knowledge_id
            )
        ).first():
            raise PublicationError("knowledge_not_ingested")
        if not c.execute(
            select(core["embedding_profiles"]).where(
                core["embedding_profiles"].c.embedding_profile_id == profile_id
            )
        ).first():
            c.execute(
                core["embedding_profiles"]
                .insert()
                .values(embedding_profile_id=profile_id, **profile)
            )
        catalog = (
            c.execute(select(s.catalogs).where(s.catalogs.c.knowledge_id == settings.knowledge_id))
            .mappings()
            .first()
        )
        if catalog and catalog["embedding_profile_id"] != profile_id:
            raise PublicationError("published_embedding_profile_is_immutable")
        if not catalog:
            c.execute(
                s.catalogs.insert().values(
                    knowledge_id=settings.knowledge_id, embedding_profile_id=profile_id
                )
            )
        if not c.execute(select(s.releases).where(s.releases.c.release_id == release_id)).first():
            c.execute(
                s.releases.insert().values(
                    release_id=release_id, knowledge_id=settings.knowledge_id, name=name
                )
            )
            docs, members, selected = (
                core["documents"],
                core["knowledge_documents"],
                core["selected_generations"],
            )
            rows = c.execute(
                select(
                    docs.c.document_id, docs.c.latest_source_version, selected.c.conversion_revision
                )
                .select_from(
                    members.join(docs).outerjoin(
                        selected,
                        and_(
                            selected.c.knowledge_id == members.c.knowledge_id,
                            selected.c.document_id == members.c.document_id,
                        ),
                    )
                )
                .where(
                    members.c.knowledge_id == settings.knowledge_id, members.c.included.is_(True)
                )
            )
            for row in rows.mappings():
                c.execute(
                    s.bindings.insert().values(
                        release_id=release_id,
                        document_id=row["document_id"],
                        expected_source_version=row["latest_source_version"],
                        expected_conversion_revision=row["conversion_revision"],
                    )
                )
        values = {"revision": s.catalogs.c.revision + 1}
        if make_default or not catalog or not catalog["default_release_id"]:
            values.update(default_release_id=release_id, enabled=True)
        c.execute(
            s.catalogs.update()
            .where(s.catalogs.c.knowledge_id == settings.knowledge_id)
            .values(**values)
        )
    return release_id


def publish(
    engine,
    settings,
    release,
    document_id,
    generation_id,
    reviewer,
    *,
    allow_partial=False,
    include_image_descriptions=False,
    feature_ids=(),
):
    """完全な世代を凍結。API呼出中やingestによる暗黙公開は行わない。"""
    if not reviewer.strip() or len(reviewer) > 128:
        raise PublicationError("reviewer_required")
    if len(feature_ids) > 20 or any(not x.strip() or len(x) > 256 for x in feature_ids):
        raise PublicationError("invalid_feature_ids")
    with ingestion_lock(engine), engine.begin() as c:
        rel = (
            c.execute(
                select(s.releases).where(
                    s.releases.c.knowledge_id == settings.knowledge_id,
                    (s.releases.c.release_id == release) | (s.releases.c.name == release),
                )
            )
            .mappings()
            .first()
        )
        if not rel:
            raise PublicationError("release_not_found")
        catalog = (
            c.execute(
                select(s.catalogs)
                .where(s.catalogs.c.knowledge_id == settings.knowledge_id)
                .with_for_update()
            )
            .mappings()
            .one()
        )
        docs, members, selected, gens = (
            core[n]
            for n in (
                "documents",
                "knowledge_documents",
                "selected_generations",
                "extraction_generations",
            )
        )
        doc = (
            c.execute(
                select(docs)
                .join(members)
                .where(
                    docs.c.document_id == document_id,
                    members.c.knowledge_id == settings.knowledge_id,
                    members.c.included.is_(True),
                    members.c.collection_status == "available",
                    docs.c.source_status == "available",
                )
            )
            .mappings()
            .first()
        )
        gen = (
            c.execute(
                select(gens).where(
                    gens.c.generation_id == generation_id, gens.c.document_id == document_id
                )
            )
            .mappings()
            .first()
        )
        selection = (
            c.execute(
                select(selected).where(
                    selected.c.knowledge_id == settings.knowledge_id,
                    selected.c.document_id == document_id,
                )
            )
            .mappings()
            .first()
        )
        if (
            not doc
            or not gen
            or not selection
            or selection["generation_id"] != generation_id
            or (
                doc["latest_source_version"] != gen["source_version"]
                or selection["conversion_revision"] != gen["conversion_revision"]
            )
        ):
            raise PublicationError("generation_is_not_current")
        if gen["processing_status"] not in {"succeeded", "partial"} or (
            gen["processing_status"] == "partial" and not allow_partial
        ):
            raise PublicationError("partial_requires_explicit_approval")
        root = c.execute(select(core["ingestion_settings"].c.storage_root)).scalar_one()
        if root != str(settings.storage_dir.resolve()):
            raise PublicationError("storage_root_mismatch")
        storage = Storage(root)
        asset = (
            c.execute(
                select(core["artifact_assets"]).where(
                    core["artifact_assets"].c.asset_id == gen["source_version"]
                )
            )
            .mappings()
            .one()
        )
        if asset["storage_status"] != "available":
            raise PublicationError("source_unavailable")
        storage.verify(asset["asset_id"], asset["storage_key"], asset["byte_size"])
        block_rows = [
            dict(r)
            for r in c.execute(
                select(core["blocks"])
                .where(core["blocks"].c.generation_id == generation_id)
                .order_by(core["blocks"].c.reading_order)
            ).mappings()
        ]
        chunk_rows = [
            dict(r)
            for r in c.execute(
                select(core["chunks"])
                .where(core["chunks"].c.generation_id == generation_id)
                .order_by(core["chunks"].c.chunk_id)
            ).mappings()
        ]
        if gen["quality_summary"].get("chunks") != len(chunk_rows) or gen["quality_summary"].get(
            "blocks"
        ) != len(block_rows):
            raise PublicationError("generation_incomplete")
        derived_blocks = {r["block_id"]: r for r in block_rows if r["artifact_type"] == "derived"}
        if include_image_descriptions and any(
            r["element_type"] != "image_description" or r["extraction_method"] != METHOD
            for r in derived_blocks.values()
        ):
            raise PublicationError("unsupported_derived_evidence")
        if not include_image_descriptions:
            block_rows = [r for r in block_rows if r["artifact_type"] == "source"]
            chunk_rows = [r for r in chunk_rows if r["artifact_type"] == "source"]
        if not chunk_rows:
            raise PublicationError("no_publishable_chunks")
        mappings = defaultdict(list)
        for row in c.execute(
            select(core["chunk_blocks"])
            .where(core["chunk_blocks"].c.generation_id == generation_id)
            .order_by(core["chunk_blocks"].c.sequence_no)
        ).mappings():
            if row["artifact_type"] == "derived" and not include_image_descriptions:
                continue
            mappings[row["chunk_id"]].append(
                {
                    k: row[k]
                    for k in (
                        "block_id",
                        "chunk_char_start",
                        "chunk_char_end",
                        "block_char_start",
                        "block_char_end",
                    )
                } | ({"artifact_type": "derived"} if row["artifact_type"] == "derived" else {})
            )
        by_id = {r["block_id"]: r for r in block_rows}
        for chunk in chunk_rows:
            if not mappings[chunk["chunk_id"]]:
                raise PublicationError("missing_chunk_provenance")
            previous_end = 0
            for m in mappings[chunk["chunk_id"]]:
                if (
                    m["block_char_start"] is None
                    or m["block_char_end"] is None
                    or not 0
                    <= previous_end
                    <= m["chunk_char_start"]
                    < m["chunk_char_end"]
                    <= len(chunk["content_text"])
                    or chunk["content_text"][previous_end : m["chunk_char_start"]].strip()
                ):
                    raise PublicationError("invalid_chunk_provenance")
                previous_end = m["chunk_char_end"]
                block = by_id.get(m["block_id"])
                if (
                    not block
                    or block["artifact_type"] != chunk["artifact_type"]
                    or not block["source_locations"]
                    or not (
                        0
                        <= m["block_char_start"]
                        < m["block_char_end"]
                        <= len(block["content_text"] or "")
                    )
                    or (
                        chunk["content_text"][m["chunk_char_start"] : m["chunk_char_end"]]
                        != (block["content_text"] or "")[
                            m["block_char_start"] : m["block_char_end"]
                        ]
                    )
                ):
                    raise PublicationError("invalid_chunk_provenance")
            if chunk["content_text"][previous_end:].strip():
                raise PublicationError("invalid_chunk_provenance")
        vector_rows = {
            r["chunk_id"]: r
            for r in c.execute(
                select(core["chunk_embeddings"]).where(
                    core["chunk_embeddings"].c.generation_id == generation_id,
                    core["chunk_embeddings"].c.embedding_profile_id
                    == catalog["embedding_profile_id"],
                )
            ).mappings()
        }
        profile = (
            c.execute(
                select(core["embedding_profiles"]).where(
                    core["embedding_profiles"].c.embedding_profile_id
                    == catalog["embedding_profile_id"]
                )
            )
            .mappings()
            .one()
        )
        frozen = []
        for chunk in chunk_rows:
            input_hash = digest(chunk["search_text"])
            vector = vector_rows.get(chunk["chunk_id"])
            if vector and vector["input_hash"] == input_hash:
                values = [float(x) for x in vector["embedding"]]
                if (
                    len(values) != profile["dimensions"]
                    or not any(values)
                    or not all(math.isfinite(x) for x in values)
                ):
                    raise PublicationError("invalid_embedding")
            names = set(chunk["identifiers"]) | set(identifiers(chunk["search_text"], limit=512))
            for mapping in mappings[chunk["chunk_id"]]:
                data = by_id[mapping["block_id"]]["content_data"]
                names.update(data.get("identifiers", []))
            frozen.append(
                {
                    "chunk_id": chunk["chunk_id"],
                    "content_text": chunk["content_text"],
                    "search_text": chunk["search_text"],
                    "title": chunk["title"],
                    "identifiers": sorted(x for x in names if isinstance(x, str) and len(x) <= 256),
                    "feature_ids": sorted(set(feature_ids)),
                    "mappings": mappings[chunk["chunk_id"]],
                    "quality_flags": chunk["quality_flags"],
                    "input_hash": input_hash,
                    "dimensions": profile["dimensions"],
                    "embedding": [float(x) for x in vector["embedding"]]
                    if vector and vector["input_hash"] == input_hash
                    else None,
                }
            )
        relations = [
            dict(r)
            for r in c.execute(
                select(core["artifact_relations"]).where(
                    core["artifact_relations"].c.from_generation_id == generation_id,
                    core["artifact_relations"].c.to_generation_id == generation_id,
                    core["artifact_relations"].c.relation_status == "confirmed",
                )
            ).mappings()
        ]
        relation_data = [
            {k: r[k] for k in ("from_block_id", "to_block_id", "relation_type")}
            for r in relations if r["from_block_id"] in by_id and r["to_block_id"] in by_id
        ]
        assets = defaultdict(list)
        for row in c.execute(
            select(core["asset_occurrences"]).where(
                core["asset_occurrences"].c.generation_id == generation_id
            )
        ).mappings():
            assets[row["block_id"]].append(
                {k: row[k] for k in ("occurrence_id", "asset_id", "asset_role", "source_locations")}
            )
        if include_image_descriptions:
            for block in derived_blocks.values():
                parent = by_id.get(block["parent_id"])
                image_id = block["content_data"].get("asset_id")
                image_assets = [a for a in assets[block["parent_id"]] if a["asset_id"] == image_id]
                if not parent or parent["artifact_type"] != "source" or not image_assets:
                    raise PublicationError("image_description_source_missing")
                assets[block["block_id"]] = image_assets
        frozen_blocks = [
            {
                k: r[k]
                for k in (
                    "block_id",
                    "parent_id",
                    "reading_order",
                    "element_type",
                    "content_text",
                    "content_data",
                    "section_path",
                    "source_locations",
                    "quality_flags",
                )
            }
            | {
                "assets": assets[r["block_id"]],
                "content_data": {
                    "headers": r["content_data"].get("headers", []),
                    "cells": [
                        {"coordinate": cell["coordinate"]}
                        for cell in r["content_data"].get("cells", [])
                        if "coordinate" in cell
                    ],
                } | ({"artifact_type": "derived"} if r["artifact_type"] == "derived" else {}),
            }
            for r in block_rows
        ]
        publication_id = digest(
            [
                rel["release_id"],
                document_id,
                generation_id,
                frozen,
                frozen_blocks,
                relation_data,
                gen["quality_summary"],
                gen["updated_at"].astimezone(timezone.utc).isoformat(),
            ]
        )
        if not c.execute(
            select(s.publications).where(s.publications.c.publication_id == publication_id)
        ).first():
            c.execute(
                s.publications.insert().values(
                    publication_id=publication_id,
                    release_id=rel["release_id"],
                    document_id=document_id,
                    generation_id=generation_id,
                    source_version=gen["source_version"],
                    source_path=doc["source_key"],
                    conversion_revision=gen["conversion_revision"],
                    embedding_profile_id=profile["embedding_profile_id"],
                    generation_updated_at=gen["updated_at"],
                    approved_by=reviewer,
                    partial=gen["processing_status"] == "partial",
                    quality_summary=gen["quality_summary"],
                    relations=relation_data,
                )
            )
            c.execute(s.chunks.insert(), [{"publication_id": publication_id, **r} for r in frozen])
            c.execute(
                s.blocks.insert(), [{"publication_id": publication_id, **r} for r in frozen_blocks]
            )
        condition = (s.bindings.c.release_id == rel["release_id"]) & (
            s.bindings.c.document_id == document_id
        )
        values = dict(
            expected_source_version=gen["source_version"],
            expected_conversion_revision=gen["conversion_revision"],
            publication_id=publication_id,
        )
        if c.execute(select(s.bindings).where(condition)).first():
            c.execute(s.bindings.update().where(condition).values(**values))
        else:
            c.execute(
                s.bindings.insert().values(
                    release_id=rel["release_id"], document_id=document_id, **values
                )
            )
        c.execute(
            s.catalogs.update()
            .where(s.catalogs.c.knowledge_id == settings.knowledge_id)
            .values(
                revision=s.catalogs.c.revision + 1,
                feature_filter_enabled=catalog["feature_filter_enabled"] or bool(feature_ids),
            )
        )
    return publication_id
