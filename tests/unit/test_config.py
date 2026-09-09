"""Configuration layering and timing validation."""

from __future__ import annotations

import dataclasses
import typing
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rtlfarm.config import (
    BlobsConfig,
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


def _dotted_keys(cls: type, prefix: str = "") -> set[str]:
    """Every leaf key of a (nested) dataclass as ``section.key``."""
    keys: set[str] = set()
    hints = typing.get_type_hints(cls)
    for f in dataclasses.fields(cls):
        hint = hints[f.name]
        if dataclasses.is_dataclass(hint) and isinstance(hint, type):
            keys |= _dotted_keys(hint, f"{prefix}{f.name}.")
        else:
            keys.add(f"{prefix}{f.name}")
    return keys


def _write_toml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "rtlfarm.toml"
    path.write_text(text)
    return path


# One row per configuration key: (dotted key, TOML literal, env string, expected).
# Every value differs from the default so the layer is proven to have applied.
ROUND_TRIP: list[tuple[str, str, str, object]] = [
    ("timing.tick_s", "0.5", "0.5", 0.5),
    ("timing.heartbeat_s", "2", "2", 2.0),
    ("timing.lease_ttl_s", "12", "12", 12.0),
    ("timing.backstop_grace_s", "7", "7", 7.0),
    ("timing.worker_dead_after_s", "40", "40", 40.0),
    ("timing.kill_grace_s", "1", "1", 1.0),
    ("timing.claim_wait_s", "10", "10", 10.0),
    ("timing.client_read_timeout_s", "16", "16", 16.0),
    ("timing.requeue_backoff_s", "[1, 2, 3]", "[1, 2, 3]", (1.0, 2.0, 3.0)),
    ("timing.full_sweep_every", "7", "7", 7),
    ("timing.readyz_tick_factor", "4", "4", 4),
    ("timing.upload_timeout_s", "45", "45", 45.0),
    ("toolchain.digest", '"0123abcd"', "0123abcd", "0123abcd"),
    ("blobs.log_bytes", "1", "1", 1),
    ("blobs.input_file_bytes", "2", "2", 2),
    ("blobs.input_job_bytes", "3", "3", 3),
    ("blobs.diagnostics_bytes", "4", "4", 4),
    ("blobs.deps_bytes", "5", "5", 5),
    ("blobs.compiled_bytes", "6", "6", 6),
    ("blobs.result_bytes", "7", "7", 7),
    ("blobs.waveform_bytes", "8", "8", 8),
    ("blobs.coverage_bytes", "9", "9", 9),
    ("client_token", '"client-secret"', "client-secret", "client-secret"),
    ("worker_token", '"worker-secret"', "worker-secret", "worker-secret"),
    ("insecure_bind", "true", "1", True),
    ("data_dir", '"/var/lib/rtlfarm"', "/var/lib/rtlfarm", "/var/lib/rtlfarm"),
    (
        "control_url",
        '"http://control:8080"',
        "http://control:8080",
        "http://control:8080",
    ),
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
# Defaults and the Frozen Configuration
################################################################################


def test_defaults_match_spec_table() -> None:
    config = load_config(toml_path=None, env={})
    assert config.timing == TimingConfig(
        tick_s=1.0,
        heartbeat_s=5.0,
        lease_ttl_s=30.0,
        backstop_grace_s=15.0,
        worker_dead_after_s=90.0,
        kill_grace_s=5.0,
        claim_wait_s=20.0,
        client_read_timeout_s=30.0,
        requeue_backoff_s=(0.0, 5.0, 30.0),
        full_sweep_every=30,
        readyz_tick_factor=3,
        upload_timeout_s=300.0,
    )
    assert config.toolchain.digest is None
    assert config.blobs == BlobsConfig(
        log_bytes=16 * 1024 * 1024,
        input_file_bytes=64 * 1024 * 1024,
        input_job_bytes=512 * 1024 * 1024,
        diagnostics_bytes=4 * 1024 * 1024,
        deps_bytes=4 * 1024 * 1024,
        compiled_bytes=256 * 1024 * 1024,
        result_bytes=1024 * 1024,
        waveform_bytes=512 * 1024 * 1024,
        coverage_bytes=64 * 1024 * 1024,
    )
    assert config.client_token is None
    assert config.worker_token is None
    assert config.insecure_bind is False
    assert config.data_dir == "data"


def test_config_is_frozen() -> None:
    """Configuration is loaded once and never mutated (a project contract)."""
    config = load_config(toml_path=None, env={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.timing.lease_ttl_s = 1.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.client_token = "x"  # type: ignore[misc]


def test_missing_toml_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"rtlfarm\.toml"):
        load_config(toml_path=tmp_path / "rtlfarm.toml", env={})


def test_a_config_path_that_cannot_be_opened_is_an_error(tmp_path: Path) -> None:
    """Any open failure, not only a missing file: a directory, say."""
    with pytest.raises(ConfigError, match="config file"):
        load_config(toml_path=tmp_path, env={})


def test_a_config_file_that_is_not_utf8_is_an_error(tmp_path: Path) -> None:
    toml = tmp_path / "rtlfarm.toml"
    toml.write_bytes(b"\xff\xfe[\x00t\x00")  # UTF-16 with a byte-order mark
    with pytest.raises(ConfigError, match="config file"):
        load_config(toml_path=toml, env={})


################################################################################
# The Layers
################################################################################


def test_round_trip_table_covers_every_key() -> None:
    assert {row[0] for row in ROUND_TRIP} == _dotted_keys(Config)


@pytest.mark.parametrize(("key", "literal", "_env", "expected"), ROUND_TRIP)
def test_every_key_loads_from_toml(
    tmp_path: Path, key: str, literal: str, _env: str, expected: object
) -> None:
    config = load_config(
        toml_path=_write_toml(tmp_path, _toml_for(key, literal)), env={}
    )
    assert _get(config, key) == expected


@pytest.mark.parametrize(("key", "_literal", "env", "expected"), ROUND_TRIP)
def test_every_key_loads_from_env(
    key: str, _literal: str, env: str, expected: object
) -> None:
    config = load_config(toml_path=None, env={_env_name(key): env})
    assert _get(config, key) == expected


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


@pytest.mark.parametrize("name", ["RTLFARM_TEST_HOOKS", "RTLFARM_UPDATE_SNAPSHOTS"])
def test_process_switches_are_not_config_keys(name: str) -> None:
    """The fault-hook and snapshot switches are not configuration keys."""
    config = load_config(toml_path=None, env={name: "1"})
    assert config == Config()


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("RTLFARM_TIMING__LEASE_TTL_S", "inf"),
        ("RTLFARM_TIMING__LEASE_TTL_S", "-inf"),
        ("RTLFARM_TIMING__LEASE_TTL_S", "nan"),
        ("RTLFARM_TIMING__REQUEUE_BACKOFF_S", "[0, NaN]"),
    ],
    ids=["inf", "-inf", "nan", "backoff-nan"],
)
def test_non_finite_env_numbers_are_rejected(name: str, raw: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_config(toml_path=None, env={name: raw})


def test_non_finite_toml_floats_are_rejected(tmp_path: Path) -> None:
    toml = _write_toml(tmp_path, "[timing]\nworker_dead_after_s = inf\n")
    with pytest.raises(ConfigError, match=r"timing\.worker_dead_after_s"):
        load_config(toml_path=toml, env={})


def test_unrelated_environment_is_ignored() -> None:
    env = {"PATH": "/usr/bin", "RTLFARMX": "1", "rtlfarm_timing__tick_s": "0.1"}
    assert load_config(toml_path=None, env=env).timing.tick_s == 1.0


@pytest.mark.parametrize("raw", ["1", "true", "True", "yes", "on"])
def test_env_truthy_booleans(raw: str) -> None:
    assert load_config(toml_path=None, env={"RTLFARM_INSECURE_BIND": raw}).insecure_bind


@pytest.mark.parametrize("raw", ["0", "false", "False", "no", "off", ""])
def test_env_falsy_booleans(raw: str) -> None:
    config = load_config(toml_path=None, env={"RTLFARM_INSECURE_BIND": raw})
    assert config.insecure_bind is False


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
    ("toml-key", "[timing]\nlease_ttl = 45\n", {}, {}, r"timing\.lease_ttl"),
    ("toml-section", "[scheduler]\ntick_s = 1\n", {}, {}, "scheduler"),
    ("toml-scalar-for-section", "timing = 5\n", {}, {}, "timing"),
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


def test_wrong_type_in_toml_is_an_error(tmp_path: Path) -> None:
    toml = _write_toml(tmp_path, '[timing]\nlease_ttl_s = "thirty"\n')
    with pytest.raises(ConfigError, match=r"timing\.lease_ttl_s"):
        load_config(toml_path=toml, env={})


def test_wrong_type_in_env_is_an_error() -> None:
    with pytest.raises(ConfigError, match="RTLFARM_TIMING__FULL_SWEEP_EVERY"):
        load_config(toml_path=None, env={"RTLFARM_TIMING__FULL_SWEEP_EVERY": "1.5"})


def test_backoff_list_must_be_numbers(tmp_path: Path) -> None:
    toml = _write_toml(tmp_path, '[timing]\nrequeue_backoff_s = ["a"]\n')
    with pytest.raises(ConfigError, match=r"timing\.requeue_backoff_s"):
        load_config(toml_path=toml, env={})


################################################################################
# validate_timing: the Orderings Between the Constants
################################################################################

DEFAULTS = TimingConfig()
DEFAULT_STAGE_TIMEOUTS = (60.0, 300.0, 300.0)  # lint, compile, simulate defaults


def _timing(**changes: Any) -> TimingConfig:
    """The defaults with some constants replaced."""
    return TimingConfig(**{**dataclasses.asdict(DEFAULTS), **changes})


# The defaults themselves are validated by
# test_committed_rtlfarm_toml_loads_and_equals_the_defaults.


def test_process_tier_profile_validates() -> None:
    profile = dataclasses.replace(
        DEFAULTS,
        lease_ttl_s=3.0,
        heartbeat_s=0.5,
        kill_grace_s=0.5,
        tick_s=0.2,
        backstop_grace_s=1.5,
    )
    validate_timing(profile, DEFAULT_STAGE_TIMEOUTS)


def test_ci_profile_validates() -> None:
    profile = dataclasses.replace(
        DEFAULTS,
        lease_ttl_s=6.0,
        heartbeat_s=1.0,
        kill_grace_s=0.5,
        backstop_grace_s=2.0,
    )
    validate_timing(profile, DEFAULT_STAGE_TIMEOUTS)


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


def test_kill_grace_must_be_below_every_timeout() -> None:
    with pytest.raises(TimingError, match="kill_grace_s"):
        validate_timing(DEFAULTS, (60.0, 5.0))
    with pytest.raises(TimingError, match="kill_grace_s"):
        validate_timing(DEFAULTS, (4.0,))


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_every_timeout_must_be_positive(bad: float) -> None:
    with pytest.raises(TimingError, match="timeout_s"):
        validate_timing(DEFAULTS, (60.0, bad))


def test_no_timeouts_means_no_timeout_checks() -> None:
    validate_timing(DEFAULTS, ())
    validate_timing(DEFAULTS)


@pytest.mark.parametrize(
    "field",
    [
        "tick_s",
        "heartbeat_s",
        "lease_ttl_s",
        "backstop_grace_s",
        "worker_dead_after_s",
        "kill_grace_s",
        "claim_wait_s",
        "client_read_timeout_s",
        "upload_timeout_s",
    ],
)
@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_time_constants_must_be_positive(field: str, bad: float) -> None:
    timing = _timing(**{field: bad})
    with pytest.raises(TimingError, match=field):
        validate_timing(timing)


@pytest.mark.parametrize("field", ["full_sweep_every", "readyz_tick_factor"])
def test_counters_must_be_at_least_one(field: str) -> None:
    with pytest.raises(TimingError, match=field):
        validate_timing(_timing(**{field: 0}))


@pytest.mark.parametrize(
    "backoff", [(0.0, -5.0), ()], ids=["negative-entry", "empty-list"]
)
def test_requeue_backoff_must_be_non_empty_and_non_negative(
    backoff: tuple[float, ...],
) -> None:
    with pytest.raises(TimingError, match="requeue_backoff_s"):
        validate_timing(dataclasses.replace(DEFAULTS, requeue_backoff_s=backoff))


def test_claim_wait_plus_five_equal_to_client_timeout_is_accepted() -> None:
    """The long-poll rule is non-strict: claim_wait_s + 5 == client_read_timeout_s."""
    validate_timing(_timing(claim_wait_s=20.0, client_read_timeout_s=25.0))


def test_all_violations_are_reported_together() -> None:
    """Two independent violations appear in one TimingError, not the first only."""
    timing = _timing(worker_dead_after_s=10.0, tick_s=7.0)
    with pytest.raises(TimingError) as info:
        validate_timing(timing)
    message = str(info.value)
    assert "worker_dead_after_s" in message
    assert "tick_s" in message
    assert message.splitlines() == [message]  # the CLI prints it as one line


positive = st.floats(
    min_value=0.01, max_value=1000.0, allow_nan=False, allow_infinity=False
)


@given(
    tick_s=positive,
    heartbeat_s=positive,
    lease_ttl_s=positive,
    backstop_grace_s=positive,
    worker_dead_after_s=positive,
    kill_grace_s=positive,
    claim_wait_s=positive,
    client_read_timeout_s=positive,
    timeouts=st.lists(positive, max_size=3),
)
def test_validate_timing_accepts_exactly_the_spec_orderings(
    tick_s: float,
    heartbeat_s: float,
    lease_ttl_s: float,
    backstop_grace_s: float,
    worker_dead_after_s: float,
    kill_grace_s: float,
    claim_wait_s: float,
    client_read_timeout_s: float,
    timeouts: list[float],
) -> None:
    """Hypothesis over the constants: accepted iff every required ordering holds.

    The oracle restates the documented orderings, so it catches an
    implementation slip either way but not a shared misreading of the design.
    """
    timing = TimingConfig(
        tick_s=tick_s,
        heartbeat_s=heartbeat_s,
        lease_ttl_s=lease_ttl_s,
        backstop_grace_s=backstop_grace_s,
        worker_dead_after_s=worker_dead_after_s,
        kill_grace_s=kill_grace_s,
        claim_wait_s=claim_wait_s,
        client_read_timeout_s=client_read_timeout_s,
    )
    expected_ok = (
        3 * heartbeat_s < lease_ttl_s
        and heartbeat_s + kill_grace_s < lease_ttl_s
        and backstop_grace_s > kill_grace_s + heartbeat_s
        and worker_dead_after_s > lease_ttl_s
        and claim_wait_s + 5 <= client_read_timeout_s
        and all(kill_grace_s < t for t in timeouts)
        and tick_s <= heartbeat_s
    )
    if expected_ok:
        validate_timing(timing, timeouts)
    else:
        with pytest.raises(TimingError):
            validate_timing(timing, timeouts)


################################################################################
# The Committed Files
################################################################################

REPO = Path(__file__).resolve().parents[2]


def test_committed_rtlfarm_toml_loads_and_equals_the_defaults() -> None:
    """The committed file writes every default out; it must still parse and agree."""
    config = load_config(toml_path=REPO / "rtlfarm.toml", env={})
    assert config == Config()
    validate_timing(config.timing, DEFAULT_STAGE_TIMEOUTS)


def test_committed_env_example_names_only_known_keys() -> None:
    env = read_dotenv(REPO / ".env.example")
    assert env, "expected at least one RTLFARM_ key in .env.example"
    assert all(name.startswith("RTLFARM_") for name in env)
    load_config(toml_path=None, env=env)


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


def test_read_dotenv_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_dotenv(tmp_path / "absent") == {}


def test_read_dotenv_rejects_a_quote_that_never_closes(tmp_path: Path) -> None:
    """A corrupted token must not be accepted silently."""
    env = tmp_path / ".env"
    env.write_text('RTLFARM_WORKER_TOKEN=w\nRTLFARM_CLIENT_TOKEN="abc\n', "utf-8")
    with pytest.raises(ConfigError, match=r"\.env, line 2: a quoted value"):
        read_dotenv(env)


def test_read_dotenv_of_an_undecodable_file_is_an_error(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_bytes(b"\xff\xfeR\x00T\x00L\x00")  # UTF-16 with a byte-order mark
    with pytest.raises(ConfigError, match=r"dotenv file .*\.env"):
        read_dotenv(env)
