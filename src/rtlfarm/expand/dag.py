"""Static expansion: a manifest plus a selection becomes the job's task graph.

Everything about a job is decided here, at submission: which tasks exist,
what each one reads, what it depends on, and its budgets. Nothing inserts a
task later, so reproducing a job means re-expanding the same manifest and
selection, and every cache key is a function of what this module produced.

Three shapes of stage:

- a whole-design stage (neither ``per_target`` nor ``fan_out``) is one task,
  target ``_``, seed 0, reading every file of the roles it consumes;
- a per-target stage is one task per selected target, seed 0;
- a fan-out stage is one task per selected target and seed.

For the last two, the ``tb`` and ``data`` roles are narrowed to the target's
own lists; ``rtl`` and ``include`` are always design-wide. A task's input
list is its stage's roles in ``consumes`` order, each role's files in manifest
ordinal order.

Dependencies follow ``depends_on`` stage by stage: a task depends on the
upstream stage's task for its own target where the upstream has one, and on
all of the upstream's tasks otherwise. Every Core dependency is of kind
``success``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field

from rtlfarm.expand.pack import Manifest
from rtlfarm.ids import WHOLE_DESIGN_TARGET, task_id
from rtlfarm.models import Pipeline, Stage, Target

#: Roles narrowed to a target's own file list on targeted stages.
TARGET_ROLES: frozenset[str] = frozenset({"tb", "data"})


class SelectionError(ValueError):
    """The selection names nothing the pipeline has, or leaves nothing to run."""


@dataclass(frozen=True)
class Selection:
    """What ``rtlfarm submit`` chose: tags, explicit targets, a seed cap, a pin."""

    tags: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    seeds: int | None = None
    seed: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "tags": list(self.tags),
            "targets": list(self.targets),
            "seeds": self.seeds,
            "seed": self.seed,
        }


@dataclass(frozen=True)
class TaskSpec:
    """One task as it will be written to the database."""

    task_id: str
    stage_kind: str
    target: str
    seed: int
    tool: str
    params: dict[str, object]
    timeout_s: int
    cacheable: bool
    bypass_cache: bool
    affects_verdict: bool
    toolchain_digest: str
    priority: int
    max_infra: int
    max_timeout: int
    max_tool_error: int
    consumes_artifacts: tuple[str, ...]
    depends_on: tuple[str, ...] = field(default_factory=tuple)

    @property
    def inputs(self) -> list[str]:
        """The ordered manifest paths this task reads."""
        inputs = self.params["inputs"]
        assert isinstance(inputs, list)
        return [str(path) for path in inputs]


@dataclass(frozen=True)
class ExpandedJob:
    job_id: str
    design: str
    submission_hash: str
    toolchain_digest: str
    tasks: tuple[TaskSpec, ...]

    def task(self, task_id: str) -> TaskSpec:
        return next(t for t in self.tasks if t.task_id == task_id)

    def dependencies(self) -> list[tuple[str, str, str]]:
        """``(task_id, depends_on_task_id, kind)`` rows, in task order."""
        return [(t.task_id, dep, "success") for t in self.tasks for dep in t.depends_on]


def select_targets(pipeline: Pipeline, selection: Selection) -> list[Target]:
    """The targets a selection keeps, in pipeline order, with seeds applied."""
    by_name = {t.name: t for t in pipeline.targets}
    unknown = [name for name in selection.targets if name not in by_name]
    if unknown:
        raise SelectionError(f"no such target: {', '.join(unknown)}")
    kept: list[Target] = []
    for target in pipeline.targets:
        if selection.targets and target.name not in selection.targets:
            continue
        if selection.tags and not set(selection.tags) & set(target.tags):
            continue
        seeds = target.seeds
        if selection.seed is not None:
            seeds = [selection.seed]
        elif selection.seeds is not None:
            seeds = seeds[: selection.seeds]
        kept.append(target.model_copy(update={"seeds": seeds}))
    return kept


def submission_hash(manifest: Manifest, selection: Selection) -> str:
    """SHA-256 over the manifest digest and the canonical selection."""
    payload = json.dumps(
        {"manifest": manifest.digest(), "selection": selection.to_dict()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def expand(
    manifest: Manifest,
    selection: Selection,
    *,
    job_id: str,
    priority: int,
    toolchain_digest: str,
    no_cache: bool = False,
) -> ExpandedJob:
    """Expand the manifest under the selection into the job's tasks."""
    pipeline = manifest.pipeline
    targets = select_targets(pipeline, selection)
    if not targets and any(s.fan_out or s.per_target for s in pipeline.stages.values()):
        raise SelectionError("the selection matched no targets")
    retries = pipeline.policies.retries
    by_stage: dict[str, list[TaskSpec]] = {}
    tasks: list[TaskSpec] = []
    for name, stage in pipeline.stages.items():
        stage_tasks: list[TaskSpec] = []
        for target, seed in _instances(stage, targets):
            deps = _dependencies(stage, target, by_stage)
            spec = TaskSpec(
                task_id=task_id(job_id, name, _target_name(target), seed),
                stage_kind=name,
                target=_target_name(target),
                seed=seed,
                tool=stage.tool,
                params=_params(manifest, pipeline, stage, target),
                timeout_s=_timeout(stage, target),
                cacheable=pipeline.policies.cache.get(name, True),
                bypass_cache=no_cache,
                affects_verdict=True,
                toolchain_digest=toolchain_digest,
                priority=priority,
                max_infra=retries.infra,
                max_timeout=retries.timeout,
                max_tool_error=retries.tool_error,
                consumes_artifacts=tuple(stage.consumes_artifacts),
                depends_on=tuple(deps),
            )
            stage_tasks.append(spec)
        by_stage[name] = stage_tasks
        tasks.extend(stage_tasks)
    return ExpandedJob(
        job_id=job_id,
        design=manifest.design,
        submission_hash=submission_hash(manifest, selection),
        toolchain_digest=toolchain_digest,
        tasks=tuple(tasks),
    )


def _instances(
    stage: Stage, targets: list[Target]
) -> Iterable[tuple[Target | None, int]]:
    if stage.fan_out is not None:
        for target in targets:
            for seed in target.seeds:
                yield target, seed
    elif stage.per_target:
        for target in targets:
            yield target, 0
    else:
        yield None, 0


def _target_name(target: Target | None) -> str:
    return WHOLE_DESIGN_TARGET if target is None else target.name


def _dependencies(
    stage: Stage, target: Target | None, by_stage: dict[str, list[TaskSpec]]
) -> list[str]:
    deps: list[str] = []
    for upstream in stage.depends_on:
        candidates = by_stage[upstream]
        if target is not None:
            same_target = [t.task_id for t in candidates if t.target == target.name]
            if same_target:
                deps.extend(same_target)
                continue
        deps.extend(t.task_id for t in candidates)
    return deps


def _timeout(stage: Stage, target: Target | None) -> int:
    if target is not None and target.timeout_s is not None:
        return target.timeout_s
    return stage.timeout_s


def _inputs(manifest: Manifest, stage: Stage, target: Target | None) -> list[str]:
    inputs: list[str] = []
    for role in stage.consumes:
        paths = manifest.paths(role)
        if target is not None and role in TARGET_ROLES:
            own = set(target.tb if role == "tb" else target.data)
            paths = [p for p in paths if p in own]
        inputs.extend(paths)
    return inputs


def _params(
    manifest: Manifest, pipeline: Pipeline, stage: Stage, target: Target | None
) -> dict[str, object]:
    design = pipeline.design
    params: dict[str, object] = {
        "defines": dict(design.defines),
        "params": dict(design.params),
        "timescale": design.timescale,
        "include_dirs": list(design.include_dirs),
        "consumes": list(stage.consumes),
        "inputs": _inputs(manifest, stage, target),
    }
    if target is not None:
        params["defines"] = {**design.defines, **target.defines}
        params["params"] = {**design.params, **target.params}
        params.update(
            {
                "top": target.top,
                "tb": list(target.tb),
                "data": list(target.data),
                "plusargs": dict(target.plusargs),
                "timeout_sim": target.timeout_sim,
                "allow_zero_time": target.allow_zero_time,
            }
        )
    return params
