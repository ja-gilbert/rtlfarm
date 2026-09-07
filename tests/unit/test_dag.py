"""Static DAG expansion: which tasks a manifest and a selection produce, what
each reads, what it depends on, and its budgets; plus selection and the
submission hash.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from rtlfarm.expand.dag import (
    ExpandedJob,
    Selection,
    SelectionError,
    TaskSpec,
    expand,
    select_targets,
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
# Which Tasks Exist
################################################################################


def test_whole_design_stage_is_one_task_with_the_placeholder_target() -> None:
    job = _expand()
    lint = job.task(f"{JOB}.lint._.s0")
    assert lint.target == "_"
    assert lint.seed == 0
    assert _ids(job, "lint") == [f"{JOB}.lint._.s0"]


def test_per_target_stage_is_one_task_per_target_at_seed_zero() -> None:
    assert _ids(_expand(), "compile") == [
        f"{JOB}.compile.tb_basic.s0",
        f"{JOB}.compile.tb_random.s0",
    ]


def test_fan_out_stage_is_one_task_per_target_and_seed() -> None:
    assert _ids(_expand(), "simulate") == [
        f"{JOB}.simulate.tb_basic.s1",
        f"{JOB}.simulate.tb_basic.s2",
        f"{JOB}.simulate.tb_random.s3",
        f"{JOB}.simulate.tb_random.s17",
        f"{JOB}.simulate.tb_random.s1017",
    ]


def test_task_order_is_stage_then_target_then_seed_and_ids_are_unique() -> None:
    job = _expand()
    kinds = [t.stage_kind for t in job.tasks]
    assert kinds == ["lint"] + ["compile"] * 2 + ["simulate"] * 5
    assert len({t.task_id for t in job.tasks}) == len(job.tasks)


def test_expansion_is_deterministic() -> None:
    assert _expand() == _expand()


################################################################################
# Dependencies
################################################################################


def test_per_target_task_depends_on_the_whole_design_task() -> None:
    job = _expand()
    assert job.task(f"{JOB}.compile.tb_basic.s0").depends_on == (f"{JOB}.lint._.s0",)


def test_fan_out_task_depends_on_its_own_target_compile() -> None:
    job = _expand()
    assert job.task(f"{JOB}.simulate.tb_random.s17").depends_on == (
        f"{JOB}.compile.tb_random.s0",
    )


def test_root_task_has_no_dependencies() -> None:
    assert _expand().task(f"{JOB}.lint._.s0").depends_on == ()


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


def test_dependency_rows_are_success_edges_in_task_order() -> None:
    rows = _expand().dependencies()
    assert rows[0] == (f"{JOB}.compile.tb_basic.s0", f"{JOB}.lint._.s0", "success")
    assert len(rows) == 2 + 5
    assert {kind for _, _, kind in rows} == {"success"}


################################################################################
# What Each Task Reads
################################################################################


def test_whole_design_task_reads_every_file_of_its_roles_in_consumes_order() -> None:
    lint = _expand().task(f"{JOB}.lint._.s0")
    assert lint.inputs == [
        "pkg/types_pkg.sv",
        "rtl/adder.sv",
        "rtl/counter.sv",
        "tb/rtlfarm_tb.svh",
    ]
    assert lint.params["consumes"] == ["rtl", "include"]


def test_per_target_task_narrows_tb_to_the_target_in_manifest_order() -> None:
    job = _expand()
    assert job.task(f"{JOB}.compile.tb_random.s0").inputs == [
        "pkg/types_pkg.sv",
        "rtl/adder.sv",
        "rtl/counter.sv",
        "tb/rtlfarm_tb.svh",
        "tb/tb_common.sv",
        "tb/tb_random.sv",
    ]
    assert job.task(f"{JOB}.compile.tb_basic.s0").inputs[-1] == "tb/tb_basic.sv"


def test_fan_out_task_narrows_data_to_the_target() -> None:
    job = _expand()
    assert job.task(f"{JOB}.simulate.tb_basic.s1").inputs == ["tb/vectors/basic.hex"]
    assert job.task(f"{JOB}.simulate.tb_random.s3").inputs == []


def test_consumes_order_beats_role_order() -> None:
    data = _pipeline()
    data["stages"]["lint"]["consumes"] = ["include", "rtl"]
    lint = _expand(_manifest(data)).task(f"{JOB}.lint._.s0")
    assert lint.inputs[0] == "tb/rtlfarm_tb.svh"


def test_consumes_artifacts_default_and_override() -> None:
    job = _expand()
    assert job.task(f"{JOB}.simulate.tb_basic.s1").consumes_artifacts == ("compiled",)
    assert job.task(f"{JOB}.lint._.s0").consumes_artifacts == ("compiled",)


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


def test_targeted_task_params_carry_the_simulation_settings() -> None:
    random = _expand().task(f"{JOB}.simulate.tb_random.s17")
    assert random.params["top"] == "tb_random"
    assert random.params["plusargs"] == {"mode": "fast"}
    assert random.params["timeout_sim"] == "1us"
    assert random.params["allow_zero_time"] is False
    assert random.params["timescale"] == "1ns/1ps"
    assert random.params["include_dirs"] == ["tb"]
    assert random.params["tb"] == ["tb/tb_random.sv", "tb/tb_common.sv"]
    assert "seed" not in random.params


def test_whole_design_task_params_have_no_target_settings() -> None:
    lint = _expand().task(f"{JOB}.lint._.s0")
    assert "top" not in lint.params
    assert "plusargs" not in lint.params
    assert lint.params["params"] == {"WIDTH": 8, "DEPTH": 4}


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


def test_cache_policy_defaults_to_cacheable() -> None:
    data = _pipeline()
    del data["policies"]["cache"]
    assert _expand(_manifest(data)).task(f"{JOB}.simulate.tb_basic.s1").cacheable


################################################################################
# Selection
################################################################################


def test_tag_selects_targets_carrying_any_named_tag() -> None:
    job = _expand(selection=Selection(tags=("smoke",)))
    assert {t.target for t in job.tasks if t.target != "_"} == {"tb_basic"}


def test_explicit_targets_select_by_name() -> None:
    job = _expand(selection=Selection(targets=("tb_random",)))
    assert _ids(job, "compile") == [f"{JOB}.compile.tb_random.s0"]


def test_unknown_target_is_an_error() -> None:
    with pytest.raises(SelectionError, match="tb_missing"):
        _expand(selection=Selection(targets=("tb_missing",)))


def test_seeds_cap_truncates_each_target_list() -> None:
    job = _expand(selection=Selection(seeds=2))
    assert _ids(job, "simulate") == [
        f"{JOB}.simulate.tb_basic.s1",
        f"{JOB}.simulate.tb_basic.s2",
        f"{JOB}.simulate.tb_random.s3",
        f"{JOB}.simulate.tb_random.s17",
    ]


def test_seed_pins_one_seed_for_every_target() -> None:
    job = _expand(selection=Selection(seed=99))
    assert _ids(job, "simulate") == [
        f"{JOB}.simulate.tb_basic.s99",
        f"{JOB}.simulate.tb_random.s99",
    ]


def test_selection_that_leaves_no_target_is_an_error() -> None:
    with pytest.raises(SelectionError, match="no targets"):
        _expand(selection=Selection(tags=("nosuchtag",)))


def test_whole_design_only_pipeline_needs_no_targets() -> None:
    data = _pipeline()
    data["targets"] = []
    del data["stages"]["compile"]
    del data["stages"]["simulate"]
    data["policies"]["cache"] = {"lint": True}
    job = _expand(_manifest(data))
    assert [t.task_id for t in job.tasks] == [f"{JOB}.lint._.s0"]


def test_select_targets_returns_targets_with_seeds_applied() -> None:
    p = parse_pipeline(_pipeline())
    kept = select_targets(p, Selection(tags=("nightly",), seeds=1))
    assert [(t.name, t.seeds) for t in kept] == [("tb_basic", [1]), ("tb_random", [3])]
    assert p.targets[1].seeds == [3, 17, 1017]


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


def test_expanded_job_is_frozen() -> None:
    job = _expand()
    with pytest.raises(dataclasses.FrozenInstanceError):
        job.design = "other"  # type: ignore[misc]
    task: TaskSpec = job.tasks[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.seed = 1  # type: ignore[misc]
