#!/bin/bash
# Generate Protocol Buffer files for MarchProxy AILB ModuleService
#
# This script generates Python gRPC stubs from MarchProxy proto files

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
MARCHPROXY_PROTO_DIR="${MARCHPROXY_PROTO_DIR:-$HOME/code/MarchProxy/proto}"
OUTPUT_DIR="$PROJECT_ROOT/services/management/app/grpc/proto"

echo "=== WaddleAI Proto Generation ==="
echo "MarchProxy proto dir: $MARCHPROXY_PROTO_DIR"
echo "Output dir: $OUTPUT_DIR"

# Check if MarchProxy proto directory exists
if [ ! -d "$MARCHPROXY_PROTO_DIR" ]; then
    echo "Error: MarchProxy proto directory not found at $MARCHPROXY_PROTO_DIR"
    echo "Set MARCHPROXY_PROTO_DIR environment variable to the correct path"
    exit 1
fi

# Ensure output directory exists
mkdir -p "$OUTPUT_DIR/marchproxy"

# Generate Python gRPC stubs
echo "Generating Python gRPC stubs..."
python -m grpc_tools.protoc \
    -I "$MARCHPROXY_PROTO_DIR" \
    --python_out="$OUTPUT_DIR" \
    --grpc_python_out="$OUTPUT_DIR" \
    marchproxy/types.proto \
    marchproxy/module.proto

# Fix imports in generated files (Python proto import path issue)
echo "Fixing import paths..."
if [ -f "$OUTPUT_DIR/marchproxy/module_pb2_grpc.py" ]; then
    sed -i 's/from marchproxy import/from . import/g' "$OUTPUT_DIR/marchproxy/module_pb2_grpc.py"
fi
if [ -f "$OUTPUT_DIR/marchproxy/module_pb2.py" ]; then
    sed -i 's/from marchproxy import/from . import/g' "$OUTPUT_DIR/marchproxy/module_pb2.py"
fi

# Create __init__.py for marchproxy package
cat > "$OUTPUT_DIR/marchproxy/__init__.py" << 'EOF'
"""
Generated Protocol Buffer stubs for MarchProxy
"""

from .types_pb2 import *
from .module_pb2 import *
from .module_pb2_grpc import *
EOF

echo "Done! Proto files generated at $OUTPUT_DIR"
echo ""
echo "Generated files:"
ls -la "$OUTPUT_DIR/marchproxy/"
