"""Identity: names, ULIDs, task and attempt ids.

Names are restricted so that every id built from them is unambiguous and safe
as a filesystem path and a URL segment. A task id is deterministic from the
expansion of a job; an attempt id is the fencing token, scoped to one attempt.
Nothing in the system parses an attempt id back into its parts, so this module
composes ids and never decomposes them.
"""

from __future__ import annotations

import os
import re

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Stage names are a closed set of kinds; ``coverage`` is reserved for the
#: fan-in coverage stage.
STAGE_KINDS: tuple[str, ...] = ("lint", "compile", "simulate", "coverage")

#: The target of a whole-design stage (one task for the whole design).
WHOLE_DESIGN_TARGET = "_"

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")


class IdError(ValueError):
    """An id could not be composed from its parts."""


class InvalidName(IdError):
    """A design or target name outside ``NAME_RE``."""


def validate_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise InvalidName(f"name {name!r} must match {NAME_RE.pattern}")
    return name


def new_ulid(now_ms: int) -> str:
    """A ULID: 48 bits of time (ms), 80 bits of randomness, Crockford base32.

    ULIDs sort lexicographically by creation time, which makes creation order
    an index order for job ids. The caller passes the time so the clock stays
    injected.
    """
    if not 0 <= now_ms < 2**48:
        raise IdError(f"now_ms {now_ms} is outside the 48-bit ULID range")
    value = (now_ms << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def task_id(
    job_id: str,
    stage_kind: str,
    target: str = WHOLE_DESIGN_TARGET,
    seed: int = 0,
) -> str:
    """``{job_id}.{stage_kind}.{target}.s{seed}``."""
    if not _ULID_RE.fullmatch(job_id):
        raise IdError(f"job_id {job_id!r} is not a ULID")
    if stage_kind not in STAGE_KINDS:
        raise IdError(f"stage_kind {stage_kind!r} is not one of {STAGE_KINDS}")
    if target != WHOLE_DESIGN_TARGET:
        validate_name(target)
    if seed < 0:
        raise IdError(f"seed {seed} must not be negative")
    return f"{job_id}.{stage_kind}.{target}.s{seed}"


def attempt_id(task: str, attempt: int) -> str:
    """``{task_id}.a{n}``, ``n`` 1-based; the fencing token.

    Minted by the claim (spec §4.6, §7.3); nothing calls this until then.
    """
    if attempt < 1:
        raise IdError(f"attempt {attempt} must be 1 or greater")
    return f"{task}.a{attempt}"
