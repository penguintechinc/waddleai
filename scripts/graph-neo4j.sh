#!/usr/bin/env bash
# graph-neo4j.sh — start/stop a real neo4j:5-community container for the
# live-Neo4j graph-platform integration harness (Task 14).
#
# Pinned by digest, not tag (dependency-pinning rule: mutable refs like
# `neo4j:5-community` are forbidden for external images in any environment).
# Resolved once via:
#   docker pull neo4j:5-community
#   docker inspect --format='{{index .RepoDigests 0}}' neo4j:5-community
# and baked in below as the default; override with NEO4J_IMAGE if a repo
# maintainer deliberately re-pins to a newer digest.
#
# Rootless: the upstream neo4j image already runs as its own non-root
# `neo4j` user (uid/gid 7474) by default -- no --user flag needed here.
#
# Bounded wait: readiness is polled via `cypher-shell` inside the container
# (a real bolt round-trip, not just "the process started") for up to 60s
# (30 x 2s); the up case is Destroy + fresh (`docker rm -f` before `docker
# run`), matching the pre-alpha/alpha "destroy + fresh" deploy tier.
set -euo pipefail

NEO4J_IMAGE="${NEO4J_IMAGE:-neo4j:5-community@sha256:037cf5756f0135cbfd66b739b6df7c7c4bb100f9ce11602f6f9538e17e02c74d}"
NAME="waddleai-graph-neo4j"
PASSWORD="${WADDLEAI_GRAPH_PASSWORD:-testpassword}"
BOLT_PORT="${WADDLEAI_GRAPH_BOLT_PORT:-7687}"
WAIT_ATTEMPTS=30
WAIT_INTERVAL_SECS=2

usage() {
  echo "usage: $0 up|down" >&2
  exit 2
}

cmd_up() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  echo "starting $NAME from $NEO4J_IMAGE ..."
  docker run -d --name "$NAME" -p "${BOLT_PORT}:7687" \
    -e NEO4J_AUTH="neo4j/${PASSWORD}" \
    "$NEO4J_IMAGE" >/dev/null

  echo "waiting for bolt (up to $((WAIT_ATTEMPTS * WAIT_INTERVAL_SECS))s) ..."
  attempt=1
  while [ "$attempt" -le "$WAIT_ATTEMPTS" ]; do
    if docker exec "$NAME" cypher-shell -u neo4j -p "$PASSWORD" "RETURN 1" >/dev/null 2>&1; then
      echo "neo4j ready on bolt://localhost:${BOLT_PORT}"
      return 0
    fi
    sleep "$WAIT_INTERVAL_SECS"
    attempt=$((attempt + 1))
  done

  echo "neo4j did not become ready within $((WAIT_ATTEMPTS * WAIT_INTERVAL_SECS))s" >&2
  docker logs "$NAME" --tail 50 >&2 || true
  exit 1
}

cmd_down() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  echo "$NAME removed (or was not running)"
}

case "${1:-up}" in
  up) cmd_up ;;
  down) cmd_down ;;
  *) usage ;;
esac
