"""Liveness and readiness.

``/healthz`` says the process is up. ``/readyz`` says it can do work: the
schema is migrated, the blob volume is writable, and, once the scheduler
exists, the last tick was recent and the file lock is held. Neither route
needs a token: the Compose healthcheck calls them.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from rtlfarm.control.app import Services, services
from rtlfarm.db.migrate import applied_versions, load_migrations

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(
    svc: Annotated[Services, Depends(services)], response: Response
) -> dict[str, object]:
    checks = {
        "migrated": _migrated(svc),
        "blobs_writable": _writable(svc),
        "tick_recent": _tick_recent(svc),
        "lock_held": svc.readiness.lock_held,
    }
    ready = all(value is not False for value in checks.values())
    if not ready:
        response.status_code = 503
    return {"status": "ok" if ready else "not_ready", "checks": checks}


def _migrated(svc: Services) -> bool:
    if not svc.readiness.migrated:
        return False
    reader = svc.db.read()
    try:
        expected = [m.version for m in load_migrations()]
        return applied_versions(reader) == expected
    finally:
        reader.close()


def _writable(svc: Services) -> bool:
    probe = svc.blobs.root / "tmp" / f"readyz-{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _tick_recent(svc: Services) -> bool | None:
    last = svc.readiness.last_tick_ms
    if last is None:
        return None
    timing = svc.config.timing
    limit_ms = int(timing.readyz_tick_factor * timing.tick_s * 1000)
    return svc.clock.now_ms() - last <= limit_ms
