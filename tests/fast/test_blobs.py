"""Blob upload and download through the API: status codes, the error codes,
the caps, the row, the crash point between rename and row, and the headers
that make a blob cacheable forever.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import httpx
import pytest

from rtlfarm import hooks
from rtlfarm.clock import DrivenClock
from rtlfarm.control.blobstore import BlobStore
from rtlfarm.db.connection import Database

################################################################################
# Helpers
################################################################################


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _headers(
    data: bytes, kind: str = "input", digest: str | None = None
) -> dict[str, str]:
    return {"X-Content-Sha256": digest or _sha(data), "X-Blob-Kind": kind}


def _rows(db: Database) -> list[tuple[str, int, int]]:
    reader = db.read()
    try:
        rows = reader.execute(
            "SELECT sha256, size, created_at_ms FROM blobs ORDER BY sha256"
        ).fetchall()
        return [(str(r[0]), int(r[1]), int(r[2])) for r in rows]
    finally:
        reader.close()


@pytest.fixture(autouse=True)
def _no_armed_hooks() -> None:
    hooks.disarm_all()


################################################################################
# Upload
################################################################################


async def test_upload_stores_the_bytes_and_records_the_row(
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    blobs: BlobStore,
    db: Database,
    clock: DrivenClock,
) -> None:
    data = b"module counter; endmodule\n"
    response = await client.post(
        "/v1/blobs", content=data, headers=as_client | _headers(data)
    )
    assert response.status_code == 201
    assert response.json() == {"sha256": _sha(data), "size": len(data)}
    assert blobs.path_for(_sha(data)).read_bytes() == data
    assert _rows(db) == [(_sha(data), len(data), clock.now_ms())]


async def test_duplicate_upload_is_200_with_one_row(
    client: httpx.AsyncClient, as_client: dict[str, str], db: Database
) -> None:
    data = b"same"
    first = await client.post(
        "/v1/blobs", content=data, headers=as_client | _headers(data)
    )
    second = await client.post(
        "/v1/blobs", content=data, headers=as_client | _headers(data)
    )
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json() == second.json()
    assert len(_rows(db)) == 1


async def test_workers_may_upload_artifacts_too(
    client: httpx.AsyncClient, as_worker: dict[str, str]
) -> None:
    data = b'{"exit_class": "PASS"}'
    response = await client.post(
        "/v1/blobs", content=data, headers=as_worker | _headers(data, "result.json")
    )
    assert response.status_code == 201


async def test_upload_needs_a_token(client: httpx.AsyncClient) -> None:
    data = b"x"
    response = await client.post("/v1/blobs", content=data, headers=_headers(data))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_digest_mismatch_is_400_and_stores_nothing(
    client: httpx.AsyncClient, as_client: dict[str, str], blobs: BlobStore, db: Database
) -> None:
    data = b"actual"
    claimed = _sha(b"claimed")
    response = await client.post(
        "/v1/blobs", content=data, headers=as_client | _headers(data, digest=claimed)
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "DIGEST_MISMATCH"
    assert error["details"] == {"expected": claimed, "actual": _sha(data)}
    assert not blobs.has(_sha(data))
    assert _rows(db) == []
    assert list((blobs.root / "tmp").iterdir()) == []


async def test_unknown_kind_is_422_with_a_pointer(
    client: httpx.AsyncClient, as_client: dict[str, str]
) -> None:
    data = b"x"
    response = await client.post(
        "/v1/blobs", content=data, headers=as_client | _headers(data, "bogus")
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION"
    assert error["details"][0]["pointer"] == "/headers/x-blob-kind"


async def test_malformed_digest_is_422_with_a_pointer(
    client: httpx.AsyncClient, as_client: dict[str, str]
) -> None:
    response = await client.post(
        "/v1/blobs",
        content=b"x",
        headers=as_client | {"X-Content-Sha256": "not-hex", "X-Blob-Kind": "input"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["pointer"] == (
        "/headers/x-content-sha256"
    )


async def test_missing_headers_are_422(
    client: httpx.AsyncClient, as_client: dict[str, str]
) -> None:
    response = await client.post("/v1/blobs", content=b"x", headers=as_client)
    assert response.status_code == 422
    pointers = {d["pointer"] for d in response.json()["error"]["details"]}
    assert pointers == {"/header/x-content-sha256", "/header/x-blob-kind"}


async def test_body_over_the_kind_cap_is_413(
    client: httpx.AsyncClient, as_client: dict[str, str], blobs: BlobStore
) -> None:
    data = b"z" * 5000  # the fixture caps an input file at 4096 bytes
    response = await client.post(
        "/v1/blobs", content=data, headers=as_client | _headers(data)
    )
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "PAYLOAD_TOO_LARGE"
    assert error["details"] == {"kind": "input", "cap": 4096}
    assert list((blobs.root / "tmp").iterdir()) == []


async def test_chunked_body_without_content_length_is_still_capped(
    client: httpx.AsyncClient, as_client: dict[str, str]
) -> None:
    sent: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        for i in range(100):
            sent.append(i)
            yield b"c" * 100

    response = await client.post(
        "/v1/blobs",
        content=body(),
        headers=as_client | {"X-Content-Sha256": "a" * 64, "X-Blob-Kind": "input"},
    )
    assert response.status_code == 413
    assert len(sent) < 100


async def test_chunked_upload_is_hashed_across_chunks(
    client: httpx.AsyncClient, as_client: dict[str, str], blobs: BlobStore
) -> None:
    pieces = [bytes([i]) * 300 for i in range(10)]
    data = b"".join(pieces)

    async def body() -> AsyncIterator[bytes]:
        for piece in pieces:
            yield piece

    response = await client.post(
        "/v1/blobs", content=body(), headers=as_client | _headers(data)
    )
    assert response.status_code == 201
    assert blobs.path_for(_sha(data)).read_bytes() == data


################################################################################
# The Crash Point Between Rename and Row
################################################################################


async def test_crash_after_rename_leaves_the_file_and_no_row_and_retry_heals(
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    blobs: BlobStore,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(hooks.ENV_VAR, "1")

    class Crash(RuntimeError):
        pass

    def crash() -> None:
        raise Crash("control plane died after the rename")

    hooks.arm("after_blob_rename_before_row", crash)
    data = b"survives the crash"
    with pytest.raises(Crash):
        await client.post("/v1/blobs", content=data, headers=as_client | _headers(data))
    assert blobs.has(_sha(data))
    assert _rows(db) == []
    hooks.disarm_all()
    retry = await client.post(
        "/v1/blobs", content=data, headers=as_client | _headers(data)
    )
    assert retry.status_code == 201
    assert [r[0] for r in _rows(db)] == [_sha(data)]


################################################################################
# Download
################################################################################


async def test_download_streams_the_bytes_with_cache_headers(
    client: httpx.AsyncClient, as_client: dict[str, str], as_worker: dict[str, str]
) -> None:
    data = b"bytes to fetch " * 100
    await client.post("/v1/blobs", content=data, headers=as_client | _headers(data))
    response = await client.get(f"/v1/blobs/{_sha(data)}", headers=as_worker)
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["ETag"] == f'"{_sha(data)}"'
    assert response.headers["Cache-Control"] == "private, immutable"
    assert response.headers["Content-Length"] == str(len(data))
    assert response.headers["Content-Type"] == "application/octet-stream"


async def test_download_of_an_unknown_digest_is_404(
    client: httpx.AsyncClient, as_client: dict[str, str]
) -> None:
    response = await client.get(f"/v1/blobs/{'f' * 64}", headers=as_client)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_download_of_a_recorded_but_missing_file_is_410(
    client: httpx.AsyncClient, as_client: dict[str, str], blobs: BlobStore
) -> None:
    data = b"will vanish"
    await client.post("/v1/blobs", content=data, headers=as_client | _headers(data))
    blobs.path_for(_sha(data)).unlink()
    response = await client.get(f"/v1/blobs/{_sha(data)}", headers=as_client)
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "BLOB_MISSING"


async def test_download_with_a_malformed_digest_is_422(
    client: httpx.AsyncClient, as_client: dict[str, str]
) -> None:
    response = await client.get("/v1/blobs/not-a-digest", headers=as_client)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION"


async def test_download_needs_a_token(
    client: httpx.AsyncClient, as_client: dict[str, str]
) -> None:
    data = b"private"
    await client.post("/v1/blobs", content=data, headers=as_client | _headers(data))
    assert (await client.get(f"/v1/blobs/{_sha(data)}")).status_code == 401
