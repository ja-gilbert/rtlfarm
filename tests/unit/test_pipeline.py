"""Pipeline loading and validation: every rule reports a JSON-pointer path,
every problem is reported at once, and a valid file becomes a frozen model.
"""

from __future__ import annotations

import copy
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


def _pointers(data: dict[str, Any]) -> list[str]:
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


def test_model_is_frozen() -> None:
    p = parse_pipeline(_valid())
    with pytest.raises(Exception, match="frozen"):
        p.design.name = "other"


def test_defaults_are_filled_in() -> None:
    data = _valid()
    del data["stages"]["simulate"]["consumes_artifacts"]
    del data["policies"]
    del data["toolchain"]
    p = parse_pipeline(data)
    assert p.stages["simulate"].consumes_artifacts == ["compiled"]
    assert p.policies.retries.infra == 3
    assert p.policies.cache == {}
    assert p.toolchain.digest is None


def test_seed_range_and_seed_list_expand_in_order() -> None:
    p = parse_pipeline(_valid())
    assert p.targets[0].seeds == [1, 2]
    assert p.targets[1].seeds == [3, 17, 1017]


def test_seeds_default_to_one_run_with_seed_one() -> None:
    assert parse_pipeline(_without(["targets", 0, "seeds"])).targets[0].seeds == [1]


def test_stage_timeouts_are_exposed_for_validate_timing() -> None:
    assert parse_pipeline(_valid()).stage_timeouts() == [60, 300, 300]


def test_load_from_a_directory_and_from_a_file(tmp_path: Path) -> None:
    text = (
        "version: 1\n"
        "design:\n  name: d\n  files: {rtl: ['rtl/*.sv']}\n"
        "stages:\n  lint: {tool: iverilog, consumes: [rtl], timeout_s: 10}\n"
    )
    (tmp_path / pipeline.PIPELINE_FILENAME).write_text(text, encoding="utf-8")
    assert pipeline.load_pipeline(tmp_path).design.name == "d"
    assert pipeline.load_pipeline(tmp_path / "rtlfarm.yaml").design.name == "d"


def test_malformed_yaml_is_reported_at_the_root(tmp_path: Path) -> None:
    (tmp_path / "rtlfarm.yaml").write_text("design: [unclosed", encoding="utf-8")
    with pytest.raises(PipelineError) as info:
        pipeline.load_pipeline(tmp_path)
    assert info.value.issues[0].pointer == ""
    assert "YAML" in info.value.issues[0].message


def test_non_mapping_document_is_rejected() -> None:
    assert _pointers(["not", "a", "mapping"]) == [""]  # type: ignore[arg-type]


################################################################################
# Shape Rules (JSON Pointers From the Model)
################################################################################


def test_unknown_version() -> None:
    assert _pointers(_mutate(["version"], 2)) == ["/version"]


def test_unknown_key_is_rejected() -> None:
    assert _pointers(_mutate(["design", "bogus"], 1)) == ["/design/bogus"]


def test_missing_required_field() -> None:
    assert _pointers(_without(["targets", 0, "top"])) == ["/targets/0/top"]


def test_unknown_tool() -> None:
    assert _pointers(_mutate(["stages", "lint", "tool"], "vcs")) == [
        "/stages/lint/tool"
    ]


def test_unknown_role_in_files() -> None:
    assert _pointers(_mutate(["design", "files", "docs"], ["*.md"])) == [
        "/design/files/docs"
    ]


def test_unknown_role_in_consumes() -> None:
    data = _mutate(["stages", "lint", "consumes"], ["rtl", "docs"])
    assert _pointers(data) == ["/stages/lint/consumes/1"]


@pytest.mark.parametrize("bad", ["", "a b", "x" * 65, "has/slash", "dot.name"])
def test_names_outside_the_pattern(bad: str) -> None:
    assert _pointers(_mutate(["design", "name"], bad)) == ["/design/name"]
    assert _pointers(_mutate(["targets", 1, "name"], bad)) == ["/targets/1/name"]
    assert _pointers(_mutate(["targets", 0, "tags"], [bad])) == ["/targets/0/tags/0"]


@pytest.mark.parametrize("bad", ["10", "ms", "10 ms", "-1ns", "1.5"])
def test_timeout_sim_format(bad: str) -> None:
    assert _pointers(_mutate(["targets", 0, "timeout_sim"], bad)) == [
        "/targets/0/timeout_sim"
    ]


def test_timeout_sim_is_required() -> None:
    assert _pointers(_without(["targets", 0, "timeout_sim"])) == [
        "/targets/0/timeout_sim"
    ]


def test_stage_timeout_must_be_positive() -> None:
    assert _pointers(_mutate(["stages", "lint", "timeout_s"], 0)) == [
        "/stages/lint/timeout_s"
    ]


def test_retry_budgets_must_not_be_negative() -> None:
    assert _pointers(_mutate(["policies", "retries", "infra"], -1)) == [
        "/policies/retries/infra"
    ]


################################################################################
# Seeds
################################################################################


def test_seed_zero_is_reserved_in_a_list() -> None:
    assert _pointers(_mutate(["targets", 1, "seeds"], [3, 0, 5])) == [
        "/targets/1/seeds/1"
    ]


def test_seed_zero_is_reserved_as_a_base() -> None:
    assert _pointers(_mutate(["targets", 0, "seeds"], {"n": 1, "base": 0})) == [
        "/targets/0/seeds"
    ]


def test_seed_above_the_32_bit_limit() -> None:
    assert _pointers(_mutate(["targets", 1, "seeds"], [MAX_SEED + 1])) == [
        "/targets/1/seeds/0"
    ]
    p = parse_pipeline(_mutate(["targets", 1, "seeds"], [MAX_SEED]))
    assert p.targets[1].seeds == [MAX_SEED]


def test_seed_range_needs_at_least_one() -> None:
    assert _pointers(_mutate(["targets", 0, "seeds"], {"n": 0, "base": 1})) == [
        "/targets/0/seeds"
    ]


def test_seed_range_with_extra_keys_is_rejected() -> None:
    assert _pointers(
        _mutate(["targets", 0, "seeds"], {"n": 1, "base": 1, "step": 2})
    ) == ["/targets/0/seeds"]


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


def test_self_dependency_is_a_cycle() -> None:
    data = _mutate(["stages", "lint", "depends_on"], ["lint"])
    assert _pointers(data) == ["/stages"]


def test_targeted_stages_without_targets() -> None:
    data = _mutate(["targets"], [])
    assert _pointers(data) == [
        "/stages/compile/per_target",
        "/stages/simulate/fan_out",
    ]


def test_depends_on_listed_twice() -> None:
    data = _mutate(["stages", "simulate", "depends_on"], ["compile", "compile"])
    assert _pointers(data) == ["/stages/simulate/depends_on/1"]


def test_empty_stages_are_rejected() -> None:
    data = _mutate(["stages"], {})
    assert _pointers(data) == ["/stages"]


def test_pinned_toolchain_digest_must_be_a_sha256() -> None:
    data = _mutate(["toolchain", "digest"], "any")
    assert _pointers(data) == ["/toolchain/digest"]
    assert (
        parse_pipeline(_mutate(["toolchain", "digest"], "e" * 64)).toolchain.digest
        == "e" * 64
    )


def test_seed_count_is_capped() -> None:
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


def test_per_target_and_fan_out_are_exclusive() -> None:
    data = _mutate(["stages", "compile", "fan_out"], "targets")
    assert _pointers(data) == ["/stages/compile"]


def test_reserved_plusargs() -> None:
    data = _mutate(["targets", 1, "plusargs"], {"seed": 4, "dump": 1, "mode": "x"})
    assert _pointers(data) == ["/targets/1/plusargs/dump", "/targets/1/plusargs/seed"]


def test_coverage_over_icarus() -> None:
    data = _valid()
    data["stages"]["coverage"] = {
        "tool": "iverilog",
        "consumes": [],
        "depends_on": ["simulate"],
        "timeout_s": 5,
    }
    assert _pointers(data) == ["/stages/coverage/tool"]


def test_consumes_role_that_declares_no_files() -> None:
    data = _without(["design", "files", "data"])
    assert _pointers(data) == ["/stages/simulate/consumes/0"]


def test_unknown_artifact_kind() -> None:
    data = _mutate(["stages", "simulate", "consumes_artifacts"], ["compiled", "waves"])
    assert _pointers(data) == ["/stages/simulate/consumes_artifacts/1"]


def test_simulate_needs_a_testbench_on_every_target() -> None:
    data = _mutate(["targets", 1, "tb"], [])
    assert _pointers(data) == ["/targets/1/tb"]


def test_duplicate_target_names() -> None:
    data = _mutate(["targets", 1, "name"], "tb_basic")
    assert _pointers(data) == ["/targets/1/name"]


def test_cache_policy_for_an_unknown_stage() -> None:
    data = _mutate(["policies", "cache"], {"lint": True, "synth": False})
    assert _pointers(data) == ["/policies/cache/synth"]


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


def test_error_message_lists_pointer_and_reason() -> None:
    with pytest.raises(PipelineError) as info:
        parse_pipeline(_mutate(["stages", "lint", "depends_on"], ["ghost"]))
    assert str(info.value) == "/stages/lint/depends_on/0: unknown stage 'ghost'"


def test_valid_input_is_not_mutated() -> None:
    data = _valid()
    snapshot = copy.deepcopy(data)
    parse_pipeline(data)
    assert data == snapshot
