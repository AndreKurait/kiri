#!/usr/bin/env python3
"""Container/network behavior checks and lifecycle benchmarks over the Engine API.

Use only against a disposable test engine. Objects have unique names and are
removed in finally blocks; pulling the test image populates the engine's cache.
No Python dependencies beyond the standard library.
"""
import argparse
import base64
import concurrent.futures
import http.client
import ipaddress
import json
import math
import os
import socket
import statistics
import struct
import time
import uuid
from urllib.parse import quote


class Connection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=60)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Engine:
    def __init__(self, path, image):
        self.path, self.image = path, image
        self.prefix = "compat-" + uuid.uuid4().hex[:12]
        self.containers, self.networks = [], []
        self.results = []

    def request(self, method, path, body=None, status=200, raw=False):
        conn = Connection(self.path)
        try:
            headers = {"Content-Type": "application/json"}
            conn.request(method, "/v1.48" + path, None if body is None else json.dumps(body), headers)
            resp = conn.getresponse()
            data = resp.read()
            assert resp.status == status, (method, path, body, resp.status, data[:600].decode(errors="replace"))
            if raw or not data:
                return data
            return json.loads(data)
        finally:
            conn.close()

    def create(self, cmd=None, host=None, endpoints=None, **config):
        body = dict(Image=self.image, Cmd=cmd or ["sleep", "120"], HostConfig=host or {},
                    Labels={"fork.compatibility": self.prefix}, **config)
        if endpoints:
            body["NetworkingConfig"] = {"EndpointsConfig": endpoints}
        value = self.request("POST", "/containers/create", body, 201)
        self.containers.append(value["Id"])
        return value["Id"]

    def inspect(self, cid):
        return self.request("GET", f"/containers/{cid}/json")

    def run(self, cmd, host=None, endpoints=None):
        cid = self.create(cmd, host, endpoints)
        self.request("POST", f"/containers/{cid}/start", status=204)
        result = self.request("POST", f"/containers/{cid}/wait")
        assert result["StatusCode"] == 0, result
        output = self.request("GET", f"/containers/{cid}/logs?stdout=1&stderr=1", raw=True)
        return cid, output

    def network(self, suffix, **settings):
        value = self.request("POST", "/networks/create",
                             dict(Name=self.prefix + suffix, Labels={"fork.compatibility": self.prefix}, **settings), 201)
        self.networks.append(value["Id"])
        return value["Id"]

    def test(self, name, fn):
        start = time.monotonic()
        try:
            fn()
            self.results.append(dict(name=name, passed=True, seconds=time.monotonic() - start))
            print("PASS", name, flush=True)
        except Exception as exc:
            self.results.append(dict(name=name, passed=False, error=str(exc), seconds=time.monotonic() - start))
            print("FAIL", name, str(exc), flush=True)

    def cleanup(self):
        for cid in reversed(self.containers):
            try:
                self.request("DELETE", f"/containers/{cid}?force=1&v=1", status=204)
            except Exception as exc:
                self.results.append(dict(name="cleanup-container", passed=False, error=str(exc)))
        for nid in reversed(self.networks):
            try:
                self.request("DELETE", f"/networks/{nid}", status=204)
            except Exception as exc:
                self.results.append(dict(name="cleanup-network", passed=False, error=str(exc)))

    def lifecycle(self):
        cid, output = self.run(["sh", "-c", "printf stdout; printf stderr >&2"])
        assert b"stdout" in output and b"stderr" in output
        assert self.inspect(cid)["State"]["Status"] == "exited"
        name = self.prefix + "-renamed"
        self.request("POST", f"/containers/{cid}/rename?name={name}", status=204)
        assert self.inspect(cid)["Name"] == "/" + name
        self.request("POST", f"/containers/{cid}/start", status=204)
        assert self.request("POST", f"/containers/{cid}/wait")["StatusCode"] == 0

    def pause_restart_exec(self):
        cid = self.create()
        self.request("POST", f"/containers/{cid}/start", status=204)
        self.request("POST", f"/containers/{cid}/start", status=304)
        self.request("POST", f"/containers/{cid}/pause", status=204)
        assert self.inspect(cid)["State"]["Paused"]
        self.request("POST", f"/containers/{cid}/unpause", status=204)
        assert not self.inspect(cid)["State"]["Paused"]
        eid = self.request("POST", f"/containers/{cid}/exec", {"Cmd": ["true"]}, 201)["Id"]
        # Docker documents 200 for detached exec start (not 204).
        self.request("POST", f"/exec/{eid}/start", {"Detach": True}, 200)
        for _ in range(100):
            state = self.request("GET", f"/exec/{eid}/json")
            if not state["Running"]:
                break
            time.sleep(.02)
        assert not state["Running"] and state["ExitCode"] == 0, state
        self.request("POST", f"/containers/{cid}/restart?t=0", status=204)
        assert self.inspect(cid)["State"]["Running"]
        self.request("POST", f"/containers/{cid}/stop?t=0", status=204)
        assert not self.inspect(cid)["State"]["Running"]
        self.request("POST", f"/containers/{cid}/stop?t=0", status=304)

    def resource_reset(self):
        cid = self.create(host={"PidsLimit": 32, "CpuQuota": 20000, "CpuPeriod": 100000})
        self.request("POST", f"/containers/{cid}/update", {"PidsLimit": -1, "CpuQuota": -1})
        # Verify enforcement after update, not just echoed inspect fields.
        self.request("POST", f"/containers/{cid}/start", status=204)
        eid = self.request("POST", f"/containers/{cid}/exec", {
            "Cmd": ["sh", "-c", "cat /sys/fs/cgroup/pids.max /sys/fs/cgroup/cpu.max > /limits"],
        }, 201)["Id"]
        self.request("POST", f"/exec/{eid}/start", {"Detach": True}, 200)
        for _ in range(100):
            state = self.request("GET", f"/exec/{eid}/json")
            if not state["Running"]:
                break
            time.sleep(.02)
        assert not state["Running"] and state["ExitCode"] == 0
        import io
        import tarfile
        archive = self.request("GET", f"/containers/{cid}/archive?path=/limits", raw=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            values = tar.extractfile(tar.getmembers()[0]).read().decode().splitlines()
        assert values == ["max", "max 100000"], values

    def invalid_updates(self):
        cid = self.create()
        for value in [
            {"RestartPolicy": {"Name": "invalid"}},
            {"RestartPolicy": {"Name": "always", "MaximumRetryCount": 2}},
            {"RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": -1}},
            {"CpuPeriod": 1}, {"Memory": -1},
            {"NanoCpus": 1000000000, "CpuQuota": 10000},
        ]:
            error = self.request("POST", f"/containers/{cid}/update", value, 400)
            assert error.get("message"), error
        assert self.inspect(cid)["HostConfig"]["RestartPolicy"]["Name"] == "no"

    def websocket_attach(self, stdin_once=False):
        cid = self.create(["cat"], OpenStdin=True, StdinOnce=stdin_once)
        self.request("POST", f"/containers/{cid}/start", status=204)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            sock.connect(self.path)
            key = base64.b64encode(os.urandom(16)).decode()
            path = f"/v1.48/containers/{cid}/attach/ws?stdin=1&stdout=1&stderr=1&stream=1"
            sock.sendall((f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\n"
                          f"Upgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n").encode())
            with sock.makefile("rb") as response:
                status = response.readline()
                assert b" 101 " in status, status
                while True:
                    line = response.readline()
                    assert line, "WebSocket upgrade headers ended prematurely"
                    if line == b"\r\n":
                        break
                data, mask = b"websocket-roundtrip\n", os.urandom(4)
                sock.sendall(bytes([0x82, 0x80 | len(data)]) + mask +
                             bytes(value ^ mask[i % 4] for i, value in enumerate(data)))
                output = b""
                while len(output) < len(data):
                    first, length = response.read(2)
                    assert first & 0xf == 2, first
                    assert not length & 0x80
                    if length == 126:
                        length = struct.unpack("!H", response.read(2))[0]
                    elif length == 127:
                        length = struct.unpack("!Q", response.read(8))[0]
                    output += response.read(length)
                assert output == data, output
                sock.sendall(b"\x88\x82" + mask + bytes(v ^ mask[i] for i, v in enumerate(b"\x03\xe8")))
        finally:
            sock.close()
        if stdin_once:
            for _ in range(100):
                if not self.inspect(cid)["State"]["Running"]:
                    break
                time.sleep(.02)
            assert not self.inspect(cid)["State"]["Running"], "StdinOnce must close stdin on disconnect"
        else:
            time.sleep(.1)
            assert self.inspect(cid)["State"]["Running"], "Disconnect must preserve stdin when StdinOnce is false"

    def addressing(self, ipv4, ipv6):
        config = []
        if ipv4:
            config.append({"Subnet": "10.231.17.0/24", "Gateway": "10.231.17.1"})
        if ipv6:
            config.append({"Subnet": "fd42:cafe:17::/64", "Gateway": "fd42:cafe:17::1"})
        nid = self.network("-addr-" + str(len(self.networks)), EnableIPv4=ipv4, EnableIPv6=ipv6,
                           IPAM={"Config": config})
        details = self.request("GET", f"/networks/{nid}")
        name = details["Name"]
        addr = {}
        if ipv4:
            addr["IPv4Address"] = "10.231.17.42"
        if ipv6:
            addr["IPv6Address"] = "fd42:cafe:17::42"
        cid, output = self.run(["sh", "-c", "ip -4 addr show eth0; ip -6 addr show eth0"],
                               {"NetworkMode": name}, {name: {"IPAMConfig": addr}})
        state = self.inspect(cid)["NetworkSettings"]["Networks"][name]
        # Addresses disappear on Docker after exit; inspect a running peer.
        live = self.create(host={"NetworkMode": name}, endpoints={name: {"IPAMConfig": addr}})
        self.request("POST", f"/containers/{live}/start", status=204)
        state = self.inspect(live)["NetworkSettings"]["Networks"][name]
        assert bool(state["IPAddress"]) == ipv4, state
        assert bool(state["GlobalIPv6Address"]) == ipv6, state
        if ipv4:
            assert ipaddress.ip_address(state["IPAddress"]) == ipaddress.ip_address(addr["IPv4Address"])
            assert b"10.231.17.42/24" in output
        if ipv6:
            assert ipaddress.ip_address(state["GlobalIPv6Address"]) == ipaddress.ip_address(addr["IPv6Address"])
            assert b"fd42:cafe:17::42/64" in output
        # Remove stopped and live endpoints so subsequent address-family tests
        # can reuse the same subnets without relying on engine-specific pruning.
        for obj in (cid, live):
            self.request("DELETE", f"/containers/{obj}?force=1", status=204)
            self.containers.remove(obj)
        self.request("DELETE", f"/networks/{nid}", status=204)
        self.networks.remove(nid)

    def connect_disconnect(self):
        n1 = self.network("-one", EnableIPv6=True)
        n2 = self.network("-two", EnableIPv6=True)
        name1 = self.request("GET", f"/networks/{n1}")["Name"]
        name2 = self.request("GET", f"/networks/{n2}")["Name"]
        cid = self.create(host={"NetworkMode": name1})
        self.request("POST", f"/containers/{cid}/start", status=204)
        self.request("POST", f"/networks/{n2}/connect", {"Container": cid, "EndpointConfig": {"Aliases": ["peer"]}})
        assert name2 in self.inspect(cid)["NetworkSettings"]["Networks"]
        _, output = self.run(["ping", "-6", "-c", "1", "-W", "3", "peer"], {"NetworkMode": name2})
        assert b"1 packets received" in output
        self.request("POST", f"/networks/{n2}/disconnect", {"Container": cid})
        assert name2 not in self.inspect(cid)["NetworkSettings"]["Networks"]

    def benchmark(self, samples, concurrency):
        def once(_):
            started = time.perf_counter()
            cid = self.create(["true"], {"NetworkMode": "none"})
            try:
                self.request("POST", f"/containers/{cid}/start", status=204)
                assert self.request("POST", f"/containers/{cid}/wait")["StatusCode"] == 0
            finally:
                self.request("DELETE", f"/containers/{cid}", status=204)
                self.containers.remove(cid)
            return (time.perf_counter() - started) * 1000
        for _ in range(5):
            once(0)
        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            values = sorted(pool.map(once, range(samples)))
        return dict(samples=samples, concurrency=concurrency, network="none",
                    median_ms=statistics.median(values),
                    p95_ms=values[math.ceil(len(values) * .95) - 1],
                    p99_ms=values[math.ceil(len(values) * .99) - 1],
                    lifecycles_per_second=samples / (time.perf_counter() - start))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--image", default="alpine:3.21")
    parser.add_argument("--output", required=True)
    parser.add_argument("--benchmark", type=int, default=0)
    args = parser.parse_args()
    engine = Engine(args.socket, args.image)
    report = {"image": args.image}
    try:
        report["engine"] = engine.request("GET", "/version")
        data = engine.request("POST", "/images/create?fromImage=" + quote(args.image, safe=""), raw=True)
        for line in data.splitlines():
            value = json.loads(line)
            assert "error" not in value, value
        for name, fn in [
            ("container-lifecycle", engine.lifecycle),
            ("pause-restart-detached-exec", engine.pause_restart_exec),
            ("resource-limit-reset", engine.resource_reset),
            ("invalid-updates-do-not-mutate", engine.invalid_updates),
            ("websocket-attach-roundtrip", engine.websocket_attach),
            ("websocket-stdin-once", lambda: engine.websocket_attach(True)),
            ("ipv4-static-address", lambda: engine.addressing(True, False)),
            ("dual-stack-static-address", lambda: engine.addressing(True, True)),
            ("ipv6-only-static-address", lambda: engine.addressing(False, True)),
            ("connect-disconnect-ipv6-dns", engine.connect_disconnect),
        ]:
            engine.test(name, fn)
        if args.benchmark and all(r["passed"] for r in engine.results):
            report["benchmarks"] = [engine.benchmark(args.benchmark, c) for c in (1, 8)]
    finally:
        engine.cleanup()
        report["checks"] = engine.results
        with open(args.output, "w") as output:
            json.dump(report, output, indent=2)
    raise SystemExit(0 if all(r["passed"] for r in engine.results) else 1)


if __name__ == "__main__":
    main()
