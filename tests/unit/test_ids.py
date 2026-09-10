"""Identity: task ids that are safe as path and URL segments, the stage
vocabulary the schema admits, and ULIDs that order by creation time."""

from __future__ import annotations

import re

import pytest

from rtlfarm import ids
from rtlfarm.db.migrate import load_migrations

JOB = "01ARYZ6S41TSV4RRFFQ69G5FAV"


################################################################################
# Task Ids
################################################################################


@pytest.mark.parametrize(
    ("job", "stage", "target", "seed", "match"),
    [
        pytest.param(JOB, "synthesize", "tb_basic", 1, "synthesize", id="stage-kind"),
        pytest.param(JOB, "simulate", "tb/basic", 1, "tb/basic", id="target-name"),
    ],
)
def test_task_id_rejects_a_part_that_would_make_an_unsafe_id(
    job: str, stage: str, target: str, seed: int, match: str
) -> None:
    """Every part is checked, so an id is always unambiguous and safe as a
    path segment and a URL segment."""
    with pytest.raises(ids.IdError, match=match):
        ids.task_id(job, stage, target, seed)


def test_stage_kinds_are_the_core_set_and_the_schema_admits_each() -> None:
    """The stage vocabulary a pipeline may use is exactly the four Core kinds of
    spec §12.2, and each must pass the tasks table's CHECK or a valid pipeline
    would fail at insert. The schema may admit more (the Preferred wave kinds)."""
    assert ids.STAGE_KINDS == ("lint", "compile", "simulate", "coverage")
    (first,) = [m for m in load_migrations() if m.version == 1]
    check = re.search(r"CHECK\(stage_kind IN \(([^)]*)\)\)", first.sql)
    assert check is not None
    admitted = {kind.strip().strip("'") for kind in check.group(1).split(",")}
    assert set(ids.STAGE_KINDS) <= admitted


################################################################################
# ULIDs
################################################################################


def test_ulid_encodes_the_timestamp_in_its_first_ten_characters() -> None:
    """The ULID specification's reference vector; job ids are indexed by it."""
    assert ids.new_ulid(now_ms=1_469_918_176_385).startswith("01ARYZ6S41")


def test_ulids_sort_by_creation_time() -> None:
    earlier = ids.new_ulid(now_ms=1_000)
    later = ids.new_ulid(now_ms=2_000)
    assert earlier < later
