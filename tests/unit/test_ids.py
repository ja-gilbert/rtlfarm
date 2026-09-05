"""Identity: names, ULIDs, task and attempt id composition."""

from __future__ import annotations

import pytest

from rtlfarm import ids

JOB = "01ARYZ6S41TSV4RRFFQ69G5FAV"


# --- names -------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["counter", "tb_basic", "fifo-sync", "A", "x" * 64, "0"]
)
def test_valid_names_are_returned_unchanged(name: str) -> None:
    assert ids.validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "x" * 65, "a b", "a/b", "a.b", "a:b", "ünïcode", "tb\n", "../x", "_" * 0],
)
def test_invalid_names_are_rejected(name: str) -> None:
    with pytest.raises(ids.InvalidName):
        ids.validate_name(name)


# --- task and attempt ids ------------------------------------------------------


def test_task_id_composition_for_a_fan_out_stage() -> None:
    assert (
        ids.task_id(JOB, "simulate", "tb_basic", 17) == f"{JOB}.simulate.tb_basic.s17"
    )


def test_task_id_composition_for_a_whole_design_stage() -> None:
    task = ids.task_id(JOB, "lint", ids.WHOLE_DESIGN_TARGET, 0)
    assert task == f"{JOB}.lint._.s0"


def test_attempt_id_is_the_task_id_plus_attempt_number() -> None:
    task = ids.task_id(JOB, "compile", "tb_basic", 0)
    assert ids.attempt_id(task, 1) == f"{JOB}.compile.tb_basic.s0.a1"
    assert ids.attempt_id(task, 12) == f"{JOB}.compile.tb_basic.s0.a12"


@pytest.mark.parametrize("n", [0, -1])
def test_attempt_numbers_start_at_one(n: int) -> None:
    with pytest.raises(ids.IdError, match="attempt"):
        ids.attempt_id(ids.task_id(JOB, "lint"), n)


def test_task_id_rejects_an_unknown_stage_kind() -> None:
    with pytest.raises(ids.IdError, match="synthesize"):
        ids.task_id(JOB, "synthesize", "tb_basic", 1)


def test_task_id_rejects_a_bad_target_name() -> None:
    with pytest.raises(ids.InvalidName):
        ids.task_id(JOB, "simulate", "tb basic", 1)


def test_task_id_rejects_a_negative_seed() -> None:
    with pytest.raises(ids.IdError, match="seed"):
        ids.task_id(JOB, "simulate", "tb_basic", -1)


def test_task_id_rejects_a_malformed_job_id() -> None:
    with pytest.raises(ids.IdError, match="job_id"):
        ids.task_id("not-a-ulid", "simulate", "tb_basic", 1)


def test_stage_kinds_are_the_closed_set_of_the_spec() -> None:
    assert ids.STAGE_KINDS == ("lint", "compile", "simulate", "coverage")


# --- ULIDs -------------------------------------------------------------------

ULID_ALPHABET = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_ulid_is_26_crockford_characters() -> None:
    ulid = ids.new_ulid(now_ms=1_700_000_000_000)
    assert len(ulid) == 26
    assert set(ulid) <= ULID_ALPHABET


def test_ulid_encodes_the_timestamp_in_its_first_ten_characters() -> None:
    # The reference example from the ULID specification.
    assert ids.new_ulid(now_ms=1_469_918_176_385).startswith("01ARYZ6S41")


def test_ulid_time_zero_encodes_as_zeros() -> None:
    assert ids.new_ulid(now_ms=0).startswith("0000000000")


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
