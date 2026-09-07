"""The control-plane application: one FastAPI app over one database, one blob
store and one clock.

Everything a route needs is built by the caller and attached to the app's
state, so tests construct the app in-process with a temporary database and a
driven clock, and ``rtlfarm control run`` constructs it once at startup.

Conventions every route follows:

- errors are ``{"error": {"code", "message", "details"}}`` with a stable code;
- ``X-Request-Id`` is echoed when the client sends one and generated when not;
- client routes require the client token, worker routes the worker token,
  compared in constant time; when neither token is configured, auth is off
  and the process may only bind to loopback unless told otherwise.
"""

from __future__ import annotations

import hmac
import ipaddress
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from rtlfarm.clock import Clock
from rtlfarm.config import Config
from rtlfarm.control.blobstore import BlobStore
from rtlfarm.db.connection import Database

REQUEST_ID_HEADER = "X-Request-Id"

_HTTP_CODES: dict[int, str] = {
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    413: "PAYLOAD_TOO_LARGE",
    422: "VALIDATION",
}


class ApiError(Exception):
    """An error with a stable machine-readable code, rendered as the envelope."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: object = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class InsecureBind(RuntimeError):
    """Auth is disabled and the bind address is not loopback."""


@dataclass
class Readiness:
    """What ``/readyz`` reports; later components update their own fields."""

    migrated: bool = False
    last_tick_ms: int | None = None
    lock_held: bool | None = None


@dataclass(frozen=True)
class Services:
    config: Config
    db: Database
    blobs: BlobStore
    clock: Clock
    readiness: Readiness


def create_app(config: Config, db: Database, blobs: BlobStore, clock: Clock) -> FastAPI:
    app = FastAPI(title="rtlfarm", version="1", docs_url=None, redoc_url=None)
    app.state.services = Services(config, db, blobs, clock, Readiness())
    app.middleware("http")(_request_id)
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    from rtlfarm.control.routes import infra

    app.include_router(infra.router)
    return app


def services(request: Request) -> Services:
    result: Services = request.app.state.services
    return result


def auth_enabled(config: Config) -> bool:
    return config.client_token is not None or config.worker_token is not None


def assert_bind_allowed(config: Config, host: str) -> None:
    """Refuse a non-loopback bind while auth is disabled, unless overridden."""
    if auth_enabled(config) or config.insecure_bind:
        return
    if host in ("localhost",) or ipaddress.ip_address(host).is_loopback:
        return
    raise InsecureBind(
        f"auth is disabled (no tokens configured) and {host!r} is not loopback; "
        "set RTLFARM_INSECURE_BIND=1 to allow it"
    )


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _check(request: Request, expected: str | None) -> None:
    config = services(request).config
    if not auth_enabled(config):
        return
    presented = _bearer(request)
    if expected is None or presented is None:
        raise ApiError(401, "UNAUTHORIZED", "a bearer token is required")
    if not hmac.compare_digest(presented.encode(), expected.encode()):
        raise ApiError(401, "UNAUTHORIZED", "the bearer token is not valid")


def client_auth(request: Request) -> None:
    """Dependency for client routes."""
    _check(request, services(request).config.client_token)


def worker_auth(request: Request) -> None:
    """Dependency for worker routes."""
    _check(request, services(request).config.worker_token)


ClientAuth = Annotated[None, Depends(client_auth)]
WorkerAuth = Annotated[None, Depends(worker_auth)]


def _envelope(status: int, code: str, message: str, details: object) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details}},
    )


async def _api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return _envelope(exc.status, exc.code, exc.message, exc.details)


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    code = _HTTP_CODES.get(exc.status_code, "ERROR")
    return _envelope(exc.status_code, code, str(exc.detail), None)


async def _validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    details = [
        {"pointer": "/" + "/".join(str(p) for p in err["loc"]), "message": err["msg"]}
        for err in exc.errors()
    ]
    return _envelope(422, "VALIDATION", "the request is not valid", details)


async def _request_id(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response
