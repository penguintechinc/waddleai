#!/usr/bin/env bash
# Container-structure test + full-app `--network none` boot for the classification image.
# Usage: scripts/test_classification_image.sh <image>
set -euo pipefail

IMAGE="${1:?usage: $0 <image>}"
CST_VERSION="v1.19.3"
CST_SHA256="fa0fa333bb6ba5c14065e7468d2904f5c82d021d7e1c763c9a45c5f2fbe9ff5f"
CST="${CST:-}"

if [ -z "$CST" ]; then
  if command -v container-structure-test >/dev/null 2>&1; then
    CST="$(command -v container-structure-test)"
  else
    dir="$(mktemp -d)"
    trap 'rm -rf "$dir"' EXIT
    curl -fsSL -o "$dir/cst" "https://github.com/GoogleContainerTools/container-structure-test/releases/download/${CST_VERSION}/container-structure-test-linux-amd64"
    echo "${CST_SHA256}  $dir/cst" | sha256sum -c - >/dev/null
    chmod +x "$dir/cst"
    CST="$dir/cst"
  fi
fi

out="$("$CST" test --image "$IMAGE" --config tests/container/classification.yaml 2>&1)" || { echo "$out"; echo "FAIL: container-structure-test"; exit 1; }
echo "$out"
passed="$(echo "$out" | sed -n 's/^Passes:[[:space:]]*//p')"
failed="$(echo "$out" | sed -n 's/^Failures:[[:space:]]*//p')"
[ "${passed:-0}" -ge 5 ] && [ "${failed:-1}" -eq 0 ] || { echo "FAIL: structure tests pass=${passed:-?} fail=${failed:-?}"; exit 1; }

# Real boot with egress fully blocked: the app must reach /readyz = 200 (model baked).
name="cls-offline-$$"
docker run -d --rm --name "$name" --network none --read-only --tmpfs /tmp \
  -e ALLOW_INSECURE_GRPC=1 -e JWT_ISSUER=https://issuer.invalid -e JWT_AUDIENCE=waddleai-classification \
  -e JWKS_URL=https://issuer.invalid/jwks "$IMAGE" >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
ready=0
for _ in $(seq 1 90); do
  if docker exec "$name" python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=3).status==200 else 1)" >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 2
done
if [ "$ready" -ne 1 ]; then docker logs "$name" 2>&1 | tail -30; echo "FAIL: not ready with --network none"; exit 1; fi
echo "OK: ready with --network none, --read-only rootfs, tmpfs /tmp"
