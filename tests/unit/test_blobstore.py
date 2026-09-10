"""The content-addressed blob store: what no route reaches directly. Ingest,
caps, digest verification and duplicates are driven through ``POST /v1/blobs``
in ``fast/test_blobs``; here are the on-disk layout, the cap wiring, and the
scope of the startup sweep.
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


def test_path_is_sharded_by_the_first_two_hex_characters(store: BlobStore) -> None:
    """The layout is the store's contract with itself: a self-consistent
    change would orphan every blob already on disk."""
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


def test_ingest_places_the_bytes_under_their_digest(store: BlobStore) -> None:
    """A store that hashes every chunk but writes only some of them files wrong
    bytes under the right digest, and nothing downstream can ever tell."""
    data = b"module counter; endmodule\n"
    pieces = [data[i : i + 10] for i in range(0, len(data), 10)]
    result = _ingest(store, "compiled", pieces)
    assert result == blobstore.IngestResult(_sha(data), len(data), created=True)
    assert store.path_for(result.sha256).read_bytes() == data
    assert list((store.root / "tmp").iterdir()) == []


################################################################################
# The Startup Sweep
################################################################################


def test_the_sweep_removes_no_stored_blob_however_old(store: BlobStore) -> None:
    """The sweep is scoped to ``tmp/``; a walk of the whole store would delete
    the content-addressed blobs at every boot. The age boundary itself is
    ``fast/test_startup``'s subject."""
    stored = _ingest(store, "input", [b"kept"])
    stale = store.root / "tmp" / "stale"
    stale.write_bytes(b"old")
    now = 1_000_000.0
    os.utime(store.path_for(stored.sha256), (now - 500, now - 500))
    os.utime(stale, (now - 500, now - 500))
    removed = store.sweep_tmp(older_than_s=300, now_s=now)
    assert removed == [stale]
    assert store.has(stored.sha256)
