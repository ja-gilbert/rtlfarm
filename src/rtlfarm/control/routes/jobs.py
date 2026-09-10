"""Submission and the read routes for jobs and tasks.

``POST /v1/jobs`` is the one write: it validates the manifest and pipeline
with pointers, resolves the toolchain digest, checks every input blob is
present, expands the task graph, and writes the job, its inputs, tasks,
dependencies and first events in one transaction. Nothing is written before
every check has passed, so a rejected submission leaves no trace, and a crash
inside the transaction leaves none either.

An ``Idempotency-Key`` makes a retried submission return the original job
instead of a second one; the same key with different work is a conflict.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response

from rtlfarm.config import TimingError, validate_timing
from rtlfarm.control.app import ApiError, ClientAuth, Services, services
from rtlfarm.control.wire import (
    JobCreated,
    JobList,
    JobState,
    JobView,
    SubmitRequest,
    TaskDetail,
    TaskList,
    TaskState,
    TaskView,
)
from rtlfarm.expand.dag import ExpandedJob, Selection, SelectionError, expand
from rtlfarm.expand.pack import Manifest, ManifestEntry, target_issues
from rtlfarm.expand.pipeline import PipelineError, parse_pipeline
from rtlfarm.ids import new_ulid
from rtlfarm.models import ROLES

_SHA256_RE = re.compile(r"[0-9a-f]{64}")

router = APIRouter(prefix="/v1", tags=["jobs"])

LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_CAP = 1000

#: SQLite binds at most 32766 parameters per statement; keep well under it.
_IN_CHUNK = 500

_JOB_COLUMNS = (
    "job_id, design_name, state, priority, label, submission_hash, "
    "toolchain_digest, no_cache, n_tasks, n_terminal, n_failed, n_cached, "
    "created_at_ms, started_at_ms, finished_at_ms"
)
_TASK_COLUMNS = (
    "task_id, job_id, stage_kind, target, seed, tool, state, attempt, exit_class, "
    "from_cache, skip_reason, timeout_s, cacheable, priority, toolchain_digest, "
    "ready_at_ms, finished_at_ms, duration_ms"
)

################################################################################
# Submission
################################################################################


@router.post("/jobs", response_model=JobCreated, status_code=201)
async def submit(
    body: SubmitRequest,
    _: ClientAuth,
    svc: Annotated[Services, Depends(services)],
    response: Response,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JobCreated:
    manifest = _manifest_from(body, svc)
    selection = Selection(
        tags=tuple(body.selection.tags),
        targets=tuple(body.selection.targets),
        seeds=body.selection.seeds,
        seed=body.selection.seed,
    )
    digest = _resolve_digest(body, manifest, svc)
    now = svc.clock.now_ms()
    try:
        job = expand(
            manifest,
            selection,
            job_id=new_ulid(now),
            priority=body.priority,
            toolchain_digest=digest,
            no_cache=body.no_cache,
        )
    except SelectionError as e:
        raise ApiError(
            422, "VALIDATION", str(e), [{"pointer": "/selection", "message": str(e)}]
        ) from None
    if not idempotency_key:
        idempotency_key = None
    async with svc.db.write() as conn:
        if idempotency_key is not None:
            existing = _replay(conn, idempotency_key, job.submission_hash)
            if existing is not None:
                response.status_code = 200
                return existing
        stored = _stored_sizes(conn, {f.sha256 for f in manifest.files})
        missing = sorted({f.sha256 for f in manifest.files} - stored.keys())
        if missing:
            raise ApiError(
                409,
                "MISSING_BLOBS",
                f"{len(missing)} input blob(s) have not been uploaded",
                {"missing": missing},
            )
        _check_sizes(body, stored, svc)
        _write_job(conn, job, manifest, body, selection, idempotency_key, now)
    return JobCreated(job_id=job.job_id, n_tasks=len(job.tasks))


def _manifest_from(body: SubmitRequest, svc: Services) -> Manifest:
    try:
        pipeline = parse_pipeline(body.manifest.pipeline)
    except PipelineError as e:
        raise ApiError(
            422,
            "VALIDATION",
            "the pipeline is not valid",
            [
                {
                    "pointer": "/manifest/pipeline" + issue.pointer,
                    "message": issue.message,
                }
                for issue in e.issues
            ],
        ) from None
    issues: list[dict[str, str]] = []
    if pipeline.design.name != body.manifest.design:
        issues.append(
            {
                "pointer": "/manifest/design",
                "message": "does not match the pipeline's design name",
            }
        )
    seen: set[str] = set()
    for i, entry in enumerate(body.manifest.files):
        if entry.path in seen:
            issues.append(
                {"pointer": f"/manifest/files/{i}/path", "message": "duplicate path"}
            )
        seen.add(entry.path)
    roles = {entry.path: entry.role for entry in body.manifest.files}
    issues.extend(
        {"pointer": "/manifest/pipeline" + issue.where, "message": issue.message}
        for issue in target_issues(pipeline, roles)
    )
    if issues:
        raise ApiError(422, "VALIDATION", "the manifest is not valid", issues)
    try:
        validate_timing(svc.config.timing, pipeline.stage_timeouts())
    except TimingError as e:
        raise ApiError(
            422,
            "VALIDATION",
            "a stage timeout conflicts with the farm's timing constants",
            [{"pointer": "/manifest/pipeline/stages", "message": str(e)}],
        ) from None
    entries = tuple(
        ManifestEntry(e.path, e.role, e.ordinal, e.sha256, e.size)
        for e in body.manifest.files
    )
    entries = tuple(sorted(entries, key=lambda e: (ROLES.index(e.role), e.ordinal)))
    return Manifest(body.manifest.design, entries, pipeline)


def _resolve_digest(body: SubmitRequest, manifest: Manifest, svc: Services) -> str:
    """Body, then the pipeline's pin, then the control plane's own pin."""
    for candidate in (
        body.toolchain_digest,
        manifest.pipeline.toolchain.digest,
        svc.config.toolchain.digest,
    ):
        if candidate:
            if not _SHA256_RE.fullmatch(candidate):
                raise ApiError(
                    422,
                    "TOOLCHAIN_UNPINNED",
                    f"the toolchain digest {candidate!r} is not a hex SHA-256",
                )
            return candidate
    raise ApiError(
        422,
        "TOOLCHAIN_UNPINNED",
        "no toolchain digest: pass one, pin it in the pipeline, or configure it",
    )


def _stored_sizes(conn: sqlite3.Connection, digests: set[str]) -> dict[str, int]:
    """The recorded size of every present blob, queried in bounded batches."""
    found: dict[str, int] = {}
    pending = sorted(digests)
    for start in range(0, len(pending), _IN_CHUNK):
        chunk = pending[start : start + _IN_CHUNK]
        marks = ", ".join("?" for _ in chunk)
        for sha256, size in conn.execute(
            f"SELECT sha256, size FROM blobs WHERE sha256 IN ({marks})", chunk
        ).fetchall():
            found[str(sha256)] = int(size)
    return found


def _check_sizes(body: SubmitRequest, stored: dict[str, int], svc: Services) -> None:
    """Declared sizes must match the stored blobs, and the caps use stored sizes."""
    caps = svc.config.blobs
    mismatches = [
        {
            "pointer": f"/manifest/files/{i}/size",
            "message": f"the stored blob is {stored[entry.sha256]} bytes",
        }
        for i, entry in enumerate(body.manifest.files)
        if entry.size != stored[entry.sha256]
    ]
    if mismatches:
        raise ApiError(422, "VALIDATION", "sizes disagree with the blobs", mismatches)
    largest = max((stored[e.sha256] for e in body.manifest.files), default=0)
    if largest > caps.input_file_bytes:
        raise ApiError(
            413,
            "PAYLOAD_TOO_LARGE",
            f"an input file is {largest} bytes; the cap is {caps.input_file_bytes}",
            {"kind": "input", "cap": caps.input_file_bytes},
        )
    total = sum(stored[e.sha256] for e in body.manifest.files)
    if total > caps.input_job_bytes:
        raise ApiError(
            413,
            "PAYLOAD_TOO_LARGE",
            f"the design pack is {total} bytes; the cap is {caps.input_job_bytes}",
            {"kind": "input", "cap": caps.input_job_bytes},
        )


def _replay(
    conn: sqlite3.Connection, key: str, submission_hash: str
) -> JobCreated | None:
    row = conn.execute(
        "SELECT job_id, n_tasks, submission_hash FROM jobs WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    if row[2] != submission_hash:
        raise ApiError(
            409,
            "IDEMPOTENCY_CONFLICT",
            "this Idempotency-Key was used for different work",
            {"job_id": row[0]},
        )
    return JobCreated(job_id=str(row[0]), n_tasks=int(row[1]))


def _write_job(
    conn: sqlite3.Connection,
    job: ExpandedJob,
    manifest: Manifest,
    body: SubmitRequest,
    selection: Selection,
    idempotency_key: str | None,
    now: int,
) -> None:
    conn.execute(
        "INSERT INTO jobs (job_id, design_name, submission_hash, manifest_json, "
        "pipeline_json, selection_json, toolchain_digest, priority, no_cache, "
        "idempotency_key, label, state, dirty, n_tasks, created_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SUBMITTED', 1, ?, ?)",
        (
            job.job_id,
            manifest.design,
            job.submission_hash,
            manifest.canonical_json(),
            json.dumps(manifest.pipeline.model_dump(mode="json"), sort_keys=True),
            json.dumps(selection.to_dict(), sort_keys=True),
            job.toolchain_digest,
            body.priority,
            int(body.no_cache),
            idempotency_key,
            body.label,
            len(job.tasks),
            now,
        ),
    )
    conn.executemany(
        "INSERT INTO job_inputs (job_id, path, role, ordinal, sha256, size) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (job.job_id, f.path, f.role, f.ordinal, f.sha256, f.size)
            for f in manifest.files
        ],
    )
    _insert_tasks(conn, job)
    conn.executemany(
        "INSERT INTO task_deps (task_id, depends_on_task_id, kind) VALUES (?, ?, ?)",
        job.dependencies(),
    )
    conn.executemany(
        "INSERT INTO task_events (task_id, seq, at_ms, actor, from_state, to_state, "
        "attempt, detail_json) VALUES (?, 1, ?, 'api', NULL, 'PENDING', 0, NULL)",
        [(t.task_id, now) for t in job.tasks],
    )


def _insert_tasks(conn: sqlite3.Connection, job: ExpandedJob) -> None:
    conn.executemany(
        "INSERT INTO tasks (task_id, job_id, stage_kind, target, seed, tool, "
        "params_json, timeout_s, cacheable, bypass_cache, affects_verdict, "
        "toolchain_digest, state, priority, max_infra, max_timeout, max_tool_error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)",
        [
            (
                t.task_id,
                job.job_id,
                t.stage_kind,
                t.target,
                t.seed,
                t.tool,
                json.dumps(
                    {**t.params, "consumes_artifacts": list(t.consumes_artifacts)},
                    sort_keys=True,
                ),
                t.timeout_s,
                int(t.cacheable),
                int(t.bypass_cache),
                int(t.affects_verdict),
                t.toolchain_digest,
                t.priority,
                t.max_infra,
                t.max_timeout,
                t.max_tool_error,
            )
            for t in job.tasks
        ],
    )


################################################################################
# Reads
################################################################################


def _limit(limit: int | None) -> int:
    return min(limit or LIST_LIMIT_DEFAULT, LIST_LIMIT_CAP)


@router.get("/jobs", response_model=JobList)
async def list_jobs(
    _: ClientAuth,
    svc: Annotated[Services, Depends(services)],
    state: Annotated[JobState | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> JobList:
    reader = svc.db.read()
    try:
        if state is None:
            rows = reader.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs "
                "ORDER BY created_at_ms DESC, job_id DESC LIMIT ?",
                (_limit(limit),),
            ).fetchall()
        else:
            rows = reader.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE state = ? "
                "ORDER BY created_at_ms DESC, job_id DESC LIMIT ?",
                (state, _limit(limit)),
            ).fetchall()
    finally:
        reader.close()
    return JobList(jobs=[_job_view(row) for row in rows])


@router.get("/jobs/{job_id}", response_model=JobView)
async def get_job(
    job_id: str, _: ClientAuth, svc: Annotated[Services, Depends(services)]
) -> JobView:
    reader = svc.db.read()
    try:
        row = reader.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    finally:
        reader.close()
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no job {job_id}")
    return _job_view(row)


@router.get("/jobs/{job_id}/tasks", response_model=TaskList)
async def list_tasks(
    job_id: str,
    _: ClientAuth,
    svc: Annotated[Services, Depends(services)],
    state: Annotated[TaskState | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> TaskList:
    reader = svc.db.read()
    try:
        if (
            reader.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            is None
        ):
            raise ApiError(404, "NOT_FOUND", f"no job {job_id}")
        if state is None:
            rows = reader.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE job_id = ? "
                "ORDER BY task_id LIMIT ?",
                (job_id, _limit(limit)),
            ).fetchall()
        else:
            rows = reader.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE job_id = ? AND state = ? "
                "ORDER BY task_id LIMIT ?",
                (job_id, state, _limit(limit)),
            ).fetchall()
    finally:
        reader.close()
    return TaskList(tasks=[_task_view(row) for row in rows])


@router.get("/tasks/{task_id}", response_model=TaskDetail)
async def get_task(
    task_id: str, _: ClientAuth, svc: Annotated[Services, Depends(services)]
) -> TaskDetail:
    reader = svc.db.read()
    try:
        row = reader.execute(
            f"SELECT {_TASK_COLUMNS}, params_json FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", f"no task {task_id}")
        deps = [
            str(r[0])
            for r in reader.execute(
                "SELECT depends_on_task_id FROM task_deps WHERE task_id = ? "
                "ORDER BY depends_on_task_id",
                (task_id,),
            ).fetchall()
        ]
    finally:
        reader.close()
    params = json.loads(row[-1])
    consumes_artifacts = params.pop("consumes_artifacts", [])
    view = _task_view(row[:-1])
    return TaskDetail(
        **view.model_dump(),
        params=params,
        depends_on=deps,
        consumes_artifacts=consumes_artifacts,
    )


def _job_view(row: Iterable[object]) -> JobView:
    data = dict(zip(_names(_JOB_COLUMNS), row, strict=True))
    data["no_cache"] = bool(data["no_cache"])
    return JobView.model_validate(data)


def _task_view(row: Iterable[object]) -> TaskView:
    data = dict(zip(_names(_TASK_COLUMNS), row, strict=True))
    data["from_cache"] = bool(data["from_cache"])
    data["cacheable"] = bool(data["cacheable"])
    return TaskView.model_validate(data)


def _names(columns: str) -> list[str]:
    return columns.replace(" ", "").split(",")
