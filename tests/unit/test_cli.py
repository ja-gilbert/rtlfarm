"""The ``rtlfarm`` entry point: ``--help`` and the global options only, so far."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from rtlfarm.cli import main


def test_help_exits_zero_and_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: rtlfarm")
    for flag in ("--url", "--token", "--json"):
        assert flag in out


def test_no_verb_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage: rtlfarm" in capsys.readouterr().err


def test_unknown_flag_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--bogus"])
    assert info.value.code == 2


def test_installed_console_script_runs() -> None:
    exe = shutil.which("rtlfarm")
    assert exe is not None, "rtlfarm is not on PATH; run under `uv run`"
    result = subprocess.run(
        [exe, "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.startswith("usage: rtlfarm")
