"""選択中世代をページ単位で埋め込み、同じモデル・入力のベクトルを再利用する。"""

from __future__ import annotations

import math
from datetime import timedelta
from uuid import uuid4

import openai
from sqlalchemy import exists, func, select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on, insert

from file_scan.config import Settings, validate_settings
from file_scan.db import tables
from file_scan.pipeline import (
    PipelineError, _finish_job, _insert_missing, _recover_jobs, _start_job, ingestion_lock,
    require_schema, utcnow, write_json,
)
from file_scan.profiles import conversion_profile, digest
from file_scan.state import current_chunks, current_documents, input_hash_sql
from file_scan.tokens import count_tokens


class EmbeddingError(PipelineError):
    """秘密情報を含まないEmbedding用のエラー。"""


def _error_code(error: Exception) -> str:
    if isinstance(error, EmbeddingError):
        return str(error)
    for error_type, code in (
        (openai.AuthenticationError, "openai_authentication_error"),
        (openai.RateLimitError, "openai_rate_limit"),
        (openai.APITimeoutError, "openai_timeout"),
        (openai.APIConnectionError, "openai_connection_error"),
        (openai.APIStatusError, "openai_api_error"),
    ):
        if isinstance(error, error_type):
            return code
    return "embedding_failed"


def _target_query(connection, knowledge_id, revisions=None):
    chunks = tables["chunks"]
    return select(chunks.c.generation_id, chunks.c.chunk_id, chunks.c.search_text,
                  input_hash_sql(connection, chunks.c.search_text).label("input_hash")).select_from(
        current_chunks(knowledge_id, revisions)).where(chunks.c.publication_state == "draft")


def _matching_embedding(profile_id, input_hash):
    embeddings, chunks = tables["chunk_embeddings"], tables["chunks"]
    return exists(select(1).where(embeddings.c.generation_id == chunks.c.generation_id,
        embeddings.c.chunk_id == chunks.c.chunk_id, embeddings.c.embedding_profile_id == profile_id,
        embeddings.c.input_hash == input_hash))


def _targets(connection, knowledge_id, revisions=None, *, profile_id=None, after=None, limit=128):
    """本文は最大limit件だけ取得。API処理中のDB接続・長期transactionは不要。"""
    query = _target_query(connection, knowledge_id, revisions)
    chunks = tables["chunks"]
    if profile_id is not None:
        query = query.where(~_matching_embedding(profile_id, input_hash_sql(connection, chunks.c.search_text)))
    if after is not None:
        query = query.where(tuple_(chunks.c.generation_id, chunks.c.chunk_id) > tuple_(*after))
    return [dict(row) for row in connection.execute(query.order_by(
        chunks.c.generation_id, chunks.c.chunk_id).limit(limit)).mappings()]


def _vectors(response, count: int, dimensions: int) -> list[list[float]]:
    if len(response.data) != count or {item.index for item in response.data} != set(range(count)):
        raise EmbeddingError("embedding_response_mismatch")
    vectors = []
    for item in sorted(response.data, key=lambda item: item.index):
        vector = item.embedding
        if len(vector) != dimensions or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) for value in vector
        ) or not any(vector):
            raise EmbeddingError("embedding_invalid_vector")
        vectors.append(vector)
    return vectors


def _cached_vectors(connection, profile_id, hashes):
    """入力ページのハッシュだけを検索。旧v2保存済みベクトルも遅延移行して利用。"""
    cache, embeddings = tables["embedding_cache"], tables["chunk_embeddings"]
    rows = connection.execute(select(cache).where(cache.c.embedding_profile_id == profile_id,
                                                  cache.c.input_hash.in_(hashes))).mappings()
    result = {row["input_hash"]: dict(row) for row in rows}
    missing = set(hashes) - result.keys()
    if missing:
        rows = connection.execute(select(embeddings).where(
            embeddings.c.embedding_profile_id == profile_id, embeddings.c.input_hash.in_(missing)
        ).ext(distinct_on(embeddings.c.input_hash)).order_by(embeddings.c.input_hash,
            embeddings.c.generation_id, embeddings.c.chunk_id)).mappings()
        result.update({row["input_hash"]: dict(row) for row in rows})
    return result


def _save_vectors(engine, profile_id, dimensions, targets, vectors, job_id, timeout):
    embeddings, cache = tables["chunk_embeddings"], tables["embedding_cache"]
    with engine.begin() as connection:
        for target, vector in zip(targets, vectors, strict=True):
            values = {"embedding_profile_id": profile_id, "input_hash": target["input_hash"],
                      "dimensions": dimensions, "embedding": vector,
                      "input_tokens": target["input_tokens"]}
            connection.execute(insert(cache).values(**values).on_conflict_do_nothing())
            values.update(generation_id=target["generation_id"], chunk_id=target["chunk_id"])
            statement = insert(embeddings).values(**values)
            connection.execute(statement.on_conflict_do_update(
                index_elements=[embeddings.c.generation_id, embeddings.c.chunk_id,
                                embeddings.c.embedding_profile_id],
                set_={name: statement.excluded[name] for name in
                      ("embedding", "input_hash", "input_tokens", "created_at")},
            ))
        connection.execute(tables["index_jobs"].update().where(
            tables["index_jobs"].c.run_id == job_id).values(
                lease_until=utcnow() + timedelta(seconds=timeout * 3 + 300)))


def embed(settings: Settings, engine) -> dict:
    validate_settings(settings)
    config = settings.embedding
    if not config.enabled:
        return {"status": "disabled", "reason": "embedding.enabled=false", "embedded": 0}
    if not config.api_key or any(marker in config.api_key.lower()
                                 for marker in ("placeholder", "replace-me", "dummy", "your-key")):
        raise EmbeddingError("OpenAI APIキーが未設定または仮キーです。embedding設定を確認してください")
    if engine.dialect.name != "postgresql":
        raise EmbeddingError("Embeddingの保存にはPostgreSQL＋pgvectorが必要です")
    require_schema(engine)
    profile = {"provider": "openai", "model": config.model, "dimensions": config.dimensions,
               "input_version": "search_text-v1"}
    profile_id = digest(profile)
    report = {"run_id": str(uuid4()), "knowledge_id": settings.knowledge_id,
              "embedding_profile_id": profile_id, **profile, "status": "succeeded",
              "embedded": 0, "reused": 0, "api_inputs": 0, "unchanged": 0, "failed": 0,
              "unready_documents": {}, "files": [], "read_batches": 0}
    with ingestion_lock(engine):
        with engine.connect() as connection:
            if not connection.execute(select(tables["knowledge_bases"]).where(
                tables["knowledge_bases"].c.knowledge_id == settings.knowledge_id)).first():
                raise EmbeddingError("先にingestで対象ナレッジを取り込んでください")
            docs, members = tables["documents"], tables["knowledge_documents"]
            formats = connection.execute(select(docs.c.format).join(members).where(
                members.c.knowledge_id == settings.knowledge_id, members.c.included.is_(True)
            ).distinct()).scalars().all()
        revisions = {fmt: conversion_profile(settings, fmt)[0] for fmt in formats}
        with engine.begin() as connection:
            _insert_missing(connection, tables["embedding_profiles"],
                            {"embedding_profile_id": profile_id}, profile)
            _recover_jobs(connection)
            current = current_documents(settings.knowledge_id, revisions)
            states = dict(connection.execute(select(current.c.state, func.count()).group_by(current.c.state)).all())
            report["unready_documents"] = {state: count for state, count in states.items()
                                           if state not in {"succeeded", "partial"}}
            query = _target_query(connection, settings.knowledge_id, revisions)
            total = connection.execute(select(func.count()).select_from(query.subquery())).scalar_one()
            matching = _matching_embedding(profile_id, input_hash_sql(connection, tables["chunks"].c.search_text))
            report["unchanged"] = connection.execute(select(func.count()).select_from(
                query.where(matching).subquery())).scalar_one()
            job_id = _start_job(connection, settings.knowledge_id, "embed",
                                digest([settings.knowledge_id, profile_id, "embed"]),
                                timeout=config.timeout_seconds * 3 + 300)
            connection.execute(tables["index_jobs"].update().where(
                tables["index_jobs"].c.run_id == job_id).values(
                    target_parameters={"embedding_profile_id": profile_id, "conversion_revisions": revisions}))
        after = None
        try:
            with openai.OpenAI(api_key=config.api_key, timeout=config.timeout_seconds, max_retries=2) as client:
                while True:
                    with engine.connect() as connection:
                        page = _targets(connection, settings.knowledge_id, revisions,
                                        profile_id=profile_id, after=after, limit=config.read_batch_size)
                        cached = _cached_vectors(connection, profile_id, [row["input_hash"] for row in page]) if page else {}
                    if not page:
                        break
                    report["read_batches"] += 1
                    after = (page[-1]["generation_id"], page[-1]["chunk_id"])
                    pending, reused = {}, []
                    for target in page:
                        target["input_tokens"] = count_tokens(target["search_text"])
                        if not 1 <= target["input_tokens"] <= 8191:
                            report["failed"] += 1
                            if len(report["files"]) < 100:
                                report["files"].append({"generation_id": target["generation_id"],
                                    "chunk_id": target["chunk_id"], "error_code": "embedding_input_token_limit"})
                        elif target["input_hash"] in cached:
                            reused.append(target)
                        else:
                            pending.setdefault(target["input_hash"], []).append(target)
                    if reused:
                        _save_vectors(engine, profile_id, config.dimensions, reused,
                            [list(cached[row["input_hash"]]["embedding"]) for row in reused],
                            job_id, config.timeout_seconds)
                        report["reused"] += len(reused)
                    groups = list(pending.values())
                    offset = 0
                    while offset < len(groups):
                        batch, tokens = [], 0
                        while offset < len(groups) and len(batch) < config.batch_size:
                            group = groups[offset]
                            length = group[0]["input_tokens"]
                            if batch and tokens + length > config.batch_max_tokens:
                                break
                            batch.append(group)
                            tokens += length
                            offset += 1
                        response = client.embeddings.create(model=config.model, dimensions=config.dimensions,
                            encoding_format="float", input=[group[0]["search_text"] for group in batch])
                        vectors = _vectors(response, len(batch), config.dimensions)
                        targets, expanded = [], []
                        for group, vector in zip(batch, vectors, strict=True):
                            targets.extend(group)
                            expanded.extend([vector] * len(group))
                        _save_vectors(engine, profile_id, config.dimensions, targets, expanded,
                                      job_id, config.timeout_seconds)
                        report["embedded"] += len(targets)
                        report["api_inputs"] += len(batch)
        except Exception as error:
            report["failed"] = total - report["embedded"] - report["reused"] - report["unchanged"]
            report["error_code"] = _error_code(error)
        if report["failed"] or report["unready_documents"]:
            report["status"] = "partial" if report["embedded"] or report["reused"] or report["unchanged"] else "failed"
            if report["unready_documents"]:
                report.setdefault("error_code", "selected_generation_not_ready")
        with engine.begin() as connection:
            _finish_job(connection, job_id, report["status"], report.get("error_code"))
    write_json(settings.report_dir / f"embed-{report['run_id']}.json", report)
    return report
