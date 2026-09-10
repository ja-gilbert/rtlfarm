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


@pytest.mark.parametrize("value", [None, "true"], ids=["unset", "truthy-word"])
def test_hooks_are_inert_unless_the_variable_is_exactly_1(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """The hard rule: an armed hook does nothing unless RTLFARM_TEST_HOOKS=1."""
    if value is None:
        monkeypatch.delenv(hooks.ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(hooks.ENV_VAR, value)
    hooks.arm("after_claim_before_response", _boom)
    hooks.point("after_claim_before_response")


def test_armed_hook_fires_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(hooks.ENV_VAR, "1")
    hooks.arm("after_claim_before_response", _boom)
    with pytest.raises(Boom):
        hooks.point("after_claim_before_response")


def test_unknown_hook_name_is_rejected_even_while_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misspelled crash point would otherwise never fire, and a fault test
    would pass without injecting anything."""
    monkeypatch.delenv(hooks.ENV_VAR, raising=False)
    with pytest.raises(hooks.UnknownHook, match="after_claim"):
        hooks.point("after_claim")
