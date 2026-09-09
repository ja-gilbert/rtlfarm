"""Submission through the API: the one transaction that creates a job, the
checks that run before it, idempotent replay, the digest resolution chain,
and the read routes for jobs and tasks.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from rtlfarm.clock import DrivenClock
from rtlfarm.config import BlobsConfig, Config, ToolchainConfig
from rtlfarm.control import app as control_app
from rtlfarm.control.blobstore import BlobStore
from rtlfarm.control.invariants import check_invariants
from rtlfarm.control.routes import jobs as job_routes
from rtlfarm.db.connection import Database
from rtlfarm.expand.dag import Selection, submission_hash
from rtlfarm.expand.pack import Manifest, pack
from rtlfarm.ids import _ULID_RE

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
DIGEST = "d" * 64

################################################################################
# Helpers
################################################################################


@pytest.fixture
def manifest() -> Manifest:
    return pack(EXAMPLES / "fake_smoke")


async def _upload_inputs(
    client: httpx.AsyncClient, headers: dict[str, str], root: Path, manifest: Manifest
) -> None:
    for entry in manifest.files:
        data = (root / entry.path).read_bytes()
        response = await client.post(
            "/v1/blobs",
            content=data,
            headers=headers
            | {"X-Content-Sha256": entry.sha256, "X-Blob-Kind": "input"},
        )
        assert response.status_code in (200, 201)


def _body(manifest: Manifest, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "manifest": manifest.to_dict(),
        "selection": {},
        "priority": 5,
        "no_cache": False,
        "toolchain_digest": DIGEST,
    }
    body.update(overrides)
    return body


@pytest.fixture
async def uploaded(
    client: httpx.AsyncClient, as_client: dict[str, str], manifest: Manifest
) -> Manifest:
    await _upload_inputs(client, as_client, EXAMPLES / "fake_smoke", manifest)
    return manifest


def _count(db: Database, table: str, where: str = "1=1", *params: object) -> int:
    reader = db.read()
    try:
        row = reader.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params)
        return int(row.fetchone()[0])
    finally:
        reader.close()


def _rows(db: Database, sql: str, *params: object) -> list[tuple[Any, ...]]:
    reader = db.read()
    try:
        return [tuple(r) for r in reader.execute(sql, params).fetchall()]
    finally:
        reader.close()


################################################################################
# The Submit Transaction
################################################################################


async def test_submit_creates_the_job_and_every_row_of_its_graph(
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    uploaded: Manifest,
    db: Database,
    clock: DrivenClock,
) -> None:
    response = await client.post("/v1/jobs", json=_body(uploaded), headers=as_client)
    assert response.status_code == 201, response.text
    created = response.json()
    job_id = created["job_id"]
    assert _ULID_RE.fullmatch(job_id)
    assert created["n_tasks"] == 7

    (job,) = _rows(
        db,
        "SELECT state, dirty, n_tasks, submission_hash, toolchain_digest, priority, "
        "no_cache, created_at_ms, design_name FROM jobs WHERE job_id = ?",
        job_id,
    )
    assert job == (
        "SUBMITTED",
        1,
        7,
        submission_hash(uploaded, Selection()),
        DIGEST,
        5,
        0,
        clock.now_ms(),
        "fake_smoke",
    )
    assert _count(db, "job_inputs", "job_id = ?", job_id) == 5
    assert _count(db, "tasks", "job_id = ? AND state = 'PENDING'", job_id) == 7
    assert _count(db, "tasks", "job_id = ? AND ready_at_ms IS NOT NULL", job_id) == 0
    assert _count(db, "task_deps") == 5
    events = _rows(
        db,
        "SELECT seq, actor, from_state, to_state, attempt FROM task_events "
        "WHERE task_id LIKE ? || '%'",
        job_id,
    )
    assert events == [(1, "api", None, "PENDING", 0)] * 7
    reader = db.read()
    try:
        assert check_invariants(reader) == []
    finally:
        reader.close()


async def test_stored_manifest_and_pipeline_are_canonical_json(
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    uploaded: Manifest,
    db: Database,
) -> None:
    await client.post("/v1/jobs", json=_body(uploaded), headers=as_client)
    ((manifest_json, pipeline_json, selection_json),) = _rows(
        db, "SELECT manifest_json, pipeline_json, selection_json FROM jobs"
    )
    assert manifest_json == uploaded.canonical_json()
    assert json.loads(pipeline_json)["design"]["name"] == "fake_smoke"
    assert json.loads(selection_json) == Selection().to_dict()


async def test_task_rows_carry_params_and_budgets(
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    uploaded: Manifest,
    db: Database,
) -> None:
    job_id = (
        await client.post("/v1/jobs", json=_body(uploaded), headers=as_client)
    ).json()["job_id"]
    (row,) = _rows(
        db,
        "SELECT params_json, timeout_s, cacheable, max_infra, max_timeout, "
        "max_tool_error, bypass_cache, affects_verdict FROM tasks WHERE task_id = ?",
        f"{job_id}.simulate.t_a.s1",
    )
    params = json.loads(row[0])
    assert params["inputs"] == ["data/t_a.txt"]
    assert params["top"] == "t_a"
    assert params["consumes_artifacts"] == ["compiled"]
    assert row[1:] == (30, 1, 3, 0, 1, 0, 1)


async def test_missing_blobs_lists_exactly_the_absent_digests(
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    manifest: Manifest,
    db: Database,
) -> None:
    root = EXAMPLES / "fake_smoke"
    first_two = manifest.files[:2]
    for entry in first_two:
        await client.post(
            "/v1/blobs",
            content=(root / entry.path).read_bytes(),
            headers=as_client
            | {"X-Content-Sha256": entry.sha256, "X-Blob-Kind": "input"},
        )
    response = await client.post("/v1/jobs", json=_body(manifest), headers=as_client)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "MISSING_BLOBS"
    assert error["details"]["missing"] == sorted(f.sha256 for f in manifest.files[2:])
    assert _count(db, "jobs") == 0


async def test_a_failure_inside_the_transaction_leaves_no_job(
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    uploaded: Manifest,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(conn: sqlite3.Connection, job: object) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(job_routes, "_insert_tasks", explode)
    with pytest.raises(RuntimeError, match="disk on fire"):
        await client.post("/v1/jobs", json=_body(uploaded), headers=as_client)
    assert _count(db, "jobs") == 0
    assert _count(db, "job_inputs") == 0
    assert not db.in_transaction
    monkeypatch.undo()
    retry = await client.post("/v1/jobs", json=_body(uploaded), headers=as_client)
    assert retry.status_code == 201
    assert _count(db, "jobs") == 1


@pytest.mark.parametrize(
    "key_header", [{}, {"Idempotency-Key": ""}], ids=["absent", "empty"]
)
async def test_without_a_usable_key_each_submission_is_a_new_job(
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    uploaded: Manifest,
    db: Database,
    key_header: dict[str, str],
) -> None:
    """Regression: an empty key was stored and replayed as a real key."""
    headers = as_client | key_header
    first = await client.post("/v1/jobs", json=_body(uploaded), headers=headers)
    second = await client.post("/v1/jobs", json=_body(uploaded), headers=headers)
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["job_id"] != second.json()["job_id"]
    assert _count(db, "jobs") == 2
    assert _count(db, "jobs", "idempotency_key IS NOT NULL") == 0


################################################################################
# Idempotency
################################################################################


async def test_replay_with_the_same_key_returns_the_original_job(
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    uploaded: Manifest,
    db: Database,
) -> None:
    headers = as_client | {"Idempotency-Key": "submit-1"}
    first = await client.post("/v1/jobs", json=_body(uploaded), headers=headers)
    second = await client.post("/v1/jobs", json=_body(uploaded), headers=headers)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json() == second.json()
    assert _count(db, "jobs") == 1


async def test_same_key_for_different_work_is_a_conflict(
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    uploaded: Manifest,
    db: Database,
) -> None:
    headers = as_client | {"Idempotency-Key": "submit-2"}
    first = await client.post("/v1/jobs", json=_body(uploaded), headers=headers)
    other = _body(uploaded, selection={"tags": ["smoke"]})
    second = await client.post("/v1/jobs", json=other, headers=headers)
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "IDEMPOTENCY_CONFLICT"
    assert error["details"] == {"job_id": first.json()["job_id"]}
    assert _count(db, "jobs") == 1


################################################################################
# Toolchain Digest Resolution
################################################################################


async def test_a_pipeline_pin_that_is_not_a_sha256_is_a_validation_error(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    """Regression: 'any' reached the schema CHECK as a 500."""
    body = _body(uploaded)
    del body["toolchain_digest"]
    body["manifest"]["pipeline"]["toolchain"] = {"digest": "any"}
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION"
    assert error["details"][0]["pointer"] == "/manifest/pipeline/toolchain/digest"


async def test_a_configured_pin_that_is_not_a_sha256_is_unpinned(
    tmp_path: Path, clock: DrivenClock, manifest: Manifest
) -> None:
    """Regression: a malformed configured pin was used as-is."""
    config = Config(client_token="c", toolchain=ToolchainConfig(digest="any"))
    db = Database(tmp_path / "bad.db", synchronous="OFF")
    db.migrate(clock)
    app = control_app.create_app(
        config, db, BlobStore(tmp_path / "b", config.blobs), clock
    )
    headers = {"Authorization": "Bearer c"}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
            await _upload_inputs(c, headers, EXAMPLES / "fake_smoke", manifest)
            body = _body(manifest)
            del body["toolchain_digest"]
            response = await c.post("/v1/jobs", json=body, headers=headers)
    finally:
        db.close()
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TOOLCHAIN_UNPINNED"
    assert "any" in response.json()["error"]["message"]


async def test_unpinned_toolchain_is_rejected(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    body = _body(uploaded)
    del body["toolchain_digest"]
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TOOLCHAIN_UNPINNED"


@pytest.mark.parametrize(
    ("body_pin", "stored"),
    [(None, "e" * 64), ("a" * 64, "a" * 64)],
    ids=["pipeline-pin-when-the-body-has-none", "body-pin-beats-the-pipeline-pin"],
)
async def test_the_body_pin_beats_the_pipeline_pin(
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    uploaded: Manifest,
    db: Database,
    body_pin: str | None,
    stored: str,
) -> None:
    body = _body(uploaded, toolchain_digest=body_pin)
    if body_pin is None:
        del body["toolchain_digest"]
    body["manifest"]["pipeline"]["toolchain"] = {"digest": "e" * 64}
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 201
    assert _rows(db, "SELECT toolchain_digest FROM jobs") == [(stored,)]


async def test_configured_pin_is_the_last_resort(
    tmp_path: Path, clock: DrivenClock, manifest: Manifest
) -> None:
    config = Config(client_token="c", toolchain=ToolchainConfig(digest="f" * 64))
    db = Database(tmp_path / "pinned.db", synchronous="OFF")
    db.migrate(clock)
    app = control_app.create_app(
        config, db, BlobStore(tmp_path / "b", config.blobs), clock
    )
    headers = {"Authorization": "Bearer c"}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
            await _upload_inputs(c, headers, EXAMPLES / "fake_smoke", manifest)
            body = _body(manifest)
            del body["toolchain_digest"]
            response = await c.post("/v1/jobs", json=body, headers=headers)
            assert response.status_code == 201
        assert _rows(db, "SELECT toolchain_digest FROM jobs") == [("f" * 64,)]
    finally:
        db.close()


################################################################################
# Validation
################################################################################


async def test_invalid_pipeline_is_422_with_pointers_into_the_manifest(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    body = _body(uploaded)
    body["manifest"]["pipeline"]["stages"]["simulate"]["depends_on"] = ["ghost"]
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION"
    assert error["details"] == [
        {
            "pointer": "/manifest/pipeline/stages/simulate/depends_on/0",
            "message": "unknown stage 'ghost'",
        }
    ]


async def test_malformed_body_is_422_with_body_pointers(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    body = _body(uploaded, priority=11)
    body["manifest"]["files"][0]["sha256"] = "nope"
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 422
    pointers = {d["pointer"] for d in response.json()["error"]["details"]}
    assert pointers == {"/body/priority", "/body/manifest/files/0/sha256"}


async def test_unknown_body_key_is_rejected(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    response = await client.post(
        "/v1/jobs", json=_body(uploaded, retries=3), headers=as_client
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["pointer"] == "/body/retries"


async def test_design_name_must_match_the_pipeline(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    body = _body(uploaded)
    body["manifest"]["design"] = "other"
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["pointer"] == "/manifest/design"


async def test_target_file_not_in_the_manifest_is_422(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    body = _body(uploaded)
    body["manifest"]["pipeline"]["targets"][0]["data"] = ["data/nope.txt"]
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["pointer"] == (
        "/manifest/pipeline/targets/0/data/0"
    )


async def test_unknown_selected_target_is_422(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    body = _body(uploaded, selection={"targets": ["t_missing"]})
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["pointer"] == "/selection"


async def test_selection_narrows_the_graph(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    body = _body(uploaded, selection={"tags": ["smoke"], "seeds": 1})
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 201
    assert response.json()["n_tasks"] == 2


async def test_declared_sizes_must_match_the_stored_blobs(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    """Regression: sizes were taken from the client, not the store."""
    body = _body(uploaded)
    body["manifest"]["files"][1]["size"] = 1  # the blob is bigger than that
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert details == [
        {
            "pointer": "/manifest/files/1/size",
            "message": f"the stored blob is {uploaded.files[1].size} bytes",
        }
    ]


async def test_design_pack_over_the_per_job_cap_is_413(
    tmp_path: Path, clock: DrivenClock, manifest: Manifest
) -> None:
    """The cap is judged on stored sizes, so it cannot be dodged by lying.

    Regression: the cap was judged on the sizes the client declared.
    """
    config = Config(
        client_token="c",
        blobs=BlobsConfig(input_job_bytes=100),
        toolchain=ToolchainConfig(digest=DIGEST),
    )
    db = Database(tmp_path / "small.db", synchronous="OFF")
    db.migrate(clock)
    app = control_app.create_app(
        config, db, BlobStore(tmp_path / "b", config.blobs), clock
    )
    headers = {"Authorization": "Bearer c"}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
            await _upload_inputs(c, headers, EXAMPLES / "fake_smoke", manifest)
            response = await c.post("/v1/jobs", json=_body(manifest), headers=headers)
    finally:
        db.close()
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "PAYLOAD_TOO_LARGE"
    assert error["details"] == {"kind": "input", "cap": 100}


async def test_an_input_over_the_per_file_cap_is_413_whatever_kind_uploaded_it(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    """Regression: the per-file cap was not enforced at submission."""
    big = b"z" * 5000  # over the fixture's 4096-byte input cap
    digest = hashlib.sha256(big).hexdigest()
    stored = await client.post(
        "/v1/blobs",
        content=big,
        headers=as_client | {"X-Content-Sha256": digest, "X-Blob-Kind": "compiled"},
    )
    assert stored.status_code == 201
    body = _body(uploaded)
    body["manifest"]["files"][0] |= {"sha256": digest, "size": 5000}
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 413
    assert response.json()["error"]["details"] == {"kind": "input", "cap": 4096}


async def test_manifest_paths_must_be_plain_relative_paths(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    """Regression: manifest paths were unvalidated at submission."""
    for bad in ("../escape.txt", "/etc/passwd", "", "src\\x.txt", "a/./b", "x/"):
        body = _body(uploaded)
        body["manifest"]["files"][0]["path"] = bad
        response = await client.post("/v1/jobs", json=body, headers=as_client)
        assert response.status_code == 422, bad
        pointers = {d["pointer"] for d in response.json()["error"]["details"]}
        assert "/body/manifest/files/0/path" in pointers, bad


async def test_stage_timeouts_are_checked_against_the_timing_constants(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    """Regression: validate_timing was never called with the stages."""
    body = _body(uploaded)
    body["manifest"]["pipeline"]["stages"]["compile"]["timeout_s"] = 1
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 422
    detail = response.json()["error"]["details"][0]
    assert detail["pointer"] == "/manifest/pipeline/stages"
    assert "kill_grace_s" in detail["message"]


async def test_stage_declared_before_its_dependency_is_accepted(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    """Regression: declaration order raised KeyError, a 500."""
    body = _body(uploaded)
    stages = body["manifest"]["pipeline"]["stages"]
    body["manifest"]["pipeline"]["stages"] = {
        "simulate": stages["simulate"],
        "compile": stages["compile"],
    }
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 201
    job_id = response.json()["job_id"]
    detail = (
        await client.get(f"/v1/tasks/{job_id}.simulate.t_a.s1", headers=as_client)
    ).json()
    assert detail["depends_on"] == [f"{job_id}.compile.t_a.s0"]


async def test_duplicate_dependency_is_a_pipeline_error_not_a_crash(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    """Regression: duplicate task_deps rows, a 500."""
    body = _body(uploaded)
    body["manifest"]["pipeline"]["stages"]["simulate"]["depends_on"] = [
        "compile",
        "compile",
    ]
    response = await client.post("/v1/jobs", json=body, headers=as_client)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["pointer"] == (
        "/manifest/pipeline/stages/simulate/depends_on/1"
    )


async def test_submit_needs_the_client_token(
    client: httpx.AsyncClient, as_worker: dict[str, str], uploaded: Manifest
) -> None:
    assert (await client.post("/v1/jobs", json=_body(uploaded))).status_code == 401
    response = await client.post("/v1/jobs", json=_body(uploaded), headers=as_worker)
    assert response.status_code == 401


################################################################################
# Reads
################################################################################


async def test_job_and_task_views(
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    uploaded: Manifest,
    clock: DrivenClock,
) -> None:
    job_id = (
        await client.post(
            "/v1/jobs", json=_body(uploaded, label="smoke run"), headers=as_client
        )
    ).json()["job_id"]
    job = (await client.get(f"/v1/jobs/{job_id}", headers=as_client)).json()
    assert job["state"] == "SUBMITTED"
    assert job["design_name"] == "fake_smoke"
    assert job["label"] == "smoke run"
    assert (job["n_tasks"], job["n_terminal"], job["n_failed"], job["n_cached"]) == (
        7,
        0,
        0,
        0,
    )
    assert job["created_at_ms"] == clock.now_ms()
    assert job["started_at_ms"] is None
    tasks = (await client.get(f"/v1/jobs/{job_id}/tasks", headers=as_client)).json()[
        "tasks"
    ]
    assert len(tasks) == 7
    assert {t["state"] for t in tasks} == {"PENDING"}
    assert [t["task_id"] for t in tasks] == sorted(t["task_id"] for t in tasks)
    sim = next(t for t in tasks if t["task_id"].endswith(".simulate.t_b.s100"))
    assert (sim["stage_kind"], sim["target"], sim["seed"], sim["tool"]) == (
        "simulate",
        "t_b",
        100,
        "fake",
    )
    assert sim["from_cache"] is False
    detail = (await client.get(f"/v1/tasks/{sim['task_id']}", headers=as_client)).json()
    assert detail["params"]["inputs"] == ["data/t_b.txt"]
    assert detail["depends_on"] == [f"{job_id}.compile.t_b.s0"]
    assert detail["consumes_artifacts"] == ["compiled"]
    assert "consumes_artifacts" not in detail["params"]


async def test_job_list_is_newest_first_with_state_filter_and_limit(
    client: httpx.AsyncClient,
    as_client: dict[str, str],
    uploaded: Manifest,
    clock: DrivenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = []
    for _ in range(3):
        ids.append(
            (
                await client.post("/v1/jobs", json=_body(uploaded), headers=as_client)
            ).json()["job_id"]
        )
        clock.advance(1)
    listed = (await client.get("/v1/jobs", headers=as_client)).json()["jobs"]
    assert [j["job_id"] for j in listed] == list(reversed(ids))
    limited = (await client.get("/v1/jobs?limit=2", headers=as_client)).json()["jobs"]
    assert [j["job_id"] for j in limited] == list(reversed(ids))[:2]
    none = (await client.get("/v1/jobs?state=RUNNING", headers=as_client)).json()[
        "jobs"
    ]
    assert none == []
    # The cap is a module constant, not configuration; lower it to observe it.
    monkeypatch.setattr(job_routes, "LIST_LIMIT_CAP", 2)
    capped = (await client.get("/v1/jobs?limit=10000", headers=as_client)).json()
    assert [j["job_id"] for j in capped["jobs"]] == list(reversed(ids))[:2]


async def test_a_state_filter_outside_the_vocabulary_is_422(
    client: httpx.AsyncClient, as_client: dict[str, str], uploaded: Manifest
) -> None:
    """Regression: the state filter was unvalidated."""
    job_id = (
        await client.post("/v1/jobs", json=_body(uploaded), headers=as_client)
    ).json()["job_id"]
    for path in ("/v1/jobs?state=running", f"/v1/jobs/{job_id}/tasks?state=DONE"):
        response = await client.get(path, headers=as_client)
        assert response.status_code == 422, path
        assert response.json()["error"]["details"][0]["pointer"] == "/query/state"


async def test_unknown_job_and_task_are_404(
    client: httpx.AsyncClient, as_client: dict[str, str]
) -> None:
    for path in ("/v1/jobs/nope", "/v1/jobs/nope/tasks", "/v1/tasks/nope"):
        response = await client.get(path, headers=as_client)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_reads_need_the_client_token(
    client: httpx.AsyncClient, as_worker: dict[str, str]
) -> None:
    assert (await client.get("/v1/jobs")).status_code == 401
    assert (await client.get("/v1/jobs", headers=as_worker)).status_code == 401
