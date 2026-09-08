"""Loading and validating ``rtlfarm.yaml``.

Every problem is reported as a JSON-pointer path into the file plus a
message, and every problem found is reported at once, so an author fixes a
file in one round. Shape errors come from the models; the rules that relate
one part of the file to another are checked here, in the order the file is
read: design, targets, stages, policies.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from rtlfarm.models import (
    ARTIFACT_KINDS,
    RESERVED_PLUSARGS,
    STAGE_KINDS,
    Pipeline,
    Stage,
)

PIPELINE_FILENAME = "rtlfarm.yaml"


@dataclass(frozen=True)
class PipelineIssue:
    """One thing wrong with the file, at a JSON-pointer path."""

    pointer: str
    message: str

    def __str__(self) -> str:
        return f"{self.pointer}: {self.message}"


class PipelineError(ValueError):
    """The file is invalid; ``issues`` lists every problem found."""

    def __init__(self, issues: list[PipelineIssue]) -> None:
        self.issues = issues
        super().__init__("\n".join(str(issue) for issue in issues))


def load_pipeline(path: Path) -> Pipeline:
    """Read and validate a pipeline file (or a directory holding one)."""
    if path.is_dir():
        path = path / PIPELINE_FILENAME
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise PipelineError([PipelineIssue("", f"not valid YAML: {e}")]) from None
    return parse_pipeline(data)


def parse_pipeline(data: object) -> Pipeline:
    """Validate already-parsed data and return the model."""
    if not isinstance(data, dict):
        raise PipelineError([PipelineIssue("", "the file must be a mapping")])
    try:
        pipeline = Pipeline.model_validate(data)
    except ValidationError as e:
        raise PipelineError(
            [
                PipelineIssue(_pointer(err["loc"]), err["msg"])
                for err in e.errors(include_url=False)
            ]
        ) from None
    issues = list(_semantic_issues(pipeline))
    if issues:
        raise PipelineError(issues)
    return pipeline


def _pointer(loc: Iterable[object]) -> str:
    """pydantic's location tuple as a JSON pointer, without its ``[key]`` marker."""
    parts = [str(part) for part in loc if str(part) != "[key]"]
    return "/" + "/".join(parts) if parts else ""


def _semantic_issues(p: Pipeline) -> Iterable[PipelineIssue]:
    seen: set[str] = set()
    for i, target in enumerate(p.targets):
        if target.name in seen:
            yield PipelineIssue(
                f"/targets/{i}/name", f"duplicate target {target.name!r}"
            )
        seen.add(target.name)
        for key in sorted(RESERVED_PLUSARGS & target.plusargs.keys()):
            yield PipelineIssue(
                f"/targets/{i}/plusargs/{key}", "reserved; the runner injects it"
            )
        if _needs_tb(p.stages) and not target.tb:
            yield PipelineIssue(
                f"/targets/{i}/tb", "a simulate stage needs a testbench file"
            )
    for name, stage in p.stages.items():
        base = f"/stages/{name}"
        if name not in STAGE_KINDS:
            yield PipelineIssue(base, f"stage name must be one of {list(STAGE_KINDS)}")
        if stage.per_target and stage.fan_out is not None:
            yield PipelineIssue(base, "per_target and fan_out are exclusive")
        if stage.fan_out is not None and not p.targets:
            yield PipelineIssue(f"{base}/fan_out", "a fan-out stage needs targets")
        if stage.per_target and not p.targets:
            yield PipelineIssue(
                f"{base}/per_target", "a per-target stage needs targets"
            )
        if name == "coverage" and stage.tool == "iverilog":
            yield PipelineIssue(f"{base}/tool", "coverage is not available on iverilog")
        for j, role in enumerate(stage.consumes):
            if role not in p.design.files:
                yield PipelineIssue(
                    f"{base}/consumes/{j}", f"role {role!r} declares no files"
                )
        for j, kind in enumerate(stage.consumes_artifacts):
            if kind not in ARTIFACT_KINDS:
                yield PipelineIssue(
                    f"{base}/consumes_artifacts/{j}",
                    f"artifact kind must be one of {list(ARTIFACT_KINDS)}",
                )
        for j, dep in enumerate(stage.depends_on):
            if dep not in p.stages:
                yield PipelineIssue(f"{base}/depends_on/{j}", f"unknown stage {dep!r}")
            elif dep in stage.depends_on[:j]:
                yield PipelineIssue(
                    f"{base}/depends_on/{j}", f"{dep!r} is listed twice"
                )
    if _has_cycle(p.stages):
        yield PipelineIssue("/stages", "depends_on forms a cycle")
    for name in p.policies.cache:
        if name not in p.stages:
            yield PipelineIssue(f"/policies/cache/{name}", f"unknown stage {name!r}")


def _needs_tb(stages: dict[str, Stage]) -> bool:
    return "simulate" in stages


def _has_cycle(stages: dict[str, Stage]) -> bool:
    """Depth-first search over ``depends_on``; unknown stages are skipped."""
    done: set[str] = set()
    path: set[str] = set()

    def visit(name: str) -> bool:
        if name in path:
            return True
        if name in done or name not in stages:
            return False
        path.add(name)
        found = any(visit(dep) for dep in stages[name].depends_on)
        path.discard(name)
        done.add(name)
        return found

    return any(visit(name) for name in stages)
