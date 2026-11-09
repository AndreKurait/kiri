# Linux containers and IPv4/IPv6 networking

This fork targets Docker-compatible containers, exec, and IPv4/IPv6 bridge
networking on Linux, including macvlan and ipvlan. Swarm, plugins, and GPU
support are excluded. Builds, distribution
inspection, and desktop integration are not part of this effort.

The reference is Docker Engine 28.0.4, API 1.48. Version 1.48 is needed for the
Docker CLI's `network create --ipv4=false` flag. Advertising this version enables
that client feature; it does not establish parity for every operation or field.

## Compatibility inventory

[docker-api-v1.48.json](docker-api-v1.48.json) enumerates all 36 container, exec,
and network operations from the pinned Moby specification. `partial` means a
route exists, not that every option works. `verified` requires recorded evidence.
All scoped routes are implemented, including WebSocket attach. Parameter and
failure semantics remain under audit.

Run `python3 tests/compatibility/inventory.py` to verify that no scoped operation
has been omitted. The vendored specification is from
`moby/moby`, tag `v28.0.4`, `api/swagger.yaml`, under Apache-2.0; its source URL
and SHA-256 are recorded in the inventory.

Container options and network drivers still need a field-by-field audit.
Do not infer rootless support or unrestricted security-profile compatibility
from a matching API version.

## Changes

- Recognize and validate API versions, returning JSON errors for unsupported
  versions instead of silently treating them as supported.
- Decode escaped paths while preserving literal `+` characters.
- Handle `Expect: 100-continue` and chunked trailers, including requests sharing
  a persistent connection. Reject ambiguous or invalid body framing.
- Add WebSocket attach with bidirectional streaming, log replay, ping/pong,
  close handling, and output backpressure.
- Return Docker's HTTP 200 for detached exec start.
- Preserve the documented status code for empty responses, including network
  connect/disconnect, and return 304 for already-started/stopped containers.
- Wait for process exit before returning from a forced stop.
- Honor `StdinOnce` on attach disconnect and retain it in container inspection.
- Remove CPU quota and PID limits through container updates, preserve omitted
  settings, and validate numeric limits before mutation.
- Validate restart policies consistently on container create and update.
- Enable API 1.48 negotiation for IPv6-only network creation.

## Validation

```sh
cargo test --locked -p kiri -p kiri-image -p kiri-proto
python3 tests/compatibility/inventory.py
cargo xtask linux
bash tests/compatibility/linux.sh
```

The Linux harness starts two disposable privileged containers: Docker 28.0.4
and the fork's native Linux engine, plus an unprivileged packet observer. Each has its own image/container state and
a Unix socket; neither exposes host ports or receives the host Docker socket.
The harness removes its containers and socket volume on exit.

It exercises lifecycle, pause/unpause, exec, resource enforcement and updates,
attach controls, shared namespaces, custom seccomp, endpoint metadata, bridge
options, observed source NAT, routed ports, dual-stack macvlan/ipvlan, static
addresses, IPv6-only networks, DNS, and network connect/disconnect. It also runs the existing IPv6 conformance suite
against both engines. Missing external IPv6 connectivity is reported as a skip,
not a successful egress test.

Reports go to `target/compatibility/`. The benchmark measures four-request
create/start/wait/delete lifecycles with cached Alpine images and
`network=none`, at concurrency 1 and 8.
It records median, p95, p99, and throughput after warmup. This does not measure
network performance or justify a general speed claim. Linux containers running
inside Docker Desktop share a Linux VM kernel; use a native Linux host for
deployment-representative performance results.

On x86-64 Linux, use `cargo xtask linux --arch amd64`, then
`KIRI_BIN_DIR="$PWD/target/linux-x86_64/release" bash tests/compatibility/linux.sh`.
The GitHub Actions workflow runs this path on Ubuntu. The harness requires musl
binaries because its disposable engine containers use Alpine; a host glibc build
does not run inside that image.

## Feature checklist

The common workflows below pass the Linux comparison harness. The feature set
is **not complete Docker container/network API compatibility**. A passing
workflow does not establish support for every option on its endpoints.

| Included and exercised | Coverage |
| --- | --- |
| Container lifecycle | Create, start, wait, stop, restart, rename, remove, pause/unpause, output and exit status |
| Exec and interactive I/O | Exec, detached exec, bidirectional WebSocket attach, stdin reconnect and `StdinOnce` |
| Basic resource updates | CPU quota and PID limit removal, cgroup enforcement, invalid updates rejected before mutation |
| IPv4/IPv6 bridge networks | IPv4-only, dual stack, IPv6-only, automatic and static addressing, subnets and gateways |
| Container connectivity | DNS A/AAAA, network aliases, connect/disconnect, internal networks, published IPv4/IPv6 ports |
| Network lifecycle | Inspection of tested address fields, removal, route cleanup and subnet reuse |

### Added feature areas

| Area | Implemented behavior |
| --- | --- |
| Attach controls | Custom/default detach keys, raw HTTP log replay and stream selection, API-version-dependent WebSocket framing and stream defaults |
| Resource controls | CPU and memory node sets, total memory-plus-swap limits, memory reservations, transactional live updates |
| Namespaces and security | Host/private PID, IPC, UTS and cgroup modes; container PID/IPC sharing; shareable `/dev/shm`; host user namespace; custom syscall/argument seccomp rules compiled with libseccomp |
| Networking | macvlan and ipvlan; bridge MTU, ICC, masquerading, default bind address, NAT/routed/isolated gateway configuration |
| Endpoint inspection | Matching network/endpoint IDs, gateway and actual MAC across container and network inspection |

### Remaining limitations

- Linux cgroup v2 cannot implement `OomKillDisable=true`; requests are explicitly
  rejected. GPU requests, Swarm, and plugins are outside the requested scope.
- User namespace remapping/rootless daemon configuration and seccomp notification
  listeners/explicit filter flags are not implemented. Custom profiles retain
  Kiri's existing vsock/io_uring host-boundary restrictions.
- Explicit macvlan/ipvlan parent devices must already exist; automatic VLAN
  parent creation is not implemented. L3 modes and all gateway-mode combinations
  still need broader conformance evidence.
- Exact parameter/error parity, recovery, sustained concurrency, and external
  IPv6 egress remain verification work, as described below.

The engine and agent now use internal protocol version 4 and must be upgraded
as a pair. Native agent builds require static libseccomp development libraries;
`cargo xtask linux` installs them in its build container. Custom filters are
compiled before cloning and installed in the child; ordinary starts retain the
existing direct `clone3` path without an external OCI runtime or shim.

### Verification still needed

Local Linux ARM64 runs passed all 22 comparison scenario groups and 85 IPv6
checks, with zero failures. Five external IPv6 checks were skipped because the
host lacks an IPv6 uplink. The reference Docker run passed the same scenario
groups and 84 IPv6 checks; Docker 28 also skips the newer automatic-subnet static
IPv6 case. The shell assertions require both a successful command and matching
output, including negative output assertions.

Exact parameter/error parity across all 36 operations, daemon crash/restart
recovery, and sustained concurrent churn remain unverified. External IPv6
egress needs a capable host. Lifecycle benchmarks are available, but do not
establish network throughput, memory efficiency, or production performance.

No release artifacts are published by this workflow. An independently branded
release must respect upstream's name/logo policy.

## Recorded Linux ARM64 performance

The November 9, 2026 run passed all 22 comparison groups on both engines,
85 IPv6 checks on Kiri and 84 on Docker (5 and 6 documented skips respectively).
The [recorded results](linux-arm64-results.json) include the tested binary hashes.
No builds or debug engines ran during this measurement.

| Engine | Concurrency | Median lifecycle | p95 | Lifecycles/second |
| --- | ---: | ---: | ---: | ---: |
| Docker 28.0.4 | 1 | 61.64 ms | 73.21 ms | 16.00 |
| Kiri fork | 1 | 5.37 ms | 8.48 ms | 177.16 |
| Docker 28.0.4 | 8 | 133.65 ms | 162.78 ms | 57.42 |
| Kiri fork | 8 | 24.57 ms | 33.37 ms | 299.75 |

These are cached-image, network-disabled lifecycle measurements on the same
Linux VM, with 50 samples after five warmups. They do not compare Youki or
measure network throughput, application speed, or bare-metal performance.
