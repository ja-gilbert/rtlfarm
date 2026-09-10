"""Pipeline loading and validation: the rules an author of rtlfarm.yaml can
break, each reported at a JSON-pointer path, and all of them at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rtlfarm.expand import pipeline
from rtlfarm.expand.pipeline import PipelineError, parse_pipeline
from rtlfarm.models import MAX_SEED, MAX_SEEDS_PER_TARGET

################################################################################
# A Valid Pipeline to Mutate
################################################################################


def _valid() -> dict[str, Any]:
    return {
        "version": 1,
        "design": {
            "name": "counter",
            "files": {
                "rtl": ["rtl/*.sv"],
                "include": ["tb/rtlfarm_tb.svh"],
                "tb": ["tb/*.sv"],
                "data": ["tb/vectors/*.hex"],
            },
            "include_dirs": ["tb"],
            "defines": {"SIM": 1},
            "params": {"WIDTH": 8},
            "timescale": "1ns/1ps",
        },
        "toolchain": {"digest": None},
        "targets": [
            {
                "name": "tb_basic",
                "top": "tb_basic",
                "tb": ["tb/tb_basic.sv"],
                "data": [],
                "seeds": {"n": 2, "base": 1},
                "tags": ["smoke"],
                "timeout_sim": "10ms",
            },
            {
                "name": "tb_random",
                "top": "tb_random",
                "tb": ["tb/tb_random.sv"],
                "data": ["tb/vectors/random.hex"],
                "seeds": [3, 17, 1017],
                "tags": ["nightly"],
                "timeout_sim": "1us",
                "plusargs": {"mode": "fast"},
                "params": {"WIDTH": 16},
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
                "consumes_artifacts": ["compiled"],
                "depends_on": ["compile"],
                "fan_out": "targets",
                "timeout_s": 300,
            },
        },
        "policies": {
            "retries": {"infra": 3, "timeout": 0, "tool_error": 1},
            "cache": {"lint": True, "compile": True, "simulate": True},
        },
    }


def _pointers(data: object) -> list[str]:
    with pytest.raises(PipelineError) as info:
        parse_pipeline(data)
    return sorted(issue.pointer for issue in info.value.issues)


def _mutate(path: list[str | int], value: Any) -> dict[str, Any]:
    data = _valid()
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return data


def _without(path: list[str | int]) -> dict[str, Any]:
    data = _valid()
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return data


################################################################################
# A Valid File
################################################################################


def test_valid_pipeline_parses() -> None:
    p = parse_pipeline(_valid())
    assert p.design.name == "counter"
    assert [t.name for t in p.targets] == ["tb_basic", "tb_random"]
    assert list(p.stages) == ["lint", "compile", "simulate"]
    assert p.stages["compile"].per_target is True
    assert p.stages["simulate"].fan_out == "targets"
    assert p.toolchain.digest is None


def test_seed_range_and_seed_list_expand_in_order() -> None:
    p = parse_pipeline(_valid())
    assert p.targets[0].seeds == [1, 2]
    assert p.targets[1].seeds == [3, 17, 1017]


def test_malformed_yaml_is_reported_at_the_root(tmp_path: Path) -> None:
    (tmp_path / "rtlfarm.yaml").write_text("design: [unclosed", encoding="utf-8")
    with pytest.raises(PipelineError) as info:
        pipeline.load_pipeline(tmp_path)
    assert info.value.issues[0].pointer == ""
    assert "YAML" in info.value.issues[0].message


################################################################################
# Shape Rules (JSON Pointers From the Model)
################################################################################

# One row per class of shape error: the mutated document and the pointer its
# error must carry. The rules live in the models; only the placement of the
# pointer (root, a key, a missing field) is checked here.
SHAPE_ERRORS = [
    pytest.param(["not", "a", "mapping"], "", id="document-not-a-mapping"),
    pytest.param(_mutate(["design", "bogus"], 1), "/design/bogus", id="unknown-key"),
    pytest.param(_without(["targets", 0, "top"]), "/targets/0/top", id="missing-field"),
]


@pytest.mark.parametrize(("data", "pointer"), SHAPE_ERRORS)
def test_shape_errors_report_the_offending_pointer(data: object, pointer: str) -> None:
    """An author fixes the file by pointer; a typo'd key silently ignored
    would leave a stage doing the wrong thing."""
    assert _pointers(data) == [pointer]


################################################################################
# Seeds
################################################################################


def test_seed_zero_is_reserved_in_a_list() -> None:
    assert _pointers(_mutate(["targets", 1, "seeds"], [3, 0, 5])) == [
        "/targets/1/seeds/1"
    ]


def test_seed_range_needs_at_least_one() -> None:
    """``n: 0`` would expand to a target with no simulate tasks: a run that
    reports success having run nothing."""
    assert _pointers(_mutate(["targets", 0, "seeds"], {"n": 0, "base": 1})) == [
        "/targets/0/seeds"
    ]


def test_seed_above_the_32_bit_limit() -> None:
    assert _pointers(_mutate(["targets", 1, "seeds"], [MAX_SEED + 1])) == [
        "/targets/1/seeds/0"
    ]
    p = parse_pipeline(_mutate(["targets", 1, "seeds"], [MAX_SEED]))
    assert p.targets[1].seeds == [MAX_SEED]


################################################################################
# Semantic Rules (Relating Parts of the File)
################################################################################


def test_stage_name_outside_the_closed_set() -> None:
    data = _valid()
    data["stages"]["synth"] = {"tool": "iverilog", "consumes": ["rtl"], "timeout_s": 5}
    assert _pointers(data) == ["/stages/synth"]


def test_depends_on_unknown_stage() -> None:
    data = _mutate(["stages", "simulate", "depends_on"], ["compile", "elaborate"])
    assert _pointers(data) == ["/stages/simulate/depends_on/1"]


def test_dependency_cycle() -> None:
    data = _mutate(["stages", "lint", "depends_on"], ["simulate"])
    assert _pointers(data) == ["/stages"]


def test_targeted_stages_without_targets() -> None:
    """Regression: no targets was reported at /selection, not the stage."""
    data = _mutate(["targets"], [])
    assert _pointers(data) == [
        "/stages/compile/per_target",
        "/stages/simulate/fan_out",
    ]


def test_depends_on_listed_twice() -> None:
    """Regression: duplicate task_deps rows, a 500 at submission."""
    data = _mutate(["stages", "simulate", "depends_on"], ["compile", "compile"])
    assert _pointers(data) == ["/stages/simulate/depends_on/1"]


def test_empty_stages_are_rejected() -> None:
    """Regression: a pipeline with no stages was accepted."""
    data = _mutate(["stages"], {})
    assert _pointers(data) == ["/stages"]


def test_pinned_toolchain_digest_must_be_a_sha256() -> None:
    """Regression: 'any' reached the schema CHECK as a 500."""
    data = _mutate(["toolchain", "digest"], "any")
    assert _pointers(data) == ["/toolchain/digest"]
    assert (
        parse_pipeline(_mutate(["toolchain", "digest"], "e" * 64)).toolchain.digest
        == "e" * 64
    )


def test_seed_count_is_capped() -> None:
    """Regression: seed ranges were unbounded; memory was the limit."""
    assert _pointers(
        _mutate(["targets", 0, "seeds"], {"n": MAX_SEEDS_PER_TARGET + 1, "base": 1})
    ) == ["/targets/0/seeds"]
    assert _pointers(
        _mutate(["targets", 1, "seeds"], list(range(1, MAX_SEEDS_PER_TARGET + 2)))
    ) == ["/targets/1/seeds"]
    p = parse_pipeline(
        _mutate(["targets", 0, "seeds"], {"n": MAX_SEEDS_PER_TARGET, "base": 1})
    )
    assert len(p.targets[0].seeds) == MAX_SEEDS_PER_TARGET


def test_reserved_plusargs() -> None:
    data = _mutate(["targets", 1, "plusargs"], {"seed": 4, "dump": 1, "mode": "x"})
    assert _pointers(data) == ["/targets/1/plusargs/dump", "/targets/1/plusargs/seed"]


def test_a_seed_listed_twice_is_rejected() -> None:
    """Regression: two equal seeds expanded to two identical task ids and the
    submit route died on the primary key, an unhandled 500."""
    assert _pointers(_mutate(["targets", 1, "seeds"], [3, 17, 3])) == [
        "/targets/1/seeds/2"
    ]


def test_duplicate_target_names() -> None:
    data = _mutate(["targets", 1, "name"], "tb_basic")
    assert _pointers(data) == ["/targets/1/name"]


################################################################################
# Reporting
################################################################################


def test_every_semantic_problem_is_reported_together() -> None:
    data = _valid()
    data["stages"]["lint"]["depends_on"] = ["nothing"]
    data["targets"][0]["plusargs"] = {"seed": 1}
    data["policies"]["cache"]["extra"] = True
    assert _pointers(data) == [
        "/policies/cache/extra",
        "/stages/lint/depends_on/0",
        "/targets/0/plusargs/seed",
    ]
