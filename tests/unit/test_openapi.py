"""The OpenAPI document is committed and snapshot-tested, so a change to any
route or model shows up as a diff rather than as a surprise to a client.

Regenerate after an intended change with
``RTLFARM_UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/test_openapi.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from rtlfarm.clock import DrivenClock
from rtlfarm.config import Config
from rtlfarm.control.app import create_app
from rtlfarm.control.blobstore import BlobStore
from rtlfarm.db.connection import Database

SNAPSHOT = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"
UPDATE = os.environ.get("RTLFARM_UPDATE_SNAPSHOTS") == "1"


def test_openapi_document_matches_the_committed_snapshot(tmp_path: Path) -> None:
    config = Config(client_token="c", worker_token="w")
    db = Database(tmp_path / "rtlfarm.db", synchronous="OFF")
    try:
        app = create_app(
            config, db, BlobStore(tmp_path / "blobs", config.blobs), DrivenClock()
        )
        actual = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    finally:
        db.close()
    if UPDATE:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(actual, encoding="utf-8")
    assert SNAPSHOT.is_file(), (
        "missing docs/openapi.json; run with RTLFARM_UPDATE_SNAPSHOTS=1"
    )
    assert actual == SNAPSHOT.read_text(encoding="utf-8")
