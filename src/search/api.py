"""ローカル試作用FastAPI。本文上限・接続キー・期限はHTTP境界で検査する。"""

import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request, Security
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer
from starlette.background import BackgroundTask
from sqlalchemy import select, text

from file_scan.db import tables as core
from search import schema as s
from search.config import async_database
from search.errors import SearchError
from search.models import SearchRequest, SearchResponse, RetrievalResponse
from search.service import RetrievalService
from file_scan.storage import _open_no_follow

logger = logging.getLogger(__name__)


class RequestBoundary:
    def __init__(self, app, config):
        self.app, self.config = app, config

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        protected = scope["path"] in {"/retrieval", "/search-with-status"} or scope[
            "path"
        ].startswith("/sources/")
        try:
            headers = dict(scope["headers"])
            if protected and not hmac.compare_digest(
                headers.get(b"authorization", b""), b"Bearer " + self.config.api_key.encode()
            ):
                raise SearchError("UNAUTHENTICATED")
            if scope["path"] in {"/retrieval", "/search-with-status"}:
                if (
                    headers.get(b"content-type", b"").split(b";", 1)[0].lower()
                    != b"application/json"
                ):
                    raise SearchError("UNSUPPORTED_MEDIA_TYPE")
                if b"content-encoding" in headers:
                    raise SearchError("UNSUPPORTED_MEDIA_TYPE")
                body = bytearray()
                async with asyncio.timeout(self.config.total_timeout):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > 32768:
                            raise SearchError("REQUEST_TOO_LARGE")
                        if not message.get("more_body", False):
                            break
                    consumed = False

                    async def replay():
                        nonlocal consumed
                        if not consumed:
                            consumed = True
                            return {"type": "http.request", "body": bytes(body), "more_body": False}
                        return await receive()

                    await self.app(scope, replay, send)
                return
            await self.app(scope, receive, send)
        except asyncio.CancelledError:
            if not scope.get("state", {}).get("client_disconnected"):
                raise
        except SearchError as error:
            await JSONResponse(error.response(request_id), status_code=error.status)(
                scope, receive, send
            )
        except TimeoutError:
            error = SearchError("SEARCH_TIMEOUT", retryable=True)
            await JSONResponse(error.response(request_id), status_code=error.status)(
                scope, receive, send
            )


def create_app(config, *, engine=None, embedder=None, allow_history=False):
    engine = engine or async_database(config.ingestion.database_url)
    service = RetrievalService(engine, config, embedder=embedder, allow_history=allow_history)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="File Scan Search API", version="1", lifespan=lifespan)
    app.add_middleware(RequestBoundary, config=config)
    app.state.service = service

    @app.exception_handler(SearchError)
    async def search_error(request, error):
        return JSONResponse(
            error.response(request.state.request_id),
            status_code=error.status,
            headers={"Retry-After": "1"} if error.code == "RATE_LIMITED" else None,
        )

    @app.exception_handler(RequestValidationError)
    async def input_error(request, error):
        safe = SearchError("INVALID_REQUEST")
        return JSONResponse(safe.response(request.state.request_id), status_code=safe.status)

    @app.exception_handler(ResponseValidationError)
    async def output_error(request, error):
        safe = SearchError("INTERNAL_ERROR")
        return JSONResponse(safe.response(request.state.request_id), status_code=safe.status)

    @app.get("/healthz")
    async def health(request: Request):
        try:
            async with asyncio.timeout(2), engine.connect() as c:
                if (await c.execute(select(s.versions.c.version))).scalar_one() != 1:
                    raise SearchError("SEARCH_UNAVAILABLE")
                extensions = dict(
                    (
                        await c.execute(
                            text(
                                "SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector','pgroonga')"
                            )
                        )
                    ).all()
                )
                if set(extensions) != {"vector", "pgroonga"}:
                    raise SearchError("SEARCH_UNAVAILABLE")
            return {
                "status": "ok",
                "execution_mode": "local_prototype",
                "access_control": "not_enforced",
                "extensions": extensions,
            }
        except SearchError:
            raise
        except Exception:
            raise SearchError("SEARCH_UNAVAILABLE", retryable=True) from None

    async def run_search(payload, request, *, standard=False):
        # ASGIの切断通知と検索処理を競合させ、切断時もDB待機・外部要求を取り消す。
        async def disconnected():
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    request.scope["state"]["client_disconnected"] = True
                    return

        work = asyncio.create_task(
            service.search(payload, request.state.request_id, standard=standard)
        )
        monitor = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({work, monitor}, return_when=asyncio.FIRST_COMPLETED)
            if monitor in done and work not in done:
                raise asyncio.CancelledError
            return await work
        except SearchError, asyncio.CancelledError:
            raise
        except Exception:
            raise SearchError("INTERNAL_ERROR") from None
        finally:
            for task in (work, monitor):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, monitor, return_exceptions=True)

    @app.post(
        "/search-with-status",
        response_model=SearchResponse,
        dependencies=[Security(HTTPBearer(auto_error=False))],
    )
    async def search(payload: SearchRequest, request: Request):
        return await run_search(payload, request)

    @app.post(
        "/retrieval",
        response_model=RetrievalResponse,
        dependencies=[Security(HTTPBearer(auto_error=False))],
    )
    async def retrieval(payload: SearchRequest, request: Request):
        result = await run_search(payload, request, standard=True)
        return {"records": result["records"]}

    @app.get("/sources/{publication_id}", dependencies=[Security(HTTPBearer(auto_error=False))])
    async def source(publication_id: str, request: Request):
        try:
            async with engine.connect() as c:
                catalog = await service.scope(
                    c,
                    SearchRequest.model_validate(
                        {
                            "knowledge_id": config.ingestion.knowledge_id,
                            "query": "source",
                            "retrieval_setting": {"top_k": 1, "score_threshold": 0},
                        }
                    ),
                )
                publication = (
                    (
                        await c.execute(
                            select(s.publications)
                            .join(s.releases)
                            .where(
                                s.publications.c.publication_id == publication_id,
                                s.releases.c.knowledge_id == config.ingestion.knowledge_id,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if not publication:
                    raise SearchError("SCOPE_NOT_FOUND")
                if publication["release_id"] != catalog["release_id"]:
                    if not allow_history:
                        raise SearchError("SCOPE_NOT_FOUND")
                    catalog = catalog | {
                        "release_id": publication["release_id"],
                        "search_mode": "history",
                    }
                from search.repository import coverage, expected

                _, eligible = coverage(await expected(c, catalog), catalog)
                if publication_id not in eligible:
                    raise SearchError("SCOPE_NOT_FOUND")
                asset = (
                    (
                        await c.execute(
                            select(core["artifact_assets"]).where(
                                core["artifact_assets"].c.asset_id == publication["source_version"],
                                core["artifact_assets"].c.storage_status == "available",
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                root = (
                    await c.execute(select(core["ingestion_settings"].c.storage_root))
                ).scalar_one()
            if Path(root) != config.ingestion.storage_dir.resolve():
                raise SearchError("SEARCH_UNAVAILABLE")
            # CASのIDからパスを導出し、任意のDB値・URL・利用者入力をパスにしない。
            digest = asset["asset_id"].removeprefix("sha256:")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise SearchError("SEARCH_UNAVAILABLE")
            path = Path(root) / "objects" / "sha256" / digest[:2] / digest

            def open_verified():
                handle = _open_no_follow(path)
                try:
                    checksum, size = hashlib.sha256(), 0
                    while data := handle.read(1024 * 1024):
                        checksum.update(data)
                        size += len(data)
                    if checksum.hexdigest() != digest or size != asset["byte_size"]:
                        raise OSError("stored_asset_integrity_failed")
                    handle.seek(0)
                    return handle
                except BaseException:
                    handle.close()
                    raise

            opening = asyncio.create_task(asyncio.to_thread(open_verified))
            try:
                handle = await asyncio.shield(opening)
            except asyncio.CancelledError:

                def close_cancelled(task):
                    if not task.cancelled() and task.exception() is None:
                        task.result().close()

                opening.add_done_callback(close_cancelled)
                raise

            def verified_stream():
                try:
                    while data := handle.read(65536):
                        yield data
                finally:
                    handle.close()

            return StreamingResponse(
                verified_stream(),
                media_type="application/octet-stream",
                background=BackgroundTask(handle.close),
                headers={
                    "Content-Disposition": 'attachment; filename="source"',
                    "Cache-Control": "no-store",
                },
            )
        except SearchError:
            raise
        except Exception:
            raise SearchError("SEARCH_UNAVAILABLE") from None

    return app
