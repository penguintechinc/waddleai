"""Contract tests for the classification v1 gRPC proto (frozen P3 interface).

Guards the .proto against field renumbering/removal (golden descriptor) and
against drift from shared.security.ner_filter.ENTITY_CONFIG (enum parity).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from google.protobuf import descriptor_pb2

from shared.security.ner_filter import ENTITY_CONFIG

_ROOT = Path(__file__).resolve().parents[3]
_PROTO_ROOT = _ROOT / "proto"
_PROTO_REL = "waddleai/classification/v1/classification.proto"
_GOLDEN = _PROTO_ROOT / "waddleai/classification/v1/classification.desc"


def _regenerate_descriptor(out: Path) -> bytes:
    """Run protoc on the committed .proto and return the FileDescriptorSet bytes."""
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable, "-m", "grpc_tools.protoc",
            "-I", str(_PROTO_ROOT),
            f"--descriptor_set_out={out}",
            _PROTO_REL,
        ],
        check=True,
        cwd=_ROOT,
    )  # fmt: skip
    return out.read_bytes()


def _file_descriptor() -> descriptor_pb2.FileDescriptorProto:
    """Load the committed golden descriptor as a FileDescriptorProto."""
    fds = descriptor_pb2.FileDescriptorSet.FromString(_GOLDEN.read_bytes())
    assert len(fds.file) == 1
    return fds.file[0]


_ENUM_PREFIX = {
    "EntityType": "ENTITY_TYPE_",
    "EntityAction": "ENTITY_ACTION_",
}


def _enum_values(name: str) -> dict[str, int]:
    """Return {unprefixed_value_name: number} for a top-level enum in the golden descriptor.

    Proto3 enum values share the package scope, so each carries an UPPER_SNAKE enum-name
    prefix (e.g. ENTITY_TYPE_PERSON); the prefix is stripped so names compare to config keys.
    """
    prefix = _ENUM_PREFIX.get(name, "")
    for enum in _file_descriptor().enum_type:
        if enum.name == name:
            return {v.name.removeprefix(prefix): v.number for v in enum.value}
    raise AssertionError(f"enum {name} missing")


def test_descriptor_matches_golden(tmp_path: Path) -> None:
    """Regenerated descriptor must equal the committed golden (catches renumber/removal)."""
    assert _regenerate_descriptor(tmp_path / "out.desc") == _GOLDEN.read_bytes()


def test_entity_type_enum_covers_entity_config() -> None:
    """EntityType and ENTITY_CONFIG keys must match exactly, in both directions."""
    enum_names = {n for n in _enum_values("EntityType") if n not in ("UNSPECIFIED", "OTHER")}
    config_keys = set(ENTITY_CONFIG)
    assert config_keys - enum_names == set(), "ENTITY_CONFIG key missing from EntityType"
    assert enum_names - config_keys == set(), "EntityType value missing from ENTITY_CONFIG"
    assert _enum_values("EntityType")["OTHER"] == 99


def test_entity_action_enum_matches_config_actions() -> None:
    """The redact|log|block actions in ENTITY_CONFIG map 1:1 to EntityAction values."""
    used = {action.upper() for action, _ in ENTITY_CONFIG.values()}
    enum_actions = {n for n in _enum_values("EntityAction") if n != "UNSPECIFIED"}
    assert used <= enum_actions
    assert enum_actions == {"LOG", "REDACT", "BLOCK"}


def test_api_version_field_present_on_requests() -> None:
    """Every request message carries string api_version as field 1."""
    messages = {m.name: m for m in _file_descriptor().message_type}
    for name in ("AnalyzeRequest", "GetEntityConfigRequest"):
        field = messages[name].field[0]
        assert (field.name, field.number) == ("api_version", 1), name
        assert field.type == descriptor_pb2.FieldDescriptorProto.TYPE_STRING


def test_entity_span_has_no_text_field() -> None:
    """Spans-only contract: EntitySpan must never carry entity text."""
    spans = next(m for m in _file_descriptor().message_type if m.name == "EntitySpan")
    assert "text" not in {f.name for f in spans.field}
