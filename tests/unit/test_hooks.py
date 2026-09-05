"""Fault-injection hooks are inert unless RTLFARM_TEST_HOOKS=1."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from rtlfarm import hooks


class Boom(Exception):
    pass


def _boom() -> None:
    raise Boom


@pytest.fixture(autouse=True)
def _clean_hooks() -> Iterator[None]:
    hooks.disarm_all()
    yield
    hooks.disarm_all()


def test_the_core_hook_set_is_the_six_of_the_spec() -> None:
    assert (
        frozenset(
            {
                "after_claim_before_response",
                "after_commit_before_response",
                "after_blob_rename_before_row",
                "after_task_terminal_before_job_aggregate",
                "between_tick_steps",
                "before_expiry_commit",
            }
        )
        == hooks.HOOK_NAMES
    )


def test_point_is_a_no_op_without_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(hooks.ENV_VAR, raising=False)
    hooks.arm("after_claim_before_response", _boom)
    hooks.point("after_claim_before_response")


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "2"])
def test_only_the_value_1_enables_hooks(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(hooks.ENV_VAR, value)
    hooks.arm("after_claim_before_response", _boom)
    hooks.point("after_claim_before_response")


def test_armed_hook_fires_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(hooks.ENV_VAR, "1")
    hooks.arm("after_claim_before_response", _boom)
    with pytest.raises(Boom):
        hooks.point("after_claim_before_response")


def test_unarmed_hook_is_a_no_op_even_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(hooks.ENV_VAR, "1")
    hooks.arm("before_expiry_commit", _boom)
    hooks.point("after_claim_before_response")


def test_disarm_all_clears_every_armed_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(hooks.ENV_VAR, "1")
    hooks.arm("after_claim_before_response", _boom)
    hooks.disarm_all()
    hooks.point("after_claim_before_response")


@pytest.mark.parametrize("enabled", [True, False])
def test_unknown_hook_name_is_rejected(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    if enabled:
        monkeypatch.setenv(hooks.ENV_VAR, "1")
    else:
        monkeypatch.delenv(hooks.ENV_VAR, raising=False)
    with pytest.raises(hooks.UnknownHook, match="after_claim"):
        hooks.point("after_claim")


def test_arming_an_unknown_hook_is_rejected() -> None:
    with pytest.raises(hooks.UnknownHook, match="nope"):
        hooks.arm("nope", _boom)
