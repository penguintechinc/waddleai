"""Test the in-repo WaddleAI proto definition and its generated Python stubs.

Guards two things: (1) proto/waddleai/v1/proxy.proto exists, is proto3,
declares `package waddleai.v1;`, and every request message carries the
house-standard `string api_version = 1;` field (gRPC versioning rule, see
backend.md); and (2) once generated via scripts/generate_proto.sh, the
Python stubs under grpc_proto/waddleai/v1/ expose the same contract, so
grpc_server.py's WaddleAIServiceServicer keeps working against them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PROTO_PATH = REPO_ROOT / "proto" / "waddleai" / "v1" / "proxy.proto"
GENERATED_PKG_DIR = REPO_ROOT / "proxy" / "apps" / "proxy_server" / "grpc_proto" / "waddleai" / "v1"

# Every message sent as an RPC request on WaddleAIService. UsageReport is
# included despite its name -- it is ReportUsage's request message, recovered
# from the vendored marchproxy stub, and functions as a request for the
# purposes of the api_version rule.
REQUEST_MESSAGE_NAMES = [
    "RouteRequest",
    "SecurityRequest",
    "StoreTurnRequest",
    "GetContextRequest",
    "SearchMemoriesRequest",
    "UsageReport",
]

RESPONSE_MESSAGE_NAMES = [
    "RouteResponse",
    "SecurityResponse",
    "StoreTurnResponse",
    "GetContextResponse",
    "SearchMemoriesResponse",
    "MemoryEntry",
    "UsageAck",
]


def _proto_text() -> str:
    return PROTO_PATH.read_text()


def _message_body(text: str, message_name: str) -> str:
    match = re.search(rf"message\s+{message_name}\s*{{(.*?)^}}", text, re.S | re.M)
    assert match, f"message {message_name} not found in {PROTO_PATH}"
    return match.group(1)


class TestProxyProtoSource:
    """Assertions against the checked-in .proto source text."""

    def test_proto_file_exists(self) -> None:
        """The checked-in proxy.proto file exists at the expected repo-relative path."""
        assert PROTO_PATH.is_file(), f"expected {PROTO_PATH} to exist"

    def test_proto3_syntax(self) -> None:
        """The proto declares `syntax = "proto3";`."""
        assert re.search(r'syntax\s*=\s*"proto3";', _proto_text())

    def test_declares_waddleai_v1_package(self) -> None:
        """The proto declares `package waddleai.v1;`."""
        assert re.search(r"package\s+waddleai\.v1;", _proto_text())

    def test_declares_go_package_option(self) -> None:
        """The proto declares the go_package option pinned to proto/waddleai/v1;waddleaiv1."""
        text = _proto_text()
        assert "option go_package" in text
        assert "proto/waddleai/v1;waddleaiv1" in text

    @pytest.mark.parametrize("message_name", REQUEST_MESSAGE_NAMES)
    def test_request_message_has_api_version_field(self, message_name: str) -> None:
        """Every request message carries `string api_version = 1;` (backend.md)."""
        body = _message_body(_proto_text(), message_name)
        assert re.search(r"string\s+api_version\s*=\s*1\s*;", body), (
            f"{message_name} is missing `string api_version = 1;`"
        )

    @pytest.mark.parametrize("message_name", RESPONSE_MESSAGE_NAMES)
    def test_response_message_has_no_api_version_field(self, message_name: str) -> None:
        """Sanity check: response/value messages are not request messages."""
        body = _message_body(_proto_text(), message_name)
        assert "api_version" not in body

    def test_no_marchproxy_proto_import(self) -> None:
        """No `import "marchproxy/...";` dependency.

        A doc-comment mention of the recovered-from stub (for provenance) is fine, an import isn't.
        """
        text = _proto_text()
        assert not re.search(r'import\s+"[^"]*marchproxy[^"]*"', text, re.I)


class TestGeneratedWaddleaiStubs:
    """Assertions against the generated package (run after generate_proto.sh)."""

    @pytest.fixture(autouse=True)
    def _generated_module(self):
        pytest.importorskip("grpc_tools")

        pb2_path = GENERATED_PKG_DIR / "proxy_pb2.py"
        grpc_path = GENERATED_PKG_DIR / "proxy_pb2_grpc.py"
        if not (pb2_path.is_file() and grpc_path.is_file()):
            pytest.skip(
                f"generated stubs not present under {GENERATED_PKG_DIR} — "
                "run scripts/generate_proto.sh first"
            )

        proxy_server_dir = str(REPO_ROOT / "proxy" / "apps" / "proxy_server")
        added = proxy_server_dir not in sys.path
        if added:
            sys.path.insert(0, proxy_server_dir)
        try:
            from grpc_proto.waddleai.v1 import proxy_pb2, proxy_pb2_grpc

            self.proxy_pb2 = proxy_pb2
            self.proxy_pb2_grpc = proxy_pb2_grpc
            yield
        finally:
            if added:
                sys.path.remove(proxy_server_dir)

    def test_generated_package_declares_waddleai_v1(self) -> None:
        """Generated proxy_pb2 module's DESCRIPTOR.package matches the proto's `waddleai.v1`."""
        assert self.proxy_pb2.DESCRIPTOR.package == "waddleai.v1"

    def test_servicer_exposes_all_rpc_methods(self) -> None:
        """Generated WaddleAIServiceServicer exposes all six RPC methods from the proto."""
        servicer_methods = {
            name
            for name in dir(self.proxy_pb2_grpc.WaddleAIServiceServicer)
            if not name.startswith("_")
        }
        expected = {
            "EvaluateRoute",
            "EvaluateSecurity",
            "StoreTurn",
            "GetContext",
            "SearchMemories",
            "ReportUsage",
        }
        assert expected <= servicer_methods

    @pytest.mark.parametrize("message_name", REQUEST_MESSAGE_NAMES)
    def test_generated_request_message_has_api_version_field(self, message_name: str) -> None:
        """Generated request message class has a string api_version field numbered 1."""
        message_cls = getattr(self.proxy_pb2, message_name)
        fields = message_cls.DESCRIPTOR.fields_by_name
        assert "api_version" in fields, f"{message_name} missing api_version field"
        assert fields["api_version"].number == 1
        assert fields["api_version"].type == fields["api_version"].TYPE_STRING

    def test_route_response_field_shape_matches_recovered_servicer_usage(self) -> None:
        """RouteResponse fields must match what grpc_server.py constructs."""
        fields = {f.name for f in self.proxy_pb2.RouteResponse.DESCRIPTOR.fields}
        assert fields == {
            "recommended_model",
            "complexity",
            "target_type",
            "confidence",
            "reasoning",
        }
