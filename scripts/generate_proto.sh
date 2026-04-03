#!/bin/bash
# Generate Protocol Buffer files for MarchProxy AILB services
#
# This script generates Python gRPC stubs from MarchProxy proto files
# for both the management and proxy services.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
MARCHPROXY_PROTO_DIR="${MARCHPROXY_PROTO_DIR:-$HOME/code/marchproxy/proto}"
MANAGEMENT_OUTPUT_DIR="$PROJECT_ROOT/services/management/app/grpc/proto"
PROXY_OUTPUT_DIR="$PROJECT_ROOT/proxy/apps/proxy_server/grpc_proto"

echo "=== WaddleAI Proto Generation ==="
echo "MarchProxy proto dir: $MARCHPROXY_PROTO_DIR"
echo "Management output dir: $MANAGEMENT_OUTPUT_DIR"
echo "Proxy output dir: $PROXY_OUTPUT_DIR"

# Check if MarchProxy proto directory exists
if [ ! -d "$MARCHPROXY_PROTO_DIR" ]; then
    echo "Error: MarchProxy proto directory not found at $MARCHPROXY_PROTO_DIR"
    echo "Set MARCHPROXY_PROTO_DIR environment variable to the correct path"
    exit 1
fi

# Define all output directories
OUTPUT_DIRS=(
    "$MANAGEMENT_OUTPUT_DIR"
    "$PROXY_OUTPUT_DIR"
)

# Ensure output directories exist
for out_dir in "${OUTPUT_DIRS[@]}"; do
    mkdir -p "$out_dir/marchproxy"
done

# Generate Python gRPC stubs for each proto file into each output directory
for proto_file in "$MARCHPROXY_PROTO_DIR"/marchproxy/*.proto; do
    filename=$(basename "$proto_file")
    echo "Generating stubs for $filename..."

    for out_dir in "${OUTPUT_DIRS[@]}"; do
        python3 -m grpc_tools.protoc \
            -I "$MARCHPROXY_PROTO_DIR" \
            --python_out="$out_dir" \
            --grpc_python_out="$out_dir" \
            "marchproxy/$filename"
    done
done

# Fix imports in all generated files across all output directories
echo "Fixing import paths..."
for out_dir in "${OUTPUT_DIRS[@]}"; do
    for py_file in "$out_dir"/marchproxy/*_pb2*.py; do
        [ -f "$py_file" ] || continue
        sed -i 's/from marchproxy import/from . import/g' "$py_file"
    done
done

# Create __init__.py for marchproxy package in each output directory
for out_dir in "${OUTPUT_DIRS[@]}"; do
    # Ensure parent __init__.py exists
    touch "$out_dir/__init__.py"

    # Build __init__.py from generated pb2 modules (excluding grpc stubs for wildcard imports)
    init_file="$out_dir/marchproxy/__init__.py"
    cat > "$init_file" << 'HEADER'
"""
Generated Protocol Buffer stubs for MarchProxy
"""

HEADER
    # Add wildcard imports for all pb2 and pb2_grpc modules found
    for py_file in "$out_dir"/marchproxy/*_pb2.py "$out_dir"/marchproxy/*_pb2_grpc.py; do
        [ -f "$py_file" ] || continue
        module=$(basename "$py_file" .py)
        echo "from .$module import *" >> "$init_file"
    done
done

echo ""
echo "Done! Proto files generated."
echo ""
for out_dir in "${OUTPUT_DIRS[@]}"; do
    echo "Generated files in $out_dir/marchproxy/:"
    ls -la "$out_dir/marchproxy/"
    echo ""
done
