"""Configuration layering and timing validation."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from rtlfarm.config import (
    Config,
    ConfigError,
    TimingConfig,
    TimingError,
    load_config,
    read_dotenv,
    validate_timing,
)

################################################################################
# Helpers
################################################################################


def _write_toml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "rtlfarm.toml"
    path.write_text(text)
    return path


# One row per kind of leaf field the loader coerces, in a nested section and at
# the top level: (dotted key, TOML literal, env string, expected). The loader
# has no per-key code, so a kind is the input class, not a key. The int literal
# for a float field proves the coercion happens.
KINDS: list[tuple[str, str, str, object]] = [
    ("timing.tick_s", "2", "2", 2.0),
    ("timing.full_sweep_every", "7", "7", 7),
    ("timing.requeue_backoff_s", "[1, 2, 3]", "[1, 2, 3]", (1.0, 2.0, 3.0)),
    ("toolchain.digest", '"0123abcd"', "0123abcd", "0123abcd"),
    ("worker_token", '"worker-secret"', "worker-secret", "worker-secret"),
    ("insecure_bind", "true", "1", True),
    ("data_dir", '"/var/lib/rtlfarm"', "/var/lib/rtlfarm", "/var/lib/rtlfarm"),
]


def _toml_for(key: str, literal: str) -> str:
    section, _, name = key.rpartition(".")
    if section:
        return f"[{section}]\n{name} = {literal}\n"
    return f"{name} = {literal}\n"


def _env_name(key: str) -> str:
    return "RTLFARM_" + key.upper().replace(".", "__")


def _get(config: Config, key: str) -> object:
    obj: object = config
    for part in key.split("."):
        obj = getattr(obj, part)
    return obj


################################################################################
# The Layers
################################################################################


@pytest.mark.parametrize(
    ("key", "literal", "env", "expected"), KINDS, ids=[row[0] for row in KINDS]
)
def test_every_field_kind_loads_from_toml_and_from_env(
    tmp_path: Path, key: str, literal: str, env: str, expected: object
) -> None:
    """A kind that stops being coerced (an int left where a float belongs) or a
    layer that stops reaching a section would load the wrong value. ``repr``
    rather than ``==`` because ``2 == 2.0``."""
    toml = _write_toml(tmp_path, _toml_for(key, literal))
    from_toml = load_config(toml_path=toml, env={})
    from_env = load_config(toml_path=None, env={_env_name(key): env})
    assert repr(_get(from_toml, key)) == repr(expected)
    assert repr(_get(from_env, key)) == repr(expected)


def test_an_empty_environment_value_means_unset() -> None:
    """``RTLFARM_CLIENT_TOKEN=`` (the shipped ``.env.example``) must leave the
    token ``None``; an empty string would count as auth enabled."""
    config = load_config(toml_path=None, env={"RTLFARM_CLIENT_TOKEN": ""})
    assert config.client_token is None


def test_env_overrides_toml_and_flags_override_env(tmp_path: Path) -> None:
    """Each layer beats the one before it, and flags reach every section."""
    toml = _write_toml(tmp_path, "[timing]\nlease_ttl_s = 45\n")
    env = {"RTLFARM_TIMING__LEASE_TTL_S": "60"}

    assert load_config(toml_path=toml, env={}).timing.lease_ttl_s == 45.0
    assert load_config(toml_path=toml, env=env).timing.lease_ttl_s == 60.0
    flags = {"timing.lease_ttl_s": 75.0, "blobs.log_bytes": 7, "client_token": "c"}
    config = load_config(toml_path=toml, env=env, overrides=flags)
    assert (config.timing.lease_ttl_s, config.blobs.log_bytes) == (75.0, 7)
    assert config.client_token == "c"


def test_layers_merge_key_by_key(tmp_path: Path) -> None:
    """A later layer replaces only the keys it names, not the whole section."""
    toml = _write_toml(tmp_path, "[timing]\nlease_ttl_s = 45\nheartbeat_s = 9\n")
    config = load_config(toml_path=toml, env={"RTLFARM_TIMING__LEASE_TTL_S": "60"})
    assert config.timing.lease_ttl_s == 60.0
    assert config.timing.heartbeat_s == 9.0
    assert config.timing.tick_s == 1.0


@pytest.mark.parametrize(
    "name",
    ["RTLFARM_TEST_HOOKS", "RTLFARM_UPDATE_SNAPSHOTS"],
    ids=["hooks", "snapshots"],
)
def test_process_switches_are_not_config_keys(name: str) -> None:
    """Treating a switch as an unknown key would abort every hook-enabled run
    and every snapshot regeneration."""
    config = load_config(toml_path=None, env={name: "1"})
    assert config == Config()


def test_a_non_finite_timing_is_rejected() -> None:
    """``nan`` compares false with everything, so it would pass every ordering."""
    with pytest.raises(ConfigError, match="RTLFARM_TIMING__LEASE_TTL_S"):
        load_config(toml_path=None, env={"RTLFARM_TIMING__LEASE_TTL_S": "nan"})


def test_env_bad_boolean_is_an_error() -> None:
    with pytest.raises(ConfigError, match="RTLFARM_INSECURE_BIND"):
        load_config(toml_path=None, env={"RTLFARM_INSECURE_BIND": "maybe"})


################################################################################
# Rejected Input
################################################################################

# (case, TOML text, environment, overrides, what the message must name). An
# empty TOML text is a valid file that sets nothing, so every row goes through
# the same call.
UNKNOWN_KEYS: list[tuple[str, str, dict[str, str], dict[str, object], str]] = [
    ("toml", "[timing]\nlease_ttl = 45\n", {}, {}, r"timing\.lease_ttl"),
    ("env", "", {"RTLFARM_TIMING__LEASE_TTL": "45"}, {}, "RTLFARM_TIMING__LEASE_TTL"),
    ("override", "", {}, {"timing.nope": 1}, r"timing\.nope"),
]


@pytest.mark.parametrize(
    ("_case", "toml", "env", "overrides", "match"),
    UNKNOWN_KEYS,
    ids=[row[0] for row in UNKNOWN_KEYS],
)
def test_an_unknown_key_is_rejected_in_every_layer(
    tmp_path: Path,
    _case: str,
    toml: str,
    env: dict[str, str],
    overrides: dict[str, object],
    match: str,
) -> None:
    """A typo cannot leave a default silently in force, whichever layer it
    enters through, and the error names the key."""
    with pytest.raises(ConfigError, match=match):
        load_config(toml_path=_write_toml(tmp_path, toml), env=env, overrides=overrides)


def test_wrong_type_in_env_is_an_error() -> None:
    with pytest.raises(ConfigError, match="RTLFARM_TIMING__FULL_SWEEP_EVERY"):
        load_config(toml_path=None, env={"RTLFARM_TIMING__FULL_SWEEP_EVERY": "1.5"})


################################################################################
# validate_timing: the Orderings Between the Constants
################################################################################

DEFAULTS = TimingConfig()
DEFAULT_STAGE_TIMEOUTS = (60.0, 300.0, 300.0)  # lint, compile, simulate defaults


def _timing(**changes: Any) -> TimingConfig:
    """The defaults with some constants replaced."""
    return TimingConfig(**{**dataclasses.asdict(DEFAULTS), **changes})


# Each row inverts exactly one required ordering relative to the defaults and
# names the constants the error must mention.
INVERTED: list[tuple[str, dict[str, float], list[str]]] = [
    (
        "3*heartbeat < lease_ttl",
        {"heartbeat_s": 10.0, "kill_grace_s": 1.0},
        ["heartbeat_s", "lease_ttl_s"],
    ),
    ("3*heartbeat == lease_ttl", {"lease_ttl_s": 15.0}, ["heartbeat_s", "lease_ttl_s"]),
    (
        "heartbeat+kill_grace < lease_ttl",
        {"kill_grace_s": 25.0, "backstop_grace_s": 100.0},
        ["kill_grace_s", "lease_ttl_s"],
    ),
    (
        "backstop > kill_grace+heartbeat",
        {"backstop_grace_s": 10.0},
        ["backstop_grace_s"],
    ),
    (
        "worker_dead_after > lease_ttl",
        {"worker_dead_after_s": 30.0},
        ["worker_dead_after_s", "lease_ttl_s"],
    ),
    (
        "claim_wait+5 <= client_read_timeout",
        {"client_read_timeout_s": 24.9},
        ["claim_wait_s", "client_read_timeout_s"],
    ),
    ("tick <= heartbeat", {"tick_s": 5.5}, ["tick_s", "heartbeat_s"]),
]


@pytest.mark.parametrize(
    ("_label", "changes", "names"), INVERTED, ids=[r[0] for r in INVERTED]
)
def test_each_inverted_ordering_is_rejected(
    _label: str, changes: dict[str, float], names: list[str]
) -> None:
    timing = _timing(**changes)
    with pytest.raises(TimingError) as info:
        validate_timing(timing, DEFAULT_STAGE_TIMEOUTS)
    for name in names:
        assert name in str(info.value)


def test_a_period_must_be_positive() -> None:
    """A zero period (a tick loop that spins) is rejected by the positivity
    rule itself, not by an ordering that happens to name the same constant."""
    with pytest.raises(TimingError, match="tick_s must be positive"):
        validate_timing(_timing(tick_s=0.0))


def test_requeue_backoff_must_not_be_empty() -> None:
    """An empty backoff list would be an IndexError at the first requeue."""
    with pytest.raises(TimingError, match="requeue_backoff_s"):
        validate_timing(dataclasses.replace(DEFAULTS, requeue_backoff_s=()))


def test_all_violations_are_reported_together() -> None:
    """Two independent violations appear in one TimingError, not the first only."""
    timing = _timing(worker_dead_after_s=10.0, tick_s=7.0)
    with pytest.raises(TimingError) as info:
        validate_timing(timing)
    message = str(info.value)
    assert "worker_dead_after_s" in message
    assert "tick_s" in message
    assert message.splitlines() == [message]  # the CLI prints it as one line


################################################################################
# The Committed Files
################################################################################

REPO = Path(__file__).resolve().parents[2]


def test_committed_rtlfarm_toml_loads_and_equals_the_defaults() -> None:
    """The committed file writes every default out; it must still parse and agree."""
    config = load_config(toml_path=REPO / "rtlfarm.toml", env={})
    assert config == Config()
    validate_timing(config.timing, DEFAULT_STAGE_TIMEOUTS)


def test_committed_env_example_loads_to_the_defaults() -> None:
    """The shipped file names only known keys and leaves every setting at its
    default, tokens included, so copying it does not silently enable auth."""
    env = read_dotenv(REPO / ".env.example")
    assert env, "expected at least one RTLFARM_ key in .env.example"
    assert all(name.startswith("RTLFARM_") for name in env)
    assert load_config(toml_path=None, env=env) == Config()


################################################################################
# Dotenv Files
################################################################################


def test_read_dotenv_parses_pairs_and_skips_noise(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# tokens\n"
        "export RTLFARM_CLIENT_TOKEN=abc # the client token\n"
        "export\tRTLFARM_INSECURE_BIND=1\n"
        "export\n"
        "\n"
        "RTLFARM_WORKER_TOKEN = 'quoted' # after the closing quote\n"
        'RTLFARM_DATA_DIR="/srv/farm"\n'
        "RTLFARM_CONTROL_URL=http://h:1#not-a-comment\n"
        "not a pair\n"
        "RTLFARM_TOOLCHAIN__DIGEST=\n",
        encoding="utf-8",
    )
    assert read_dotenv(env) == {
        "RTLFARM_CLIENT_TOKEN": "abc",
        "RTLFARM_INSECURE_BIND": "1",
        "RTLFARM_WORKER_TOKEN": "quoted",
        "RTLFARM_DATA_DIR": "/srv/farm",
        "RTLFARM_CONTROL_URL": "http://h:1#not-a-comment",
        "RTLFARM_TOOLCHAIN__DIGEST": "",
    }


def test_read_dotenv_rejects_a_quote_that_never_closes(tmp_path: Path) -> None:
    """A corrupted token must not be accepted silently."""
    env = tmp_path / ".env"
    env.write_text('RTLFARM_WORKER_TOKEN=w\nRTLFARM_CLIENT_TOKEN="abc\n', "utf-8")
    with pytest.raises(ConfigError, match=r"\.env, line 2: a quoted value"):
        read_dotenv(env)
