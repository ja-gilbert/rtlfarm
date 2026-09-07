"""Configuration: layering, the frozen dataclasses, and timing validation.

Layering, lowest to highest precedence: dataclass defaults → ``rtlfarm.toml``
(committed, non-secret) → ``RTLFARM_*`` environment (``__`` nests a section,
``RTLFARM_TIMING__LEASE_TTL_S``) → CLI flags. Everything is loaded once into a
frozen dataclass. Every timing constant is configuration, never a literal in
code, and ``validate_timing()`` encodes the orderings the lease,
heartbeat and kill-grace mechanisms depend on.

The loader is deliberately plain: each layer becomes a flat ``{"section.key":
value}`` mapping, the mappings are merged key by key, and the result is
poured into the dataclasses. Unknown keys and wrong types are errors, so a
typo in an environment variable cannot silently leave a default in force.
"""

from __future__ import annotations

import dataclasses
import json
import math
import tomllib
import types
import typing
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from rtlfarm import hooks

ENV_PREFIX = "RTLFARM_"

#: ``RTLFARM_*`` variables that are process switches, not configuration keys.
NON_CONFIG_ENV: frozenset[str] = frozenset({hooks.ENV_VAR})


class ConfigError(ValueError):
    """A configuration source could not be loaded."""


class TimingError(ValueError):
    """The timing constants violate an ordering that ``validate_timing`` enforces."""


@dataclass(frozen=True)
class TimingConfig:
    """The farm's timing constants, with the production defaults.

    A garbage-collection grace period joins this section when the sweep exists.
    """

    tick_s: float = 1.0
    heartbeat_s: float = 5.0
    lease_ttl_s: float = 30.0
    backstop_grace_s: float = 15.0
    worker_dead_after_s: float = 90.0
    kill_grace_s: float = 5.0
    claim_wait_s: float = 20.0
    client_read_timeout_s: float = 30.0
    requeue_backoff_s: tuple[float, ...] = (0.0, 5.0, 30.0)
    full_sweep_every: int = 30
    readyz_tick_factor: int = 3
    upload_timeout_s: float = 300.0


@dataclass(frozen=True)
class ToolchainConfig:
    """The toolchain digest jobs are pinned to when a submission names none."""

    digest: str | None = None


_MIB = 1024 * 1024


@dataclass(frozen=True)
class BlobsConfig:
    """Size caps per blob kind, in bytes, enforced while an upload streams in."""

    log_bytes: int = 16 * _MIB
    input_file_bytes: int = 64 * _MIB
    input_job_bytes: int = 512 * _MIB
    diagnostics_bytes: int = 4 * _MIB
    deps_bytes: int = 4 * _MIB
    compiled_bytes: int = 256 * _MIB
    result_bytes: int = 1 * _MIB
    waveform_bytes: int = 512 * _MIB
    coverage_bytes: int = 64 * _MIB


@dataclass(frozen=True)
class Config:
    timing: TimingConfig = field(default_factory=TimingConfig)
    toolchain: ToolchainConfig = field(default_factory=ToolchainConfig)
    blobs: BlobsConfig = field(default_factory=BlobsConfig)
    # The two static bearer tokens; both unset means auth is disabled.
    client_token: str | None = None
    worker_token: str | None = None
    insecure_bind: bool = False
    # Where the CLI (``--url``) and workers find the control plane.
    control_url: str = "http://127.0.0.1:8080"


# --- validate_timing ---------------------------------------------------------


def validate_timing(timing: TimingConfig, timeouts_s: Iterable[float] = ()) -> None:
    """Raise ``TimingError`` unless every required ordering of the constants holds.

    ``timeouts_s`` are the per-stage wall-clock timeouts of the pipeline in
    force, when known; without them the two ``timeout_s`` rules are skipped.
    A caller that knows the stage timeouts (pipeline validation) must pass
    them, or those two rules are never enforced. All violations are reported
    in one message.
    """
    t = timing
    timeouts = tuple(timeouts_s)
    checks: list[tuple[bool, str]] = [
        # Sanity: every period is a positive duration, every counter at least one.
        *(
            (getattr(t, name) > 0, f"{name} must be positive, got {getattr(t, name)}")
            for name in (
                "tick_s",
                "heartbeat_s",
                "lease_ttl_s",
                "backstop_grace_s",
                "worker_dead_after_s",
                "kill_grace_s",
                "claim_wait_s",
                "client_read_timeout_s",
                "upload_timeout_s",
            )
        ),
        (
            t.full_sweep_every >= 1,
            f"full_sweep_every must be >= 1, got {t.full_sweep_every}",
        ),
        (
            t.readyz_tick_factor >= 1,
            f"readyz_tick_factor must be >= 1, got {t.readyz_tick_factor}",
        ),
        (
            len(t.requeue_backoff_s) >= 1 and all(b >= 0 for b in t.requeue_backoff_s),
            f"requeue_backoff_s must be a non-empty list of non-negative seconds, "
            f"got {list(t.requeue_backoff_s)}",
        ),
        # The orderings the lease, heartbeat, backstop and transport rely on.
        (
            3 * t.heartbeat_s < t.lease_ttl_s,
            f"3 x heartbeat_s ({3 * t.heartbeat_s}) must be below lease_ttl_s "
            f"({t.lease_ttl_s}) so a lease survives two missed heartbeats",
        ),
        (
            t.heartbeat_s + t.kill_grace_s < t.lease_ttl_s,
            f"heartbeat_s + kill_grace_s ({t.heartbeat_s + t.kill_grace_s}) must be "
            f"below lease_ttl_s ({t.lease_ttl_s}) so a self-fencing worker's "
            "processes are dead before its lease expires",
        ),
        (
            t.backstop_grace_s > t.kill_grace_s + t.heartbeat_s,
            f"backstop_grace_s ({t.backstop_grace_s}) must exceed kill_grace_s + "
            f"heartbeat_s ({t.kill_grace_s + t.heartbeat_s}) so the worker's own "
            "TIMEOUT commit lands before the control-plane backstop",
        ),
        (
            t.worker_dead_after_s > t.lease_ttl_s,
            f"worker_dead_after_s ({t.worker_dead_after_s}) must exceed lease_ttl_s "
            f"({t.lease_ttl_s})",
        ),
        (
            t.claim_wait_s + 5 <= t.client_read_timeout_s,
            f"claim_wait_s + 5 ({t.claim_wait_s + 5}) must not exceed "
            f"client_read_timeout_s ({t.client_read_timeout_s}) so a long-poll never "
            "trips the client timeout",
        ),
        (
            t.tick_s <= t.heartbeat_s,
            f"tick_s ({t.tick_s}) must not exceed heartbeat_s ({t.heartbeat_s})",
        ),
        (
            all(x > 0 for x in timeouts),
            f"every timeout_s must be positive, got {list(timeouts)}",
        ),
        (
            all(t.kill_grace_s < x for x in timeouts),
            f"kill_grace_s ({t.kill_grace_s}) must be below every timeout_s "
            f"({list(timeouts)})",
        ),
    ]
    failures = [message for ok, message in checks if not ok]
    if failures:
        raise TimingError("; ".join(failures))


# --- loading ------------------------------------------------------------------

_Flat = dict[str, object]


def load_config(
    *,
    toml_path: Path | None,
    env: Mapping[str, str],
    overrides: Mapping[str, object] | None = None,
) -> Config:
    """Load the layered configuration.

    ``toml_path`` may be ``None`` (no file layer); a path that does not exist
    is an error. ``env`` is the process environment (pass ``os.environ``);
    values are taken verbatim, so quoting or trailing whitespace in a ``.env``
    line is the ``.env`` reader's business, not this loader's. ``overrides``
    are CLI flags as ``{"section.key": value}``.
    """
    merged: _Flat = {}
    if toml_path is not None:
        merged.update(_from_toml(toml_path))
    merged.update(_from_env(env))
    merged.update(_from_overrides(overrides or {}))
    return _build(Config, merged, prefix="")


def _from_toml(path: Path) -> _Flat:
    try:
        with path.open("rb") as f:  # TOML is UTF-8 by definition
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file {path} does not exist") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config file {path}: {e}") from None
    flat: _Flat = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                flat[f"{key}.{sub_key}"] = _coerce(f"{key}.{sub_key}", sub_value)
        else:
            flat[key] = _coerce(key, value)
    return flat


def _from_env(env: Mapping[str, str]) -> _Flat:
    flat: _Flat = {}
    for name, raw in env.items():
        if not name.startswith(ENV_PREFIX) or name in NON_CONFIG_ENV:
            continue
        key = name.removeprefix(ENV_PREFIX).lower().replace("__", ".")
        flat[key] = _parse_text(name, key, raw)
    return flat


def _from_overrides(overrides: Mapping[str, object]) -> _Flat:
    return {key: _coerce(key, value) for key, value in overrides.items()}


def _field_kinds() -> dict[str, str]:
    """``{"section.key": kind}`` for every leaf field of ``Config``."""
    kinds: dict[str, str] = {}

    def walk(cls: type, prefix: str) -> None:
        hints = typing.get_type_hints(cls)
        for f in dataclasses.fields(cls):
            hint = hints[f.name]
            if dataclasses.is_dataclass(hint) and isinstance(hint, type):
                walk(hint, f"{prefix}{f.name}.")
            else:
                kinds[f"{prefix}{f.name}"] = _kind_of(hint)

    walk(Config, "")
    return kinds


def _kind_of(hint: object) -> str:
    if hint is bool:
        return "bool"
    if hint is int:
        return "int"
    if hint is float:
        return "float"
    if hint is str:
        return "str"
    if isinstance(hint, types.UnionType) and set(typing.get_args(hint)) == {
        str,
        type(None),
    }:
        return "optional_str"
    if typing.get_origin(hint) is tuple:
        return "float_tuple"
    raise TypeError(f"unsupported config field type {hint!r}")  # pragma: no cover


_KINDS = _field_kinds()

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _parse_text(source: str, key: str, raw: str) -> object:
    """Parse an environment string into the field's type.

    ``source`` is the variable name, for error messages.
    """
    kind = _KINDS.get(key)
    if kind is None:
        raise ConfigError(f"{source}: unknown configuration key {key!r}")
    try:
        if kind == "bool":
            lowered = raw.strip().lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
            raise ValueError(f"expected one of {sorted(_TRUE | _FALSE)}")
        if kind == "int":
            return int(raw)
        if kind == "float":
            return _coerce(key, float(raw))
        if kind == "float_tuple":
            return _coerce(key, json.loads(raw))
        if kind == "optional_str":
            return raw or None
        return raw
    except (ValueError, ConfigError) as e:
        raise ConfigError(f"{source}: {e}") from None


def _coerce(key: str, value: object) -> object:
    """Check an already-typed value (TOML or override) against the field's type."""
    kind = _KINDS.get(key)
    if kind is None:
        raise ConfigError(f"unknown configuration key {key!r}")
    ok: bool
    match kind:
        case "bool":
            ok = isinstance(value, bool)
        case "int":
            ok = isinstance(value, int) and not isinstance(value, bool)
        case "float":
            ok = _is_number(value)
            if ok:
                value = float(typing.cast(float, value))
        case "str":
            ok = isinstance(value, str)
        case "optional_str":
            ok = value is None or isinstance(value, str)
        case "float_tuple":
            ok = isinstance(value, list | tuple) and all(_is_number(x) for x in value)
            if ok:
                value = tuple(float(x) for x in typing.cast(Iterable[float], value))
        case _:  # pragma: no cover
            ok = False
    if not ok:
        raise ConfigError(f"{key}: expected {kind}, got {value!r}")
    return value


def _is_number(value: object) -> bool:
    """An int or a finite float (``inf`` and ``nan`` are never valid timings)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _build[T](cls: type[T], flat: _Flat, prefix: str) -> T:
    """Construct ``cls`` from the merged flat mapping.

    Keys absent from ``flat`` keep their dataclass defaults.
    """
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, object] = {}
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        hint = hints[f.name]
        key = f"{prefix}{f.name}"
        if dataclasses.is_dataclass(hint) and isinstance(hint, type):
            kwargs[f.name] = _build(hint, flat, f"{key}.")
        elif key in flat:
            kwargs[f.name] = flat[key]
    return cls(**kwargs)
