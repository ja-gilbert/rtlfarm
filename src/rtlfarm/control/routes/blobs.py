"""Blob upload and download: one route each, for clients and workers alike.

``POST /v1/blobs`` streams the body into the store under the digest the
client claims in ``X-Content-Sha256``, capped by the kind in ``X-Blob-Kind``,
then records the ``blobs`` row. The file is renamed into place before the
row is written; the crash hook between the two is where the process-tier
test kills the control plane, and the retry that follows must find the
file and simply write the row.

``GET /v1/blobs/{sha256}`` streams the bytes with ``ETag`` set to the digest
and ``Cache-Control: private, immutable``: content-addressed bytes never
change, so a client may cache them forever.
"""

from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Header, Path, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from rtlfarm import hooks
from rtlfarm.control.app import AnyAuth, ApiError, Services, services
from rtlfarm.control.blobstore import (
    BlobTooLarge,
    DigestMismatch,
    InvalidDigest,
    UnknownBlobKind,
)

router = APIRouter(prefix="/v1/blobs", tags=["blobs"])

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_CHUNK = 1 << 16


class BlobResponse(BaseModel):
    sha256: str
    size: int


@router.post("", response_model=BlobResponse, status_code=201)
async def upload(
    request: Request,
    response: Response,
    _: AnyAuth,
    svc: Annotated[Services, Depends(services)],
    x_content_sha256: Annotated[str, Header()],
    x_blob_kind: Annotated[str, Header()],
) -> BlobResponse:
    try:
        stored = await svc.blobs.ingest(
            x_blob_kind, request.stream(), expected_sha256=x_content_sha256
        )
    except UnknownBlobKind as e:
        raise ApiError(
            422,
            "VALIDATION",
            str(e),
            [{"pointer": "/header/x-blob-kind", "message": str(e)}],
        ) from None
    except InvalidDigest as e:
        raise ApiError(
            422,
            "VALIDATION",
            str(e),
            [{"pointer": "/header/x-content-sha256", "message": str(e)}],
        ) from None
    except BlobTooLarge as e:
        raise ApiError(
            413, "PAYLOAD_TOO_LARGE", str(e), {"kind": e.kind, "cap": e.cap}
        ) from None
    except DigestMismatch as e:
        raise ApiError(
            400,
            "DIGEST_MISMATCH",
            str(e),
            {"expected": e.expected, "actual": e.actual},
        ) from None
    hooks.point("after_blob_rename_before_row")
    async with svc.db.write() as conn:
        cursor = conn.execute(
            "INSERT INTO blobs (sha256, size, created_at_ms) VALUES (?, ?, ?) "
            "ON CONFLICT (sha256) DO NOTHING",
            (stored.sha256, stored.size, svc.clock.now_ms()),
        )
        created = cursor.rowcount == 1
    response.status_code = 201 if created else 200
    return BlobResponse(sha256=stored.sha256, size=stored.size)


@router.get("/{sha256}")
async def download(
    _: AnyAuth,
    svc: Annotated[Services, Depends(services)],
    sha256: Annotated[str, Path(pattern=_DIGEST_PATTERN)],
) -> StreamingResponse:
    reader = svc.db.read()
    try:
        row = reader.execute(
            "SELECT size FROM blobs WHERE sha256 = ?", (sha256,)
        ).fetchone()
    finally:
        reader.close()
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no blob {sha256}")
    path = svc.blobs.path_for(sha256)
    if not path.is_file():
        raise ApiError(410, "BLOB_MISSING", f"blob {sha256} is recorded but absent")
    headers = {
        "ETag": f'"{sha256}"',
        "Cache-Control": "private, immutable",
        "Content-Length": str(row[0]),
    }
    return StreamingResponse(
        _file_chunks(path), media_type="application/octet-stream", headers=headers
    )


async def _file_chunks(path: pathlib.Path) -> AsyncIterator[bytes]:
    async with await anyio.open_file(path, "rb") as f:
        while chunk := await f.read(_CHUNK):
            yield chunk
