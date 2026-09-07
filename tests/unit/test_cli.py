"""The ``rtlfarm`` entry point: the global options, ``dev pack``,
``dev pin-toolchain`` and ``toolchain manifest``."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

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
    for flag in ("--url", "--token", "--json", "dev", "toolchain"):
        assert flag in out


def test_no_verb_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage: rtlfarm" in capsys.readouterr().err


def test_dev_without_a_command_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["dev"]) == 2
    assert "usage: rtlfarm" in capsys.readouterr().err


def test_unknown_flag_is_a_usage_error() -> None:
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
