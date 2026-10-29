#!/bin/bash
# Regenerate the classification v1 gRPC stubs + golden FileDescriptorSet.
#
# Deterministic: grpcio-tools is pinned EXACTLY (stubs embed the protobuf gencode
# version, so a different grpcio-tools produces a different diff). The CI drift
# gate runs this script and then `git diff --exit-code`.
#
# Bash 3.2 compatible. Usage: scripts/gen-classification-proto.sh

set -euo pipefail

GRPCIO_TOOLS_VERSION="1.81.1"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
PROTO_ROOT="$ROOT/proto"
PROTO_REL="waddleai/classification/v1/classification.proto"
DESC_OUT="$PROTO_ROOT/waddleai/classification/v1/classification.desc"
OUT_DIR="$ROOT/services/classification/app/grpc_gen"
PY="${PYTHON:-python3}"

installed="$("$PY" -c 'import importlib.metadata as m; print(m.version("grpcio-tools"))' 2>/dev/null || true)"
if [ "$installed" != "$GRPCIO_TOOLS_VERSION" ]; then
    echo "Error: grpcio-tools==$GRPCIO_TOOLS_VERSION required (found '${installed:-none}')." >&2
    echo "  pip install 'grpcio-tools==$GRPCIO_TOOLS_VERSION'" >&2
    exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

"$PY" -m grpc_tools.protoc \
    -I "$PROTO_ROOT" \
    --python_out="$TMP" \
    --grpc_python_out="$TMP" \
    --descriptor_set_out="$DESC_OUT" \
    "$PROTO_REL"

mkdir -p "$OUT_DIR"
src="$TMP/waddleai/classification/v1"
for f in classification_pb2.py classification_pb2_grpc.py; do
    [ -f "$src/$f" ] || { echo "Error: protoc did not produce $f" >&2; exit 1; }
    # Flat package: rewrite the absolute cross-import to a relative one.
    sed -E 's/^from waddleai\.classification\.v1 import classification_pb2/from . import classification_pb2/' \
        "$src/$f" > "$OUT_DIR/$f"
done

cat > "$OUT_DIR/__init__.py" <<'PYEOF'
"""Generated gRPC stubs for waddleai.classification.v1 (generated)."""
PYEOF

echo "Generated: $OUT_DIR/classification_pb2{,_grpc}.py and $DESC_OUT"
