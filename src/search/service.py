"""共通検索サービス。外部Embedding中はDB接続を保持しない。"""

import asyncio
import logging
import math
import time
from uuid import uuid4

import openai
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError, TimeoutError as PoolTimeout

from file_scan.db import tables as core
from search import repository as repo, schema as s
from search.errors import SearchError
from search.evidence import assemble, fit_response
from search.ranking import fuse

logger = logging.getLogger(__name__)


def validate_vector(vector, dimensions):
    if (
        len(vector) != dimensions
        or not any(vector)
        or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in vector
        )
    ):
        raise SearchError("SEARCH_UNAVAILABLE")
    return vector


class QueryEmbedding:
    def __init__(self, config):
        self.config = config

    async def __call__(self, query, profile):
        config = self.config.ingestion.embedding
        if (
            not config.enabled
            or not config.api_key
            or any(
                marker in config.api_key.lower()
                for marker in ("placeholder", "replace-me", "dummy", "your-key")
            )
        ):
            raise SearchError("SEARCH_UNAVAILABLE")
        if profile["provider"] != "openai" or profile["input_version"] != "search_text-v1":
            raise SearchError("SEARCH_UNAVAILABLE")
        try:
            async with openai.AsyncOpenAI(
                api_key=config.api_key, timeout=self.config.embedding_timeout, max_retries=0
            ) as client:
                response = await client.embeddings.create(
                    model=profile["model"],
                    dimensions=profile["dimensions"],
                    encoding_format="float",
                    input=[query],
                )
            if len(response.data) != 1 or response.data[0].index != 0:
                raise SearchError("SEARCH_UNAVAILABLE")
            return validate_vector(response.data[0].embedding, profile["dimensions"])
        except openai.AuthenticationError:
            raise SearchError("SEARCH_UNAVAILABLE") from None
        except openai.APIError:
            raise SearchError("SEARCH_UNAVAILABLE", retryable=True) from None


class RetrievalService:
    def __init__(self, engine, config, *, embedder=None, allow_history=False):
        self.engine, self.config = engine, config
        self.embedder = embedder or QueryEmbedding(config)
        self.allow_history = allow_history
        self.slots = asyncio.Semaphore(4)

    async def scope(self, connection, request):
        return await repo.resolve(
            connection,
            request,
            self.config.ingestion.knowledge_id,
            allow_history=self.allow_history,
        )

    async def search(self, request, request_id, *, standard=False):
        started = time.monotonic()
        result = None
        error = None
        acquired = False
        try:
            async with asyncio.timeout(self.config.total_timeout):
                try:
                    await asyncio.wait_for(self.slots.acquire(), timeout=1)
                except TimeoutError:
                    raise SearchError("RATE_LIMITED", retryable=True) from None
                acquired = True
                result = await self._search(request, request_id)
                if standard and result["retrieval_status"]["coverage_state"] != "complete":
                    raise SearchError("COVERAGE_NOT_COMPLETE")
                if standard and result["retrieval_status"]["result_state"] == "budget_exhausted":
                    raise SearchError("RESULT_BUDGET_EXHAUSTED")
                return result
        except asyncio.CancelledError:
            error = "REQUEST_CANCELLED"
            raise
        except SearchError as exc:
            error = exc.code
            raise
        except TimeoutError, PoolTimeout:
            error = "SEARCH_TIMEOUT"
            raise SearchError("SEARCH_TIMEOUT", retryable=True) from None
        except SQLAlchemyError as exc:
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
            error = "SEARCH_TIMEOUT" if sqlstate == "57014" else "SEARCH_UNAVAILABLE"
            raise SearchError(error, retryable=True) from None
        finally:
            if acquired:
                self.slots.release()
            # 本文・質問・キー・生SQLを保存しない。監視障害は応答を失敗にしない。
            if result or error:
                try:
                    async with asyncio.timeout(0.5):
                        status = result["retrieval_status"] if result else {}
                        summary = {k: v for k, v in status.items() if k not in {"message"}}
                        async with self.engine.begin() as c:
                            await c.execute(
                                s.events.insert().values(
                                    request_id=request_id,
                                    knowledge_id=request.knowledge_id if result else None,
                                    release_id=status.get("resolved_release_id"),
                                    snapshot_id=status.get("snapshot_id"),
                                    result_status="rejected"
                                    if error
                                    in {
                                        "COVERAGE_NOT_COMPLETE",
                                        "RESULT_BUDGET_EXHAUSTED",
                                        "SCOPE_NOT_FOUND",
                                        "INVALID_REQUEST",
                                        "RATE_LIMITED",
                                    }
                                    else "failed"
                                    if error
                                    else "success"
                                    if result["records"]
                                    else "no_results",
                                    duration_ms=int((time.monotonic() - started) * 1000),
                                    summary=summary,
                                    error_code=error,
                                )
                            )
                except Exception:
                    logger.warning("search_event_write_failed")

    async def _search(self, request, request_id):
        async with self.engine.connect() as c:
            await c.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": str(self.config.statement_timeout_ms)},
            )
            before = await self.scope(c, request)
            profile = (
                (
                    await c.execute(
                        select(core["embedding_profiles"]).where(
                            core["embedding_profiles"].c.embedding_profile_id
                            == before["embedding_profile_id"]
                        )
                    )
                )
                .mappings()
                .one()
            )
            planned = (
                await c.execute(
                    select(func.count())
                    .select_from(s.bindings)
                    .where(
                        s.bindings.c.release_id == before["release_id"],
                        s.bindings.c.publication_id.is_not(None),
                    )
                )
            ).scalar_one()
        vector = None
        if planned:
            async with asyncio.timeout(self.config.embedding_timeout):
                vector = validate_vector(
                    await self.embedder(request.query, dict(profile)), profile["dimensions"]
                )
        async with self.engine.connect() as c:
            c = await c.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            async with c.begin():
                await c.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {"timeout": str(self.config.statement_timeout_ms)},
                )
                scope = await self.scope(c, request)
                if (
                    scope["revision"] != before["revision"]
                    or scope["embedding_profile_id"] != profile["embedding_profile_id"]
                ):
                    raise SearchError("SEARCH_UNAVAILABLE", retryable=True)
                counts, eligible = repo.coverage(await repo.expected(c, scope), scope)
                if eligible:
                    counts["index_pending_documents"] = (
                        await c.execute(
                            select(func.count(func.distinct(s.publications.c.document_id)))
                            .select_from(s.chunks.join(s.publications))
                            .where(
                                s.chunks.c.publication_id.in_(eligible),
                                s.chunks.c.embedding.is_(None),
                            )
                        )
                    ).scalar_one()
                state = (
                    "unknown"
                    if scope["scan_status"] != "succeeded" or counts["unknown_documents"]
                    else ("incomplete" if any(counts.values()) else "complete")
                )
                ranked, rows = [], {}
                if eligible:
                    if vector is None:
                        raise SearchError("SEARCH_UNAVAILABLE", retryable=True)
                    ranked = fuse(
                        await repo.lanes(
                            c,
                            request.query,
                            vector,
                            eligible,
                            request.filters(),
                            self.config.candidate_limit,
                        )
                    )
                    rows = await repo.hydrate(c, [key for key, _ in ranked])
                records, reasons, result_state = await assemble(
                    c, ranked, rows, scope["release_id"], request.retrieval_setting, self.config
                )
                status = {
                    "snapshot_id": str(uuid4()),
                    "search_mode": scope["search_mode"],
                    "resolved_release_id": scope["release_id"],
                    "catalog_revision": scope["catalog_revision"],
                    "publication_revision": scope["revision"],
                    "policy_revision": None,
                    "access_control": "not_enforced",
                    "execution_mode": "local_prototype",
                    "embedding_profile_id": profile["embedding_profile_id"],
                    "search_config_version": self.config.config_version,
                    "coverage_scope": "configured_knowledge_release",
                    "coverage_state": state,
                    **(
                        {k: None for k in counts} if scope["scan_status"] != "succeeded" else counts
                    ),
                    "result_state": result_state,
                    "returned_records": len(records),
                    "limit_reasons": reasons,
                    "message": "登録範囲の準備が完了しています。"
                    if state == "complete"
                    else "検索対象に未準備または未確認の資料があります。取得した根拠の範囲で回答してください。",
                }
                result = fit_response(
                    {
                        "api_version": "1",
                        "request_id": request_id,
                        "records": records,
                        "retrieval_status": status,
                    },
                    self.config.response_bytes,
                )
        # 別の最新読み取りで採用切替・台帳変更を検出し、途中の結果を返さない。
        async with self.engine.connect() as c:
            await c.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": str(self.config.statement_timeout_ms)},
            )
            latest = await self.scope(c, request)
            if any(
                latest[k] != scope[k]
                for k in ("revision", "catalog_revision", "release_id", "scan_status")
            ):
                raise SearchError("SEARCH_UNAVAILABLE", retryable=True)
            checked_counts, checked_ids = repo.coverage(await repo.expected(c, latest), latest)
            if sorted(checked_ids) != sorted(eligible) or any(
                checked_counts[k] != counts[k]
                for k in checked_counts
                if k != "index_pending_documents"
            ):
                raise SearchError("SEARCH_UNAVAILABLE", retryable=True)
        return result
