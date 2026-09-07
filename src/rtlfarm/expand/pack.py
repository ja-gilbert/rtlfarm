"""Packing a design directory into its canonical manifest.

The manifest is the unit of submission: the design name, every input file
with its role, position, digest and size, and the validated pipeline. It is
canonical so that the same directory always packs to the same bytes and the
same digest, whatever order the filesystem lists files in.

Rules, each with a test: paths are relative and forward-slash; a glob may not
be absolute or contain ``..``; a symlink anywhere on a file's path is
rejected, as is a file that resolves outside the pack root; bytes are hashed
as they are (a CRLF file is a different input); a file matched by two roles
is an error naming both; ``.git``, ``__pycache__`` and the caller's own
output are never packed; every file a target names must be packed under
that role.

Ordinals fix compilation order within a role: glob declaration order first,
then path order within a glob. A task's file list is built later by taking
its stage's roles in ``consumes`` order and each role's files in ordinal
order.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rtlfarm.expand.pipeline import PIPELINE_FILENAME, load_pipeline
from rtlfarm.models import ROLES, Pipeline

MANIFEST_VERSION = 1

#: Directory names never packed, at any depth.
EXCLUDED_DIRS: frozenset[str] = frozenset({".git", "__pycache__"})

_CHUNK = 1 << 20


@dataclass(frozen=True)
class PackIssue:
    """One thing wrong with the pack: ``where`` is a JSON pointer into the
    pipeline file or a pack-relative path."""

    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


class PackError(ValueError):
    """The directory cannot be packed; ``issues`` lists every problem found."""

    def __init__(self, issues: list[PackIssue]) -> None:
        self.issues = issues
        super().__init__("\n".join(str(issue) for issue in issues))


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    role: str
    ordinal: int
    sha256: str
    size: int


@dataclass(frozen=True)
class Manifest:
    design: str
    files: tuple[ManifestEntry, ...]
    pipeline: Pipeline
    manifest_version: int = MANIFEST_VERSION

    def to_dict(self) -> dict[str, object]:
        """The manifest as plain data, the shape that is submitted."""
        return {
            "manifest_version": self.manifest_version,
            "design": self.design,
            "files": [
                {
                    "path": f.path,
                    "role": f.role,
                    "ordinal": f.ordinal,
                    "sha256": f.sha256,
                    "size": f.size,
                }
                for f in self.files
            ],
            "pipeline": self.pipeline.model_dump(mode="json"),
        }

    def canonical_json(self) -> str:
        """Sorted keys, no insignificant whitespace: the bytes that are digested."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        """SHA-256 of the canonical JSON, as hex."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def paths(self, role: str) -> list[str]:
        """The packed paths of one role, in ordinal order."""
        return [f.path for f in self.files if f.role == role]


def hash_file(path: Path) -> tuple[str, int]:
    """SHA-256 hex digest and size of a file, read as bytes."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def pack(root: Path, *, exclude: Iterable[str] = ()) -> Manifest:
    """Pack the design at ``root``; ``exclude`` lists pack-relative paths to skip."""
    if not (root / PIPELINE_FILENAME).is_file():
        raise PackError([PackIssue(PIPELINE_FILENAME, "no such file in the pack")])
    pipeline = load_pipeline(root)
    excluded = {PurePosixPath(p).as_posix() for p in exclude}
    issues: list[PackIssue] = []
    matched: dict[str, tuple[str, int, str]] = {}  # path -> (role, ordinal, pointer)
    by_role = sorted(pipeline.design.files.items(), key=lambda kv: ROLES.index(kv[0]))
    for role, globs in by_role:
        ordinal = 0
        for i, pattern in enumerate(globs):
            pointer = f"/design/files/{role}/{i}"
            problem = _bad_pattern(pattern)
            if problem:
                issues.append(PackIssue(pointer, problem))
                continue
            paths = _expand_glob(root, pattern, excluded)
            if not paths:
                issues.append(PackIssue(pointer, "glob matched no files"))
                continue
            for rel in paths:
                if rel in matched:
                    other = matched[rel][0]
                    if other != role:
                        issues.append(
                            PackIssue(rel, f"matched by two roles: {other} and {role}")
                        )
                    continue
                matched[rel] = (role, ordinal, pointer)
                ordinal += 1
    for rel in sorted(matched):
        problem = _bad_location(root, rel)
        if problem:
            issues.append(PackIssue(rel, problem))
    issues.extend(_target_issues(pipeline, matched))
    if issues:
        raise PackError(issues)
    entries = []
    for rel, (entry_role, ordinal, _) in matched.items():
        sha256, size = hash_file(root / rel)
        entries.append(ManifestEntry(rel, entry_role, ordinal, sha256, size))
    entries.sort(key=lambda e: (ROLES.index(e.role), e.ordinal))
    return Manifest(pipeline.design.name, tuple(entries), pipeline)


def _bad_pattern(pattern: str) -> str | None:
    posix = PurePosixPath(pattern)
    if "\\" in pattern:
        return "use forward slashes"
    if posix.is_absolute() or pattern.startswith("/"):
        return "must be relative to the pack"
    if ".." in posix.parts:
        return "may not contain '..'"
    return None


def _expand_glob(root: Path, pattern: str, excluded: set[str]) -> list[str]:
    """Pack-relative paths matched by ``pattern``, files only, sorted."""
    found: list[str] = []
    for path in root.glob(pattern):
        rel = path.relative_to(root).as_posix()
        if EXCLUDED_DIRS & set(PurePosixPath(rel).parts) or rel in excluded:
            continue
        if path.is_symlink() or path.is_file():
            found.append(rel)
    return sorted(found)


def _bad_location(root: Path, rel: str) -> str | None:
    """A symlink on any component, or a real path outside the pack, is rejected."""
    current = root
    for part in PurePosixPath(rel).parts:
        current = current / part
        if current.is_symlink():
            return f"symlink at {current.relative_to(root).as_posix()}"
    if not current.is_file():
        return "not a regular file"
    try:
        current.resolve(strict=True).relative_to(root.resolve(strict=True))
    except ValueError:
        return "resolves outside the pack"
    return None


def _target_issues(
    pipeline: Pipeline, matched: dict[str, tuple[str, int, str]]
) -> list[PackIssue]:
    issues: list[PackIssue] = []
    for i, target in enumerate(pipeline.targets):
        for role, paths in (("tb", target.tb), ("data", target.data)):
            for j, rel in enumerate(paths):
                if matched.get(rel, ("", 0, ""))[0] != role:
                    issues.append(
                        PackIssue(
                            f"/targets/{i}/{role}/{j}",
                            f"{rel!r} is not packed under role {role!r}",
                        )
                    )
    return issues
