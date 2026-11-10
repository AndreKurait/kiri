# Floci interoperability and timings

Floci's JVM dev image at source revision
`3806dd522fe4275431fec6b2de7917a0e5c573b2` exposed three integration problems:
socket permissions for its unprivileged process, missing container detection,
and unsolicited HTTP upgrades on attach and exec. The fork now provides
Docker-compatible defaults for the standalone root Linux daemon, creates
`/.dockerenv`, and negotiates streaming responses with the client.

After these fixes, local Java SDK checks passed for ECS task lifecycle and
ephemeral volumes (12 tests), ElastiCache (15), Lambda DNS (3), PostgreSQL
connections and IAM authentication (7), and a Lambda with 33 MB of code (1).
These are real container-backed checks, not proof of every Floci feature.

## Measured Lambda workload

On November 10, 2026, both engines used the same two Linux ARM64 CPUs in a
Docker Desktop VM and identical Floci and Python Lambda image IDs. Other
validation containers were restricted to different CPUs. Engines ran
sequentially. Downloads and one warmup function were excluded.

Each engine created ten fresh functions and checked the returned payload from
the first invocation plus twenty warm invocations per function.

| Median elapsed time | Docker 28.0.4 | Kiri |
| --- | ---: | ---: |
| Create function through first result | 113.5 ms | 52.6 ms |
| Warm invocation | 4.66 ms | 5.34 ms |
| Delete function | 1,085.8 ms | 1,014.4 ms |
| Create, 21 invocations, delete (sum of measured segments) | 1,296.7 ms | 1,187.1 ms |

Kiri was **2.16 times faster to the first result**, while warm invocations were
about 15% slower in this run. The complete measured lifecycle was about 9%
faster. The startup improvement does not imply an equivalent improvement for
already-running functions or for other Floci services.

This is a small local benchmark of a simple echo handler and a JVM Floci
build. It is not a native-Floci benchmark. First-result p95 was 128.1 ms for
Docker and 65.6 ms for Kiri; warm-invocation p95 was 7.74 ms and 8.78 ms.

[Raw measurements and binary hashes](floci-lambda-2026-11-10.json) retain every
sample. Reproduce with `bash tests/compatibility/floci.sh`; use
`FLOCI_CPUSET=0,1` to constrain both engines to the same CPUs.
