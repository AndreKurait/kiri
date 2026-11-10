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

## Floci shutdown fix and follow-up measurement

The roughly one-second delete time above came primarily from Floci's Lambda
shutdown behavior. Its Python runtime ran as PID 1 without an init process,
so SIGTERM did not terminate it promptly. Floci now requests an init process
and preserves a bounded drain for external extension shutdown callbacks and
their final logs. The isolated fix was merged into AndreKurait/floci main
through PR #628 and dev through PR #629.

A follow-up comparison used 20 fresh functions per engine, each with one
first invocation and 20 warm invocations, followed by deletion. Both engines
used CPUs 0 and 1 sequentially, identical packaged JVM Floci images and cached
Python 3.12 images, with one warmup function excluded and no concurrent Maven
build. The workload has no external Lambda extensions.

| Median elapsed time, with Floci fix | Docker 28.0.4 | Kiri |
| --- | ---: | ---: |
| Create function through first result | 104.8 ms | 61.3 ms |
| Warm invocation | 3.86 ms | 4.31 ms |
| Delete function | 66.2 ms | 8.7 ms |
| Create, 21 invocations, delete | 260.2 ms | 157.0 ms |

Kiri completed this lifecycle about 1.66 times faster than Docker. First-result
latency was about 1.71 times faster and deletion about 7.59 times faster.
Warm invocations were about 11% slower on Kiri in this run.

The larger improvement was the Floci fix: Kiri's median lifecycle dropped from
1,148.5 ms to 157.0 ms, about 7.32 times faster. Docker improved from 1,294.8 ms
to 260.2 ms, about 4.98 times faster. These are separate effects; the Floci
improvement should not be attributed to changing container runtimes.

[Paired before/after measurements](floci-lambda-shutdown-2026-11-10.json)
include the source revisions, image IDs, and samples. The optimized source
tree is identical to merged dev revision
`e477e8e5dd50f9a6402c8fcc5bd01e9426b12f39`. These measurements cover a small
local Lambda workload, not all Floci services or a native Floci build.

### Dedicated Linux benchmark

The existing Linux compatibility workflow has an opt-in `benchmark` input.
It builds the validated Floci revision above, finishes all compilation before
measurement, and runs Docker and Kiri sequentially on CPUs 0 and 1 in disposable
engines. Each engine measures 20 Lambda functions with 20 warm invocations each,
plus ten Redis and ten PostgreSQL lifecycles. One warmup per workload is excluded.
The service probes verify Redis PING/SET/GET and a PostgreSQL table, insert, and
query before deleting the resources.

Run `gh workflow run linux-compatibility.yml --ref <branch> -f benchmark=true`
on a branch containing this workflow version. The `floci-runtime-benchmark`
artifact contains raw samples, image identities, binary hashes, CPU affinity,
and daemon/application logs. Normal compatibility CI keeps its short Lambda
smoke test. Inspect artifact completeness and matching image IDs before
interpreting a benchmark; GitHub runner hardware and run-order effects still
limit comparisons across separate runs.

## Broader compatibility validation

The merged dev JVM image at `e477e8e5dd50f9a6402c8fcc5bd01e9426b12f39`
was also exercised on both Linux ARM64 engines. The following counts combine
the baseline suites with focused follow-up runs, deduplicating test cases.
They are compatibility evidence, not controlled performance measurements.
The [validation summary](floci-validation-2026-11-10.json) records revisions,
binary hashes, case counts, report-manifest hashes, and remaining limitations.

| Suite | Passing cases on Docker | Passing cases on Kiri |
| --- | ---: | ---: |
| Python SDK, including opt-in configurations | 905 | 905 |
| Go SDK | 216 | 216 |
| Node SDK | 505 | 505 |
| AWS CLI, including Neptune retry | 324 | 324 |
| Java SDK baseline cases covered by passing runs | 3,033 / 3,033 | 3,033 / 3,033 |
| OpenTofu | 49 | 49 |
| Terraform | 124 | 124 |
| CDK | 26 | 26 |
| Python IAM enforcement | 52 | 52 |

The six Flink 1.18 cases normally disable themselves on an ARM64 JVM because
their runtime image is AMD64-only. A follow-up run used a real AMD64 Java 17
client and the AMD64 Flink image under the host's installed emulation, with
Floci still running on ARM64. All six unchanged cases passed with zero skips
on both engines. This covers functionality under emulation, not native AMD64
performance. Passing these suites does not establish full Docker API parity.

Terraform's first run overloaded this shared machine with 15 concurrent Bats
files per engine and timed out. A retry limited each engine to two files at a
time. All 15 files exited successfully and all 124 JUnit cases passed on each
engine. Docker's reporting wrapper then failed while printing its summary,
after a mounted-script edit during execution; its exit code 2 is retained.
Independent aggregation verified all per-file exit codes and JUnit results.
Kiri's wrapper completed normally with exit code 0.

Kiri's archive upload path needed an additional compatibility fix. Docker
clients can send plain or gzip-compressed tar archives to containers, including
with chunked HTTP transfer encoding. Kiri now detects gzip after decoding the
agent's copy framing, streams decompression, and preserves permission bits.
Regression checks cover plain and compressed uploads to stopped and running
containers with fixed-length and chunked requests. All 57 native Linux agent
tests and the 20 affected Java follow-up cases passed.

Other follow-up failures came from the validation setup or test assertions:
callback tests needed a reachable published port; IAM policy tests and strict
signature tests needed their documented, separate server configurations; and
API Gateway tests needed to assert the unwrapped HTTP response and use a signed
JWT with a real discovery/JWKS fixture. The latter fixture change is proposed
separately in AndreKurait/floci PRs #631 (dev) and #632 (main).

Parallel functional-suite elapsed times should not be interpreted as engine
speedups. They include provider polling, application startup, image preparation,
and shared-machine contention. For example, the RDS Terraform run waited about
60 seconds before its first instance-status query, then polled at approximately
10-second intervals. Such waits can dominate an otherwise fast container
lifecycle.
