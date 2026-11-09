#!/usr/bin/env bash
# Disposable Linux engines. No host ports, Docker socket mounts, or production
# engine objects beyond the uniquely named test containers and socket volume.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN=${KIRI_BIN_DIR:-"$ROOT/target/linux/release"}
REPORTS=${KIRI_REPORT_DIR:-"$ROOT/target/compatibility"}
RUN="kiri-compat-$(date +%s)-$$"
mkdir -p "$REPORTS"
test -x "$BIN/kiri"
test -x "$BIN/kiri-init"
cleanup() {
  docker rm -fv "$RUN-kiri" "$RUN-reference" >/dev/null 2>&1 || true
  docker volume rm "$RUN-sockets" "$RUN-state" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM
docker volume create "$RUN-sockets" >/dev/null
docker volume create "$RUN-state" >/dev/null
docker run -d --name "$RUN-reference" --privileged \
  -e DOCKER_TLS_CERTDIR= -v "$RUN-sockets:/sockets" \
  -v "$ROOT:/src:ro" -v "$REPORTS:/reports" \
  docker:28.0.4-dind sh -c \
  'apk add --no-cache python3 bash curl >/dev/null && exec dockerd-entrypoint.sh dockerd --host=unix:///sockets/reference.sock' >/dev/null
docker run -d --name "$RUN-kiri" --privileged \
  -e KIRI_SOCKET=/sockets/kiri.sock \
  -v "$RUN-sockets:/sockets" -v "$RUN-state:/var/lib/kiri" -v "$BIN:/opt/kiri:ro" \
  -v "$ROOT:/src:ro" -v "$REPORTS:/reports" \
  docker:28.0.4-cli sh -c \
  'apk add --no-cache python3 bash curl >/dev/null && exec /opt/kiri/kiri daemon' >/dev/null
for engine in reference kiri; do
  ready=0
  for _ in $(seq 120); do
    if docker exec "$RUN-$engine" docker -H "unix:///sockets/$engine.sock" version >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  if [ "$ready" != 1 ]; then
    docker logs "$RUN-$engine"
    exit 1
  fi
done
status=0
for engine in reference kiri; do
  echo "Container/API checks: $engine"
  docker exec "$RUN-$engine" python3 /src/tests/compatibility/containers.py \
    --socket "/sockets/$engine.sock" --output "/reports/$engine.json" \
    --benchmark "${KIRI_BENCH_SAMPLES:-50}" || status=1
  echo "IPv6 checks: $engine"
  docker exec -e DOCKER_HOST="unix:///sockets/$engine.sock" -e V6C_CLIENT=1 \
    "$RUN-$engine" bash /src/tests/ipv6-conformance.sh \
    >"$REPORTS/$engine-ipv6.log" 2>&1 || status=1
  tail -15 "$REPORTS/$engine-ipv6.log"
  docker logs "$RUN-$engine" >"$REPORTS/$engine-daemon.log" 2>&1
done
exit "$status"
