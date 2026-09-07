"""The shipped example packs: they pack, they expand, and their expansion is
pinned by snapshot fixtures so that any change to a pack, to packing, or to
expansion shows up as a named diff.

Regenerate the snapshots after an intended change with
``RTLFARM_UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/test_examples.py``
and review the diff before committing it.
"""

from __future__ import annotations

import dataclasses
import json
import os
from importlib.resources import files
from pathlib import Path

import pytest

from rtlfarm.expand.dag import Selection, expand
from rtlfarm.expand.pack import pack

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
SNAPSHOTS = Path(__file__).resolve().parents[1] / "fixtures" / "dag"
UPDATE = os.environ.get("RTLFARM_UPDATE_SNAPSHOTS") == "1"

JOB = "01J0000000000000000000ABCD"
DIGEST = "sha256:" + "e" * 64

PACKS = ["fake_smoke", "counter"]
SELECTIONS = {
    "full": Selection(),
    "smoke": Selection(tags=("smoke",)),
    "seed7": Selection(seed=7),
}

################################################################################
# Snapshots
################################################################################


def _snapshot(name: str, selection: Selection) -> dict[str, object]:
    manifest = pack(EXAMPLES / name)
    job = expand(manifest, selection, job_id=JOB, priority=5, toolchain_digest=DIGEST)
    return {
        "manifest": manifest.to_dict(),
        "manifest_digest": manifest.digest(),
        "selection": selection.to_dict(),
        "job_id": job.job_id,
        "design": job.design,
        "submission_hash": job.submission_hash,
        "tasks": [dataclasses.asdict(task) for task in job.tasks],
        "dependencies": job.dependencies(),
    }


@pytest.mark.parametrize("name", PACKS)
@pytest.mark.parametrize("selection", list(SELECTIONS))
def test_expansion_matches_the_snapshot(name: str, selection: str) -> None:
    actual = json.dumps(
        _snapshot(name, SELECTIONS[selection]), indent=2, sort_keys=True
    )
    path = SNAPSHOTS / f"{name}_{selection}.json"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual + "\n", encoding="utf-8")
    assert path.is_file(), (
        f"missing snapshot {path.name}; run with RTLFARM_UPDATE_SNAPSHOTS=1"
    )
    assert actual + "\n" == path.read_text(encoding="utf-8")


################################################################################
# The Packs Themselves
################################################################################


def test_fake_smoke_is_two_targets_over_the_fake_tool() -> None:
    m = pack(EXAMPLES / "fake_smoke")
    assert m.design == "fake_smoke"
    assert [f.path for f in m.files] == [
        "src/design.txt",
        "tb/t_a.txt",
        "tb/t_b.txt",
        "data/t_a.txt",
        "data/t_b.txt",
    ]
    assert {s.tool for s in m.pipeline.stages.values()} == {"fake"}
    job = expand(m, Selection(), job_id=JOB, priority=5, toolchain_digest=DIGEST)
    assert [t.stage_kind for t in job.tasks] == ["compile"] * 2 + ["simulate"] * 5


def test_counter_is_lint_compile_simulate_over_icarus() -> None:
    m = pack(EXAMPLES / "counter")
    assert m.design == "counter"
    assert m.paths("rtl") == ["rtl/counter.sv"]
    assert m.paths("include") == ["tb/rtlfarm_tb.svh"]
    assert m.paths("tb") == ["tb/tb_counter.sv"]
    assert list(m.pipeline.stages) == ["lint", "compile", "simulate"]
    job = expand(m, Selection(), job_id=JOB, priority=5, toolchain_digest=DIGEST)
    assert [t.seed for t in job.tasks if t.stage_kind == "simulate"] == [1, 2, 3]


def test_counter_ships_the_testbench_header_verbatim() -> None:
    shipped = (files("rtlfarm.tools") / "rtlfarm_tb.svh").read_bytes()
    packed = (EXAMPLES / "counter" / "tb" / "rtlfarm_tb.svh").read_bytes()
    assert packed == shipped
    text = shipped.decode("utf-8")
    for macro in (
        "RTLFARM_INIT",
        "RTLFARM_SEED_PROCESS",
        "RTLFARM_WATCHDOG",
        "RTLFARM_PASS",
        "RTLFARM_FAIL",
    ):
        assert f"`define {macro}" in text
    assert "\r" not in text


def test_example_packs_have_no_pinned_toolchain() -> None:
    for name in PACKS:
        assert pack(EXAMPLES / name).pipeline.toolchain.digest is None
