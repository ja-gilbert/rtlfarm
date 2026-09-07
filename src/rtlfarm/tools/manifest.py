"""The toolchain manifest: a generated description of the tools a worker runs.

The manifest is produced by executing the tools, never written by hand, so it
records what is actually installed. Its digest, the SHA-256 of its canonical
JSON, is the ``toolchain_digest`` that every job is pinned to and every worker
reports: change any recorded component and the digest changes, which makes a
tool upgrade a cold cache by design rather than a silent difference.

Recorded: the base OS from ``/etc/os-release`` (a container cannot learn its
own image digest, so none is recorded), the Python interpreter, the fake tool
that ships inside this package, and each simulator found on ``PATH``, with
its version line, path and binary digest.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path

MANIFEST_VERSION = 1

#: Simulators recorded when present on PATH, with the arguments that print a version.
SIMULATORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("iverilog", ("-V",)),
    ("vvp", ("-V",)),
)

_ENV_KEY = "RTLFARM_TOOLCHAIN__DIGEST"
_ENV_LINE_RE = re.compile(rf"^{_ENV_KEY}=.*$", re.M)


def generate(
    *,
    env: Mapping[str, str] | None = None,
    os_release: Path = Path("/etc/os-release"),
    python: Path | None = None,
) -> dict[str, object]:
    """Build the manifest from the tools installed on this machine."""
    path_env = None if env is None else env.get("PATH", "")
    interpreter = python or Path(sys.executable)
    tools: dict[str, object] = {
        "python": _tool_info(interpreter, ("--version",)),
        "fake": {
            "version_line": f"rtlfarm {_package_version()}",
            "path": f"{interpreter} -m rtlfarm.tools.fake",
            "sha256": None,
        },
    }
    for name, args in SIMULATORS:
        found = shutil.which(name, path=path_env)
        if found is not None:
            tools[name] = _tool_info(Path(found), args)
    return {
        "manifest_version": MANIFEST_VERSION,
        "base_image": _base_image(os_release),
        "tools": tools,
        "rtlfarm_adapters_version": _package_version(),
        "build_flags": {},
    }


def canonical_json(manifest: Mapping[str, object]) -> str:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def digest(manifest: Mapping[str, object]) -> str:
    """The toolchain digest: SHA-256 of the canonical JSON, as hex."""
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def write(manifest: Mapping[str, object], path: Path) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8")


def read(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError(
            f"{path} is not a version {MANIFEST_VERSION} toolchain manifest"
        )
    return data


def pin_env(env_path: Path, digest_value: str) -> None:
    """Set ``RTLFARM_TOOLCHAIN__DIGEST`` in a dotenv file, keeping every other line."""
    line = f"{_ENV_KEY}={digest_value}"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if _ENV_LINE_RE.search(text):
        text = _ENV_LINE_RE.sub(line, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    env_path.write_text(text, encoding="utf-8")


def _tool_info(path: Path, args: tuple[str, ...]) -> dict[str, object]:
    completed = subprocess.run(
        [str(path), *args], capture_output=True, text=True, check=False
    )
    output = completed.stdout or completed.stderr
    first = output.strip().splitlines()[0] if output.strip() else ""
    return {"version_line": first, "path": str(path), "sha256": _sha256(path)}


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            hasher.update(chunk)
    return hasher.hexdigest()


def _base_image(os_release: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    if os_release.is_file():
        for raw in os_release.read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.startswith("#"):
                key, value = raw.split("=", 1)
                fields[key.strip()] = value.strip().strip('"')
    return {
        "name": fields.get("ID", "unknown"),
        "os_release_version": fields.get("VERSION_ID", "unknown"),
    }


def _package_version() -> str:
    return version("rtlfarm")
