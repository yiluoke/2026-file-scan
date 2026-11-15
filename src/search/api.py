"""ローカル試作用FastAPI。本文上限・接続キー・期限はHTTP境界で検査する。"""

import asyncio
import hashlib
import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request, Security
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer
from starlette.background import BackgroundTask
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeout

from file_scan.db import tables as core
from file_scan.storage import _open_no_follow
from search import repository as repo
from search import schema as s
from search.config import async_database
from search.errors import SearchError
from search.evidence import IMAGE_ASSET_ROLES
from search.models import SearchInput, SearchRequest, SearchResponse, RetrievalResponse
from search.service import RetrievalService
from search.stub import fixed_response


DownloadMode = Literal["current", "history"]
IMAGE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/tiff": "tiff",
    "image/bmp": "bmp",
}


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
    request_model = SearchInput if config.test_mode else SearchRequest
    if config.test_mode:
        engine, service = None, None
    else:
        engine = engine or async_database(config.ingestion.database_url)
        service = RetrievalService(engine, config, embedder=embedder, allow_history=allow_history)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if engine is not None:
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
        if config.test_mode:
            return {
                "status": "ok",
                "execution_mode": "test",
                "access_control": "not_enforced",
                "database": "not_used",
            }
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
                if not await s.indexes_ready(c):
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
        if config.test_mode:
            return fixed_response(request.state.request_id, config.config_version)

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
    async def search(payload: request_model, request: Request):
        return await run_search(payload, request)

    @app.post(
        "/retrieval",
        response_model=RetrievalResponse,
        dependencies=[Security(HTTPBearer(auto_error=False))],
    )
    async def retrieval(payload: request_model, request: Request):
        result = await run_search(payload, request, standard=True)
        return {"records": result["records"]}

    async def download(publication_id, mode, *, occurrence_id=None):
        if config.test_mode or (mode == "history" and not allow_history):
            raise SearchError("SCOPE_NOT_FOUND")
        try:
            async with asyncio.timeout(config.total_timeout):
                asset, root = await download_asset(publication_id, mode, occurrence_id)
                return await verified_download(asset, root, is_image=occurrence_id is not None)
        except SearchError:
            raise
        except TimeoutError, PoolTimeout:
            raise SearchError("SEARCH_TIMEOUT", retryable=True) from None
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "57014":
                raise SearchError("SEARCH_TIMEOUT", retryable=True) from None
            raise SearchError("SEARCH_UNAVAILABLE", retryable=True) from None
        except Exception:
            raise SearchError("SEARCH_UNAVAILABLE") from None

    async def download_asset(publication_id, mode, occurrence_id):
        knowledge_id = config.ingestion.knowledge_id
        async with engine.connect() as c:
            c = await c.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            async with c.begin():
                await c.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {"timeout": str(config.statement_timeout_ms)},
                )
                catalog = await repo.catalog(c, knowledge_id)
                publication = (
                    (
                        await c.execute(
                            select(
                                s.publications.c.release_id,
                                s.publications.c.document_id,
                                s.publications.c.source_version,
                            )
                            .join(s.releases)
                            .where(
                                s.publications.c.publication_id == publication_id,
                                s.releases.c.knowledge_id == knowledge_id,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if not publication:
                    raise SearchError("SCOPE_NOT_FOUND")
                if mode == "current":
                    if not catalog["default_release_id"]:
                        raise SearchError("RELEASE_UNRESOLVED")
                    if publication["release_id"] != catalog["default_release_id"]:
                        raise SearchError("SCOPE_NOT_FOUND")
                scope = catalog | {"release_id": publication["release_id"], "search_mode": mode}
                _, eligible = repo.coverage(
                    await repo.expected(c, scope, document_id=publication["document_id"]), scope
                )
                if publication_id not in eligible:
                    raise SearchError("SCOPE_NOT_FOUND")

                asset_id = publication["source_version"]
                if occurrence_id is not None:
                    asset_id = await image_asset_id(c, publication_id, occurrence_id)
                    if asset_id is None:
                        raise SearchError("SCOPE_NOT_FOUND")
                assets = core["artifact_assets"]
                asset = (
                    (
                        await c.execute(
                            select(assets.c.asset_id, assets.c.byte_size, assets.c.mime_type).where(
                                assets.c.asset_id == asset_id,
                                assets.c.storage_status == "available",
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
        return asset, Path(root)

    @app.get(
        "/sources/{publication_id}",
        response_class=StreamingResponse,
        dependencies=[Security(HTTPBearer(auto_error=False))],
    )
    async def source(publication_id: str, mode: DownloadMode = "current"):
        return await download(publication_id, mode)

    @app.get(
        "/sources/{publication_id}/images/{occurrence_id}",
        response_class=StreamingResponse,
        dependencies=[Security(HTTPBearer(auto_error=False))],
    )
    async def image(publication_id: str, occurrence_id: str, mode: DownloadMode = "current"):
        return await download(publication_id, mode, occurrence_id=occurrence_id)

    return app


async def image_asset_id(connection, publication_id, occurrence_id):
    # 現行の取り込みテーブルは使わず、この公開コピーに凍結した出現だけを認める。
    matches = (
        await connection.execute(
            select(s.blocks.c.assets).where(
                s.blocks.c.publication_id == publication_id,
                s.blocks.c.assets.contains([{"occurrence_id": occurrence_id}]),
            )
        )
    ).scalars()
    return next(
        (
            entry["asset_id"]
            for assets in matches
            for entry in assets
            if entry["occurrence_id"] == occurrence_id and entry["asset_role"] in IMAGE_ASSET_ROLES
        ),
        None,
    )


async def verified_download(asset, root, *, is_image):
    # CASのIDからパスを導出し、任意のDB値・URL・利用者入力をパスにしない。
    digest = asset["asset_id"].removeprefix("sha256:")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise SearchError("SEARCH_UNAVAILABLE")
    path = root / "objects" / "sha256" / digest[:2] / digest

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

    media_type, filename = "application/octet-stream", "image" if is_image else "source"
    if is_image and (extension := IMAGE_EXTENSIONS.get(asset["mime_type"])):
        media_type, filename = asset["mime_type"], f"image.{extension}"
    return StreamingResponse(
        verified_stream(),
        media_type=media_type,
        background=BackgroundTask(handle.close),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
