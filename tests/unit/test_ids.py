"""Identity: names, ULIDs, task and attempt id composition."""

from __future__ import annotations

import re

import pytest

from rtlfarm import ids
from rtlfarm.db.migrate import load_migrations

JOB = "01ARYZ6S41TSV4RRFFQ69G5FAV"


################################################################################
# Names
################################################################################


@pytest.mark.parametrize(
    "name", ["counter", "tb_basic", "fifo-sync", "A", "x" * 64, "0"]
)
def test_valid_names_are_returned_unchanged(name: str) -> None:
    assert ids.validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "x" * 65, "a b", "a/b", "a.b", "a:b", "ünïcode", "tb\n", "../x"],
)
def test_invalid_names_are_rejected(name: str) -> None:
    with pytest.raises(ids.InvalidName):
        ids.validate_name(name)


################################################################################
# Task and Attempt Ids
################################################################################


def test_attempt_id_is_the_task_id_plus_attempt_number() -> None:
    task = ids.task_id(JOB, "compile", "tb_basic", 0)
    assert ids.attempt_id(task, 1) == f"{JOB}.compile.tb_basic.s0.a1"
    assert ids.attempt_id(task, 12) == f"{JOB}.compile.tb_basic.s0.a12"


@pytest.mark.parametrize("n", [0, -1])
def test_attempt_numbers_start_at_one(n: int) -> None:
    with pytest.raises(ids.IdError, match="attempt"):
        ids.attempt_id(ids.task_id(JOB, "lint"), n)


@pytest.mark.parametrize(
    ("job", "stage", "target", "seed", "match"),
    [
        pytest.param(JOB, "synthesize", "tb_basic", 1, "synthesize", id="stage-kind"),
        pytest.param(JOB, "simulate", "tb basic", 1, "tb basic", id="target-name"),
        pytest.param(JOB, "simulate", "tb_basic", -1, "seed", id="negative-seed"),
        pytest.param("not-a-ulid", "simulate", "tb_basic", 1, "job_id", id="job-id"),
    ],
)
def test_task_id_rejects_a_part_that_would_make_an_unsafe_id(
    job: str, stage: str, target: str, seed: int, match: str
) -> None:
    """Every part is checked, so an id is always unambiguous and safe as a
    path segment and a URL segment."""
    with pytest.raises(ids.IdError, match=match):
        ids.task_id(job, stage, target, seed)


def test_every_stage_kind_is_admitted_by_the_schema() -> None:
    """The pipeline validator accepts exactly ``ids.STAGE_KINDS``; each of them
    must also pass the tasks table's CHECK, or a valid pipeline would fail at
    insert. The schema may admit more (the Preferred wave kinds)."""
    (first,) = [m for m in load_migrations() if m.version == 1]
    check = re.search(r"CHECK\(stage_kind IN \(([^)]*)\)\)", first.sql)
    assert check is not None
    admitted = {kind.strip().strip("'") for kind in check.group(1).split(",")}
    assert set(ids.STAGE_KINDS) <= admitted


################################################################################
# ULIDs
################################################################################

ULID_ALPHABET = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_ulid_is_26_crockford_characters() -> None:
    ulid = ids.new_ulid(now_ms=1_700_000_000_000)
    assert len(ulid) == 26
    assert set(ulid) <= ULID_ALPHABET


@pytest.mark.parametrize(
    ("now_ms", "prefix"),
    [(1_469_918_176_385, "01ARYZ6S41"), (0, "0000000000")],
    ids=["specification-reference-vector", "time-zero"],
)
def test_ulid_encodes_the_timestamp_in_its_first_ten_characters(
    now_ms: int, prefix: str
) -> None:
    assert ids.new_ulid(now_ms=now_ms).startswith(prefix)


def test_ulids_sort_by_creation_time() -> None:
    earlier = ids.new_ulid(now_ms=1_000)
    later = ids.new_ulid(now_ms=2_000)
    assert earlier < later


def test_ulids_at_the_same_millisecond_differ() -> None:
    a = ids.new_ulid(now_ms=5_000)
    b = ids.new_ulid(now_ms=5_000)
    assert a[:10] == b[:10]
    assert a != b


@pytest.mark.parametrize("bad", [-1, 2**48])
def test_ulid_timestamp_out_of_range_is_rejected(bad: int) -> None:
    with pytest.raises(ids.IdError, match="now_ms"):
        ids.new_ulid(now_ms=bad)


def test_a_fresh_ulid_is_a_valid_job_id() -> None:
    job = ids.new_ulid(now_ms=1_700_000_000_000)
    assert ids.task_id(job, "lint").startswith(job)
