#!/bin/bash
# Compile XDP BPF program for WaddleAI

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_FILE="$SCRIPT_DIR/xdp_filter.c"
OUTPUT_FILE="$SCRIPT_DIR/xdp_filter.o"

echo "Compiling XDP BPF program..."
echo "Source: $SOURCE_FILE"
echo "Output: $OUTPUT_FILE"

# Check if clang is installed
if ! command -v clang &> /dev/null; then
    echo "Error: clang not found. Install with: apt-get install clang"
    exit 1
fi

# Check if BPF headers are available
if [ ! -d "/usr/include/bpf" ] && [ ! -d "/usr/include/linux" ]; then
    echo "Error: BPF headers not found. Install with: apt-get install libbpf-dev linux-headers-$(uname -r)"
    exit 1
fi

# Compile with optimization
clang -O2 -target bpf \
    -c "$SOURCE_FILE" \
    -o "$OUTPUT_FILE" \
    -I/usr/include/bpf \
    -I/usr/include \
    -Wall

if [ $? -eq 0 ]; then
    echo "✓ Compilation successful: $OUTPUT_FILE"
    ls -lh "$OUTPUT_FILE"

    # Verify it's a valid BPF object
    if command -v readelf &> /dev/null; then
        echo ""
        echo "BPF sections:"
        readelf -S "$OUTPUT_FILE" | grep -E "xdp|maps"
    fi
else
    echo "✗ Compilation failed"
    exit 1
fi