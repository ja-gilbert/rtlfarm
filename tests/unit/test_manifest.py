"""The toolchain manifest: generated from the installed tools, deterministic,
digested canonically, and pinned into a dotenv file.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from rtlfarm.tools import manifest

################################################################################
# Helpers
################################################################################


def _stub_tool(directory: Path, name: str, version_line: str) -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\necho '{version_line}'\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _os_release(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "os-release"
    path.write_text(text, encoding="utf-8")
    return path


################################################################################
# Generation
################################################################################


def test_manifest_records_python_and_the_fake_tool(tmp_path: Path) -> None:
    data = manifest.generate(env={"PATH": str(tmp_path)})
    assert data["manifest_version"] == 1
    tools = data["tools"]
    assert isinstance(tools, dict)
    python = tools["python"]
    assert python["path"] == sys.executable
    assert python["version_line"].startswith("Python 3.")
    assert (
        python["sha256"]
        == hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    )
    fake = tools["fake"]
    assert fake["version_line"].startswith("rtlfarm ")
    assert fake["path"].endswith("-m rtlfarm.tools.fake")
    assert fake["sha256"] is None
    assert data["rtlfarm_adapters_version"] == fake["version_line"].split()[1]
    assert data["build_flags"] == {}


def test_simulators_on_path_are_recorded_with_their_binary_digest(
    tmp_path: Path,
) -> None:
    exe = _stub_tool(tmp_path, "iverilog", "Icarus Verilog version 12.0 (stable) ()")
    _stub_tool(tmp_path, "vvp", "Icarus Verilog runtime version 12.0 (stable) ()")
    tools = manifest.generate(env={"PATH": str(tmp_path)})["tools"]
    assert isinstance(tools, dict)
    assert tools["iverilog"] == {
        "version_line": "Icarus Verilog version 12.0 (stable) ()",
        "path": str(exe),
        "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
    }
    assert tools["vvp"]["version_line"].startswith("Icarus Verilog runtime")


def test_absent_simulators_are_omitted_not_faked(tmp_path: Path) -> None:
    tools = manifest.generate(env={"PATH": str(tmp_path)})["tools"]
    assert isinstance(tools, dict)
    assert set(tools) == {"python", "fake"}


def test_base_image_comes_from_os_release(tmp_path: Path) -> None:
    release = _os_release(
        tmp_path, 'PRETTY_NAME="Ubuntu 24.04"\nID=ubuntu\nVERSION_ID="24.04"\n'
    )
    data = manifest.generate(env={"PATH": str(tmp_path)}, os_release=release)
    assert data["base_image"] == {"name": "ubuntu", "os_release_version": "24.04"}


def test_missing_os_release_is_reported_as_unknown(tmp_path: Path) -> None:
    data = manifest.generate(
        env={"PATH": str(tmp_path)}, os_release=tmp_path / "absent"
    )
    assert data["base_image"] == {"name": "unknown", "os_release_version": "unknown"}


def test_generation_is_deterministic(tmp_path: Path) -> None:
    _stub_tool(tmp_path, "iverilog", "Icarus Verilog version 12.0 (stable) ()")
    env = {"PATH": str(tmp_path)}
    first = manifest.generate(env=env)
    second = manifest.generate(env=env)
    assert first == second
    assert manifest.digest(first) == manifest.digest(second)


################################################################################
# Digest and Files
################################################################################


def test_digest_is_the_sha256_of_the_canonical_json(tmp_path: Path) -> None:
    data = manifest.generate(env={"PATH": str(tmp_path)})
    text = manifest.canonical_json(data)
    assert text == json.dumps(data, sort_keys=True, separators=(",", ":"))
    assert manifest.digest(data) == hashlib.sha256(text.encode()).hexdigest()
    assert len(manifest.digest(data)) == 64


def test_any_recorded_change_changes_the_digest(tmp_path: Path) -> None:
    data = manifest.generate(env={"PATH": str(tmp_path)})
    changed = json.loads(json.dumps(data))
    changed["tools"]["python"]["version_line"] += " (patched)"
    assert manifest.digest(changed) != manifest.digest(data)


def test_write_and_read_round_trip(tmp_path: Path) -> None:
    data = manifest.generate(env={"PATH": str(tmp_path)})
    path = tmp_path / "toolchain.json"
    manifest.write(data, path)
    assert manifest.read(path) == data
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_read_rejects_a_file_that_is_not_a_manifest(tmp_path: Path) -> None:
    path = tmp_path / "toolchain.json"
    path.write_text('{"manifest_version": 2}', encoding="utf-8")
    with pytest.raises(ValueError, match="toolchain manifest"):
        manifest.read(path)


################################################################################
# Pinning Into .env
################################################################################


def test_pin_creates_the_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    manifest.pin_env(env, "a" * 64)
    assert (
        env.read_text(encoding="utf-8")
        == "RTLFARM_TOOLCHAIN__DIGEST=" + "a" * 64 + "\n"
    )


def test_pin_replaces_the_existing_line_and_keeps_the_rest(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "RTLFARM_CLIENT_TOKEN=c\nRTLFARM_TOOLCHAIN__DIGEST=old\nRTLFARM_WORKER_TOKEN=w\n",
        encoding="utf-8",
    )
    manifest.pin_env(env, "b" * 64)
    assert env.read_text(encoding="utf-8") == (
        "RTLFARM_CLIENT_TOKEN=c\nRTLFARM_TOOLCHAIN__DIGEST=" + "b" * 64 + "\n"
        "RTLFARM_WORKER_TOKEN=w\n"
    )


def test_pin_appends_when_the_key_is_absent(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("RTLFARM_CLIENT_TOKEN=c", encoding="utf-8")
    manifest.pin_env(env, "c" * 64)
    assert env.read_text(encoding="utf-8") == (
        "RTLFARM_CLIENT_TOKEN=c\nRTLFARM_TOOLCHAIN__DIGEST=" + "c" * 64 + "\n"
    )


def test_pinned_digest_loads_through_the_config_env_layer(tmp_path: Path) -> None:
    from rtlfarm.config import load_config

    env = tmp_path / ".env"
    manifest.pin_env(env, "d" * 64)
    pairs = dict(line.split("=", 1) for line in env.read_text().splitlines())
    assert load_config(toml_path=None, env=pairs).toolchain.digest == "d" * 64
    assert os.environ.get("RTLFARM_TOOLCHAIN__DIGEST") is None
