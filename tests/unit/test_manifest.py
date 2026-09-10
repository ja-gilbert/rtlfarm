"""The toolchain manifest: generated from the installed tools, deterministic,
sensitive to every recorded component, and pinned into a dotenv file.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from rtlfarm.tools import manifest

################################################################################
# Helpers
################################################################################


def _stub_tool(directory: Path, name: str, version_line: str) -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\necho '{version_line}'\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


################################################################################
# Generation
################################################################################


def test_absent_simulators_are_omitted_not_faked(tmp_path: Path) -> None:
    """A placeholder entry would give two different machines one digest."""
    tools = manifest.generate(env={"PATH": str(tmp_path)})["tools"]
    assert isinstance(tools, dict)
    assert set(tools) == {"python", "fake"}


def test_simulators_on_path_are_recorded_with_their_binary_digest(
    tmp_path: Path,
) -> None:
    """Two machines whose simulator builds differ but print the same version
    line must not share a toolchain digest: it is the job pin and the cache
    key, so a shared digest reuses one build's results for the other."""
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


def test_any_recorded_change_changes_the_digest(tmp_path: Path) -> None:
    data = manifest.generate(env={"PATH": str(tmp_path)})
    changed = json.loads(json.dumps(data))
    changed["tools"]["python"]["version_line"] += " (patched)"
    assert manifest.digest(changed) != manifest.digest(data)


################################################################################
# Pinning Into .env
################################################################################


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


def test_pinned_digest_loads_through_the_config_env_layer(tmp_path: Path) -> None:
    from rtlfarm.config import load_config

    env = tmp_path / ".env"
    manifest.pin_env(env, "d" * 64)
    pairs = dict(line.split("=", 1) for line in env.read_text().splitlines())
    assert load_config(toml_path=None, env=pairs).toolchain.digest == "d" * 64
    assert os.environ.get("RTLFARM_TOOLCHAIN__DIGEST") is None
