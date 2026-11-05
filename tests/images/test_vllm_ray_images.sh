#!/usr/bin/env bash
# tests/images/test_vllm_ray_images.sh: image contract gate (A7).
# Usage: test_vllm_ray_images.sh <image-ref> <ray-version> <py-minor>
# Fails loud on any mismatch. Zero checks run is a failure.
set -euo pipefail
[ "$#" -eq 3 ] || { echo "usage: $0 <image-ref> <ray-version> <py-minor>"; exit 2; }
img="$1"; ray_ver="$2"; py_minor="$3"
checks=0

uid=$(docker run --rm --entrypoint /usr/bin/id "$img" -u)
[ "$uid" = "1000" ] || { echo "FAIL: uid=$uid want 1000"; exit 1; }
checks=$((checks + 1))

got=$(docker run --rm --entrypoint python3 "$img" -c 'import ray,sys;print(ray.__version__, "%d.%d" % sys.version_info[:2])')
[ "$got" = "$ray_ver $py_minor" ] || { echo "FAIL: got '$got' want '$ray_ver $py_minor'"; exit 1; }
checks=$((checks + 1))

for b in ray python3 bash nvidia-smi rocm-smi xpu-smi; do
  if path=$(docker run --rm --entrypoint sh "$img" -c "command -v $b" 2>/dev/null); then
    echo "found $b $path"
  else
    echo "absent $b"   # vendor-specific tools may be absent; the Tetragon allowlist uses only the found set
  fi
  checks=$((checks + 1))
done
[ "$checks" -gt 0 ] || { echo "FAIL: zero checks ran"; exit 1; }
echo "PASS: $img uid=1000 ray=$ray_ver py=$py_minor checks=$checks"
