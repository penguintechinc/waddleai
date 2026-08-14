#!/bin/bash
# Generate Protocol Buffer files for the in-repo WaddleAI proto definitions.
#
# Generates Python gRPC stubs from proto/waddleai/**/*.proto into the proxy
# service's grpc_proto/waddleai/ package. Protos are authored in-repo under
# proto/waddleai/ — no external proto vendor checkout or env var is needed.
#
# Bash 3.2 compatible (macOS default) — no declare -A, no mapfile.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PROTO_ROOT="$PROJECT_ROOT/proto"
WADDLEAI_PROTO_DIR="$PROTO_ROOT/waddleai"
PROXY_OUTPUT_DIR="$PROJECT_ROOT/proxy/apps/proxy_server/grpc_proto"
WADDLEAI_OUTPUT_DIR="$PROXY_OUTPUT_DIR/waddleai"

echo "=== WaddleAI Proto Generation ==="
echo "Proto source dir: $WADDLEAI_PROTO_DIR"
echo "Proxy output dir: $WADDLEAI_OUTPUT_DIR"

if [ ! -d "$WADDLEAI_PROTO_DIR" ]; then
    echo "Error: proto source directory not found at $WADDLEAI_PROTO_DIR"
    exit 1
fi

if [ -z "$(find "$WADDLEAI_PROTO_DIR" -name '*.proto' -print -quit)" ]; then
    echo "Error: no .proto files found under $WADDLEAI_PROTO_DIR"
    exit 1
fi

mkdir -p "$PROXY_OUTPUT_DIR"

# Generate Python gRPC stubs for every proto under proto/waddleai/**/*.proto,
# preserving the version subdirectory (waddleai/v1, waddleai/v2, ...) in the
# generated output.
while IFS= read -r proto_file; do
    rel_path="${proto_file#"$PROTO_ROOT"/}"
    echo "Generating stubs for $rel_path..."
    python3 -m grpc_tools.protoc \
        -I "$PROTO_ROOT" \
        --python_out="$PROXY_OUTPUT_DIR" \
        --grpc_python_out="$PROXY_OUTPUT_DIR" \
        "$rel_path"
done < <(find "$WADDLEAI_PROTO_DIR" -name '*.proto' | sort)

# Fix imports to be package-relative. grpc_proto/ is appended to sys.path as
# a bare package root by proxy/apps/proxy_server/main.py, so generated
# cross-file imports (e.g. `from waddleai.v1 import proxy_pb2`) must become
# relative (`from . import proxy_pb2`) to resolve the same way the previously
# vendored proto stubs did.
echo "Fixing import paths..."
find "$WADDLEAI_OUTPUT_DIR" -name '*_pb2*.py' -print0 | while IFS= read -r -d '' py_file; do
    sed -i.bak -E 's/from waddleai\.v[0-9]+ import/from . import/g' "$py_file"
    rm -f "$py_file.bak"
done

# Ensure every generated package directory has an __init__.py, wildcard
# importing its own pb2/pb2_grpc modules for convenient access.
find "$WADDLEAI_OUTPUT_DIR" -type d | sort | while IFS= read -r pkg_dir; do
    init_file="$pkg_dir/__init__.py"
    {
        echo '"""'
        echo "Generated Protocol Buffer stubs for WaddleAI (in-repo proto)."
        echo '"""'
        echo
    } > "$init_file"
    for py_file in "$pkg_dir"/*_pb2.py "$pkg_dir"/*_pb2_grpc.py; do
        [ -f "$py_file" ] || continue
        module=$(basename "$py_file" .py)
        echo "from .$module import *" >> "$init_file"
    done
done

touch "$PROXY_OUTPUT_DIR/__init__.py"

echo ""
echo "Done! Proto files generated."
echo ""
echo "Generated files under $WADDLEAI_OUTPUT_DIR:"
find "$WADDLEAI_OUTPUT_DIR" -type f -name '*.py' | sort
