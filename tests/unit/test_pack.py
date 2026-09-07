"""Packing a design directory: which files are taken, in what order, with
what digests, and every rule that rejects a pack.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from rtlfarm.expand import pack as packing
from rtlfarm.expand.pack import Manifest, PackError, pack
from rtlfarm.expand.pipeline import PipelineError

################################################################################
# Building Packs
################################################################################


def _pipeline(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": 1,
        "design": {
            "name": "counter",
            "files": {
                "rtl": ["pkg/*.sv", "rtl/*.sv"],
                "include": ["tb/rtlfarm_tb.svh"],
                "tb": ["tb/*.sv"],
                "data": ["tb/vectors/*.hex"],
            },
        },
        "targets": [
            {
                "name": "tb_basic",
                "top": "tb_basic",
                "tb": ["tb/tb_basic.sv"],
                "data": ["tb/vectors/basic.hex"],
                "timeout_sim": "10ms",
            }
        ],
        "stages": {
            "compile": {
                "tool": "iverilog",
                "consumes": ["rtl", "include", "tb"],
                "per_target": True,
                "timeout_s": 60,
            },
            "simulate": {
                "tool": "iverilog",
                "consumes": ["data"],
                "depends_on": ["compile"],
                "fan_out": "targets",
                "timeout_s": 60,
            },
        },
    }
    data.update(overrides)
    return data


FILES: dict[str, bytes] = {
    "pkg/types_pkg.sv": b"package types_pkg; endpackage\n",
    "rtl/counter.sv": b"module counter; endmodule\n",
    "rtl/adder.sv": b"module adder; endmodule\n",
    "tb/rtlfarm_tb.svh": b"`define RTLFARM_PASS\n",
    "tb/tb_basic.sv": b"module tb_basic; endmodule\n",
    "tb/vectors/basic.hex": b"00\n01\n",
}


def _make_pack(
    root: Path, files: dict[str, bytes] = FILES, pipeline: dict[str, Any] | None = None
) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (root / "rtlfarm.yaml").write_text(
        yaml.safe_dump(pipeline or _pipeline()), encoding="utf-8"
    )
    return root


def _issues(root: Path, **kwargs: Any) -> list[str]:
    with pytest.raises(PackError) as info:
        pack(root, **kwargs)
    return sorted(str(issue) for issue in info.value.issues)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return _make_pack(tmp_path / "design")


################################################################################
# What Gets Packed, and in What Order
################################################################################


def test_manifest_lists_every_matched_file_by_role_then_ordinal(root: Path) -> None:
    m = pack(root)
    assert m.design == "counter"
    assert m.manifest_version == 1
    assert [(f.role, f.ordinal, f.path) for f in m.files] == [
        ("rtl", 0, "pkg/types_pkg.sv"),
        ("rtl", 1, "rtl/adder.sv"),
        ("rtl", 2, "rtl/counter.sv"),
        ("include", 0, "tb/rtlfarm_tb.svh"),
        ("tb", 0, "tb/tb_basic.sv"),
        ("data", 0, "tb/vectors/basic.hex"),
    ]


def test_glob_declaration_order_beats_path_order(tmp_path: Path) -> None:
    files = {"z_pkg/p.sv": b"p", "a_rtl/r.sv": b"r", "tb/t.sv": b"t"}
    p = _pipeline()
    p["design"]["files"] = {"rtl": ["z_pkg/*.sv", "a_rtl/*.sv"], "tb": ["tb/*.sv"]}
    p["stages"]["compile"]["consumes"] = ["rtl", "tb"]
    p["stages"]["simulate"]["consumes"] = []
    p["targets"][0]["tb"] = ["tb/t.sv"]
    p["targets"][0]["data"] = []
    m = pack(_make_pack(tmp_path, files, p))
    assert m.paths("rtl") == ["z_pkg/p.sv", "a_rtl/r.sv"]


def test_recursive_globs_and_matched_directories(tmp_path: Path) -> None:
    files = dict(FILES)
    files["rtl/sub/deep.sv"] = b"module deep; endmodule\n"
    p = _pipeline()
    p["design"]["files"]["rtl"] = ["pkg/*.sv", "rtl/**/*.sv"]
    m = pack(_make_pack(tmp_path, files, p))
    assert m.paths("rtl") == [
        "pkg/types_pkg.sv",
        "rtl/adder.sv",
        "rtl/counter.sv",
        "rtl/sub/deep.sv",
    ]


def test_paths_are_relative_and_forward_slash(root: Path) -> None:
    for f in pack(root).files:
        assert not f.path.startswith("/")
        assert "\\" not in f.path
        assert ".." not in f.path.split("/")


################################################################################
# Digests
################################################################################


def _entry(m: Manifest, path: str) -> packing.ManifestEntry:
    return next(f for f in m.files if f.path == path)


def test_sha256_and_size_match_the_bytes(root: Path) -> None:
    entry = _entry(pack(root), "rtl/counter.sv")
    content = FILES["rtl/counter.sv"]
    assert entry.sha256 == hashlib.sha256(content).hexdigest()
    assert entry.size == len(content)


def test_bytes_are_hashed_as_is(tmp_path: Path) -> None:
    lf = _make_pack(tmp_path / "lf")
    crlf_files = dict(FILES)
    crlf_files["rtl/counter.sv"] = FILES["rtl/counter.sv"].replace(b"\n", b"\r\n")
    crlf = _make_pack(tmp_path / "crlf", crlf_files)
    assert _entry(pack(lf), "rtl/counter.sv").sha256 != (
        _entry(pack(crlf), "rtl/counter.sv").sha256
    )


def test_digest_is_stable_under_creation_order_and_mtime(tmp_path: Path) -> None:
    a = _make_pack(tmp_path / "a", dict(reversed(list(FILES.items()))))
    b = _make_pack(tmp_path / "b")
    for path in b.rglob("*"):
        os.utime(path, (0, 0))
    assert pack(a).digest() == pack(b).digest()
    assert pack(a).canonical_json() == pack(b).canonical_json()


def test_digest_changes_when_one_byte_changes(tmp_path: Path) -> None:
    a = _make_pack(tmp_path / "a")
    changed = dict(FILES)
    changed["tb/vectors/basic.hex"] = b"00\n02\n"
    b = _make_pack(tmp_path / "b", changed)
    assert pack(a).digest() != pack(b).digest()


def test_canonical_json_is_sorted_compact_and_carries_the_pipeline(root: Path) -> None:
    text = pack(root).canonical_json()
    data = json.loads(text)
    assert text == json.dumps(data, sort_keys=True, separators=(",", ":"))
    assert data["manifest_version"] == 1
    assert data["pipeline"]["targets"][0]["seeds"] == [1]
    assert data["files"][0] == {
        "path": "pkg/types_pkg.sv",
        "role": "rtl",
        "ordinal": 0,
        "sha256": hashlib.sha256(FILES["pkg/types_pkg.sv"]).hexdigest(),
        "size": len(FILES["pkg/types_pkg.sv"]),
    }


def test_hash_file_reads_large_files_in_chunks(tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    content = os.urandom(3 * 1024 * 1024 + 17)
    big.write_bytes(content)
    assert packing.hash_file(big) == (hashlib.sha256(content).hexdigest(), len(content))


################################################################################
# Exclusions
################################################################################


def test_git_pycache_and_own_output_are_never_packed(tmp_path: Path) -> None:
    files = dict(FILES)
    files["rtl/.git/objects/x.sv"] = b"not rtl"
    files["rtl/__pycache__/y.sv"] = b"not rtl"
    files["rtl/manifest.sv"] = b"module m; endmodule\n"
    p = _pipeline()
    p["design"]["files"]["rtl"] = ["pkg/*.sv", "rtl/**/*.sv"]
    root = _make_pack(tmp_path, files, p)
    m = pack(root, exclude=["rtl/manifest.sv"])
    packed = {f.path for f in m.files}
    assert "rtl/.git/objects/x.sv" not in packed
    assert "rtl/__pycache__/y.sv" not in packed
    assert "rtl/manifest.sv" not in packed
    assert "rtl/counter.sv" in packed


################################################################################
# Rejections
################################################################################


def test_missing_pipeline_file(tmp_path: Path) -> None:
    assert _issues(tmp_path) == ["rtlfarm.yaml: no such file in the pack"]


def test_invalid_pipeline_is_a_pipeline_error(root: Path) -> None:
    (root / "rtlfarm.yaml").write_text("version: 2\n", encoding="utf-8")
    with pytest.raises(PipelineError):
        pack(root)


def test_glob_that_matches_nothing(root: Path) -> None:
    p = _pipeline()
    p["design"]["files"]["rtl"] = ["pkg/*.sv", "rtl/*.sv", "missing/*.sv"]
    (root / "rtlfarm.yaml").write_text(yaml.safe_dump(p), encoding="utf-8")
    assert _issues(root) == ["/design/files/rtl/2: glob matched no files"]


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("/abs/*.sv", "must be relative to the pack"),
        ("../outside/*.sv", "may not contain '..'"),
        ("rtl\\*.sv", "use forward slashes"),
    ],
)
def test_bad_glob_patterns(root: Path, pattern: str, reason: str) -> None:
    p = _pipeline()
    p["design"]["files"]["rtl"] = ["pkg/*.sv", "rtl/*.sv", pattern]
    (root / "rtlfarm.yaml").write_text(yaml.safe_dump(p), encoding="utf-8")
    assert _issues(root) == [f"/design/files/rtl/2: {reason}"]


def test_file_matched_by_two_roles(root: Path) -> None:
    p = _pipeline()
    p["design"]["files"]["include"] = ["tb/rtlfarm_tb.svh", "rtl/counter.sv"]
    (root / "rtlfarm.yaml").write_text(yaml.safe_dump(p), encoding="utf-8")
    assert _issues(root) == ["rtl/counter.sv: matched by two roles: rtl and include"]


def test_symlinked_file_is_rejected(root: Path) -> None:
    (root / "rtl/linked.sv").symlink_to(root / "rtl/counter.sv")
    assert _issues(root) == ["rtl/linked.sv: symlink at rtl/linked.sv"]


def test_symlinked_directory_component_is_rejected(root: Path) -> None:
    (root / "real").mkdir()
    (root / "real/extra.sv").write_bytes(b"module extra; endmodule\n")
    (root / "linked").symlink_to(root / "real", target_is_directory=True)
    p = _pipeline()
    p["design"]["files"]["rtl"] = ["pkg/*.sv", "rtl/*.sv", "linked/*.sv"]
    (root / "rtlfarm.yaml").write_text(yaml.safe_dump(p), encoding="utf-8")
    assert _issues(root) == ["linked/extra.sv: symlink at linked"]


def test_symlink_to_a_file_outside_the_pack_is_rejected(root: Path) -> None:
    outside = root.parent / "outside.sv"
    outside.write_bytes(b"module outside; endmodule\n")
    (root / "rtl/outside.sv").symlink_to(outside)
    assert _issues(root) == ["rtl/outside.sv: symlink at rtl/outside.sv"]


def test_target_file_must_be_packed_under_its_role(root: Path) -> None:
    p = _pipeline()
    p["targets"][0]["tb"] = ["tb/tb_missing.sv"]
    p["targets"][0]["data"] = ["rtl/counter.sv"]
    (root / "rtlfarm.yaml").write_text(yaml.safe_dump(p), encoding="utf-8")
    assert _issues(root) == [
        "/targets/0/data/0: 'rtl/counter.sv' is not packed under role 'data'",
        "/targets/0/tb/0: 'tb/tb_missing.sv' is not packed under role 'tb'",
    ]


def test_every_problem_is_reported_together(root: Path) -> None:
    (root / "rtl/linked.sv").symlink_to(root / "rtl/counter.sv")
    p = _pipeline()
    p["design"]["files"]["rtl"] = ["pkg/*.sv", "rtl/*.sv", "missing/*.sv"]
    (root / "rtlfarm.yaml").write_text(yaml.safe_dump(p), encoding="utf-8")
    assert _issues(root) == [
        "/design/files/rtl/2: glob matched no files",
        "rtl/linked.sv: symlink at rtl/linked.sv",
    ]


def test_manifest_is_frozen(root: Path) -> None:
    m: Manifest = pack(root)
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.design = "other"  # type: ignore[misc]
