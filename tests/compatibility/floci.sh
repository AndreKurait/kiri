#!/usr/bin/env bash
# Same Floci image and verified Lambda workload, sequentially on isolated engines.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BIN=${KIRI_BIN_DIR:-"$ROOT/target/linux/release"}
REPORTS=${KIRI_REPORT_DIR:-"$ROOT/target/floci-benchmark"}
IMAGE=${FLOCI_IMAGE:-ghcr.io/andrekurait/floci@sha256:dff59622e40cd03fb3d72e6ef2fb6b258efb78499227a1115051887158974639}
RUN="kiri-floci-$(date +%s)-$$"
CLIENT=${FLOCI_CLIENT_IMAGE:-"$RUN-client"}
SERVICE_SAMPLES=${FLOCI_SERVICE_SAMPLES:-0}
mkdir -p "$REPORTS"
cleanup() {
  for runtime in docker kiri; do
    docker logs "$RUN-$runtime" >"$REPORTS/$runtime-daemon.log" 2>&1 || true
    docker rm -fv "$RUN-$runtime" >/dev/null 2>&1 || true
    docker volume rm "$RUN-$runtime-state" >/dev/null 2>&1 || true
  done
  rm -f "$REPORTS/$RUN-client.tar" "$REPORTS/$RUN-floci.tar"
  if [ -z "${FLOCI_CLIENT_IMAGE:-}" ]; then docker image rm "$CLIENT" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM
if [ -z "${FLOCI_CLIENT_IMAGE:-}" ]; then
  docker build -t "$CLIENT" - >"$REPORTS/client-build.log" 2>&1 <<'EOF'
FROM python:3.12-alpine
RUN pip install --no-cache-dir boto3==1.43.108 'psycopg[binary]==3.2.13'
EOF
fi
docker image save -o "$REPORTS/$RUN-client.tar" "$CLIENT"
if [ "${FLOCI_LOCAL_IMAGE:-0}" = 1 ]; then
  docker image save -o "$REPORTS/$RUN-floci.tar" "$IMAGE"
fi
uname -a >"$REPORTS/host-kernel.txt"
sha256sum "$BIN/kiri" "$BIN/kiri-init" >"$REPORTS/binaries.sha256"
for runtime in docker kiri; do
  docker volume create "$RUN-$runtime-state" >/dev/null
  if [ "$runtime" = docker ]; then
    base=docker:28.0.4-dind
    command='apk add --no-cache curl >/dev/null && exec dockerd-entrypoint.sh dockerd --host=unix:///run/docker.sock'
    socket=/run/docker.sock
  else
    base=docker:28.0.4-cli
    command='apk add --no-cache curl >/dev/null && exec /opt/kiri/kiri daemon'
    socket=/run/kiri/docker.sock
  fi
  docker run -d --privileged --cpuset-cpus "${FLOCI_CPUSET:-}" --name "$RUN-$runtime" -e DOCKER_TLS_CERTDIR= \
    -v "$RUN-$runtime-state:/var/lib" -v "$BIN:/opt/kiri:ro" \
    -v "$ROOT/tests/compatibility:/tests:ro" -v "$REPORTS:/reports" \
    "$base" sh -c "$command" >/dev/null
  D=(docker exec "$RUN-$runtime" docker -H "unix://$socket")
  ready=0
  for _ in $(seq 120); do
    if "${D[@]}" version >"$REPORTS/$runtime-version.txt" 2>&1; then ready=1; break; fi
    sleep 1
  done
  if [ "$ready" != 1 ]; then docker logs "$RUN-$runtime"; exit 1; fi
  "${D[@]}" load -i "/reports/$RUN-client.tar" >"$REPORTS/$runtime-load.log" 2>&1
  if [ "${FLOCI_LOCAL_IMAGE:-0}" = 1 ]; then
    "${D[@]}" load -i "/reports/$RUN-floci.tar" >"$REPORTS/$runtime-pull.log" 2>&1
  else
    "${D[@]}" pull "$IMAGE" >"$REPORTS/$runtime-pull.log" 2>&1
  fi
  "${D[@]}" pull public.ecr.aws/lambda/python:3.12 >"$REPORTS/$runtime-lambda-pull.log" 2>&1
  if [ "$SERVICE_SAMPLES" -gt 0 ]; then
    for service_image in valkey/valkey:8 postgres:16-alpine; do
      "${D[@]}" pull "$service_image" >>"$REPORTS/$runtime-service-pulls.log" 2>&1
      "${D[@]}" image inspect "$service_image" >>"$REPORTS/$runtime-service-images.jsonl"
    done
  fi
  "${D[@]}" image inspect "$IMAGE" >"$REPORTS/$runtime-image.json"
  "${D[@]}" image inspect public.ecr.aws/lambda/python:3.12 >"$REPORTS/$runtime-lambda-image.json"
  "${D[@]}" network create floci-test >/dev/null
  gid=$(docker exec "$RUN-$runtime" stat -c %g "$socket")
  "${D[@]}" run -d --name floci --network floci-test --group-add "$gid" \
    -v "$socket:/var/run/docker.sock" \
    -e FLOCI_BASE_URL=http://floci:4566 -e FLOCI_HOSTNAME=floci \
    -e FLOCI_SERVICES_RDS_ENDPOINT_HOST=floci \
    -e FLOCI_SERVICES_DOCKER_NETWORK=floci-test "$IMAGE" >/dev/null
  ip=$("${D[@]}" inspect -f '{{(index .NetworkSettings.Networks "floci-test").IPAddress}}' floci)
  ready=0
  for _ in $(seq 120); do
    if docker exec "$RUN-$runtime" curl -sf "http://$ip:4566/_floci/health" >"$REPORTS/$runtime-health.json"; then ready=1; break; fi
    sleep .25
  done
  if [ "$ready" != 1 ]; then "${D[@]}" logs floci; exit 1; fi
  docker inspect -f '{{json .HostConfig.CpusetCpus}}' "$RUN-$runtime" >"$REPORTS/$runtime-cpuset.json"
  "${D[@]}" exec floci sh -c 'grep Cpus_allowed_list /proc/1/status; grep Cpus_allowed_list /proc/self/status' \
    >"$REPORTS/$runtime-affinity.txt"
  probe=(run --rm --network floci-test --entrypoint python
    -v /tests/floci-lambda.py:/probe.py:ro "$CLIENT" /probe.py)
  "${D[@]}" "${probe[@]}" --samples 1 --warm-invocations 20 >"$REPORTS/$runtime-warmup.json"
  "${D[@]}" "${probe[@]}" --samples "${FLOCI_SAMPLES:-10}" --warm-invocations 20 >"$REPORTS/$runtime-lambda.json"
  if [ "$SERVICE_SAMPLES" -gt 0 ]; then
    service_probe=(run --rm --network floci-test --entrypoint python
      -v /tests/floci-services.py:/probe.py:ro "$CLIENT" /probe.py)
    "${D[@]}" "${service_probe[@]}" --samples 1 >"$REPORTS/$runtime-services-warmup.json"
    "${D[@]}" "${service_probe[@]}" --samples "$SERVICE_SAMPLES" >"$REPORTS/$runtime-services.json"
  fi
  "${D[@]}" logs floci >"$REPORTS/$runtime-floci.log" 2>&1
  "${D[@]}" ps -a >"$REPORTS/$runtime-containers.txt"
  docker stop "$RUN-$runtime" >/dev/null
  echo "PASS $runtime: Lambda responses validated; timings in $REPORTS/$runtime-lambda.json"
done
