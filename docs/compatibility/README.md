# Linux containers and IPv4/IPv6 networking

This fork targets Docker-compatible containers, exec, and IPv4/IPv6 bridge
networking on Linux. Swarm and plugins are excluded. Builds, distribution
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

Container options and non-bridge network drivers still need a field-by-field
audit. Do not infer support for macvlan, ipvlan, rootless operation, or arbitrary
security profiles from a matching API version.

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
and the fork's native Linux engine. Each has its own image/container state and
a Unix socket; neither exposes host ports or receives the host Docker socket.
The harness removes its containers and socket volume on exit.

It exercises container lifecycle, pause/unpause, exec, resource limit removal,
invalid updates, static IPv4 and IPv6 addresses, IPv6-only networks, DNS, and
network connect/disconnect. It also runs the existing IPv6 conformance suite
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

### Known missing or incomplete features

These are five **areas**, not five individual missing flags. They are identified
in the implementation; this is not yet an exhaustive parameter audit.

| Area | Concrete gaps | Implementation evidence |
| --- | --- | --- |
| Attach controls | `detachKeys`; raw HTTP attach does not honor historical `logs` or `stream=false`; older API WebSocket semantics | `api.rs::attach`, `websocket.rs` |
| Advanced resource controls | `CpusetCpus`, `CpusetMems`, configurable `MemorySwap`, `MemoryReservation`, `OomKillDisable` | `api.rs` resource parsing; `kiri-init/src/linux/cgroup.rs::apply_limits` fixes swap to zero |
| Namespace and security selection | Docker `PidMode`, `IpcMode`, `UTSMode`, `UsernsMode`, `CgroupnsMode`; custom seccomp profiles; GPU device requests | Container spec/create parsing, `engine.rs::security_flags`, `hostopts.rs::check_device_requests` |
| Advanced networking | macvlan/ipvlan creation; bridge MTU, gateway mode, inter-container communication and masquerade toggles, default host binding address | Network create accepts bridge; `network.rs` stores driver options without applying these settings |
| Endpoint inspection metadata | Container network `NetworkID` is empty; endpoint IDs are missing/placeholders; IPv4 gateway and automatically assigned MAC are not accurately reported in all inspect views | `api.rs::network_settings`, `api.rs::network_json` |

Only Swarm and plugins were explicitly excluded by the user. Non-bridge
networking and advanced container options above remain gaps against the broader
request; they are not silently treated as completed or excluded.

### Verification still needed

Local Linux ARM64 runs passed all 10 comparison scenario groups and 85 IPv6
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
