"""The shapes that cross the wire: request bodies and typed responses.

Request models forbid unknown keys, so a misspelled field is a 422 with a
pointer rather than a silently ignored setting. Response models are what the
read routes promise; the OpenAPI document is generated from them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rtlfarm.models import MAX_SEED, Name, Role, Sha256

#: SQLite stores INTEGER as a signed 64-bit value; larger is a request error.
MAX_INT = 2**63 - 1

#: The most files one design pack may declare.
MAX_MANIFEST_FILES = 20_000

JobState = Literal["SUBMITTED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED"]
TaskState = Literal[
    "PENDING",
    "READY",
    "LEASED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "TIMED_OUT",
    "INFRA_FAILED",
    "SKIPPED",
    "CANCELED",
]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManifestEntryBody(_Request):
    path: str = Field(min_length=1)
    role: Role
    ordinal: int = Field(ge=0, le=MAX_INT)
    sha256: Sha256
    size: int = Field(ge=0, le=MAX_INT)

    @field_validator("path")
    @classmethod
    def _pack_relative(cls, value: str) -> str:
        """The same rules a pack obeys: relative, forward-slash, no ``.`` or ``..``.

        Segments are checked raw rather than through ``PurePosixPath``, which
        would silently drop a ``.`` segment before it could be seen.
        """
        if "\\" in value or "\x00" in value:
            raise ValueError("must use forward slashes and contain no NUL")
        if any(segment in ("", ".", "..") for segment in value.split("/")):
            raise ValueError(
                "must be a relative path with no empty, '.' or '..' segments"
            )
        return value


class ManifestBody(_Request):
    manifest_version: Literal[1]
    design: Name
    files: list[ManifestEntryBody] = Field(max_length=MAX_MANIFEST_FILES)
    #: Validated by the pipeline validator so its errors carry pointers.
    pipeline: dict[str, Any]


class SelectionBody(_Request):
    tags: list[Name] = Field(default_factory=list)
    targets: list[Name] = Field(default_factory=list)
    seeds: int | None = Field(default=None, ge=1)
    seed: int | None = Field(default=None, ge=1, le=MAX_SEED)


class SubmitRequest(_Request):
    manifest: ManifestBody
    selection: SelectionBody = SelectionBody()
    priority: int = Field(default=5, ge=0, le=9)
    no_cache: bool = False
    toolchain_digest: Sha256 | None = None
    label: str | None = Field(default=None, max_length=128)


class JobCreated(BaseModel):
    job_id: str
    n_tasks: int


class JobView(BaseModel):
    job_id: str
    design_name: str
    state: str
    priority: int
    label: str | None
    submission_hash: str
    toolchain_digest: str
    no_cache: bool
    n_tasks: int
    n_terminal: int
    n_failed: int
    n_cached: int
    created_at_ms: int
    started_at_ms: int | None
    finished_at_ms: int | None


class JobList(BaseModel):
    jobs: list[JobView]


class TaskView(BaseModel):
    task_id: str
    job_id: str
    stage_kind: str
    target: str
    seed: int
    tool: str
    state: str
    attempt: int
    exit_class: str | None
    from_cache: bool
    skip_reason: str | None
    timeout_s: int
    cacheable: bool
    priority: int
    toolchain_digest: str
    ready_at_ms: int | None
    finished_at_ms: int | None
    duration_ms: int | None


class TaskDetail(TaskView):
    params: dict[str, Any]
    depends_on: list[str]
    consumes_artifacts: list[str]


class TaskList(BaseModel):
    tasks: list[TaskView]
