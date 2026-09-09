"""The content-addressed blob store: streaming ingest under a cap, digest
verification, atomic placement, duplicates, and the startup sweep.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import os
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

import pytest

from rtlfarm.config import BlobsConfig
from rtlfarm.control import blobstore
from rtlfarm.control.blobstore import (
    BlobStore,
    BlobTooLarge,
    DigestMismatch,
    InvalidDigest,
    UnknownBlobKind,
)

################################################################################
# Helpers
################################################################################


CAPS = BlobsConfig(
    log_bytes=100,
    input_file_bytes=1_000,
    input_job_bytes=5_000,
    diagnostics_bytes=200,
    deps_bytes=300,
    compiled_bytes=4 * 1024 * 1024,
    result_bytes=400,
    waveform_bytes=500,
    coverage_bytes=600,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _chunks(pieces: Iterable[bytes]) -> AsyncIterator[bytes]:
    for piece in pieces:
        yield piece


def _ingest(
    store: BlobStore,
    kind: str,
    pieces: Iterable[bytes],
    digest: str | None = None,
) -> blobstore.IngestResult:
    data = b"".join(pieces)
    return asyncio.run(
        store.ingest(kind, _chunks(pieces), expected_sha256=digest or _sha(data))
    )


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs", CAPS)


################################################################################
# Layout
################################################################################


def test_store_creates_its_directories(tmp_path: Path) -> None:
    BlobStore(tmp_path / "blobs", CAPS)
    for name in ("sha256", "tmp", "trash"):
        assert (tmp_path / "blobs" / name).is_dir()


def test_path_is_sharded_by_the_first_two_hex_characters(store: BlobStore) -> None:
    digest = "ab" + "c" * 62
    assert store.path_for(digest) == store.root / "sha256" / "ab" / digest


# The blob kinds of spec §13.2 and the BlobsConfig field that caps each one.
KINDS_AND_CAP_FIELDS = {
    "log.stdout": "log_bytes",
    "log.stderr": "log_bytes",
    "input": "input_file_bytes",
    "diagnostics.json": "diagnostics_bytes",
    "deps.txt": "deps_bytes",
    "compiled": "compiled_bytes",
    "result.json": "result_bytes",
    "waveform.fst": "waveform_bytes",
    "coverage.dat": "coverage_bytes",
    "coverage.info": "coverage_bytes",
}


def test_every_kind_of_the_spec_is_capped_by_its_own_config_field() -> None:
    """Exactly the spec's kinds exist, and raising one field raises the cap of
    exactly the kinds it names, so an operator's override reaches the right
    blobs. Anything else is refused by name."""
    assert set(blobstore.KIND_CAPS) == set(KINDS_AND_CAP_FIELDS)
    for kind, field in KINDS_AND_CAP_FIELDS.items():
        raised = dataclasses.replace(CAPS, **{field: 7_777})
        assert blobstore.cap_for(kind, raised) == 7_777
    with pytest.raises(UnknownBlobKind, match="bogus"):
        blobstore.cap_for("bogus", CAPS)


################################################################################
# Ingest
################################################################################


@pytest.mark.parametrize(
    ("data", "chunk"),
    [(b"module counter; endmodule\n", 10), (os.urandom(3 * 1024 * 1024 + 123), 65_536)],
    ids=["two-pieces", "three-megabytes-in-64k-pieces"],
)
def test_ingest_places_the_bytes_under_their_digest(
    store: BlobStore, data: bytes, chunk: int
) -> None:
    pieces = [data[i : i + chunk] for i in range(0, len(data), chunk)]
    result = _ingest(store, "compiled", pieces)
    assert result == blobstore.IngestResult(_sha(data), len(data), created=True)
    assert store.path_for(result.sha256).read_bytes() == data
    assert store.has(result.sha256)
    assert store.size(result.sha256) == len(data)
    assert list((store.root / "tmp").iterdir()) == []


def test_duplicate_ingest_keeps_the_first_copy_and_reports_it(
    store: BlobStore,
) -> None:
    data = b"same bytes"
    first = _ingest(store, "input", [data])
    path = store.path_for(first.sha256)
    before = path.stat().st_ino
    second = _ingest(store, "input", [data])
    assert second == blobstore.IngestResult(first.sha256, len(data), created=False)
    assert path.stat().st_ino == before
    assert list((store.root / "tmp").iterdir()) == []


def test_empty_blob_is_allowed(store: BlobStore) -> None:
    result = _ingest(store, "result.json", [])
    assert result.size == 0
    assert result.sha256 == _sha(b"")
    assert store.path_for(result.sha256).read_bytes() == b""


def test_digest_mismatch_leaves_nothing_behind(store: BlobStore) -> None:
    data = b"actual bytes"
    with pytest.raises(DigestMismatch) as info:
        _ingest(store, "input", [data], digest=_sha(b"claimed bytes"))
    assert info.value.actual == _sha(data)
    assert info.value.expected == _sha(b"claimed bytes")
    assert not store.has(_sha(data))
    assert not store.has(_sha(b"claimed bytes"))
    assert list((store.root / "tmp").iterdir()) == []


def test_malformed_digest_is_rejected_before_reading(store: BlobStore) -> None:
    consumed: list[bytes] = []

    async def spy() -> AsyncIterator[bytes]:
        consumed.append(b"x")
        yield b"x"

    with pytest.raises(InvalidDigest):
        asyncio.run(store.ingest("input", spy(), expected_sha256="SHA256:" + "a" * 57))
    assert consumed == []


def test_unknown_kind_is_rejected_before_reading(store: BlobStore) -> None:
    consumed: list[bytes] = []

    async def spy() -> AsyncIterator[bytes]:
        consumed.append(b"x")
        yield b"x"

    with pytest.raises(UnknownBlobKind):
        asyncio.run(store.ingest("bogus", spy(), expected_sha256="a" * 64))
    assert consumed == []


def test_cap_is_enforced_while_streaming(store: BlobStore) -> None:
    """The upload is cut off at the first chunk that crosses the cap, not at
    the end, so a body without a Content-Length cannot exhaust the disk."""
    yielded: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        for i in range(10):
            yielded.append(i)
            yield b"x" * 30

    with pytest.raises(BlobTooLarge) as info:
        asyncio.run(store.ingest("log.stdout", body(), expected_sha256="a" * 64))
    assert (info.value.kind, info.value.cap) == ("log.stdout", 100)
    assert yielded == [0, 1, 2, 3]
    assert list((store.root / "tmp").iterdir()) == []


def test_exactly_at_the_cap_is_accepted(store: BlobStore) -> None:
    data = b"y" * 100
    assert _ingest(store, "log.stderr", [data]).size == 100
    with pytest.raises(BlobTooLarge):
        _ingest(store, "log.stderr", [data + b"z"])


def test_fsync_mode_still_stores_correctly(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "blobs", CAPS, fsync=True)
    data = b"durable"
    result = _ingest(store, "input", [data])
    assert store.path_for(result.sha256).read_bytes() == data


################################################################################
# The Startup Sweep
################################################################################


def test_sweep_removes_only_stale_uploads(store: BlobStore) -> None:
    tmp = store.root / "tmp"
    stale = tmp / "stale"
    fresh = tmp / "fresh"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"new")
    now = 1_000_000.0
    os.utime(stale, (now - 500, now - 500))
    os.utime(fresh, (now - 10, now - 10))
    kept = _ingest(store, "input", [b"kept"])
    removed = store.sweep_tmp(older_than_s=300, now_s=now)
    assert removed == [stale]
    assert not stale.exists()
    assert fresh.exists()
    assert store.has(kept.sha256)
