"""The pipeline definition as data: what ``rtlfarm.yaml`` may say.

These models describe shape and simple bounds only (pydantic does that
checking). Rules that relate one part of the file to another, such as a
``depends_on`` naming a stage that exists, live in ``rtlfarm.expand.pipeline``
so that every rule reports a JSON-pointer path into the file.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from rtlfarm.ids import NAME_RE

#: A design, target, stage or tag name.
Name = Annotated[str, StringConstraints(pattern=NAME_RE.pattern)]

#: The file roles a pipeline may declare and a stage may consume.
ROLES: tuple[str, ...] = ("rtl", "include", "tb", "data")
Role = Literal["rtl", "include", "tb", "data"]

#: The tools the Core tier can run.
TOOLS: tuple[str, ...] = ("fake", "iverilog")
Tool = Literal["fake", "iverilog"]

#: The stage kinds of the Core pipeline; the stage name is its kind.
STAGE_KINDS: tuple[str, ...] = ("lint", "compile", "simulate", "coverage")

#: The upstream artifact kinds a stage may consume.
ARTIFACT_KINDS: tuple[str, ...] = ("compiled",)

#: The largest legal seed: the testbench reads it into a 32-bit integer.
MAX_SEED = 2**31 - 1

#: Plusargs the runner injects; a target may not set them.
RESERVED_PLUSARGS: frozenset[str] = frozenset({"seed", "dump"})

#: A simulation time such as ``10ms`` or ``1.5us``.
SIM_TIME_RE = re.compile(r"^\d+(\.\d+)?(fs|ps|ns|us|ms|s)$")
SimTime = Annotated[str, StringConstraints(pattern=SIM_TIME_RE.pattern)]

#: A plusarg or define value: a scalar, rendered as text on the command line.
Scalar = str | int | float | bool


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Design(_Frozen):
    name: Name
    files: dict[Role, list[str]]
    include_dirs: list[str] = Field(default_factory=list)
    defines: dict[str, Scalar] = Field(default_factory=dict)
    params: dict[str, Scalar] = Field(default_factory=dict)
    timescale: str | None = None


class Toolchain(_Frozen):
    digest: str | None = None


class Target(_Frozen):
    name: Name
    top: Name
    tb: list[str] = Field(default_factory=list)
    data: list[str] = Field(default_factory=list)
    seeds: list[Annotated[int, Field(ge=1, le=MAX_SEED)]] = Field(
        default_factory=lambda: [1]
    )
    tags: list[Name] = Field(default_factory=list)
    timeout_sim: SimTime
    timeout_s: int | None = Field(default=None, gt=0)
    allow_zero_time: bool = False
    plusargs: dict[str, Scalar] = Field(default_factory=dict)
    params: dict[str, Scalar] = Field(default_factory=dict)
    defines: dict[str, Scalar] = Field(default_factory=dict)

    @field_validator("seeds", mode="before")
    @classmethod
    def _expand_seed_range(cls, value: object) -> object:
        """``{n, base}`` denotes ``base, base + 1, …, base + n - 1``."""
        if not isinstance(value, dict):
            return value
        if set(value) != {"n", "base"}:
            raise ValueError("a seed range has exactly the keys n and base")
        n, base = value["n"], value["base"]
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValueError("n must be an integer of at least 1")
        if not isinstance(base, int) or isinstance(base, bool) or base < 1:
            raise ValueError("base must be an integer of at least 1 (0 is reserved)")
        return list(range(base, base + n))


class Stage(_Frozen):
    tool: Tool
    consumes: list[Role] = Field(default_factory=list)
    consumes_artifacts: list[str] = Field(default_factory=lambda: ["compiled"])
    depends_on: list[str] = Field(default_factory=list)
    per_target: bool = False
    fan_out: Literal["targets"] | None = None
    timeout_s: int = Field(gt=0)


class Retries(_Frozen):
    infra: int = Field(default=3, ge=0)
    timeout: int = Field(default=0, ge=0)
    tool_error: int = Field(default=1, ge=0)


class Policies(_Frozen):
    retries: Retries = Retries()
    cache: dict[str, bool] = Field(default_factory=dict)


class Pipeline(_Frozen):
    version: Literal[1]
    design: Design
    toolchain: Toolchain = Toolchain()
    targets: list[Target] = Field(default_factory=list)
    stages: dict[str, Stage]
    policies: Policies = Policies()

    def stage_timeouts(self) -> list[int]:
        """Every stage's wall-clock timeout, for ``validate_timing``."""
        return [stage.timeout_s for stage in self.stages.values()]
