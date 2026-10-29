"""Contract tests for the classification v1 gRPC proto (frozen P3 interface).

Guards the .proto against field renumbering/removal (golden descriptor) and
against drift from shared.security.ner_filter.ENTITY_CONFIG (enum parity).
"""

from __future__ import annotations

import importlib.metadata
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


_PINNED_GRPCIO_TOOLS = "1.81.1"

# Hard-coded FROZEN contract (P3 depends on it). Deliberately NOT derived from the
# proto/.desc: a renumber that regenerated the golden must still fail here.
_FROZEN_ENUMS: dict[str, dict[str, int]] = {
    "FilterPhase": {
        "FILTER_PHASE_UNSPECIFIED": 0, "FILTER_PHASE_INPUT": 1, "FILTER_PHASE_OUTPUT": 2,
    },
    "FailMode": {"FAIL_MODE_UNSPECIFIED": 0, "FAIL_MODE_OPEN": 1, "FAIL_MODE_CLOSED": 2},
    "EntityAction": {
        "ENTITY_ACTION_UNSPECIFIED": 0, "ENTITY_ACTION_LOG": 1,
        "ENTITY_ACTION_REDACT": 2, "ENTITY_ACTION_BLOCK": 3,
    },
    "NerStatus": {
        "NER_STATUS_UNSPECIFIED": 0, "NER_STATUS_OK": 1, "NER_STATUS_GATE_DISABLED": 2,
        "NER_STATUS_BACKEND_UNAVAILABLE": 3, "NER_STATUS_DEGRADED": 4,
    },
    "DegradedReason": {
        "DEGRADED_REASON_UNSPECIFIED": 0, "DEGRADED_REASON_BACKEND_UNAVAILABLE": 1,
        "DEGRADED_REASON_WORKER_FAILURE": 2, "DEGRADED_REASON_ANALYSIS_ERROR": 3,
        "DEGRADED_REASON_OVERLOADED": 4, "DEGRADED_REASON_TIMEOUT": 5,
    },
    "EntityType": {
        "ENTITY_TYPE_UNSPECIFIED": 0, "ENTITY_TYPE_PERSON": 1, "ENTITY_TYPE_US_SSN": 2,
        "ENTITY_TYPE_US_PASSPORT": 3, "ENTITY_TYPE_US_DRIVER_LICENSE": 4,
        "ENTITY_TYPE_MEDICAL_LICENSE": 5, "ENTITY_TYPE_EMAIL_ADDRESS": 6,
        "ENTITY_TYPE_PHONE_NUMBER": 7, "ENTITY_TYPE_CREDIT_CARD": 8,
        "ENTITY_TYPE_IBAN_CODE": 9, "ENTITY_TYPE_CRYPTO": 10, "ENTITY_TYPE_LOCATION": 11,
        "ENTITY_TYPE_IP_ADDRESS": 12, "ENTITY_TYPE_URL": 13, "ENTITY_TYPE_NRP": 14,
        "ENTITY_TYPE_DATE_TIME": 15, "ENTITY_TYPE_ORG": 16, "ENTITY_TYPE_OTHER": 99,
    },
}  # fmt: skip

_FROZEN_MESSAGES: dict[str, dict[str, int]] = {
    "AnalyzeRequest": {
        "api_version": 1, "text": 2, "language": 3, "phase": 4, "org_public_id": 5,
        "flag_distinct_id": 6, "disabled_entity_labels": 7, "fail_mode": 8,
    },
    "AnalyzeResponse": {
        "spans": 1, "ner_status": 2, "backend": 3, "degraded": 4, "degraded_reason": 5,
        "block_recommended": 6, "cache_hit": 7, "analyzed_chars": 8, "truncated": 9,
        "model_fingerprint": 10,
    },
    "EntitySpan": {
        "entity_type": 1, "entity_label": 2, "start_cp": 3, "end_cp": 4, "start_byte": 5,
        "end_byte": 6, "raw_score": 7, "confidence": 8, "action": 9,
    },
    "GetEntityConfigRequest": {"api_version": 1},
    "EntityConfigItem": {
        "entity_type": 1, "entity_label": 2, "default_action": 3, "confidence_weight": 4,
    },
    "GetEntityConfigResponse": {"items": 1},
}  # fmt: skip


def test_contract_numbers_are_frozen() -> None:
    """Every enum value and field number equals the hard-coded frozen table (exact sets)."""
    fd = _file_descriptor()
    enums = {e.name: {v.name: v.number for v in e.value} for e in fd.enum_type}
    assert enums == _FROZEN_ENUMS
    msgs = {m.name: {f.name: f.number for f in m.field} for m in fd.message_type}
    assert msgs == _FROZEN_MESSAGES


def test_removed_numbers_stay_reserved() -> None:
    """Every enum/message reserves 100-199 so retired numbers cannot be silently reused."""
    fd = _file_descriptor()
    for enum in fd.enum_type:
        assert [(r.start, r.end) for r in enum.reserved_range] == [(100, 199)], enum.name
    for msg in fd.message_type:
        assert [(r.start, r.end) for r in msg.reserved_range] == [(100, 200)], msg.name


def test_grpcio_tools_matches_pin() -> None:
    """Installed grpcio-tools must equal the pin the gen script and CI use."""
    assert importlib.metadata.version("grpcio-tools") == _PINNED_GRPCIO_TOOLS
