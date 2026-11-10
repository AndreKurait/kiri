#!/usr/bin/env bash
# Exercise the real Linux daemon and Unix credentials in disposable containers.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN=${KIRI_BIN_DIR:-"$ROOT/target/linux/release"}
RUN="kiri-socket-$(date +%s)-$$"
cleanup() {
  for variant in docker-group fallback managed; do
    docker rm -fv "$RUN-$variant" >/dev/null 2>&1 || true
    docker volume rm "$RUN-$variant-state" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT
trap 'exit 130' INT TERM
for variant in docker-group fallback managed; do
  docker volume create "$RUN-$variant-state" >/dev/null
  docker run -d --privileged --name "$RUN-$variant" \
    -e "SOCKET_TEST_VARIANT=$variant" \
    -v "$BIN:/opt/kiri:ro" -v "$ROOT/tests/compatibility:/tests:ro" \
    -v "$RUN-$variant-state:/var/lib/kiri" \
    docker:28.0.4-cli sh -c '
      apk add --no-cache python3 >/dev/null
      if [ "$SOCKET_TEST_VARIANT" = fallback ]; then
        sed -i "/^docker:/d" /etc/group
      elif [ "$SOCKET_TEST_VARIANT" = managed ]; then
        export KIRI_SOCKET_GROUP=root
      fi
      exec /opt/kiri/kiri daemon
    ' >/dev/null
  ready=0
  for _ in $(seq 60); do
    if docker exec "$RUN-$variant" docker -H unix:///run/kiri/docker.sock version >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  if [ "$ready" != 1 ]; then docker logs "$RUN-$variant"; exit 1; fi
  docker exec "$RUN-$variant" python3 /tests/socket-access.py "$variant"
done
