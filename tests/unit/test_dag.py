"""Static DAG expansion: the rules no shipped pack exercises. The full
expansion of the shipped packs is pinned by ``test_examples``, and the
selection rules run through the submit route in ``fast/test_submit``.
"""

from __future__ import annotations

from typing import Any

from rtlfarm.expand.dag import (
    ExpandedJob,
    Selection,
    expand,
    submission_hash,
)
from rtlfarm.expand.pack import Manifest, ManifestEntry
from rtlfarm.expand.pipeline import parse_pipeline

JOB = "01J0000000000000000000ABCD"
DIGEST = "sha256:" + "t" * 64

################################################################################
# A Manifest Built in Memory
################################################################################


def _pipeline() -> dict[str, Any]:
    return {
        "version": 1,
        "design": {
            "name": "counter",
            "files": {
                "rtl": ["pkg/*.sv", "rtl/*.sv"],
                "include": ["tb/*.svh"],
                "tb": ["tb/*.sv"],
                "data": ["tb/vectors/*.hex"],
            },
            "include_dirs": ["tb"],
            "defines": {"SIM": 1, "WIDTH_DEF": 8},
            "params": {"WIDTH": 8, "DEPTH": 4},
            "timescale": "1ns/1ps",
        },
        "targets": [
            {
                "name": "tb_basic",
                "top": "tb_basic",
                "tb": ["tb/tb_basic.sv"],
                "data": ["tb/vectors/basic.hex"],
                "seeds": {"n": 2, "base": 1},
                "tags": ["smoke", "nightly"],
                "timeout_sim": "10ms",
            },
            {
                "name": "tb_random",
                "top": "tb_random",
                "tb": ["tb/tb_random.sv", "tb/tb_common.sv"],
                "data": [],
                "seeds": [3, 17, 1017],
                "tags": ["nightly"],
                "timeout_sim": "1us",
                "timeout_s": 45,
                "plusargs": {"mode": "fast"},
                "params": {"WIDTH": 16},
                "defines": {"RANDOM": 1},
            },
        ],
        "stages": {
            "lint": {
                "tool": "iverilog",
                "consumes": ["rtl", "include"],
                "timeout_s": 60,
            },
            "compile": {
                "tool": "iverilog",
                "consumes": ["rtl", "include", "tb"],
                "depends_on": ["lint"],
                "per_target": True,
                "timeout_s": 300,
            },
            "simulate": {
                "tool": "iverilog",
                "consumes": ["data"],
                "depends_on": ["compile"],
                "fan_out": "targets",
                "timeout_s": 120,
            },
        },
        "policies": {
            "retries": {"infra": 2, "timeout": 1, "tool_error": 3},
            "cache": {"lint": True, "compile": True, "simulate": False},
        },
    }


ENTRIES = [
    ("pkg/types_pkg.sv", "rtl", 0),
    ("rtl/adder.sv", "rtl", 1),
    ("rtl/counter.sv", "rtl", 2),
    ("tb/rtlfarm_tb.svh", "include", 0),
    ("tb/tb_basic.sv", "tb", 0),
    ("tb/tb_common.sv", "tb", 1),
    ("tb/tb_random.sv", "tb", 2),
    ("tb/vectors/basic.hex", "data", 0),
    ("tb/vectors/other.hex", "data", 1),
]


def _manifest(data: dict[str, Any] | None = None) -> Manifest:
    entries = tuple(
        ManifestEntry(path, role, ordinal, "a" * 63 + str(i), 10 + i)
        for i, (path, role, ordinal) in enumerate(ENTRIES)
    )
    return Manifest("counter", entries, parse_pipeline(data or _pipeline()))


EVERYTHING = Selection()


def _expand(
    manifest: Manifest | None = None, selection: Selection = EVERYTHING, **kw: Any
) -> ExpandedJob:
    options: dict[str, Any] = {
        "job_id": JOB,
        "priority": 5,
        "toolchain_digest": DIGEST,
        "no_cache": False,
    }
    options.update(kw)
    return expand(manifest or _manifest(), selection, **options)


def _ids(job: ExpandedJob, stage: str) -> list[str]:
    return [t.task_id for t in job.tasks if t.stage_kind == stage]


################################################################################
# Dependencies
################################################################################


def test_whole_design_stage_downstream_of_a_fan_out_depends_on_all_of_it() -> None:
    data = _pipeline()
    data["stages"]["coverage"] = {
        "tool": "fake",
        "consumes": [],
        "depends_on": ["simulate"],
        "timeout_s": 30,
    }
    job = _expand(_manifest(data))
    assert job.task(f"{JOB}.coverage._.s0").depends_on == tuple(_ids(job, "simulate"))


def test_a_stage_declared_before_its_dependency_still_expands() -> None:
    """Declaration order is not dependency order; expansion follows the graph.

    Regression: declaration order raised KeyError, a 500.
    """
    data = _pipeline()
    stages = data["stages"]
    data["stages"] = {
        "simulate": stages["simulate"],
        "compile": stages["compile"],
        "lint": stages["lint"],
    }
    job = _expand(_manifest(data))
    assert [t.stage_kind for t in job.tasks] == ["lint"] + ["compile"] * 2 + [
        "simulate"
    ] * 5
    assert job.task(f"{JOB}.simulate.tb_basic.s1").depends_on == (
        f"{JOB}.compile.tb_basic.s0",
    )
    assert job.task(f"{JOB}.compile.tb_basic.s0").depends_on == (f"{JOB}.lint._.s0",)


################################################################################
# What Each Task Reads
################################################################################


def test_consumes_order_beats_role_order() -> None:
    data = _pipeline()
    data["stages"]["lint"]["consumes"] = ["include", "rtl"]
    lint = _expand(_manifest(data)).task(f"{JOB}.lint._.s0")
    assert lint.inputs[0] == "tb/rtlfarm_tb.svh"


################################################################################
# Parameters and Budgets
################################################################################


def test_target_params_and_defines_merge_over_the_design() -> None:
    job = _expand()
    random = job.task(f"{JOB}.simulate.tb_random.s3")
    basic = job.task(f"{JOB}.simulate.tb_basic.s1")
    assert random.params["params"] == {"WIDTH": 16, "DEPTH": 4}
    assert random.params["defines"] == {"SIM": 1, "WIDTH_DEF": 8, "RANDOM": 1}
    assert basic.params["params"] == {"WIDTH": 8, "DEPTH": 4}
    assert basic.params["defines"] == {"SIM": 1, "WIDTH_DEF": 8}


def test_timeout_comes_from_the_target_when_it_overrides_the_stage() -> None:
    job = _expand()
    assert job.task(f"{JOB}.simulate.tb_random.s3").timeout_s == 45
    assert job.task(f"{JOB}.simulate.tb_basic.s1").timeout_s == 120
    assert job.task(f"{JOB}.lint._.s0").timeout_s == 60


def test_budgets_priority_digest_and_cache_flags() -> None:
    job = _expand(priority=9, no_cache=True)
    sim = job.task(f"{JOB}.simulate.tb_basic.s1")
    assert (sim.max_infra, sim.max_timeout, sim.max_tool_error) == (2, 1, 3)
    assert sim.priority == 9
    assert sim.toolchain_digest == DIGEST
    assert sim.bypass_cache is True
    assert sim.cacheable is False
    assert job.task(f"{JOB}.compile.tb_basic.s0").cacheable is True
    assert sim.affects_verdict is True
    assert sim.tool == "iverilog"


################################################################################
# A Pipeline Without Targets
################################################################################


def test_whole_design_only_pipeline_needs_no_targets() -> None:
    data = _pipeline()
    data["targets"] = []
    del data["stages"]["compile"]
    del data["stages"]["simulate"]
    data["policies"]["cache"] = {"lint": True}
    job = _expand(_manifest(data))
    assert [t.task_id for t in job.tasks] == [f"{JOB}.lint._.s0"]


################################################################################
# Submission Hash
################################################################################


def test_submission_hash_covers_manifest_and_selection() -> None:
    m = _manifest()
    base = submission_hash(m, Selection())
    assert base == submission_hash(m, Selection())
    assert base != submission_hash(m, Selection(tags=("smoke",)))
    assert base != submission_hash(m, Selection(seeds=1))
    changed = _pipeline()
    changed["design"]["params"]["WIDTH"] = 9
    assert base != submission_hash(_manifest(changed), Selection())
    assert _expand(m).submission_hash == base
