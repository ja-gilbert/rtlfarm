"""The ``rtlfarm`` entry point: the global options, ``dev pack``,
``dev pin-toolchain``, ``toolchain manifest``, ``control run`` and
``admin migrate``."""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from rtlfarm.cli import control as control_cli
from rtlfarm.cli import main
from rtlfarm.expand.pack import pack
from rtlfarm.tools import manifest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

################################################################################
# Global Options and Usage Errors
################################################################################


def test_help_exits_zero_and_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: rtlfarm")
    for flag in ("--url", "--token", "--json", "dev", "toolchain", "control", "admin"):
        assert flag in out


@pytest.mark.parametrize("argv", [[], ["dev"]], ids=["no-verb", "verb-without-command"])
def test_a_missing_verb_or_command_is_a_usage_error(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2
    assert "usage: rtlfarm" in capsys.readouterr().err


def test_an_unknown_flag_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """A mistyped flag exits 2 with the usage line instead of running the verb."""
    with pytest.raises(SystemExit) as info:
        main(["control", "run", "--prot", "8080"])
    assert info.value.code == 2
    assert "usage: rtlfarm" in capsys.readouterr().err


def test_installed_console_script_runs() -> None:
    exe = shutil.which("rtlfarm")
    assert exe is not None, "rtlfarm is not on PATH; run under `uv run`"
    result = subprocess.run(
        [exe, "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.startswith("usage: rtlfarm")


################################################################################
# dev pack
################################################################################


def test_dev_pack_prints_the_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["dev", "pack", str(EXAMPLES / "counter")]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["design"] == "counter"
    assert data["manifest_version"] == 1
    assert [f["role"] for f in data["files"]] == ["rtl", "include", "tb"]


def test_dev_pack_json_is_the_canonical_form(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--json", "dev", "pack", str(EXAMPLES / "fake_smoke")]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert out.rstrip("\n") == pack(EXAMPLES / "fake_smoke").canonical_json()


def test_dev_pack_exclude_leaves_a_file_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "pack"
    shutil.copytree(EXAMPLES / "fake_smoke", root)
    (root / "src" / "extra.txt").write_text("extra\n", encoding="utf-8")
    assert main(["dev", "pack", str(root), "--exclude", "src/extra.txt"]) == 0
    paths = [f["path"] for f in json.loads(capsys.readouterr().out)["files"]]
    assert "src/extra.txt" not in paths
    assert "src/design.txt" in paths


def test_dev_pack_reports_problems_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["dev", "pack", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "rtlfarm.yaml" in captured.err


def test_dev_pack_reports_pipeline_problems_by_pointer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "pack"
    shutil.copytree(EXAMPLES / "fake_smoke", root)
    text = (root / "rtlfarm.yaml").read_text(encoding="utf-8")
    (root / "rtlfarm.yaml").write_text(text.replace("version: 1", "version: 3"))
    assert main(["dev", "pack", str(root)]) == 1
    assert "/version" in capsys.readouterr().err


################################################################################
# toolchain manifest and dev pin-toolchain
################################################################################


def test_toolchain_manifest_prints_the_manifest_and_digest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["toolchain", "manifest"]) == 0
    out = capsys.readouterr().out
    body, last = out.rsplit("\n", 2)[0], out.rstrip("\n").rsplit("\n", 1)[1]
    data = json.loads(body)
    assert data["manifest_version"] == 1
    assert "python" in data["tools"]
    assert last == f"digest: {manifest.digest(data)}"


def test_toolchain_manifest_writes_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "toolchain.json"
    assert main(["--json", "toolchain", "manifest", "-o", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert manifest.read(out) == printed


def test_dev_pin_toolchain_writes_the_digest_into_env(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    env.write_text("RTLFARM_CLIENT_TOKEN=c\n", encoding="utf-8")
    assert main(["dev", "pin-toolchain", "--env", str(env)]) == 0
    digest = capsys.readouterr().out.strip()
    assert len(digest) == 64
    assert env.read_text(encoding="utf-8") == (
        f"RTLFARM_CLIENT_TOKEN=c\nRTLFARM_TOOLCHAIN__DIGEST={digest}\n"
    )


def test_dev_pin_toolchain_from_a_manifest_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = manifest.generate(env={"PATH": str(tmp_path)})
    manifest.write(data, tmp_path / "toolchain.json")
    env = tmp_path / ".env"
    assert (
        main(
            [
                "--json",
                "dev",
                "pin-toolchain",
                "--manifest",
                str(tmp_path / "toolchain.json"),
                "--env",
                str(env),
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"digest": manifest.digest(data), "env": str(env)}
    assert f"RTLFARM_TOOLCHAIN__DIGEST={manifest.digest(data)}" in env.read_text()


################################################################################
# control run and admin migrate
################################################################################


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[tuple[FastAPI, str, int]]:
    """``serve`` replaced by a recorder, so ``control run`` returns at once."""
    calls: list[tuple[FastAPI, str, int]] = []
    monkeypatch.setattr(
        control_cli, "serve", lambda app, host, port: calls.append((app, host, port))
    )
    return calls


def test_control_run_prepares_and_serves_on_the_requested_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    served: list[tuple[FastAPI, str, int]],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("RTLFARM_CLIENT_TOKEN=c\n", encoding="utf-8")
    assert main(["control", "run", "--host", "0.0.0.0", "--port", "9000"]) == 0
    ((app, host, port),) = served
    assert (host, port) == ("0.0.0.0", 9000)
    assert app.state.services.config.client_token == "c"
    assert app.state.services.readiness.migrated is True
    assert (tmp_path / "data" / "rtlfarm.db").is_file()
    assert (tmp_path / "data" / "blobs" / "tmp").is_dir()
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert "control_plane_started" in [line["event"] for line in lines]


def test_control_run_refuses_a_non_loopback_bind_without_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    served: list[tuple[FastAPI, str, int]],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["control", "run", "--host", "0.0.0.0"]) == 1
    assert "not loopback" in capsys.readouterr().err
    assert served == []
    assert not (tmp_path / "data").exists()


def test_control_run_honors_the_insecure_override_and_the_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    served: list[tuple[FastAPI, str, int]],
) -> None:
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / "farm.env"
    env_file.write_text("RTLFARM_INSECURE_BIND=1\n", encoding="utf-8")
    assert (
        main(["control", "run", "--host", "0.0.0.0", "--env-file", str(env_file)]) == 0
    )
    ((app, host, _),) = served
    assert host == "0.0.0.0"
    assert app.state.services.config.insecure_bind is True
    assert app.state.services.config.client_token is None


def test_process_environment_beats_the_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    served: list[tuple[FastAPI, str, int]],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("RTLFARM_CLIENT_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("RTLFARM_CLIENT_TOKEN", "from-process")
    assert main(["control", "run"]) == 0
    ((app, _, _),) = served
    assert app.state.services.config.client_token == "from-process"


def test_control_run_closes_the_database_when_stopped_by_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uvicorn re-raises the stop signal once it has drained; the database
    must still be closed (a WAL left behind would show it was not) and the
    exit must be clean."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("RTLFARM_CLIENT_TOKEN=c\n", encoding="utf-8")
    before = signal.getsignal(signal.SIGTERM)

    def stopped_by_sigterm(app: FastAPI, **options: object) -> None:
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(uvicorn, "run", stopped_by_sigterm)
    with pytest.raises(SystemExit) as info:
        main(["control", "run"])
    assert info.value.code == 0
    assert (tmp_path / "data" / "rtlfarm.db").is_file()
    assert not (tmp_path / "data" / "rtlfarm.db-wal").exists()
    assert signal.getsignal(signal.SIGTERM) is before


def test_the_env_file_beats_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "farm.toml").write_text('data_dir = "from-toml"\n', encoding="utf-8")
    (tmp_path / ".env").write_text("RTLFARM_DATA_DIR=from-env\n", encoding="utf-8")
    assert main(["--json", "admin", "migrate", "--config", "farm.toml"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["database"] == str(Path("from-env") / "rtlfarm.db")
    assert (tmp_path / "from-env" / "rtlfarm.db").is_file()
    assert not (tmp_path / "from-toml").exists()


def test_the_config_file_in_the_working_directory_is_read_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rtlfarm.toml").write_text('data_dir = "vol"\n', encoding="utf-8")
    assert main(["--json", "admin", "migrate"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["database"] == str(Path("vol") / "rtlfarm.db")


@pytest.mark.parametrize("flag", ["--config", "--env-file"])
def test_a_file_named_on_the_command_line_must_exist(
    flag: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["admin", "migrate", flag, "absent"]) == 1
    assert "absent" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


# (verb, the one variable that is wrong, what the message must name). Every
# row also has a plain file named "afile" in the working directory, so the two
# data_dir rows point the volume at a file.
OPERATOR_MISTAKES = [
    pytest.param(
        ["control", "run"],
        "RTLFARM_TIMING__LEASE_TTL_S",
        "1",
        "lease_ttl_s",
        id="control-run-timing-ordering",
    ),
    pytest.param(
        ["control", "run"],
        "RTLFARM_DATA_DIR",
        "afile",
        "afile",
        id="control-run-unusable-volume",
    ),
    pytest.param(
        ["admin", "migrate"],
        "RTLFARM_TIMING__LEASE_TTL_S",
        "thirty",
        "RTLFARM_TIMING__LEASE_TTL_S",
        id="admin-migrate-unparsable-value",
    ),
    pytest.param(
        ["admin", "migrate"],
        "RTLFARM_DATA_DIR",
        "afile",
        "afile",
        id="admin-migrate-unusable-data-dir",
    ),
]


@pytest.mark.parametrize(("argv", "variable", "value", "names"), OPERATOR_MISTAKES)
def test_an_operator_mistake_is_one_line_on_stderr_and_exit_one(
    argv: list[str],
    variable: str,
    value: str,
    names: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    served: list[tuple[FastAPI, str, int]],
) -> None:
    """A bad configuration, a violated timing ordering or an unusable volume
    is reported by name and nothing is served or written."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "afile").write_text("", encoding="utf-8")
    monkeypatch.setenv(variable, value)
    assert main(argv) == 1
    (line,) = capsys.readouterr().err.splitlines()
    assert names in line
    assert served == []
    assert not (tmp_path / "data").exists()


def test_admin_migrate_applies_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["--json", "admin", "migrate"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first == {
        "database": str(Path("data") / "rtlfarm.db"),
        "applied": [1],
        "at": [1],
    }
    assert main(["admin", "migrate"]) == 0
    assert "applied nothing; at 1" in capsys.readouterr().out
