"""The application skeleton: health and readiness, the error envelope, request
ids, bearer auth on client and worker routes, and the loopback guard.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from rtlfarm.clock import DrivenClock
from rtlfarm.config import Config
from rtlfarm.control import app as control_app
from rtlfarm.control.app import ClientAuth, WorkerAuth
from rtlfarm.control.blobstore import BlobStore

################################################################################
# Health and Readiness
################################################################################


async def test_healthz_needs_no_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_is_ready_when_migrated_and_writable(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["migrated"] is True
    assert body["checks"]["blobs_writable"] is True
    assert body["checks"]["tick_recent"] is None
    assert body["checks"]["lock_held"] is None


async def test_readyz_fails_when_the_blob_volume_is_not_writable(
    client: httpx.AsyncClient, blobs: BlobStore
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    tmp = blobs.root / "tmp"
    tmp.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        response = await client.get("/readyz")
    finally:
        tmp.chmod(stat.S_IRWXU)
    assert response.status_code == 503
    assert response.json()["checks"]["blobs_writable"] is False


async def test_readyz_fails_before_migration(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    app.state.services.readiness.migrated = False
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["migrated"] is False


async def test_readyz_tracks_the_last_tick_against_the_driven_clock(
    app: FastAPI, client: httpx.AsyncClient, clock: DrivenClock, config: Config
) -> None:
    readiness = app.state.services.readiness
    readiness.last_tick_ms = clock.now_ms()
    assert (await client.get("/readyz")).json()["checks"]["tick_recent"] is True
    clock.advance(
        int(config.timing.readyz_tick_factor * config.timing.tick_s * 1000) + 1
    )
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["tick_recent"] is False


################################################################################
# Envelope and Request Ids
################################################################################


async def test_unknown_route_uses_the_error_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/nothing-here")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "NOT_FOUND", "message": "Not Found", "details": None}
    }


async def test_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-Id": "req-123"})
    assert response.headers["X-Request-Id"] == "req-123"


async def test_request_id_is_generated_when_absent(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    generated = response.headers["X-Request-Id"]
    assert len(generated) == 32
    other = (await client.get("/healthz")).headers["X-Request-Id"]
    assert other != generated


async def test_validation_errors_carry_json_pointers(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    @app.get("/v1/_probe/{n}")
    async def probe(n: int) -> dict[str, int]:
        return {"n": n}

    response = await client.get("/v1/_probe/not-a-number")
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION"
    assert body["details"][0]["pointer"] == "/path/n"


################################################################################
# Bearer Auth
################################################################################


def _add_probes(app: FastAPI) -> None:
    @app.get("/v1/_client")
    async def client_probe(_: ClientAuth) -> dict[str, str]:
        return {"who": "client"}

    @app.get("/v1/_worker")
    async def worker_probe(_: WorkerAuth) -> dict[str, str]:
        return {"who": "worker"}


async def test_client_routes_accept_only_the_client_token(
    app: FastAPI,
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    as_worker: dict[str, str],
) -> None:
    _add_probes(app)
    assert (await client.get("/v1/_client", headers=as_client)).status_code == 200
    for headers in ({}, as_worker, {"Authorization": "Bearer wrong"}):
        response = await client.get("/v1/_client", headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_worker_routes_accept_only_the_worker_token(
    app: FastAPI,
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    as_worker: dict[str, str],
) -> None:
    _add_probes(app)
    assert (await client.get("/v1/_worker", headers=as_worker)).status_code == 200
    assert (await client.get("/v1/_worker", headers=as_client)).status_code == 401
    assert (await client.get("/v1/_worker")).status_code == 401


async def test_malformed_authorization_header_is_unauthorized(
    app: FastAPI, client: httpx.AsyncClient, tokens: tuple[str, str]
) -> None:
    _add_probes(app)
    client_token = tokens[0]
    for value in ("Basic abc", "Bearer", client_token, f"bearer  {client_token}"):
        response = await client.get("/v1/_client", headers={"Authorization": value})
        assert response.status_code == 401, value


async def test_lowercase_bearer_scheme_is_accepted(
    app: FastAPI, client: httpx.AsyncClient, tokens: tuple[str, str]
) -> None:
    _add_probes(app)
    headers = {"Authorization": f"bearer {tokens[1]}"}
    assert (await client.get("/v1/_worker", headers=headers)).status_code == 200


async def test_auth_is_disabled_only_when_both_tokens_are_unset(
    tmp_path: Path, clock: DrivenClock
) -> None:
    from rtlfarm.db.connection import Database

    db = Database(tmp_path / "open.db", synchronous="OFF")
    db.migrate(clock)
    try:
        for config, client_ok, worker_ok in (
            (Config(), True, True),
            (Config(client_token="c"), False, False),
            (Config(worker_token="w"), False, False),
        ):
            app = control_app.create_app(
                config, db, BlobStore(tmp_path / "b", config.blobs), clock
            )
            _add_probes(app)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
                assert ((await c.get("/v1/_client")).status_code == 200) is client_ok
                assert ((await c.get("/v1/_worker")).status_code == 200) is worker_ok
    finally:
        db.close()


################################################################################
# The Loopback Guard
################################################################################


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.0.0.9"])
def test_loopback_bind_is_allowed_without_tokens(host: str) -> None:
    control_app.assert_bind_allowed(Config(), host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "192.168.1.2"])
def test_non_loopback_bind_without_tokens_is_refused(host: str) -> None:
    with pytest.raises(control_app.InsecureBind, match="not loopback"):
        control_app.assert_bind_allowed(Config(), host)


def test_non_loopback_bind_is_allowed_with_a_token_or_the_override() -> None:
    control_app.assert_bind_allowed(Config(client_token="c"), "0.0.0.0")
    control_app.assert_bind_allowed(Config(insecure_bind=True), "0.0.0.0")
