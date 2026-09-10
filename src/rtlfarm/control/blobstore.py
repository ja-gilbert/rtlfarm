"""The content-addressed blob store on the control-plane volume.

Layout under the store root::

    sha256/ab/abcdef…   immutable bytes, named by their SHA-256
    tmp/<upload_id>     an upload in progress; renamed into place on success
    trash/<sha256>      quarantine before deletion; nothing writes here yet

An upload streams through a hasher into ``tmp/`` and is checked as bytes
arrive: the size against the kind's cap, so chunked transfer encoding cannot
bypass it, and at the end the digest against the one the client claimed. Only
then is the file renamed onto its canonical path, which is atomic, so the
store never holds a partial file under a real name. Hashing and writing run
in a worker thread so a large upload never stalls the event loop that serves
heartbeats. The store owns files only; the ``blobs`` row is written by the
caller inside its own transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from rtlfarm.config import BlobsConfig

#: Every blob kind, mapped to the configuration field that caps it.
KIND_CAPS: dict[str, str] = {
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

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class BlobError(ValueError):
    """Base of every ingest failure; the temporary file is always gone."""


class UnknownBlobKind(BlobError):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"unknown blob kind {kind!r}")


class InvalidDigest(BlobError):
    def __init__(self, digest: str) -> None:
        self.digest = digest
        super().__init__(f"not a lowercase hex SHA-256: {digest!r}")


class BlobTooLarge(BlobError):
    def __init__(self, kind: str, cap: int) -> None:
        self.kind = kind
        self.cap = cap
        super().__init__(f"a {kind} blob may not exceed {cap} bytes")


class DigestMismatch(BlobError):
    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"body hashes to {actual}, header said {expected}")


@dataclass(frozen=True)
class IngestResult:
    sha256: str
    size: int
    created: bool


def cap_for(kind: str, caps: BlobsConfig) -> int:
    """The byte cap for ``kind``, or ``UnknownBlobKind``."""
    try:
        field = KIND_CAPS[kind]
    except KeyError:
        raise UnknownBlobKind(kind) from None
    cap: int = getattr(caps, field)
    return cap


def validate_digest(digest: str) -> str:
    if not _SHA256_RE.fullmatch(digest):
        raise InvalidDigest(digest)
    return digest


class BlobStore:
    def __init__(self, root: Path, caps: BlobsConfig, *, fsync: bool = False) -> None:
        self.root = root
        self.caps = caps
        self.fsync = fsync
        for name in ("sha256", "tmp", "trash"):
            (root / name).mkdir(parents=True, exist_ok=True)

    def path_for(self, sha256: str) -> Path:
        """``sha256/ab/abcdef…``: two-character shards keep directories small."""
        return self.root / "sha256" / sha256[:2] / sha256

    def has(self, sha256: str) -> bool:
        return self.path_for(sha256).is_file()

    def size(self, sha256: str) -> int | None:
        """The stored size, or ``None`` when the file is absent."""
        try:
            return self.path_for(sha256).stat().st_size
        except FileNotFoundError:
            return None

    async def ingest(
        self, kind: str, chunks: AsyncIterator[bytes], *, expected_sha256: str
    ) -> IngestResult:
        """Stream ``chunks`` into the store as a blob of ``kind``.

        The kind and the claimed digest are checked before a byte is read.
        Raises ``BlobTooLarge`` the moment the cap is exceeded and
        ``DigestMismatch`` at the end; either way nothing remains in ``tmp/``.
        """
        cap = cap_for(kind, self.caps)
        expected = validate_digest(expected_sha256)
        tmp = self.root / "tmp" / uuid.uuid4().hex
        hasher = hashlib.sha256()
        size = 0
        try:
            with tmp.open("wb") as out:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > cap:
                        raise BlobTooLarge(kind, cap)
                    await asyncio.to_thread(_absorb, hasher, out, chunk)
                if self.fsync:
                    out.flush()
                    os.fsync(out.fileno())
            actual = hasher.hexdigest()
            if actual != expected:
                raise DigestMismatch(expected, actual)
            final = self.path_for(expected)
            if final.is_file():
                tmp.unlink()
                return IngestResult(expected, size, created=False)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, final)
            if self.fsync:
                _fsync_dir(final.parent)
            return IngestResult(expected, size, created=True)
        finally:
            tmp.unlink(missing_ok=True)

    def sweep_tmp(self, *, older_than_s: float, now_s: float) -> list[Path]:
        """Delete uploads in ``tmp/`` last modified before ``now_s - older_than_s``.

        Run at startup: anything still there is an upload whose request died.
        Younger files may belong to an upload in flight and are left alone.
        """
        removed: list[Path] = []
        for path in sorted((self.root / "tmp").iterdir()):
            if path.is_file() and now_s - path.stat().st_mtime > older_than_s:
                path.unlink()
                removed.append(path)
        return removed


def _absorb(hasher: hashlib._Hash, out: BinaryIO, chunk: bytes) -> None:
    """Hash and write one chunk; runs in a worker thread."""
    hasher.update(chunk)
    out.write(chunk)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
