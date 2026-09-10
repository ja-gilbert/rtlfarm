"""The fast tier: the control plane in-process behind an ASGI transport, a
real SQLite file in a temporary directory, and a driven clock.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from rtlfarm.clock import DrivenClock
from rtlfarm.config import BlobsConfig, Config
from rtlfarm.control.app import create_app
from rtlfarm.control.blobstore import BlobStore
from rtlfarm.db.connection import Database

CLIENT_TOKEN = "client-secret"
WORKER_TOKEN = "worker-secret"


@pytest.fixture
def clock() -> DrivenClock:
    return DrivenClock(start_ms=1_700_000_000_000)


@pytest.fixture
def config() -> Config:
    return Config(
        blobs=BlobsConfig(
            input_file_bytes=4096, input_job_bytes=16_384, log_bytes=1024
        ),
        client_token=CLIENT_TOKEN,
        worker_token=WORKER_TOKEN,
    )


@pytest.fixture
def db(tmp_path: Path, clock: DrivenClock) -> Iterator[Database]:
    database = Database(tmp_path / "rtlfarm.db", synchronous="OFF")
    database.migrate(clock)
    yield database
    database.close()


@pytest.fixture
def blobs(tmp_path: Path, config: Config) -> BlobStore:
    return BlobStore(tmp_path / "blobs", config.blobs)


@pytest.fixture
def app(config: Config, db: Database, blobs: BlobStore, clock: DrivenClock) -> FastAPI:
    application = create_app(config, db, blobs, clock)
    application.state.services.readiness.migrated = True
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://farm") as c:
        yield c


@pytest.fixture
def as_client() -> dict[str, str]:
    """Headers that present the client token."""
    return {"Authorization": f"Bearer {CLIENT_TOKEN}"}


@pytest.fixture
def as_worker() -> dict[str, str]:
    """Headers that present the worker token."""
    return {"Authorization": f"Bearer {WORKER_TOKEN}"}
